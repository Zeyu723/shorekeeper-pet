from __future__ import annotations

import json
import sys
import tkinter as tk
from pathlib import Path
from queue import Queue

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import BubbleChat, ShorekeeperPet, SideBubble  # noqa: E402


def mapped(window: tk.Misc | None) -> bool:
    return bool(window is not None and window.winfo_exists() and window.winfo_ismapped())


def topmost(window: tk.Misc) -> bool:
    return bool(window.attributes("-topmost"))


def make_pet() -> ShorekeeperPet:
    pet = ShorekeeperPet.__new__(ShorekeeperPet)
    pet.root = tk.Tk()
    pet.root.title("Pet auxiliary-window state test")
    pet.root.geometry("190x160+100+100")
    pet.root.attributes("-topmost", False)
    pet.topmost_var = tk.BooleanVar(master=pet.root, value=False)
    pet.canvas_width = 190
    pet.canvas_height = 160
    pet.pending_chat_replies = Queue()
    pet.side_bubble = SideBubble(
        pet, work_area_provider=lambda _root: (0, 0, 1200, 800)
    )
    pet.chat = BubbleChat(pet)
    pet.say = lambda *_args, **_kwargs: None
    pet._save_settings = lambda: None
    pet.root.update()
    return pet


def main() -> None:
    pet = make_pet()
    try:
        pet.side_bubble.start_reply()
        pet.side_bubble.set_text("这是一个需要独立侧边气泡显示的长回复。")
        pet.chat.open()
        pet.root.update()

        assert pet.chat.input_win is not None
        assert pet.side_bubble.window is not None
        assert topmost(pet.root) is False
        assert topmost(pet.chat.input_win) is False
        assert topmost(pet.side_bubble.window) is False

        pet._monitor_work_area_provider = (
            lambda _x, _y: (-1920, -1080, 0, 0)
        )
        pet.root.geometry("190x160+-1800+-900")
        pet.root.update_idletasks()
        pet.chat.reposition_input()
        pet.root.update()
        input_x = pet.chat.input_win.winfo_x()
        input_y = pet.chat.input_win.winfo_y()
        input_w = pet.chat.input_win.winfo_width()
        input_h = pet.chat.input_win.winfo_height()
        assert input_x >= -1920
        assert input_x + input_w <= 0
        assert input_y >= -1080
        assert input_y + input_h <= 0

        pet._monitor_work_area_provider = (
            lambda _x, _y: (0, 0, 1200, 800)
        )
        pet.root.geometry("190x160+100+100")
        pet.root.update_idletasks()
        pet.chat.reposition_input()
        pet.side_bubble.reposition()

        old_input = pet.chat.input_win
        pet.chat._show_input()
        new_input = pet.chat.input_win
        assert old_input is not None and new_input is not None
        assert new_input is not old_input
        pet.chat._check_focus(old_input)
        assert pet.chat.input_win is new_input

        pet.topmost_var.set(True)
        pet._toggle_topmost()
        pet.root.update()
        assert topmost(pet.root) is True
        assert topmost(pet.chat.input_win) is True
        assert topmost(pet.side_bubble.window) is True

        pet._hide_for_five_minutes()
        pet.root.update()
        assert mapped(pet.root) is False
        assert mapped(pet.chat.input_win) is False
        assert mapped(pet.side_bubble.window) is False
        input_before_focus_check = pet.chat.input_win
        pet.chat._check_focus()
        assert pet.chat.input_win is input_before_focus_check

        pet.side_bubble.start_reply()
        pet.side_bubble.set_text("安静期间才到达的长回复也不能穿透显示。")
        pet.side_bubble.complete_reply()
        pet.root.update()
        assert mapped(pet.side_bubble.window) is False
        assert pet.side_bubble.close_after_id is None

        pet._return_from_hide()
        pet.root.update()
        assert mapped(pet.root) is True
        assert mapped(pet.chat.input_win) is True
        assert mapped(pet.side_bubble.window) is True
        assert pet.side_bubble.close_after_id is not None
        assert topmost(pet.chat.input_win) is True
        assert topmost(pet.side_bubble.window) is True

        print(json.dumps({
            "ok": True,
            "newAuxWindowsInheritTopmost": True,
            "togglePropagates": True,
            "quietModeHidesAndRestoresGroup": True,
        }, ensure_ascii=False, indent=2))
    finally:
        try:
            pet.chat.close()
            pet.side_bubble.close(animated=False)
        finally:
            pet.root.destroy()


if __name__ == "__main__":
    main()
