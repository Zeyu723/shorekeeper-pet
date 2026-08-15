"""Headless test — verify catalog loading and IPC without a display."""
import sys, json, os
from pathlib import Path

actions_dir = Path(r"C:\Users\zeyu\.hermes\shorekeeper-pet\actions\hermes_events")
catalog_path = actions_dir / "catalog.json"

with open(catalog_path, "r", encoding="utf-8") as f:
    catalog = json.load(f)

print(f"Catalog loaded: {len(catalog['actions'])} actions")
errors = 0
for a in catalog["actions"]:
    aid = a["action_id"]
    event = a["event"]["name"]
    manifest_path = actions_dir / aid / "manifest.json"
    frames_dir = actions_dir / aid / "frames"

    if manifest_path.exists():
        with open(manifest_path, "r", encoding="utf-8") as f:
            mf = json.load(f)
        frame_files = mf.get("frames", [])
        actual_frames = len([f2 for f2 in os.listdir(frames_dir) if f2.endswith(".png")]) if frames_dir.is_dir() else 0
        ok = actual_frames > 0 and len(frame_files) == actual_frames
        if not ok:
            errors += 1
        status = "OK" if ok else "FAIL"
        print(f"  [{status}] {aid:30s} event={event:35s} frames={actual_frames}/{len(frame_files)}")
    else:
        errors += 1
        print(f"  [FAIL] {aid:30s} MISSING MANIFEST")

# Test event to action mapping
event_map = {a["event"]["name"]: a["action_id"] for a in catalog["actions"]}
print(f"\nEvent mapping ({len(event_map)} events):")
for ev, aid in sorted(event_map.items()):
    print(f"  {ev:40s} -> {aid}")

# Test the hook handler
sys.path.insert(0, r"C:\Users\zeyu\AppData\Local\hermes\hooks\shorekeeper-pet")
import handler
print(f"\nHandler loaded: {handler.handle}")
print(f"Handler PET_PORT: {handler.PET_PORT}")

# Test IPC: start a listener, send an event, verify it arrives
import socket, threading, time

received = []

def listener():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 51207))
    sock.listen(1)
    sock.settimeout(3)
    try:
        conn, _ = sock.accept()
        data = conn.recv(4096)
        received.append(data.decode("utf-8").strip())
        conn.close()
    except socket.timeout:
        pass
    sock.close()

t = threading.Thread(target=listener, daemon=True)
t.start()
time.sleep(0.3)

# Send a test event
handler.handle("agent:start", {"session_id": "test123", "platform": "cli"})
t.join(timeout=3)

if received:
    msg = json.loads(received[0])
    print(f"\nIPC test PASSED: {msg}")
else:
    print("\nIPC test FAILED: no message received")

if errors == 0 and received:
    print("\n=== ALL TESTS PASSED ===")
else:
    print(f"\n=== {errors} errors, IPC {'OK' if received else 'FAILED'} ===")
