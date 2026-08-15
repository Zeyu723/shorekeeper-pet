# -*- coding: utf-8 -*-
"""Send a fake streaming chat reply to the pet for visual testing."""
import json
import time
import urllib.request

STORY = (
    "好呀，我来给你讲一个长一点的故事吧。在很远很远的海边，有一座安静的小屋，"
    "屋里住着一位守灯的人。她每天傍晚都会爬上高高的灯塔，点亮那盏灯，让远航的船"
    "只在黑夜里也能找到回家的方向。有人问她，一个人守着灯塔不孤独吗？她笑了笑说，"
    "海风、浪花、星星，还有偶尔停靠的信天翁，都是她的朋友。而且她知道，在灯塔照"
    "不到的地方，有人在等着灯光亮起，等着船靠岸，等着重逢的那一天。所以她从不觉"
    "得孤独，因为她守望的从来都不是一盏灯，而是每一次平安的归来。今晚的海也很安"
    "静，你听，浪拍岸的声音，像不像在说晚安。"
)

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

# stream in 5 chunks with pauses (simulates delta cadence)
for i in range(1, 6):
    post("delta", STORY[: int(len(STORY) * i / 5)])
    time.sleep(1.2)

time.sleep(1.0)
post("complete", STORY)
