from __future__ import annotations

import json
import sys
import tkinter as tk
from pathlib import Path
from queue import Queue

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import BubbleChat, PetHTTPHandler  # noqa: E402


class FakeSideBubble:
    def __init__(self) -> None:
        self.started = 0
        self.texts: list[str] = []
        self.completed = 0
        self.closed = 0

    def start_reply(self) -> None:
        self.started += 1

    def set_text(self, text: str) -> None:
        self.texts.append(text)

    def complete_reply(self) -> None:
        self.completed += 1

    def close(self, animated: bool = True) -> None:
        self.closed += 1


class FakePet:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("BubbleChat recovery test")
        self.root.geometry("190x160+20+20")
        self.pending_chat_replies = Queue()
        self.said: list[tuple[str, bool]] = []
        self.side_bubble = FakeSideBubble()
        self.head_bubble_clears = 0

    def say(self, message: str, hold: bool = False) -> None:
        self.said.append((message, hold))

    def clear_head_bubble(self) -> None:
        self.head_bubble_clears += 1


def widget_texts(widget: tk.Misc) -> list[str]:
    texts: list[str] = []
    for child in widget.winfo_children():
        try:
            value = child.cget("text")
        except tk.TclError:
            value = ""
        if value:
            texts.append(str(value))
        texts.extend(widget_texts(child))
    return texts


def main() -> None:
    pet = FakePet()
    PetHTTPHandler.chat_outbox = Queue()
    chat = BubbleChat(pet)

    try:
        chat.open()
        assert chat.active is True
        assert chat.input_win is not None
        texts = widget_texts(chat.input_win)
        assert "Enter 发送 · Esc 关闭" in texts
        assert all("✕" not in value for value in texts), texts

        chat._entry.insert(0, "测试消息")
        chat._send()
        outgoing = PetHTTPHandler.chat_outbox.get(timeout=2)
        assert outgoing["text"] == "测试消息"
        assert outgoing["session_id"] == ""
        assert isinstance(outgoing["request_id"], str) and outgoing["request_id"]

        # Hermes-main-window semantics: the second message goes out
        # IMMEDIATELY — no local waiting, no local queue. The gateway
        # serializes mid-turn submits itself.
        chat._entry.insert(0, "上一条没回也立刻发")
        chat._send()
        second = PetHTTPHandler.chat_outbox.get(timeout=2)
        assert second["text"] == "上一条没回也立刻发"
        assert second["request_id"] != outgoing["request_id"]

        # First reply renders; a stale delta for the SAME request after
        # complete must not overwrite it.
        chat.on_reply_delta("你好", request_id=outgoing["request_id"])
        assert pet.said[-1] == ("你好", True)
        chat.on_reply_complete("你好", request_id=outgoing["request_id"])
        assert ("你好", False) in pet.said

        # Second reply renders independently, right after.
        chat.on_reply_delta("第二", request_id=second["request_id"])
        chat.on_reply_complete("第二条完成", request_id=second["request_id"])
        assert ("第二条完成", False) in pet.said

        # Reply routing boundary: 15 chars stays above the pet; the 16th
        # migrates the full accumulated reply into the side bubble.
        closes_before_begin = pet.side_bubble.closed
        chat._begin_reply()
        assert pet.side_bubble.closed == closes_before_begin + 1
        clears_before_migration = pet.head_bubble_clears
        short_text = "123456789012345"
        long_text = short_text + "6"
        chat._render_reply_text(short_text, final=False)
        assert pet.said[-1] == (short_text, True)
        assert pet.side_bubble.texts == []

        chat._render_reply_text(long_text, final=False)
        assert pet.head_bubble_clears == clears_before_migration + 1
        assert pet.side_bubble.started == 1
        assert pet.side_bubble.texts[-1] == long_text

        chat._render_reply_text(long_text, final=True)
        assert pet.side_bubble.completed == 1
        assert pet.side_bubble.started == 1

        # Completion must continue from the already typed prefix; it must not
        # shrink a long side-bubble reply back to the first typing batch.
        chat._begin_reply()
        pet.side_bubble.texts.clear()
        streaming_text = "123456789012345678901234567890"
        chat._stream_buf = streaming_text
        chat._tick_typing()
        chat._tick_typing()
        chat._tick_typing()
        assert len(pet.side_bubble.texts[-1]) == 18
        completed_before_final = pet.side_bubble.completed
        chat.on_reply_complete(streaming_text)
        assert pet.side_bubble.texts[-1] == streaming_text
        assert pet.side_bubble.completed == completed_before_final + 1
        assert chat._type_after is None

        rendered_after_complete = list(pet.side_bubble.texts)
        chat.on_reply_delta("这是 complete 之后才晚到的旧前缀")
        assert pet.side_bubble.texts == rendered_after_complete
        assert chat._type_after is None

        chat._begin_reply()
        chat.on_reply_delta("新一轮")
        assert pet.said[-1] == ("新一轮", True)

        chat.close()
        assert chat.active is False
        assert chat.input_win is None

        print(json.dumps({
            "ok": True,
            "inputHint": "Enter 发送 · Esc 关闭",
            "customXPresent": False,
            "outbox": outgoing,
            "deltaBubble": ["你好", True],
            "completeBubble": ["你好", False],
        }, ensure_ascii=False, indent=2))
    finally:
        try:
            chat.close()
        finally:
            pet.root.destroy()


if __name__ == "__main__":
    main()
