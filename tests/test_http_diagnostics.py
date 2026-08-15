from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path
from queue import Queue

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import PetHTTPHandler  # noqa: E402


def request(url: str, *, method: str = "GET", payload: object | None = None):
    data = None
    headers: dict[str, str] = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, method=method, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=2) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def main() -> None:
    queue: Queue = Queue()
    PetHTTPHandler.event_queue = queue
    PetHTTPHandler.reset_status()
    server = HTTPServer(("127.0.0.1", 0), PetHTTPHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        status, initial = request(f"http://127.0.0.1:{port}/status")
        assert status == 200
        assert initial["ok"] is True
        assert initial["receivedCount"] == 0
        assert initial["lastEvent"] is None

        event = {
            "event": "tool.started",
            "originalType": "tool.start",
            "source": "desktop-plugin",
            "sessionId": "session-1",
            "toolId": "tool-1",
            "toolName": "terminal",
            "timestamp": 123456,
        }
        status, posted = request(f"http://127.0.0.1:{port}/event", method="POST", payload=event)
        assert status == 200
        assert posted == {"ok": True}
        assert queue.get(timeout=1) == event

        status, snapshot = request(f"http://127.0.0.1:{port}/health")
        assert status == 200
        assert snapshot["receivedCount"] == 1
        assert snapshot["lastEvent"]["event"] == "tool.started"
        assert snapshot["lastEvent"]["originalType"] == "tool.start"
        assert snapshot["lastEvent"]["toolName"] == "terminal"

        status, invalid = request(f"http://127.0.0.1:{port}/event", method="POST", payload={"no_event": True})
        assert status == 400
        assert invalid["ok"] is False

        status, missing = request(f"http://127.0.0.1:{port}/missing")
        assert status == 404
        assert missing["ok"] is False

        print(json.dumps({
            "ok": True,
            "port": port,
            "receivedCount": snapshot["receivedCount"],
            "lastEvent": snapshot["lastEvent"],
            "invalidPayloadStatus": invalid["ok"],
        }, ensure_ascii=False, indent=2))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    main()
