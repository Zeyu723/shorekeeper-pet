from __future__ import annotations

import json
import sys
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import BubbleChat, ShorekeeperPet, SideBubble  # noqa: E402


class FakePet:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("SideBubble test pet")
        self.root.geometry("190x160+100+100")
        self.canvas_width = 190
        self.canvas_height = 160
        self.root.update_idletasks()


def widget_text(widget: tk.Text) -> str:
    return widget.get("1.0", "end-1c")


def main() -> None:
    pet = FakePet()
    current_work_area = [(0, 0, 1200, 800)]
    bubble = SideBubble(
        pet, work_area_provider=lambda _root: current_work_area[0]
    )

    try:
        assert SideBubble.AUTO_CLOSE_MS == 15_000

        # Win32 API declarations are process-global. Another module may have
        # installed an incompatible structure type before SideBubble runs;
        # _default_work_area must restore its own exact signature.
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class ForeignMonitorInfo(ctypes.Structure):
                _fields_ = [
                    ("cbSize", wintypes.DWORD),
                    ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD),
                ]

            get_info = ctypes.windll.user32.GetMonitorInfoW
            old_argtypes, old_restype = get_info.argtypes, get_info.restype
            try:
                foreign_argtypes = [
                    wintypes.HANDLE,
                    ctypes.POINTER(ForeignMonitorInfo),
                ]
                foreign_restype = ctypes.c_void_p
                get_info.argtypes = foreign_argtypes
                get_info.restype = foreign_restype
                area = SideBubble._default_work_area(pet.root)
                assert get_info.argtypes == foreign_argtypes
                assert get_info.restype is foreign_restype
            finally:
                get_info.argtypes, get_info.restype = old_argtypes, old_restype
            assert area[2] > area[0]
            assert area[3] > area[1]

        # Real ShorekeeperPet wiring must inject one SideBubble into BubbleChat.
        wiring_pet = ShorekeeperPet.__new__(ShorekeeperPet)
        wiring_pet.root = pet.root
        wiring_pet._create_chat_renderers()
        assert isinstance(wiring_pet.side_bubble, SideBubble)
        assert isinstance(wiring_pet.chat, BubbleChat)
        assert wiring_pet.chat.side_bubble is wiring_pet.side_bubble
        repositioned: list[bool] = []
        input_repositioned: list[bool] = []
        wiring_pet.side_bubble.reposition = lambda: repositioned.append(True)
        wiring_pet.chat.reposition_input = lambda: input_repositioned.append(True)
        wiring_pet._reposition_chat_bubbles()
        assert repositioned == [True]
        assert input_repositioned == [True]

        # Pet on the left: bubble sits to the right and its tail is on the left.
        bubble.start_reply()
        pet.root.update()
        assert bubble.window is not None
        assert bool(bubble.window.overrideredirect()) is True
        assert bubble.window.title() == "守岸人 · 长回复"
        assert bubble.side == "right"
        assert bubble.tail_side == "left"
        assert bubble.window.winfo_x() >= pet.root.winfo_x() + pet.canvas_width

        # Pet on the right: bubble mirrors to the left and remains in work area.
        pet.root.geometry("190x160+900+100")
        pet.root.update_idletasks()
        bubble.reposition()
        pet.root.update()
        assert bubble.side == "left"
        assert bubble.tail_side == "right"
        assert bubble.window.winfo_x() + bubble.window.winfo_width() <= pet.root.winfo_x()

        # Clamp vertically at the current monitor work-area boundary.
        pet.root.geometry("190x160+900+700")
        pet.root.update_idletasks()
        bubble.reposition()
        pet.root.update()
        assert bubble.window.winfo_y() >= current_work_area[0][1]
        assert (
            bubble.window.winfo_y() + bubble.window.winfo_height()
            <= current_work_area[0][3]
        )

        # A 130% pet near the middle of a narrow monitor must not be covered by
        # the fixed-width bubble. The bubble chooses usable side space and
        # narrows its scrollable body while remaining entirely in rcWork.
        current_work_area[0] = (0, 0, 700, 500)
        pet.canvas_width = 247
        pet.canvas_height = 208
        pet.root.geometry("247x208+226+100")
        pet.root.update_idletasks()
        bubble.reposition()
        pet.root.update()
        bx1 = bubble.window.winfo_x()
        bx2 = bx1 + bubble.window.winfo_width()
        px1 = pet.root.winfo_x()
        px2 = px1 + pet.canvas_width
        assert bx2 <= px1 or bx1 >= px2
        assert bx1 >= current_work_area[0][0] + bubble.EDGE_PAD
        assert bx2 <= current_work_area[0][2] - bubble.EDGE_PAD
        assert bubble.body_width < SideBubble.BODY_W

        # Full text is appended when possible, safely replaced otherwise,
        # and every update follows the stream to the bottom.
        long_text = "\n".join(f"第 {i} 行" for i in range(80))
        bubble.set_text(long_text)
        pet.root.update()
        pet.root.update()
        assert bubble.text_widget is not None
        assert widget_text(bubble.text_widget) == long_text
        assert bubble.text_widget.yview()[1] >= 0.999

        # Dynamic height: the bubble hugs its text, capped at 8 visible
        # lines. Two lines must shrink the window far below BODY_H; 80
        # lines must hit exactly the cap.
        two_lines = "mua~ 亲到了，爸爸 (/ω＼)\n测了一下午气泡，你也休息下眼睛嘛 (´-`ʃƪ)"
        bubble.set_text(two_lines)
        pet.root.update()
        h_two = bubble.window.winfo_height()
        assert h_two < SideBubble.BODY_H - 80, h_two
        assert bubble.text_widget.yview() == (0.0, 1.0)  # no scroll needed

        one_line = "只有一行的短长回复"
        bubble.set_text(one_line)
        pet.root.update()
        h_one = bubble.window.winfo_height()
        assert h_one < h_two, (h_one, h_two)

        bubble.set_text(long_text)
        pet.root.update()
        assert bubble.window.winfo_height() >= SideBubble.BODY_H - 2
        assert bubble.text_widget.yview()[1] >= 0.999

        appended = long_text + "\n最后一行"
        bubble.set_text(appended)
        pet.root.update()
        assert widget_text(bubble.text_widget) == appended
        assert bubble.text_widget.yview()[1] >= 0.999

        bubble.set_text("替换后的完整文本")
        pet.root.update()
        assert widget_text(bubble.text_widget) == "替换后的完整文本"

        # Streaming never starts the timer. Completion does. Scrolling renews
        # the timer; clicking the content pins it and reveals the blue close X.
        assert bubble.close_after_id is None
        bubble._on_scroll_activity()
        assert bubble.close_after_id is None
        bubble.complete_reply()
        first_timer = bubble.close_after_id
        assert first_timer is not None
        assert bubble.pinned is False

        bubble._on_scroll_activity()
        second_timer = bubble.close_after_id
        assert second_timer is not None and second_timer != first_timer
        assert bubble.pinned is False

        bubble._on_content_click()
        pet.root.update()
        assert bubble.pinned is True
        assert bubble.close_after_id is None
        assert bubble.close_label is not None
        assert bool(bubble.close_label.winfo_ismapped()) is True
        assert bubble.close_label.cget("fg") == SideBubble.CLOSE_FG

        bubble._on_close_click()
        assert bubble.window is None
        bubble.set_text("同一条回复的后续流式文本不应重开")
        assert bubble.window is None
        bubble.complete_reply()
        assert bubble.window is None

        bubble.start_reply()
        pet.root.update()
        assert bubble.window is not None
        bubble.close(animated=False)

        print(json.dumps({
            "ok": True,
            "leftPetSide": "right",
            "rightPetSide": "left",
            "tailMirrors": True,
            "autoScrollBottom": True,
            "autoCloseMs": SideBubble.AUTO_CLOSE_MS,
            "scrollRenewsTimer": True,
            "clickPinsAndShowsClose": True,
        }, ensure_ascii=False, indent=2))
    finally:
        bubble.close(animated=False)
        pet.root.destroy()


if __name__ == "__main__":
    main()
