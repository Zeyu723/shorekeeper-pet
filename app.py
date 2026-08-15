"""Shorekeeper Desktop Pet — Hermes Event Integration Edition.

A pixel-art desktop pet that reacts to Hermes agent lifecycle events.
Transparent background, always-on-top, draggable, with idle behavior.

Events arrive via local TCP socket (127.0.0.1:51207) from Hermes hooks.
The hook handler connects, sends one JSON line, and disconnects.

Architecture:
  - Main thread: tkinter event loop + animation tick
  - Background thread: TCP socket listener → pushes events to a Queue
  - AnimationEngine: priority-based state machine with coalescing
"""

from __future__ import annotations

import json
import os
import random
import re
import socket
import sys
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from queue import Queue, Empty
from typing import Any, Callable

import tkinter as tk
from PIL import Image, ImageTk


# ─── Constants ────────────────────────────────────────────────────

APP_NAME = "ShorekeeperPet"
VERSION = "2.1.0"

IPC_HOST = "127.0.0.1"
IPC_PORT = 51207      # TCP IPC (hooks + manual events)
HTTP_PORT = 51208     # HTTP listener (desktop plugin events)

TRANSPARENT_COLOR = "#010101"  # near-black, used as Windows color-key

CANVAS_WIDTH = 190
CANVAS_HEIGHT = 160

TICK_MS = 80  # base animation tick (~12.5 fps ceiling)
POLL_INTERVAL_S = 2.0  # /api/status poll interval for fallback

ACTIONS_DIR_NAME = "actions/hermes_events"
IDLE_SPRITE_NAME = "assets/shorekeeper-laying.png"


# ─── Path Helpers ─────────────────────────────────────────────────

def resource_path(relative: str) -> Path:
    """Resolve a path relative to the script dir or PyInstaller bundle."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative


def project_root() -> Path:
    """Directory containing app.py (and actions/, assets/)."""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def settings_path() -> Path:
    root = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root / "settings.json"


def present_auxiliary_window(pet: Any, window: tk.Toplevel) -> None:
    """Show one pet-owned Toplevel, unless quiet mode is currently active."""
    if bool(getattr(pet, "_quiet_hidden", False)):
        hidden = getattr(pet, "_hidden_aux_windows", None)
        if hidden is None:
            hidden = []
            pet._hidden_aux_windows = hidden
        if all(existing is not window for existing in hidden):
            hidden.append(window)
        window.withdraw()
        return
    window.deiconify()
    window.lift()


# ─── Action Package ──────────────────────────────────────────────

class ActionPackage:
    """One Hermes event → animation mapping, loaded from manifest.json."""

    def __init__(self, action_id: str, manifest: dict, frames: list[ImageTk.PhotoImage]):
        self.id = action_id
        self.event_name: str = manifest.get("event", {}).get("name", "")
        self.event_source: str = manifest.get("event", {}).get("source", "hook")
        self.state: str = manifest.get("state", "unknown")
        self.playback: str = manifest.get("playback", "once")  # "once" | "hold"
        self.priority: int = manifest.get("lifecycle", {}).get("priority", 0)
        self.fallback_state: str = manifest.get("lifecycle", {}).get("fallback_state", "awake.idle")
        self.coalesce_ms: int = manifest.get("lifecycle", {}).get("coalesce_ms", 0)
        self.restart_on_repeat: bool = manifest.get("lifecycle", {}).get("restart_on_repeat", False)
        self.frame_duration_ms: int = manifest.get("frame_duration_ms", 100)
        self.frames = frames
        self.frame_count = len(frames)

    def __repr__(self):
        return f"<Action {self.id} prio={self.priority} {self.playback}>"


# ─── Animation Engine ────────────────────────────────────────────

class AnimationEngine:
    """Priority-based state machine driven by Hermes events.

    - Higher priority events preempt lower ones.
    - ``hold`` actions loop until another event changes state.
    - ``once`` actions play through then transition to fallback_state.
    - Repeated events within coalesce_ms are ignored.
    """

    IDLE = "__idle__"

    def __init__(self, actions_dir: Path, idle_frames: list[ImageTk.PhotoImage]):
        self.actions: dict[str, ActionPackage] = {}
        self.event_to_action: dict[str, str] = {}
        self.state_to_action: dict[str, str] = {}  # for fallback resolution
        self.idle_frames = idle_frames
        self.idle_duration_ms = 120

        self.current_action_id: str | None = None
        self.frame_index = 0
        self._last_frame_advance = 0.0
        self._last_event_ts: dict[str, float] = {}  # event_name → monotonic ms

        self._load_catalog(actions_dir)

    def _load_catalog(self, actions_dir: Path) -> None:
        catalog_path = actions_dir / "catalog.json"
        if not catalog_path.exists():
            print(f"[pet] Warning: catalog.json not found at {catalog_path}")
            return

        with open(catalog_path, "r", encoding="utf-8") as f:
            catalog = json.load(f)

        loaded = 0
        for entry in catalog.get("actions", []):
            aid = entry["action_id"]
            action_pkg_dir = actions_dir / aid
            manifest_path = action_pkg_dir / "manifest.json"
            if not manifest_path.exists():
                continue

            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)

            # Load PNG frames
            frame_paths = manifest.get("frames", [])
            pil_frames = []
            for fp_rel in frame_paths:
                fp = action_pkg_dir / fp_rel
                if fp.exists():
                    img = Image.open(fp).convert("RGBA")
                    pil_frames.append(img)

            if not pil_frames:
                # Try frames/ directory directly
                frames_dir = action_pkg_dir / "frames"
                if frames_dir.is_dir():
                    for i in range(99):
                        fp = frames_dir / f"frame_{i:03d}.png"
                        if fp.exists():
                            pil_frames.append(Image.open(fp).convert("RGBA"))
                        else:
                            break

            if not pil_frames:
                print(f"[pet] Warning: no frames for {aid}")
                continue

            # Composite onto transparent color background
            tk_frames = [self._composite(pil) for pil in pil_frames]

            pkg = ActionPackage(aid, manifest, tk_frames)
            self.actions[aid] = pkg
            self.event_to_action[pkg.event_name] = aid
            self.state_to_action[pkg.state] = aid
            loaded += 1

        print(f"[pet] Loaded {loaded} action packages from {actions_dir}")

    @staticmethod
    def _composite(pil_img: Image.Image) -> ImageTk.PhotoImage:
        """Paste RGBA frame onto TRANSPARENT_COLOR background for color-keying."""
        bg = Image.new("RGBA", pil_img.size, (1, 1, 1, 255))  # #010101
        bg.paste(pil_img, mask=pil_img)
        return ImageTk.PhotoImage(bg)

    def reload_at_scale(self, scale: float, idle_pil_frames: list[Image.Image]) -> None:
        """Rebuild all frames at a new scale. Call on scale change."""
        actions_dir = project_root() / ACTIONS_DIR_NAME
        # Reload action frames
        for aid, pkg in self.actions.items():
            manifest_path = actions_dir / aid / "manifest.json"
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)

            frame_paths = manifest.get("frames", [])
            tk_frames = []
            for fp_rel in frame_paths:
                fp = actions_dir / aid / fp_rel
                if fp.exists():
                    pil = Image.open(fp).convert("RGBA")
                    new_size = (
                        max(1, int(pil.width * scale)),
                        max(1, int(pil.height * scale)),
                    )
                    pil = pil.resize(new_size, Image.Resampling.NEAREST)
                    tk_frames.append(self._composite(pil))
            pkg.frames = tk_frames
            pkg.frame_count = len(tk_frames)

        # Reload idle frames
        new_idle = [self._composite(pil.resize(
            (max(1, int(pil.width * scale)), max(1, int(pil.height * scale))),
            Image.Resampling.NEAREST,
        )) for pil in idle_pil_frames]
        self.idle_frames = new_idle

        self.frame_index = 0

    def handle_event(self, event_name: str) -> bool:
        """Process an incoming event. Returns True if state changed."""
        action_id = self.event_to_action.get(event_name)
        if not action_id:
            return False

        action = self.actions[action_id]

        # Coalescing
        now_ms = time.monotonic() * 1000
        last = self._last_event_ts.get(event_name, 0)
        if action.coalesce_ms > 0 and (now_ms - last) < action.coalesce_ms:
            return False
        self._last_event_ts[event_name] = now_ms

        # Hold actions that are already active: don't restart
        if (self.current_action_id == action_id
                and action.playback == "hold"
                and not action.restart_on_repeat):
            return False

        # Priority check: lower priority can't interrupt higher
        if self.current_action_id and self.current_action_id != action_id:
            current = self.actions.get(self.current_action_id)
            if current and current.priority > action.priority:
                return False

        # Switch
        self.current_action_id = action_id
        self.frame_index = 0
        self._last_frame_advance = now_ms
        return True

    def tick(self) -> ImageTk.PhotoImage:
        """Advance one frame. Returns the image to display."""
        now_ms = time.monotonic() * 1000

        if self.current_action_id is None:
            return self._tick_idle(now_ms)

        action = self.actions[self.current_action_id]
        frame_dur = action.frame_duration_ms

        if now_ms - self._last_frame_advance >= frame_dur:
            self._last_frame_advance = now_ms
            self.frame_index += 1

            if self.frame_index >= action.frame_count:
                if action.playback == "hold":
                    self.frame_index = 0  # loop
                else:
                    # once: completed → transition to fallback
                    self._transition_to_fallback(action.fallback_state)
                    return self.tick()  # re-evaluate immediately

        if self.current_action_id is None:
            return self._tick_idle(now_ms)

        action = self.actions[self.current_action_id]
        idx = self.frame_index % action.frame_count
        return action.frames[idx]

    def _tick_idle(self, now_ms: float) -> ImageTk.PhotoImage:
        """Idle breathing animation."""
        if now_ms - self._last_frame_advance >= self.idle_duration_ms:
            self._last_frame_advance = now_ms
            self.frame_index += 1
        idx = self.frame_index % len(self.idle_frames)
        return self.idle_frames[idx]

    def _transition_to_fallback(self, fallback_state: str) -> None:
        """Resolve a fallback state to an action, or go idle."""
        # Map common fallback states
        if fallback_state in ("awake.idle", "idle", "sleeping.idle", ""):
            self.current_action_id = None
            self.frame_index = 0
            return

        # Try to find a hold action for this state
        action_id = self.state_to_action.get(fallback_state)
        if action_id and self.actions[action_id].playback == "hold":
            self.current_action_id = action_id
            self.frame_index = 0
        else:
            self.current_action_id = None
            self.frame_index = 0

    @property
    def current_state_label(self) -> str:
        if self.current_action_id is None:
            return "idle"
        a = self.actions[self.current_action_id]
        return f"{a.id} ({a.state})"


# ─── IPC Server (background thread) ──────────────────────────────

class IPCServer:
    """Minimal TCP server. Each connection sends one JSON line."""

    def __init__(self, event_queue: Queue, host: str = IPC_HOST, port: int = IPC_PORT):
        self.queue = event_queue
        self.host = host
        self.port = port
        self._sock: socket.socket | None = None
        self._running = False

    def start(self) -> bool:
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind((self.host, self.port))
            self._sock.listen(8)
            self._sock.settimeout(1.0)
            self._running = True
            t = threading.Thread(target=self._accept_loop, daemon=True)
            t.start()
            print(f"[pet] IPC listening on {self.host}:{self.port}")
            return True
        except OSError as e:
            print(f"[pet] IPC bind failed: {e}")
            self._running = False
            return False

    def _accept_loop(self) -> None:
        while self._running:
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle_conn, args=(conn,), daemon=True).start()

    def _handle_conn(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(2.0)
            data = b""
            while b"\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
                if len(data) > 65536:
                    break
            line = data.split(b"\n", 1)[0].strip()
            if line:
                msg = json.loads(line.decode("utf-8"))
                event = msg.get("event", "")
                if event:
                    self.queue.put(msg)
        except Exception:
            pass
        finally:
            conn.close()

    def stop(self) -> None:
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass


# ─── HTTP Listener (receives events from desktop plugin) ────────

from http.server import HTTPServer, BaseHTTPRequestHandler


class PetHTTPHandler(BaseHTTPRequestHandler):
    """Receive desktop-plugin events and expose local diagnostics."""

    # Class-level state shared by the single local HTTP server.
    event_queue: Queue | None = None
    reply_queue: Queue | None = None
    chat_outbox: Queue | None = None
    started_at_ms = int(time.time() * 1000)
    received_count = 0
    last_event: dict[str, Any] | None = None
    recent_events: list[dict[str, Any]] = []
    status_lock = threading.Lock()
    MAX_RECENT_EVENTS = 20

    @classmethod
    def reset_status(cls) -> None:
        """Reset diagnostics when the pet process starts (also useful in tests)."""
        with cls.status_lock:
            cls.started_at_ms = int(time.time() * 1000)
            cls.received_count = 0
            cls.last_event = None
            cls.recent_events = []

    @classmethod
    def record_event(cls, msg: dict[str, Any]) -> None:
        summary = {
            "event": msg.get("event"),
            "originalType": msg.get("originalType"),
            "source": msg.get("source"),
            "sessionId": msg.get("sessionId"),
            "toolId": msg.get("toolId"),
            "toolName": msg.get("toolName"),
            "timestamp": msg.get("timestamp"),
            "receivedAt": int(time.time() * 1000),
        }
        with cls.status_lock:
            cls.received_count += 1
            cls.last_event = summary
            cls.recent_events.append(summary)
            if len(cls.recent_events) > cls.MAX_RECENT_EVENTS:
                del cls.recent_events[:-cls.MAX_RECENT_EVENTS]

    @classmethod
    def status_snapshot(cls) -> dict[str, Any]:
        with cls.status_lock:
            return {
                "ok": True,
                "pid": os.getpid(),
                "startedAt": cls.started_at_ms,
                "receivedCount": cls.received_count,
                "lastEvent": cls.last_event,
                "recentEvents": list(cls.recent_events),
            }

    def _write_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path in ("/health", "/status"):
            self._write_json(200, self.status_snapshot())
            return
        if self.path == "/chat/outbox":
            # plugin polls this; drain pending chat requests
            items = []
            outbox = getattr(self.__class__, "chat_outbox", None)
            if outbox is not None:
                while True:
                    try:
                        items.append(outbox.get_nowait())
                    except Exception:
                        break
            self._write_json(200, {"requests": items})
            return
        self._write_json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        if self.path == "/chat/reply":
            # plugin pushes streaming reply text for the pet's bubble
            try:
                length = int(self.headers.get("Content-Length", 0))
                msg = json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                self._write_json(400, {"ok": False, "error": "bad json"})
                return
            q = self.__class__.reply_queue
            if q is not None:
                q.put(msg)
            self._write_json(200, {"ok": True})
            return
        if self.path != "/event":
            self._write_json(404, {"ok": False, "error": "not found"})
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            data = self.rfile.read(length)
            msg = json.loads(data.decode("utf-8"))
            if not isinstance(msg, dict) or not isinstance(msg.get("event"), str):
                raise ValueError("event must be a JSON object with a string event")
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
            self._write_json(400, {"ok": False, "error": str(error)})
            return

        self.record_event(msg)
        if PetHTTPHandler.event_queue is not None:
            PetHTTPHandler.event_queue.put(msg)
        self._write_json(200, {"ok": True})

    def do_OPTIONS(self) -> None:
        """CORS preflight for the Electron renderer fetch."""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def log_message(self, format, *args):
        pass  # suppress console spam


# ─── Hook Auto-Installer ─────────────────────────────────────────

HOOK_DIR_NAME = "shorekeeper-pet"

HOOK_YAML = """\
name: shorekeeper-pet
description: Forward agent lifecycle events to the Shorekeeper desktop pet via TCP.
events:
  - gateway:startup
  - session:start
  - session:end
  - session:reset
  - agent:start
  - agent:step
  - agent:end
  - command:*
