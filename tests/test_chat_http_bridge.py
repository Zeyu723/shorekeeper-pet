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
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, method=method, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=2) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def main() -> None:
    PetHTTPHandler.event_queue = Queue()
    PetHTTPHandler.chat_outbox = Queue()
    PetHTTPHandler.reply_queue = Queue()
    PetHTTPHandler.reset_status()

    server = HTTPServer(("127.0.0.1", 0), PetHTTPHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        outgoing = {"text": "测试当前会话", "session_id": ""}
        PetHTTPHandler.chat_outbox.put(outgoing)

        status, first = request(f"http://127.0.0.1:{port}/chat/outbox")
        assert status == 200
        assert first == {"requests": [outgoing]}

        status, drained = request(f"http://127.0.0.1:{port}/chat/outbox")
        assert status == 200
        assert drained == {"requests": []}

        reply = {"phase": "delta", "text": "收到", "timestamp": 123}
        status, posted = request(
            f"http://127.0.0.1:{port}/chat/reply",
            method="POST",
            payload=reply,
        )
        assert status == 200
        assert posted == {"ok": True}
        assert PetHTTPHandler.reply_queue.get(timeout=1) == reply

        print(json.dumps({
            "ok": True,
            "outbox": first,
            "drained": drained,
            "reply": reply,
        }, ensure_ascii=False, indent=2))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    main()
