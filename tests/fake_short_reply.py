# -*- coding: utf-8 -*-
"""Send a short (~20 char) streaming reply to test the head-top bubble."""
import json
import time
import urllib.request

SHORT = "今夜的海很安静，早点休息吧，漂泊者。"

def post(phase: str, text: str) -> None:
    payload = json.dumps({"phase": phase, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:51208/chat/reply",
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=3) as r:
        print(phase, "->", r.read().decode())

# stream in 3 chunks (stays under 20 for the first two, crosses at the last)
for i in range(1, 4):
    post("delta", SHORT[: int(len(SHORT) * i / 3)])
    time.sleep(1.0)

time.sleep(0.8)
post("complete", SHORT)