"""

HOOK_HANDLER = '''\
"""Shorekeeper Pet — Hermes event forwarder (auto-installed)."""
from __future__ import annotations
import json, socket, time

PET_HOST = "127.0.0.1"
PET_PORT = 51207

def handle(event_type: str, context: dict) -> None:
    msg = {"event": event_type, "timestamp": int(time.time() * 1000), "source": "hook"}
    for k in ("session_id", "platform", "model", "provider", "chat_type"):
        v = context.get(k)
        if v:
            msg[k] = v
    if context.get("message"):
        msg["message_preview"] = str(context["message"])[:80]
    try:
        line = (json.dumps(msg, ensure_ascii=False) + "\\n").encode("utf-8")
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.3)
        s.connect((PET_HOST, PET_PORT))
        s.sendall(line)
        s.close()
    except OSError:
        pass
'''


def auto_install_hook() -> bool:
    """Copy hook files into Hermes hooks/ dir if not already there.

    Returns True if files were written or already present.
    The hook is picked up on next gateway restart.
    """
    # Find HERMES_HOME
    hermes_home = os.environ.get("HERMES_HOME", "")
    if not hermes_home:
        # Standard location on this machine
        hermes_home = str(Path.home() / "AppData" / "Local" / "hermes")

    hooks_dir = Path(hermes_home) / "hooks" / HOOK_DIR_NAME
    yaml_path = hooks_dir / "HOOK.yaml"
    handler_path = hooks_dir / "handler.py"

    try:
        # Check if already installed (content matches)
        if yaml_path.exists() and handler_path.exists():
            return True

        hooks_dir.mkdir(parents=True, exist_ok=True)
        if not yaml_path.exists():
            yaml_path.write_text(HOOK_YAML, encoding="utf-8")
        if not handler_path.exists():
            handler_path.write_text(HOOK_HANDLER, encoding="utf-8")

        print(f"[pet] Hook auto-installed to {hooks_dir}")
        print("[pet] Will activate on next Hermes gateway restart.")
        return True
    except OSError as e:
        print(f"[pet] Hook auto-install failed: {e}")
        return False


# ─── Status Poller (fallback when hooks aren't active) ───────────

class StatusPoller:
    """Polls /api/status as a fallback signal source.

    Discovers the Hermes backend port from desktop.log, then polls
    gateway_busy + active_agents every few seconds. When the state
    changes, pushes an event to the queue so the pet animates.

    Less precise than hooks (no tool-level detail) but works without
    a gateway restart.
    """

    def __init__(self, event_queue: Queue):
        self.queue = event_queue
        self._running = False
        self._thread: threading.Thread | None = None
        self._port: int | None = None
        self._last_busy = False

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        print("[pet] Status poller started (fallback mode)")

    def stop(self) -> None:
        self._running = False

    def _discover_port(self) -> int | None:
        """Find the Hermes serve backend port from desktop.log."""
        candidates = [
            Path(os.environ.get("HERMES_HOME", "")) / "logs" / "desktop.log",
            Path.home() / "AppData" / "Local" / "hermes" / "logs" / "desktop.log",
        ]
        for log_path in candidates:
            if not log_path.exists():
                continue
            try:
                lines = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
                for line in reversed(lines):
                    if "BACKEND_READY" in line and "port=" in line:
                        port_str = line.rsplit("port=", 1)[-1].strip()
                        port = int(port_str)
                        # Verify the port is actually listening
                        try:
                            req = urllib.request.Request(
                                f"http://127.0.0.1:{port}/api/status", method="GET"
                            )
                            with urllib.request.urlopen(req, timeout=1) as resp:
                                data = json.loads(resp.read().decode("utf-8"))
                                if "gateway_running" in data:
                                    return port
                        except (OSError, json.JSONDecodeError, ValueError):
                            continue
            except OSError:
                continue
        return None

    def _poll_loop(self) -> None:
        while self._running:
            # Discover port if needed
            if self._port is None:
                self._port = self._discover_port()
                if self._port is None:
                    time.sleep(POLL_INTERVAL_S * 3)  # slow retry
                    continue

            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{self._port}/api/status", method="GET"
                )
                with urllib.request.urlopen(req, timeout=2) as resp:
                    data = json.loads(resp.read().decode("utf-8"))

                busy = bool(data.get("gateway_busy"))
                agents = int(data.get("active_agents", 0))

                if busy and not self._last_busy:
                    self.queue.put({"event": "agent:start", "source": "poll"})
                    self._last_busy = True
                elif not busy and self._last_busy:
                    self.queue.put({"event": "response.completed", "source": "poll"})
                    self._last_busy = False

            except (OSError, json.JSONDecodeError, ValueError):
                # Port might be stale (gateway restarted), rediscover next loop
                self._port = None

            time.sleep(POLL_INTERVAL_S)


# ─── Hermes Backend Client (JSON-RPC over local HTTP/WS) ────────

class HermesBackendClient:
    """Talks to the local Hermes desktop backend (hermes serve).

    Discovery: read the latest HERMES_BACKEND_READY port from desktop.log.
    Local loopback connections need no auth (verified: /api/health reports
    auth_required: false).

    Sending:   POST /api/rpc  {"method": "prompt.submit", ...}
    Receiving: WS   /api/ws   events: message.start / delta / complete
    """

    LOG_PATH = Path(os.environ.get("APPDATA", Path.home())) / ".." / "Local" / "hermes" / "logs" / "desktop.log"

    def __init__(self) -> None:
        self._port: int | None = None
        self._ws = None
        self._ws_thread: threading.Thread | None = None
        self._running = False
        self._event_cbs: list = []  # list[callable(dict)]
        self._req_id = 0

    # ── Discovery ──

    def discover_port(self) -> int | None:
        """Find the live desktop backend port from desktop.log."""
        # Try each candidate port found in the log, newest first
        try:
            text = self.LOG_PATH.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return None
        ports = re.findall(r"HERMES_BACKEND_READY port=(\d+)", text)
        for p in reversed(ports[-8:]):  # newest first, cap 8 tries
            port = int(p)
            if self._probe(port):
                self._port = port
                return port
        return None

    def _probe(self, port: int) -> bool:
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/health", method="GET")
            with urllib.request.urlopen(req, timeout=1.5) as r:
                data = json.loads(r.read().decode("utf-8"))
                return bool(data.get("ok"))
        except Exception:
            return False

    @property
    def port(self) -> int | None:
        if self._port is None or not self._probe(self._port):
            self._port = None
            self.discover_port()
        return self._port

    # ── JSON-RPC over HTTP ──

    def rpc(self, method: str, params: dict | None = None, timeout: float = 10.0) -> dict:
        """POST a JSON-RPC request to the backend. Raises on transport error."""
        if self.port is None:
            raise ConnectionError("Hermes backend not found (is the desktop app running?)")
        self._req_id += 1
        payload = json.dumps({
            "jsonrpc": "2.0", "id": self._req_id,
            "method": method, "params": params or {},
        }).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{self._port}/api/rpc",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    # ── Sessions ──

    def list_sessions(self, limit: int = 6) -> list[dict]:
        """Recent sessions: [{session_id, title, updated_at}, ...]"""
        try:
            resp = self.rpc("session.list", {"limit": limit + 2}, timeout=5.0)
            rows = resp.get("result") or resp.get("data") or []
            if isinstance(rows, dict):
                rows = rows.get("sessions") or rows.get("items") or []
            out = []
            for s in rows[:limit]:
                sid = s.get("session_id") or s.get("id") or ""
                title = s.get("title") or s.get("name") or "未命名会话"
                out.append({"session_id": str(sid), "title": str(title),
                            "updated_at": s.get("updated_at") or s.get("updated") or ""})
            return out
        except Exception:
            return []

    # ── WebSocket event stream ──

    def on_event(self, cb) -> None:
        """Register callback(dict) for every gateway event."""
        self._event_cbs.append(cb)

    def start_ws(self) -> bool:
        """Connect to /api/ws and pump events to callbacks. Non-blocking."""
        if self._ws is not None:
            return True
        port = self.port
        if port is None:
            return False
        try:
            import websocket  # websocket-client package
        except ImportError:
            print("[chat] websocket-client not installed — streaming disabled")
            return False
        url = f"ws://127.0.0.1:{port}/api/ws"
        try:
            ws = websocket.WebSocketApp(
                url,
                on_message=self._on_ws_message,
                on_error=lambda _ws, err: print(f"[chat] ws error: {err}"),
                on_close=lambda _ws, code, msg: self._ws_closed(),
            )
            self._ws = ws
            self._running = True
            self._ws_thread = threading.Thread(target=ws.run_forever, daemon=True)
            self._ws_thread.start()
            return True
        except Exception as e:
            print(f"[chat] ws connect failed: {e}")
            self._ws = None
            return False

    def _on_ws_message(self, _ws, raw: str) -> None:
        try:
            frame = json.loads(raw)
        except (ValueError, TypeError):
            return
        # {"jsonrpc":"2.0","method":"event","params":{type,payload,session_id}}
        params = frame.get("params") or {}
        if frame.get("method") == "event" or "type" in params:
            event = {
                "type": params.get("type", ""),
                "session_id": params.get("session_id", ""),
                "payload": params.get("payload") or {},
            }
            for cb in self._event_cbs:
                try:
                    cb(event)
                except Exception:
                    pass

    def _ws_closed(self) -> None:
        self._ws = None
        if self._running:
            # auto-reconnect after a pause (backend may have restarted)
            def _retry():
                time.sleep(2.0)
                if self._running:
                    self.start_ws()
            threading.Thread(target=_retry, daemon=True).start()

    def stop_ws(self) -> None:
        self._running = False
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None

    def send_message(self, session_id: str, text: str) -> dict:
        """Send a user message into a session via prompt.submit."""
        return self.rpc("prompt.submit", {
            "session_id": session_id,
            "text": text,
        }, timeout=15.0)


# ─── Bubble Chat (double-click to open) ──────────────────────────

class BubbleChat:
    """Chat that lives in the pet's own speech bubble — no big window.

    Double-click the pet → a small input bubble appears beside her.
    Type + Enter → message sends; her reply streams into her head-top
    speech bubble with typewriter effect, just like her idle lines.

    Transport: HTTP to the desktop plugin bridge (plugin.js proxies
    authenticated gateway RPC for us — direct API calls 401 without token).
    """

    INPUT_W = 220
    FONT = ("Microsoft YaHei UI", 9)
    # bubble text typing speed (ms per char batch)
    TYPE_STEP = 2   # chars per tick
    TYPE_MS = 60    # tick interval
    LONG_REPLY_THRESHOLD = 15

    def __init__(self, pet: "ShorekeeperPet") -> None:
        self.pet = pet
        self.active = False
        self.input_win: tk.Toplevel | None = None
        self.session_id: str = ""
        self.session_title: str = ""
        self._stream_buf: str = ""
        self._shown_len = 0
        self._type_after: str | None = None
        self._focus_after: str | None = None
        self._reply_complete = False
        self._active_request_id = ""
        self._hint_label: tk.Label | None = None
        self.side_bubble = getattr(pet, "side_bubble", None)
        self._using_side_bubble = False

    # ── open / close ──

    def open(self, select_session: bool = False) -> None:
        if self.active:
            self.close()
            return
        self.active = True
        self._show_input()
        self.pet.say("我在听。")
        if select_session:
            self.pet.say("右键菜单里可以切换会话。")

    def close(self) -> None:
        self.active = False
        self._cancel_typing()
        self._hide_input()

    def toggle(self) -> None:
        self.close() if self.active else self.open()

    # ── input bubble (small Toplevel near the pet) ──

    def _show_input(self) -> None:
        self._hide_input()
        w = tk.Toplevel(self.pet.root)
        w.overrideredirect(True)
        topmost_var = getattr(self.pet, "topmost_var", None)
        w.attributes(
            "-topmost",
            bool(topmost_var.get()) if topmost_var is not None else True,
        )
        w.configure(bg="#f8fbff")

        frm = tk.Frame(w, bg="#6baee8", bd=1)
        frm.pack(padx=1, pady=1)
        inner = tk.Frame(frm, bg="#f8fbff")
        inner.pack()

        self._entry = tk.Entry(inner, width=int(self.INPUT_W / 9),
                               bg="white", fg="#17365d", relief="flat",
                               font=self.FONT, insertbackground="#17365d")
        self._entry.pack(ipady=4, padx=6, pady=4)
        self._entry.insert(0, "")
        self._entry.configure(state="normal")
        self._entry.focus_set()
        self._entry.bind("<Return>", lambda _e: self._send())
        self._entry.bind("<Escape>", lambda _e: self.close())

        self._hint_label = tk.Label(
            inner,
            text="Enter 发送 · Esc 关闭",
            bg="#f8fbff",
            fg="#7a9ab8",
            font=("Microsoft YaHei UI", 7),
        )
        self._hint_label.pack(pady=(0, 3))

        self.input_win = w
        self.reposition_input()
        present_auxiliary_window(self.pet, w)

        # auto-close when focus is lost (feels like a bubble popping away)
        w.bind(
            "<FocusOut>",
            lambda _e, expected=w: self._schedule_focus_check(expected),
        )

    def reposition_input(self) -> None:
        """Keep an open input bubble centered below the current pet size."""
        w = self.input_win
        if w is None:
            return
        self.pet.root.update_idletasks()
        pet_w = int(getattr(self.pet, "canvas_width", CANVAS_WIDTH))
        pet_h = int(getattr(self.pet, "canvas_height", CANVAS_HEIGHT))
        pet_x = int(self.pet.root.winfo_x())
        pet_y = int(self.pet.root.winfo_y())
        work_area_for_point = getattr(self.pet, "_work_area_for_point", None)
        if callable(work_area_for_point):
            left, top, right, bottom = work_area_for_point(
                pet_x + pet_w // 2, pet_y + pet_h // 2)
        else:
            left = int(self.pet.root.winfo_vrootx())
            top = int(self.pet.root.winfo_vrooty())
            right = left + int(self.pet.root.winfo_vrootwidth())
            bottom = top + int(self.pet.root.winfo_vrootheight())

        w.update_idletasks()
        ww = int(w.winfo_reqwidth())
        wh = int(w.winfo_reqheight())
        margin = 4
        desired_x = pet_x - (ww - pet_w) // 2
        x = max(left + margin, min(desired_x, right - ww - margin))
        below_y = pet_y + pet_h + 6
        desired_y = (
            below_y if below_y + wh <= bottom - margin
            else pet_y - wh - 6
        )
        y = max(top + margin, min(desired_y, bottom - wh - margin))
        # '+-N' is Tk's absolute-negative virtual desktop coordinate form.
        w.geometry(f"{ww}x{wh}+{x}+{y}")
        # keep glued to pet on move
        self._input_pos = (x, y)

    def _schedule_focus_check(self, expected_window: tk.Toplevel) -> None:
        if expected_window is not self.input_win:
            return
        self._cancel_focus_check()
        self._focus_after = self.pet.root.after(
            400, lambda: self._run_focus_check(expected_window))

    def _run_focus_check(self, expected_window: tk.Toplevel) -> None:
        self._focus_after = None
        self._check_focus(expected_window)

    def _cancel_focus_check(self) -> None:
        if self._focus_after is not None:
            try:
                self.pet.root.after_cancel(self._focus_after)
            except (tk.TclError, ValueError):
                pass
            self._focus_after = None

    def _check_focus(self, expected_window: tk.Toplevel | None = None) -> None:
        if expected_window is not None and expected_window is not self.input_win:
            return
        if bool(getattr(self.pet, "_quiet_hidden", False)):
            return
        if self.input_win is None:
            return
        try:
            focused = self.input_win.focus_get()
        except tk.TclError:
            self.close()
            return
        if focused is None:
            self.close()

    def _hide_input(self) -> None:
        self._cancel_focus_check()
        if self.input_win is not None:
            try:
                self.input_win.destroy()
            except tk.TclError:
                pass
            self.input_win = None
        self._hint_label = None

    # ── sending ──

    def _new_prompt(self, text: str) -> dict[str, str]:
        return {
            "text": text,
            "session_id": self.session_id,
            "request_id": uuid.uuid4().hex,
        }

    def _send(self) -> None:
        if self.input_win is None:
            return
        text = self._entry.get().strip()
        if not text:
            return
        self._entry.delete(0, "end")
        self._dispatch_prompt(self._new_prompt(text))

    def _dispatch_prompt(self, prompt: dict[str, str]) -> None:
        """Send immediately — the gateway natively queues mid-turn submits."""
        self.pet.say("…")

        def worker() -> None:
            ok = self._post_via_plugin(prompt)
            if ok is False:
                def report_failure() -> None:
                    self.pet.say("（连不上 Hermes…检查桌面版是否在运行）")
                self.pet.root.after(0, report_failure)

        threading.Thread(target=worker, daemon=True).start()

    def _post_via_plugin(self, prompt: dict[str, str]) -> bool | None:
        """Stash one identified message in the pet's local outbox."""
        try:
            PetHTTPHandler.chat_outbox.put(dict(prompt))
            return True
        except Exception as e:
            print(f"[chat] outbox stash failed: {e}")
            return False

    # ── reply rendering (typewriter into her bubble) ──

    def _begin_reply(self) -> None:
        """Reset the renderer before one newly submitted chat reply."""
        self._cancel_typing()
        self._reply_complete = False
        self._using_side_bubble = False
        if self.side_bubble is not None:
            self.side_bubble.close(animated=False)
        clear = getattr(self.pet, "clear_head_bubble", None)
        if callable(clear):
            clear()

    def _render_reply_text(self, text: str, *, final: bool) -> None:
        """Route a reply to the head bubble or the independent side bubble."""
        long_reply = len(text.strip()) > self.LONG_REPLY_THRESHOLD
        if (long_reply or self._using_side_bubble) and self.side_bubble is not None:
            if not self._using_side_bubble:
                clear = getattr(self.pet, "clear_head_bubble", None)
                if callable(clear):
                    clear()
                self.side_bubble.start_reply()
                self._using_side_bubble = True
            self.side_bubble.set_text(text)
            if final:
                self.side_bubble.complete_reply()
            return

        self.pet.say(text if text else "…", hold=not final)

    def _accept_request(self, request_id: str) -> None:
        """A different request_id means a new reply: reset the renderer.

        The gateway serializes mid-turn submits, so the first event of each
        new request is our signal to close the old side bubble and clear
        stale state (this replaced the old send-side _begin_reply call).
        """
        if not request_id or request_id == self._active_request_id:
            return
        if self._active_request_id or self._reply_complete or self._using_side_bubble:
            self._begin_reply()
        self._active_request_id = request_id

    def on_reply_delta(self, text: str, request_id: str = "") -> None:
        """Render a delta for the currently-displayed reply only."""
        self._accept_request(request_id)
        if self._reply_complete:
            return
        self._stream_buf = text  # full accumulated text so far
        if self._type_after is None:
            self._tick_typing()

    def on_reply_complete(self, text: str, request_id: str = "") -> None:
        self._accept_request(request_id)
        self._reply_complete = True
        self._cancel_typing(reset=False)
        self._stream_buf = text
        self._shown_len = len(text)
        self._render_reply_text(text, final=True)
        self._shown_len = 0
        self._stream_buf = ""
        self._active_request_id = ""

    def _cancel_typing(self, *, reset: bool = True) -> None:
        if self._type_after is not None:
            try:
                self.pet.root.after_cancel(self._type_after)
            except (tk.TclError, ValueError):
                pass
            self._type_after = None
        if reset:
            self._stream_buf = ""
            self._shown_len = 0

    def _tick_typing(self, final: bool = False) -> None:
        if self._type_after is not None:
            try:
                self.pet.root.after_cancel(self._type_after)
            except (tk.TclError, ValueError):
                pass
            self._type_after = None
        buf = self._stream_buf
        if self._shown_len < len(buf):
            self._shown_len = min(len(buf), self._shown_len + self.TYPE_STEP * 3)
        visible = buf[:self._shown_len]
        self._render_reply_text(visible if visible else "…", final=False)
        if self._shown_len < len(buf) or (final and self._shown_len < len(buf)):
            self._type_after = self.pet.root.after(
                self.TYPE_MS, lambda: self._tick_typing(final=final))
        elif final:
            # done: let the bubble auto-expire as usual
            self._shown_len = 0
            self._stream_buf = ""
            self._render_reply_text(buf, final=True)


# ─── Side Bubble (long chat replies) ─────────────────────────────

class SideBubble:
    """Independent scrollable bubble for replies longer than 15 characters."""

    BODY_W = 300
    BODY_H = 170          # cap = exactly 8 lines (8*17 + 34 padding)
    MIN_BODY_W = 140
    MAX_VISIBLE_LINES = 8
    TAIL_W = 16
    GAP = 10
    EDGE_PAD = 6
    AUTO_CLOSE_MS = 15_000

    BG = "#e8f2fb"        # liquid-glass: slightly deeper tint reads as translucent
    BG_ALPHA = 0.88       # window-level transparency (Windows -alpha)
    BORDER = "#6baee8"
    TEXT_FG = "#17365d"
    CLOSE_FG = "#7a9ab8"
    CLOSE_HOVER = "#4f95cf"
    FONT = ("Microsoft YaHei UI", 9)
    LINE_H = 17           # measured font line height for dynamic sizing
    BODY_PAD_Y = 34      # frame(20) + Text pady(14)
    MIN_BODY_H = 44      # one line + padding — the smallest the bubble gets

    def __init__(
        self,
        pet: "ShorekeeperPet",
        work_area_provider: Callable[[tk.Misc], tuple[int, int, int, int]] | None = None,
    ) -> None:
        self.pet = pet
        self._work_area_provider = work_area_provider or self._default_work_area
        self.window: tk.Toplevel | None = None
        self.canvas: tk.Canvas | None = None
        self.content_frame: tk.Frame | None = None
        self.text_widget: tk.Text | None = None
        self.scrollbar: tk.Scrollbar | None = None
        self.close_label: tk.Label | None = None
        self.close_after_id: str | None = None
        self.pinned = False
        self.completed = False
        self.dismissed = False
        self.last_text = ""
        self.side = "right"
        self.tail_side = "left"
        self.body_width = self.BODY_W
        self.current_h = self.BODY_H   # live height (class BODY_H is the cap)
        self._tail_y = self.current_h // 3

    @property
    def total_width(self) -> int:
        return self.body_width + self.TAIL_W

    def _height_for_text(self, text: str) -> int:
        """Bubble height that hugs the actual wrapped text, capped at 8 lines.

        Measures real display lines from the Text widget itself (handles
        CJK/ASCII mix correctly); falls back to a character estimate only
        if the widget isn't ready yet.
        """
        if not text:
            return self.MIN_BODY_H
        if self.text_widget is not None:
            try:
                displayed = int(self.text_widget.tk.call(
                    self.text_widget._w, "count", "-update", "-displaylines",
                    "1.0", "end"))
                lines = max(1, min(displayed, self.MAX_VISIBLE_LINES))
                return max(self.MIN_BODY_H,
                           min(self.BODY_H, lines * self.LINE_H + self.BODY_PAD_Y))
            except (tk.TclError, ValueError):
                pass
        # fallback estimate (first open, before the widget exists)
        usable = max(10, self.body_width - 18 - 14)
        per_line = max(1, usable // 12)
        wrapped = sum(
            max(1, -(-len(raw) // per_line)) for raw in text.splitlines() or [""]
        )
        lines = max(1, min(wrapped, self.MAX_VISIBLE_LINES))
        return max(self.MIN_BODY_H,
                   min(self.BODY_H, lines * self.LINE_H + self.BODY_PAD_Y))

    def _apply_height(self, text: str) -> None:
        """Resize window/canvas to fit `text` and re-anchor the tail."""
        self.current_h = self._height_for_text(text)
        if self.window is None:
            return
        try:
            self.reposition()
        except tk.TclError:
            pass

    @staticmethod
    def _default_work_area(root: tk.Misc) -> tuple[int, int, int, int]:
        """Return the nearest monitor's work area in this process's coordinates."""
        if sys.platform == "win32":
            try:
                import ctypes
                from ctypes import wintypes

                class MONITORINFO(ctypes.Structure):
                    _fields_ = (
                        ("cbSize", wintypes.DWORD),
                        ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT),
                        ("dwFlags", wintypes.DWORD),
                    )

                user32 = ctypes.windll.user32
                monitor_from_window = ctypes.WINFUNCTYPE(
                    wintypes.HANDLE,
                    wintypes.HWND,
                    wintypes.DWORD,
                )(("MonitorFromWindow", user32))
                get_monitor_info = ctypes.WINFUNCTYPE(
                    wintypes.BOOL,
                    wintypes.HANDLE,
                    ctypes.POINTER(MONITORINFO),
                )(("GetMonitorInfoW", user32))

                monitor = monitor_from_window(int(root.winfo_id()), 2)
                info = MONITORINFO()
                info.cbSize = ctypes.sizeof(MONITORINFO)
                if monitor and get_monitor_info(monitor, ctypes.byref(info)):
                    r = info.rcWork
                    return int(r.left), int(r.top), int(r.right), int(r.bottom)
            except (OSError, AttributeError, tk.TclError):
                pass

        left = int(root.winfo_vrootx())
        top = int(root.winfo_vrooty())
        return (
            left,
            top,
            left + int(root.winfo_vrootwidth()),
            top + int(root.winfo_vrootheight()),
        )

    def start_reply(self) -> None:
        """Open a fresh side bubble without starting its close timer."""
        self.close(animated=False)
        self.dismissed = False
        self.pinned = False
        self.completed = False
        self.last_text = ""
        self.body_width = self.BODY_W

        w = tk.Toplevel(self.pet.root)
        w.title("守岸人 · 长回复")
        w.overrideredirect(True)
        topmost_var = getattr(self.pet, "topmost_var", None)
        w.attributes(
            "-topmost",
            bool(topmost_var.get()) if topmost_var is not None else True,
        )
        w.configure(bg=TRANSPARENT_COLOR)
        try:
            w.attributes("-transparentcolor", TRANSPARENT_COLOR)
        except tk.TclError:
            pass
        try:
            # liquid-glass: whole window slightly translucent
            w.attributes("-alpha", self.BG_ALPHA)
        except tk.TclError:
            pass

        self.canvas = tk.Canvas(
            w,
            width=self.total_width,
            height=self.current_h,
            bg=TRANSPARENT_COLOR,
            highlightthickness=0,
            bd=0,
        )
        self.canvas.place(x=0, y=0, width=self.total_width, height=self.current_h)

        self.content_frame = tk.Frame(w, bg=self.BG, bd=0)
        self.content_frame.grid_rowconfigure(0, weight=1)
        self.content_frame.grid_columnconfigure(0, weight=1)

        self.text_widget = tk.Text(
            self.content_frame,
            wrap="word",
            state="disabled",
            bg=self.BG,
            fg=self.TEXT_FG,
            insertbackground=self.TEXT_FG,
            selectbackground="#cfe9fb",
            selectforeground=self.TEXT_FG,
            relief="flat",
            bd=0,
            highlightthickness=0,
            padx=7,
            pady=7,
            font=self.FONT,
            cursor="arrow",
        )
        # No visible scrollbar — this is her speech bubble, not a document.
        # Wheel scrolling still works via _on_mousewheel below.
        self.scrollbar = None
        self.text_widget.grid(row=0, column=0, sticky="nsew")
        self.text_widget.bind("<MouseWheel>", self._on_mousewheel)
        self.text_widget.bind("<Button-1>", self._on_content_click)

        self.close_label = tk.Label(
            w,
            text="✕",
            bg=self.BG,
            fg=self.CLOSE_FG,
            font=("Microsoft YaHei UI", 10, "bold"),
            cursor="hand2",
            bd=0,
            padx=3,
            pady=0,
        )
        self.close_label.bind("<Enter>", self._on_close_enter)
        self.close_label.bind("<Leave>", self._on_close_leave)
        self.close_label.bind("<Button-1>", self._on_close_click)

        self.window = w
        self.reposition()
        present_auxiliary_window(self.pet, w)

    def _draw_shell(self) -> None:
        if self.canvas is None:
            return
        cv = self.canvas
        cv.delete("shell")
        body_x = self.TAIL_W if self.side == "right" else 0
        tail_y = self._tail_y

        if self.tail_side == "left":
            points = (0, tail_y, body_x + 3, tail_y - 11, body_x + 3, tail_y + 11)
        else:
            tip_x = self.total_width
            points = (tip_x, tail_y, self.body_width - 3, tail_y - 11,
                      self.body_width - 3, tail_y + 11)
        cv.create_polygon(
            *points,
            fill=self.BG,
            outline=self.BORDER,
            width=2,
            tags=("shell", "tail"),
        )

        x1, y1 = body_x + 1, 1
        x2, y2 = body_x + self.body_width - 2, self.current_h - 2
        radius = 14
        rounded = (
            x1 + radius, y1, x2 - radius, y1, x2, y1,
            x2, y1 + radius, x2, y2 - radius, x2, y2,
            x2 - radius, y2, x1 + radius, y2, x1, y2,
            x1, y2 - radius, x1, y1 + radius, x1, y1,
        )
        cv.create_polygon(
            *rounded,
            smooth=True,
            splinesteps=20,
            fill=self.BG,
            outline=self.BORDER,
            width=2,
            tags=("shell", "body"),
        )
        cv.tag_lower("shell")

    def _layout_body(self) -> None:
        if self.content_frame is None:
            return
        body_x = self.TAIL_W if self.side == "right" else 0
        self.content_frame.place(
            x=body_x + 9,
            y=10,
            width=self.body_width - 18,
            height=self.current_h - 20,
        )
        if self.close_label is not None:
            if self.pinned:
                self.close_label.place(
                    x=body_x + self.body_width - 30,
                    y=7,
                    width=20,
                    height=20,
                )
                self.close_label.lift()
            else:
                self.close_label.place_forget()

    def reposition(self) -> None:
        """Follow the pet, mirror at the current monitor midpoint, and clamp."""
        if self.window is None:
            return
        try:
            self.pet.root.update_idletasks()
            px = int(self.pet.root.winfo_x())
            py = int(self.pet.root.winfo_y())
            pw = int(getattr(self.pet, "canvas_width", self.pet.root.winfo_width()))
            ph = int(getattr(self.pet, "canvas_height", self.pet.root.winfo_height()))
            left, top, right, bottom = self._work_area_provider(self.pet.root)
        except tk.TclError:
            return

        pet_center = px + pw / 2
        monitor_center = (left + right) / 2
        preferred = "right" if pet_center < monitor_center else "left"
        spaces = {
            "left": max(0, px - self.GAP - (left + self.EDGE_PAD)),
            "right": max(
                0, (right - self.EDGE_PAD) - (px + pw + self.GAP)),
        }
        other = "left" if preferred == "right" else "right"
        full_width = self.BODY_W + self.TAIL_W
        if spaces[preferred] >= full_width:
            self.side = preferred
        elif spaces[other] >= full_width:
            self.side = other
        else:
            self.side = max((preferred, other), key=spaces.__getitem__)

        usable_body = spaces[self.side] - self.TAIL_W
        self.body_width = min(
            self.BODY_W,
            max(self.MIN_BODY_W, int(usable_body)),
        )
        self.tail_side = "left" if self.side == "right" else "right"

        if self.canvas is not None:
            self.canvas.configure(width=self.total_width, height=self.current_h)
            self.canvas.place(
                x=0, y=0, width=self.total_width, height=self.current_h)

        if self.side == "right":
            x = px + pw + self.GAP
        else:
            x = px - self.GAP - self.total_width
        x = max(left + self.EDGE_PAD, min(x, right - self.total_width - self.EDGE_PAD))

        target_y = py + int(ph * 0.42)
        desired_tail_y = self.current_h // 3
        y = target_y - desired_tail_y
        y = max(top + self.EDGE_PAD, min(y, bottom - self.current_h - self.EDGE_PAD))
        self._tail_y = max(28, min(target_y - y, self.current_h - 28))

        self.window.geometry(f"{self.total_width}x{self.current_h}+{int(x)}+{int(y)}")
        self._draw_shell()
        self._layout_body()

    def set_text(self, full_text: str) -> None:
        """Append a prefix delta when possible; replace safely otherwise."""
        if self.dismissed:
            return
        if self.window is None:
            self.start_reply()
        if self.text_widget is None:
            return

        widget = self.text_widget
        widget.configure(state="normal")
        if full_text.startswith(self.last_text):
            suffix = full_text[len(self.last_text):]
            if suffix:
                widget.insert("end", suffix)
        else:
            widget.delete("1.0", "end")
            widget.insert("1.0", full_text)
        widget.configure(state="disabled")
        self.last_text = full_text
        self._apply_height(full_text)
        widget.see("end")
        widget.update_idletasks()
        # after the resize settles, guarantee the stream sticks to the bottom
        widget.see("end")

    def complete_reply(self) -> None:
        if self.dismissed:
            return
        self.completed = True
        if not self.pinned:
            self._schedule_close()

    def _on_mousewheel(self, event: tk.Event) -> str:
        if self.text_widget is not None and getattr(event, "delta", 0):
            steps = -1 if event.delta > 0 else 1
            self.text_widget.yview_scroll(steps, "units")
        self._on_scroll_activity()
        return "break"

    def _on_scroll_activity(self, _event: tk.Event | None = None) -> None:
        if self.completed and not self.pinned:
            self._schedule_close()

    def _on_content_click(self, _event: tk.Event | None = None) -> None:
        if self.window is None:
            return
        self.pinned = True
        self._cancel_close()
        self._layout_body()

    def _on_close_enter(self, _event: tk.Event | None = None) -> None:
        if self.close_label is not None:
            self.close_label.configure(fg=self.CLOSE_HOVER)

    def _on_close_leave(self, _event: tk.Event | None = None) -> None:
        if self.close_label is not None:
            self.close_label.configure(fg=self.CLOSE_FG)

    def _on_close_click(self, _event: tk.Event | None = None) -> str:
        self.close(animated=False)
        self.dismissed = True
        return "break"

    def _schedule_close(self, delay_ms: int | None = None) -> None:
        self._cancel_close()
        if (
            self.window is None
            or self.pinned
            or not self.completed
            or bool(getattr(self.pet, "_quiet_hidden", False))
        ):
            return
        delay = self.AUTO_CLOSE_MS if delay_ms is None else int(delay_ms)
        self.close_after_id = self.pet.root.after(
            delay, lambda: self.close(animated=True))

    def _cancel_close(self) -> None:
        if self.close_after_id is not None:
            try:
                self.pet.root.after_cancel(self.close_after_id)
            except (tk.TclError, ValueError):
                pass
            self.close_after_id = None

    def set_quiet_hidden(self, hidden: bool) -> None:
        if hidden:
            self._cancel_close()
        elif self.completed and not self.pinned and self.window is not None:
            self._schedule_close()

    def close(self, animated: bool = True) -> None:
        """Destroy the independent bubble; animated is reserved for UI polish."""
        self._cancel_close()
        if self.window is not None:
            try:
                self.window.destroy()
            except tk.TclError:
                pass
        self.window = None
        self.canvas = None
        self.content_frame = None
        self.text_widget = None
        self.scrollbar = None
        self.close_label = None
        self.last_text = ""
        self.completed = False
        self.pinned = False


# ─── Custom Popup Menu ───────────────────────────────────────────

class PetMenu:
    """Custom canvas popup menu — light ice-blue theme matching pet bubbles.

    Replaces the ugly native tk.Menu with a Toplevel + Canvas approach that
    supports hover highlighting, checkmarks, radio dots, and styled separators.
    """

    BG = "#f8fbff"
    BORDER = "#6baee8"
    TEXT = "#17365d"
    TEXT_DANGER = "#c0392b"
    TEXT_MUTED = "#7a9ab8"
    HOVER = "#d8ebfa"
    SEPARATOR = "#c5dcef"
    CHECK = "#4a90d9"

    ITEM_H = 26
    SEP_H = 7
    HEADER_H = 20
    PAD = 6
    WIDTH = 152
    FONT = ("Microsoft YaHei UI", 9)

    def __init__(self, pet: "ShorekeeperPet") -> None:
        self.pet = pet
        self.win: tk.Toplevel | None = None
        self._items: list[dict[str, Any]] = []
        self._item_rects: list[tuple[int, int, int] | None] = []
        self._hover_idx: int | None = None
        self._cv: tk.Canvas | None = None

    # ── Build ──

    def _build_items(self) -> list[dict[str, Any]]:
        return [
            {"type": "command", "label": "💬 打开聊天",
             "command": self.pet.open_chat},
            {"type": "command", "label": "🖥 打开 Hermes",
             "command": self._open_hermes_app},
            {"type": "separator"},
            {"type": "check", "label": "自由走动",
             "checked": self.pet.wander_var.get(), "command": self._toggle_wander},
            {"type": "check", "label": "总在最前",
             "checked": self.pet.topmost_var.get(), "command": self._toggle_topmost},
            {"type": "separator"},
            {"type": "submenu", "label": "大小", "expanded": False},
            {"type": "separator"},
            {"type": "command", "label": "安静 5 分钟",
             "command": self.pet._hide_for_five_minutes},
            {"type": "separator"},
            {"type": "command", "label": "退出", "danger": True,
             "command": self.pet.close},
        ]

    # Win32 helpers for the "Open Hermes" menu item (loaded lazily).
    _WIN32_SHOW = None

    def _open_hermes_app(self) -> None:
        """Bring the Hermes Desktop main window up from any state.

        Covers: foreground, background, minimized (iconic), and hidden-to-
        tray. Falls back to launching the exe when Hermes is not running.
        """
        try:
            if PetMenu._WIN32_SHOW is None:
                import ctypes

                class _W32:
                    user32 = ctypes.windll.user32

                # SW_HIDE=0 SW_SHOW=5 SW_RESTORE=9
                _W32.user32.ShowWindow.restype = ctypes.c_bool
                _W32.user32.SetForegroundWindow.restype = ctypes.c_bool
                PetMenu._WIN32_SHOW = _W32
            import subprocess
            w32 = PetMenu._WIN32_SHOW
            script = (
                "$p = Get-Process -Name 'Hermes' -ErrorAction SilentlyContinue "
                "| Where-Object { $_.MainWindowHandle -ne 0 } "
                "| Sort-Object MainWindowHandle -Descending "
                "| Select-Object -First 1;"
                "if ($p) {"
                "  $h = $p.MainWindowHandle;"
                "  Add-Type 'using System;using System.Runtime.InteropServices;"
                "public class W{[DllImport(\"user32.dll\")]public static extern bool ShowWindow(IntPtr h,int n);"
                "[DllImport(\"user32.dll\")]public static extern bool SetForegroundWindow(IntPtr h);}';"
                "  [void][W]::ShowWindow($h, 5);"     # SW_SHOW (un-hide from tray)
                "  [void][W]::ShowWindow($h, 9);"     # SW_RESTORE (un-minimize)
                "  [void][W]::SetForegroundWindow($h)"
                "} else {"
                "  $exe = '$env:LOCALAPPDATA/hermes/hermes-agent/apps/desktop/release/win-unpacked/Hermes.exe';"
                "  if (Test-Path $exe) { Start-Process $exe } else { Start-Process 'hermes://open' -ErrorAction SilentlyContinue } "
                "}"
            )
            subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-Command", script],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as e:
            print(f"[menu] activate Hermes failed: {e}")

    def show(self, x: int, y: int) -> None:
        if self.win is not None:
            self.close()
            return  # second right-click just closes

        self._submenu_open = False
        self._items = self._build_items()

        self._build_window(x, y)

    def _build_window(self, x: int, y: int) -> None:
        # Measure total height
        h = self.PAD * 2
        for item in self._items:
            t = item["type"]
            if t == "separator":
                h += self.SEP_H
            elif t == "header":
                h += self.HEADER_H
            else:
                h += self.ITEM_H

        # Clamp to screen
        sw = self.pet.root.winfo_screenwidth()
        sh = self.pet.root.winfo_screenheight()
        x = max(2, min(x, sw - self.WIDTH - 2))
        y = max(2, min(y, sh - h - 2))

        if self.win is not None:
            self.win.destroy()
            self.win = None
        self.win = tk.Toplevel(self.pet.root)
        self.win.overrideredirect(True)
        self.win.geometry(f"{self.WIDTH}x{h}+{x}+{y}")
        self.win.attributes("-topmost", True)
        self.win.configure(bg=self.BG)

        cv = tk.Canvas(
            self.win, width=self.WIDTH, height=h, bg=self.BG,
            highlightthickness=1, highlightbackground=self.BORDER, bd=0,
        )
        cv.pack(fill="both", expand=True)
        self._cv = cv

        # Draw items
        yp = self.PAD
        self._item_rects = []
        for i, item in enumerate(self._items):
            t = item["type"]
            if t == "separator":
                cv.create_line(10, yp + 3, self.WIDTH - 10, yp + 3,
                               fill=self.SEPARATOR, width=1)
                yp += self.SEP_H
                self._item_rects.append(None)
                continue
            if t == "header":
                cv.create_text(14, yp + self.HEADER_H // 2, text=item["label"],
                               anchor="w", fill=self.TEXT_MUTED, font=self.FONT,
                               tags=("header",))
                yp += self.HEADER_H
                self._item_rects.append(None)
                continue

            indent = 16 if item.get("indent") else 0
            # Background rect (for hover)
            cv.create_rectangle(2, yp, self.WIDTH - 2, yp + self.ITEM_H,
                                fill=self.BG, outline="", tags=("bg", f"bg{i}"))
            # Prefix symbol
            prefix = ""
            if t == "check":
                prefix = "✓" if item["checked"] else ""
            elif t == "radio":
                prefix = "●" if item["selected"] else "○"
            elif t == "submenu":
                prefix = "▾" if item.get("expanded") else "▸"
            if prefix:
                pcolor = self.CHECK if (prefix in "✓●") else self.TEXT_MUTED
                cv.create_text(14 + indent, yp + self.ITEM_H // 2,
                               text=prefix, anchor="w", fill=pcolor,
                               font=self.FONT, tags=(f"item{i}",))
            # Label text
            color = self.TEXT_DANGER if item.get("danger") else self.TEXT
            label = item["label"]
            if t == "submenu" and self._submenu_open:
                # show current size on the submenu row when collapsed
                pass
            cv.create_text(30 + indent, yp + self.ITEM_H // 2,
                           text=label, anchor="w", fill=color,
                           font=self.FONT, tags=(f"item{i}",))
            self._item_rects.append((yp, yp + self.ITEM_H, i))
            yp += self.ITEM_H

        # Events
        cv.bind("<Motion>", self._on_motion)
        cv.bind("<Button-1>", self._on_click)
        self.win.bind("<Escape>", lambda _e: self.close())
        # Grace-period close: leaving starts a 400ms timer; re-entering
        # cancels it. A stray 1px slip no longer kills the menu.
        self.win.bind("<Leave>", lambda _e: self._schedule_close())
        self.win.bind("<Enter>", lambda _e: self._cancel_scheduled_close())

        self._hover_idx = None
        self.win.focus_set()

    def _schedule_close(self) -> None:
        self._cancel_scheduled_close()
        self._close_after = self.pet.root.after(400, self.close)

    def _cancel_scheduled_close(self) -> None:
        if getattr(self, "_close_after", None) is not None:
            try:
                self.pet.root.after_cancel(self._close_after)
            except (tk.TclError, ValueError):
                pass
            self._close_after = None

    def _toggle_submenu(self) -> None:
        """Expand/collapse the 大小 submenu in place."""
        self._submenu_open = not self._submenu_open
        base = self._build_items()
        out = []
        for item in base:
            out.append(item)
            if item["type"] == "submenu":
                item["expanded"] = self._submenu_open
                if self._submenu_open:
                    cur = self.pet.scale_var.get()
                    for val, name in ((80, "小"), (100, "标准"), (130, "大")):
                        out.append({
                            "type": "radio", "label": name, "indent": True,
                            "selected": cur == val,
                            "command": (lambda v=val: self._set_size(v)),
                        })
        self._items = out
        # rebuild at the same on-screen position
        x = y = None
        if self.win is not None:
            x = self.win.winfo_x()
            y = self.win.winfo_y()
        if x is None:
            x = self.pet.root.winfo_x()
            y = self.pet.root.winfo_y()
        # keep the menu from jumping down when it grows: anchor top-left
        self._build_window(x, y)

    # ── Interaction ──

    def _hit_test(self, y: int) -> int | None:
        for entry in self._item_rects:
            if entry is None:
                continue
            y0, y1, i = entry
            if y0 <= y <= y1:
                return i
        return None

    def _on_motion(self, event: tk.Event) -> None:
        if self._cv is None:
            return
        idx = self._hit_test(event.y)
        if idx == self._hover_idx:
            return
        # clear old
        if self._hover_idx is not None:
            self._cv.itemconfig(f"bg{self._hover_idx}", fill=self.BG)
        # set new
        self._hover_idx = idx
        if idx is not None:
            self._cv.itemconfig(f"bg{idx}", fill=self.HOVER)

    def _on_click(self, event: tk.Event) -> None:
        idx = self._hit_test(event.y)
        if idx is None:
            self.close()
            return
        item = self._items[idx]
        if item["type"] == "submenu":
            # keep the menu open; expand/collapse in place
            self._toggle_submenu()
            return
        cmd = item.get("command")
        self.close()
        if cmd:
            cmd()

    def close(self) -> None:
        self._cancel_scheduled_close()
        if self.win is not None:
            self.win.destroy()
            self.win = None
        self._cv = None
        self._hover_idx = None

    # ── Toggle wrappers ──

    def _toggle_wander(self) -> None:
        self.pet.wander_var.set(not self.pet.wander_var.get())
        self.pet._setting_changed()

    def _toggle_topmost(self) -> None:
        self.pet.topmost_var.set(not self.pet.topmost_var.get())
        self.pet._toggle_topmost()

    def _set_size(self, val: int) -> None:
        self.pet.scale_var.set(val)
        self.pet._resize()


# ─── Main Application ────────────────────────────────────────────

class ShorekeeperPet:

    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("守岸人桌宠")
        self.root.overrideredirect(True)
        self.root.configure(bg=TRANSPARENT_COLOR)
        self.root.attributes("-topmost", True)
        try:
            self.root.attributes("-transparentcolor", TRANSPARENT_COLOR)
        except tk.TclError:
            pass

        self.settings = self._load_settings()
        self.dragging = False
        self.drag_origin: tuple[int, int] = (0, 0)
        self.press_origin: tuple[int, int] = (0, 0)
        self.direction = int(self.settings.get("direction", -1))
        self.next_decision = time.monotonic() + 3
        self.walking_now = False
        self.bubble_after: str | None = None
        self.particles: list[dict[str, Any]] = []
        self._quiet_hidden = False
        self._hidden_aux_windows: list[tk.Toplevel] = []

        self.topmost_var = tk.BooleanVar(value=bool(self.settings.get("topmost", True)))
        self.wander_var = tk.BooleanVar(value=bool(self.settings.get("wander", True)))
        self.scale_var = tk.IntVar(value=int(self.settings.get("scale", 100)))
        if self.scale_var.get() not in (80, 100, 130):
            self.scale_var.set(100)
        self._scale_factor = self.scale_var.get() / 100.0
        self.canvas_width, self.canvas_height = self._canvas_size_for_scale(
            self._scale_factor)
        self.root.attributes("-topmost", self.topmost_var.get())

        self.canvas = tk.Canvas(
            self.root,
            width=self.canvas_width,
            height=self.canvas_height,
            bg=TRANSPARENT_COLOR,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )
        self.canvas.pack()

        # Load idle sprite
        self._idle_pil_frames = self._load_idle_sprite()

        # Load animation engine
        actions_dir = project_root() / ACTIONS_DIR_NAME
        idle_tk = [AnimationEngine._composite(self._scale_frame(pil))
                    for pil in self._idle_pil_frames]
        self.engine = AnimationEngine(actions_dir, idle_tk)

        # Hermes backend client + chat bubbles
        self.backend = HermesBackendClient()
        self._create_chat_renderers()
        self.pending_chat_replies: Queue = Queue()
        PetHTTPHandler.reply_queue = self.pending_chat_replies
        PetHTTPHandler.chat_outbox = Queue()

        # If scale is not 100, reload at proper scale
        if self._scale_factor != 1.0:
            self.engine.reload_at_scale(self._scale_factor, self._idle_pil_frames)

        self.sprite_item = self.canvas.create_image(
            self.canvas_width // 2,
            self.canvas_height - self._sprite_bottom_margin(),
            anchor="s",
            image=self.engine.tick(),
            tags=("sprite",),
        )

        # IPC
        self.event_queue: Queue = Queue()
        self.ipc = IPCServer(self.event_queue)
        ipc_ok = self.ipc.start()
        if not ipc_ok:
            print("[pet] IPC server failed — event-driven animations disabled.")

        # Auto-install hook into Hermes (activates on next gateway restart)
        auto_install_hook()

        # Start status poller as fallback (works immediately, less precise)
        self.poller = StatusPoller(self.event_queue)
        self.poller.start()

        # Start HTTP listener for desktop plugin events
        PetHTTPHandler.event_queue = self.event_queue
        PetHTTPHandler.reset_status()
        self._http_server: HTTPServer | None = None
        try:
            self._http_server = HTTPServer((IPC_HOST, HTTP_PORT), PetHTTPHandler)
            threading.Thread(target=self._http_server.serve_forever, daemon=True).start()
            print(f"[pet] HTTP listener on {IPC_HOST}:{HTTP_PORT}/event")
        except OSError as e:
            print(f"[pet] HTTP listener failed: {e}")

        # UI
        self._build_menu()
        self._bind_events()
        self._place_initially()
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        # Startup greeting
        self.root.after(400, lambda: self.say("我会一直守在这里。"))
        self.root.after(TICK_MS, self._tick)

    def _create_chat_renderers(self) -> None:
        """Create the side bubble first so BubbleChat receives the same instance."""
        self.side_bubble = SideBubble(self)
        self.chat = BubbleChat(self)

    def _reposition_chat_bubbles(self) -> None:
        side_bubble = getattr(self, "side_bubble", None)
        if side_bubble is not None:
            side_bubble.reposition()
        chat = getattr(self, "chat", None)
        if chat is not None:
            chat.reposition_input()

    # ─── Settings ───

    def _load_settings(self) -> dict[str, Any]:
        try:
            return json.loads(settings_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_settings(self) -> None:
        data = {
            "x": self.root.winfo_x(),
            "y": self.root.winfo_y(),
            "topmost": self.topmost_var.get(),
            "wander": self.wander_var.get(),
            "scale": self.scale_var.get(),
            "direction": self.direction,
        }
        try:
            settings_path().write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass

    # ─── Idle Sprite ───

    @staticmethod
    def _canvas_size_for_scale(scale: float) -> tuple[int, int]:
        return (
            max(1, round(CANVAS_WIDTH * scale)),
            max(1, round(CANVAS_HEIGHT * scale)),
        )

    def _sprite_bottom_margin(self) -> int:
        return max(4, round(8 * self._scale_factor))

    def _load_idle_sprite(self) -> list[Image.Image]:
        sprite = Image.open(resource_path(IDLE_SPRITE_NAME)).convert("RGBA")
        alpha = sprite.getchannel("A")
        bbox = alpha.getbbox()
        if not bbox:
            raise RuntimeError("角色素材是空的。")
        sprite = sprite.crop(bbox)

        # Resize to fit within 160×120 at 100% scale
        max_w, max_h = 160, 120
        ratio = min(max_w / sprite.width, max_h / sprite.height, 1.0)
        if ratio < 1.0:
            sprite = sprite.resize(
                (max(1, int(sprite.width * ratio)), max(1, int(sprite.height * ratio))),
                Image.Resampling.NEAREST,
            )

        # Hard alpha for clean color-keying
        a = sprite.getchannel("A").point(lambda v: 255 if v >= 72 else 0)
        sprite.putalpha(a)

        # 6-frame breathing: vertical offset cycle
        offsets = [(0, 3), (0, 2), (1, 1), (0, 2), (0, 3), (-1, 2)]
        frames = []
        for x_shift, y_shift in offsets:
            frame = Image.new("RGBA", (sprite.width + 8, sprite.height + 8), (0, 0, 0, 0))
            frame.paste(sprite, (4 + x_shift, 5 - y_shift), sprite)
            frames.append(frame)
        return frames

    def _scale_frame(self, pil: Image.Image) -> Image.Image:
        if self._scale_factor == 1.0:
            return pil
        new_size = (
            max(1, int(pil.width * self._scale_factor)),
            max(1, int(pil.height * self._scale_factor)),
        )
        return pil.resize(new_size, Image.Resampling.NEAREST)

    # ─── Menu ───

    def _build_menu(self) -> None:
        self.menu = PetMenu(self)

    # ─── Events ───

    def _bind_events(self) -> None:
        self.canvas.bind("<ButtonPress-1>", self._drag_start)
        self.canvas.bind("<B1-Motion>", self._drag_move)
        self.canvas.bind("<ButtonRelease-1>", self._drag_end)
        self.canvas.bind("<Double-Button-1>", lambda _e: self.open_chat())
        self.canvas.bind("<Button-3>", self._show_menu)

    def _place_initially(self) -> None:
        self.root.update_idletasks()
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        default_x = screen_w - self.canvas_width - 40
        default_y = screen_h - self.canvas_height - 70
        x = int(self.settings.get("x", default_x))
        y = int(self.settings.get("y", default_y))
        x, y = self._clamp_position(x, y)
        # Tk uses '+-N' for an absolute negative virtual-desktop coordinate.
        self.root.geometry(
            f"{self.canvas_width}x{self.canvas_height}+{x}+{y}")

    # ─── Drag & Menu ───

    def _drag_start(self, event: tk.Event) -> None:
        self.dragging = True
        self.drag_origin = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())
        self.press_origin = (event.x_root, event.y_root)

    def _drag_move(self, event: tk.Event) -> None:
        if not self.dragging:
            return
        x = event.x_root - self.drag_origin[0]
        y = event.y_root - self.drag_origin[1]
        x, y = self._clamp_position(x, y)
        self.root.geometry(f"+{x}+{y}")
        self._reposition_chat_bubbles()

    def _drag_end(self, event: tk.Event) -> None:
        if not self.dragging:
            return
        self.dragging = False
        distance = abs(event.x_root - self.press_origin[0]) + abs(event.y_root - self.press_origin[1])
        if distance < 7:
            self.interact()
        self._save_settings()

    def _show_menu(self, event: tk.Event) -> None:
        self.menu.show(event.x_root, event.y_root)

    def _work_area_for_point(
        self, x: int, y: int
    ) -> tuple[int, int, int, int]:
        provider = getattr(self, "_monitor_work_area_provider", None)
        if callable(provider):
            return tuple(map(int, provider(int(x), int(y))))

        if sys.platform == "win32":
            try:
                import ctypes
                from ctypes import wintypes

                class MONITORINFO(ctypes.Structure):
                    _fields_ = (
                        ("cbSize", wintypes.DWORD),
                        ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT),
                        ("dwFlags", wintypes.DWORD),
                    )

                user32 = ctypes.windll.user32
                monitor_from_point = ctypes.WINFUNCTYPE(
                    wintypes.HANDLE,
                    wintypes.POINT,
                    wintypes.DWORD,
                )(("MonitorFromPoint", user32))
                get_monitor_info = ctypes.WINFUNCTYPE(
                    wintypes.BOOL,
                    wintypes.HANDLE,
                    ctypes.POINTER(MONITORINFO),
                )(("GetMonitorInfoW", user32))
                monitor = monitor_from_point(
                    wintypes.POINT(int(x), int(y)), 2)
                info = MONITORINFO()
                info.cbSize = ctypes.sizeof(MONITORINFO)
                if monitor and get_monitor_info(monitor, ctypes.byref(info)):
                    area = info.rcWork
                    return (
                        int(area.left), int(area.top),
                        int(area.right), int(area.bottom),
                    )
            except (OSError, AttributeError, tk.TclError):
                pass

        left = int(self.root.winfo_vrootx())
        top = int(self.root.winfo_vrooty())
        return (
            left,
            top,
            left + int(self.root.winfo_vrootwidth()),
            top + int(self.root.winfo_vrootheight()),
        )

    def _clamp_position(self, x: int, y: int) -> tuple[int, int]:
        center_x = int(x) + self.canvas_width // 2
        center_y = int(y) + self.canvas_height // 2
        left, top, right, bottom = self._work_area_for_point(
            center_x, center_y)
        max_x = max(left, right - self.canvas_width)
        max_y = max(top, bottom - self.canvas_height)
        return (
            max(left, min(int(x), max_x)),
            max(top, min(int(y), max_y)),
        )

    # ─── Settings callbacks ───

    def _auxiliary_windows(self) -> list[tk.Toplevel]:
        candidates = (
            getattr(getattr(self, "chat", None), "input_win", None),
            getattr(getattr(self, "side_bubble", None), "window", None),
        )
        windows: list[tk.Toplevel] = []
        for window in candidates:
            if window is None:
                continue
            try:
                if window.winfo_exists():
                    windows.append(window)
            except tk.TclError:
                pass
        return windows

    def _toggle_topmost(self) -> None:
        enabled = bool(self.topmost_var.get())
        self.root.attributes("-topmost", enabled)
        for window in self._auxiliary_windows():
            window.attributes("-topmost", enabled)
        self._save_settings()

    def _setting_changed(self) -> None:
        self._save_settings()

    def _resize(self) -> None:
        self.root.update_idletasks()
        old_center_x = self.root.winfo_x() + self.canvas_width / 2
        old_bottom_y = self.root.winfo_y() + self.canvas_height

        self._scale_factor = self.scale_var.get() / 100.0
        self.canvas_width, self.canvas_height = self._canvas_size_for_scale(
            self._scale_factor)
        self.canvas.configure(width=self.canvas_width, height=self.canvas_height)

        new_x = round(old_center_x - self.canvas_width / 2)
        new_y = round(old_bottom_y - self.canvas_height)
        new_x, new_y = self._clamp_position(new_x, new_y)
        self.root.geometry(
            f"{self.canvas_width}x{self.canvas_height}+{new_x}+{new_y}")

        self.engine.reload_at_scale(self._scale_factor, self._idle_pil_frames)
        self.engine.frame_index = 0
        self.canvas.coords(
            self.sprite_item,
            self.canvas_width // 2,
            self.canvas_height - self._sprite_bottom_margin(),
        )
        self.canvas.itemconfigure(self.sprite_item, image=self.engine.tick())
        self._reposition_chat_bubbles()
        self.say(random.choice(("这样合适吗？", "换了一个大小。", "我在这里。")))
        self._save_settings()

    # ─── Main Tick ───

    def _tick(self) -> None:
        if not self.root.winfo_exists():
            return

        # Drain chat replies (plugin → pet bubble)
        while True:
            try:
                msg = self.pending_chat_replies.get_nowait()
            except Empty:
                break
            phase = msg.get("phase", "")
            text = str(msg.get("text", ""))
            request_id = str(msg.get("request_id", ""))
            if phase == "delta":
                self.chat.on_reply_delta(text, request_id=request_id)
            elif phase == "complete":
                self.chat.on_reply_complete(text, request_id=request_id)

        # Drain event queue
        while True:
            try:
                msg = self.event_queue.get_nowait()
            except Empty:
                break
            event_name = msg.get("event", "")
            if event_name:
                self.engine.handle_event(event_name)

        # Advance animation
        frame = self.engine.tick()
        self.canvas.itemconfigure(self.sprite_item, image=frame)

        # Idle wander
        now = time.monotonic()
        if now >= self.next_decision:
            self.walking_now = self.wander_var.get() and random.random() < 0.5
            if random.random() < 0.35:
                self.direction *= -1
            self.next_decision = now + random.uniform(2.2, 5.5)

        if self.walking_now and not self.dragging:
            x = self.root.winfo_x() + self.direction * 2
            clamped_x, _ = self._clamp_position(x, self.root.winfo_y())
            if clamped_x != x:
                self.direction *= -1
            self.root.geometry(f"+{clamped_x}+{self.root.winfo_y()}")
            self._reposition_chat_bubbles()

        self._animate_particles()
        self.root.after(TICK_MS, self._tick)

    # ─── Interaction ───

    def open_chat(self, select_session: bool = False) -> None:
        """Double-click / menu entry: open the chat panel beside the pet."""
        self.chat.open(select_session=select_session)

    def interact(self) -> None:
        lines = (
            "今夜的海很安静。",
            "漂泊累了吗？在这里休息吧。",
            "那只蝴蝶，也很喜欢你。",
            "我会替你守望这片桌面。",
            "你的心声，我听见了。",
            "别忘了稍微休息一下。",
            "你回来了，真好。",
            "一直在等你呢。",
        )
        self.say(random.choice(lines))
        self._make_sparkles()

    def say(self, message: str, hold: bool = False) -> None:
        self.canvas.delete("bubble")
        if self.bubble_after is not None:
            try:
                self.root.after_cancel(self.bubble_after)
            except tk.TclError:
                pass

        # Long (chat) messages: smaller font + taller bubble, clamp lines
        lines = message.count("\n") + 1
        if len(message) > 60:
            display = message[:200] + ("…" if len(message) > 200 else "")
            y1, y2 = 2, min(150, self.canvas_height - 14)
            box_width = self.canvas_width - 12
            fsize = 8
            justify_left = True
        else:
            display = message
            box_width = min(
                self.canvas_width - 12,
                min(180, max(120, 16 + len(message) * 14)),
            )
            y1, y2 = 6, 50
            fsize = 9
            justify_left = False

        x1 = (self.canvas_width - box_width) // 2
        x2 = x1 + box_width

        self.canvas.create_rectangle(x1, y1, x2, y2, fill="#f8fbff",
                                     outline="#6baee8", width=2, tags=("bubble",))
        self.canvas.create_polygon(
            self.canvas_width // 2 - 8, y2, self.canvas_width // 2 + 5, y2,
            self.canvas_width // 2, y2 + 10,
            fill="#f8fbff", outline="#6baee8", tags=("bubble",),
        )
        self.canvas.create_text(
            self.canvas_width // 2, (y1 + y2) // 2,
            text=display, fill="#17365d",
            font=("Microsoft YaHei UI", fsize),
            justify="left" if justify_left else "center",
            width=box_width - 12,
            tags=("bubble",),
        )
        self.canvas.tag_raise("bubble")
        if hold:
            self.bubble_after = None  # stays until replaced
        else:
            self.bubble_after = self.root.after(2900, lambda: self.canvas.delete("bubble"))

    def clear_head_bubble(self) -> None:
        """Remove the head bubble and cancel its pending expiry callback."""
        self.canvas.delete("bubble")
        if self.bubble_after is not None:
            try:
                self.root.after_cancel(self.bubble_after)
            except (tk.TclError, ValueError):
                pass
            self.bubble_after = None

    def _make_sparkles(self) -> None:
        colors = ("#7de7ff", "#b6f5ff", "#8aa7ff", "#ffffff")
        for _ in range(5):
            item = self.canvas.create_text(
                random.randint(30, max(30, self.canvas_width - 30)),
                random.randint(55, max(55, self.canvas_height - 15)),
                text=random.choice(("✦", "·", "✧")),
                fill=random.choice(colors),
                font=("Segoe UI Symbol", random.randint(10, 15), "bold"),
                tags=("particle",),
            )
            self.particles.append({"item": item, "life": random.randint(8, 14), "drift": random.choice((-1, 0, 1))})

    def _animate_particles(self) -> None:
        alive: list[dict[str, Any]] = []
        for particle in self.particles:
            item = int(particle["item"])
            life = int(particle["life"]) - 1
            if life <= 0:
                self.canvas.delete(item)
                continue
            particle["life"] = life
            self.canvas.move(item, int(particle["drift"]), -2)
            alive.append(particle)
        self.particles = alive

    # ─── Misc ───

    def _hide_for_five_minutes(self) -> None:
        self._save_settings()
        self._quiet_hidden = True
        self.side_bubble.set_quiet_hidden(True)
        self._hidden_aux_windows = []
        for window in self._auxiliary_windows():
            try:
                if window.winfo_ismapped():
                    self._hidden_aux_windows.append(window)
                    window.withdraw()
            except tk.TclError:
                pass
        self.root.withdraw()
        self.root.after(5 * 60 * 1000, self._return_from_hide)

    def _return_from_hide(self) -> None:
        self._quiet_hidden = False
        self.root.deiconify()
        enabled = bool(self.topmost_var.get())
        for window in getattr(self, "_hidden_aux_windows", []):
            try:
                if window.winfo_exists():
                    window.attributes("-topmost", enabled)
                    window.deiconify()
                    window.lift()
            except tk.TclError:
                pass
        self._hidden_aux_windows = []
        self._reposition_chat_bubbles()
        self.side_bubble.set_quiet_hidden(False)
        self.say("我回来了。")

    def close(self) -> None:
        self.poller.stop()
        self.ipc.stop()
        if self._http_server:
            self._http_server.shutdown()
        self.chat.close()
        self.side_bubble.close(animated=False)
        self.backend.stop_ws()
        self._save_settings()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


# ─── Entry Point ─────────────────────────────────────────────────

def check_assets() -> int:
    path = resource_path(IDLE_SPRITE_NAME)
    with Image.open(path) as image:
        image.verify()
    print(f"OK: {path}")
    return 0


if __name__ == "__main__":
    if "--check" in sys.argv:
        raise SystemExit(check_assets())
    ShorekeeperPet().run()
