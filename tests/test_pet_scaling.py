from __future__ import annotations

import json
import sys
import tkinter as tk
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import CANVAS_HEIGHT, CANVAS_WIDTH, ShorekeeperPet  # noqa: E402


class FakeEngine:
    def __init__(self) -> None:
        self.reloaded_scale: float | None = None
        self.frame_index = 99

    def reload_at_scale(self, scale: float, _idle_frames: list[Image.Image]) -> None:
        self.reloaded_scale = scale

    def tick(self) -> str:
        return ""


class FakeSideBubble:
    def __init__(self) -> None:
        self.repositions = 0

    def reposition(self) -> None:
        self.repositions += 1


def make_pet() -> ShorekeeperPet:
    pet = ShorekeeperPet.__new__(ShorekeeperPet)
    pet.root = tk.Tk()
    pet.root.title("Pet scaling test")
    pet.root.geometry(f"{CANVAS_WIDTH}x{CANVAS_HEIGHT}+200+200")
    pet.scale_var = tk.IntVar(master=pet.root, value=100)
    pet._scale_factor = 1.0
    pet.canvas_width = CANVAS_WIDTH
    pet.canvas_height = CANVAS_HEIGHT
    pet.canvas = tk.Canvas(
        pet.root,
        width=CANVAS_WIDTH,
        height=CANVAS_HEIGHT,
        highlightthickness=0,
        bd=0,
    )
    pet.canvas.pack()
    pet.sprite_item = pet.canvas.create_image(
        CANVAS_WIDTH // 2,
        CANVAS_HEIGHT - 8,
        anchor="s",
        image="",
    )
    pet._idle_pil_frames = [Image.new("RGBA", (168, 128), (0, 0, 0, 0))]
    pet.engine = FakeEngine()
    pet.side_bubble = FakeSideBubble()
    pet.settings = {}
    pet._save_settings = lambda: None
    pet.say = lambda *_args, **_kwargs: None
    pet.root.update_idletasks()
    return pet


def bottom_center(pet: ShorekeeperPet) -> tuple[float, int]:
    pet.root.update_idletasks()
    return (
        pet.root.winfo_x() + pet.canvas_width / 2,
        pet.root.winfo_y() + pet.canvas_height,
    )


def main() -> None:
    assert ShorekeeperPet._canvas_size_for_scale(0.8) == (152, 128)
    assert ShorekeeperPet._canvas_size_for_scale(1.0) == (190, 160)
    assert ShorekeeperPet._canvas_size_for_scale(1.3) == (247, 208)

    pet = make_pet()
    try:
        anchor_before = bottom_center(pet)
        pet.scale_var.set(130)
        pet._resize()
        pet.root.update()

        assert (pet.canvas_width, pet.canvas_height) == (247, 208)
        assert pet.root.winfo_width() == 247
        assert pet.root.winfo_height() == 208
        assert pet.engine.reloaded_scale == 1.3
        assert pet.engine.frame_index == 0
        assert pet.canvas.coords(pet.sprite_item) == [123.0, 198.0]
        assert pet.side_bubble.repositions == 1

        anchor_after_large = bottom_center(pet)
        assert abs(anchor_after_large[0] - anchor_before[0]) <= 1
        assert abs(anchor_after_large[1] - anchor_before[1]) <= 1

        # Every 160x120 event frame and the 168x128 idle frame fit at 130%.
        assert round(160 * 1.3) <= pet.canvas_width
        assert round(120 * 1.3) <= pet.canvas_height
        assert round(168 * 1.3) <= pet.canvas_width
        assert round(128 * 1.3) <= pet.canvas_height

        pet.scale_var.set(80)
        pet._resize()
        pet.root.update()
        assert (pet.canvas_width, pet.canvas_height) == (152, 128)
        anchor_after_small = bottom_center(pet)
        assert abs(anchor_after_small[0] - anchor_after_large[0]) <= 1
        assert abs(anchor_after_small[1] - anchor_after_large[1]) <= 1

        # Position clamping follows the monitor nearest the proposed pet centre,
        # allows negative virtual-desktop coordinates, and uses rcWork rather
        # than a hard-coded taskbar height.
        negative_work_area = (-1920, -1080, 0, 0)
        pet._monitor_work_area_provider = (
            lambda _x, _y: negative_work_area
        )
        assert pet._clamp_position(-99_999, -99_999) == (-1920, -1080)
        assert pet._clamp_position(99_999, 99_999) == (
            negative_work_area[2] - pet.canvas_width,
            negative_work_area[3] - pet.canvas_height,
        )

        pet.settings = {"x": -1800, "y": -900}
        pet._place_initially()
        pet.root.update()
        assert pet.root.winfo_x() == -1800
        assert pet.root.winfo_y() == -900

        print(json.dumps({
            "ok": True,
            "sizes": {"80": [152, 128], "100": [190, 160], "130": [247, 208]},
            "bottomCenterStable": True,
            "largeFramesFit": True,
            "dynamicClamp": True,
        }, ensure_ascii=False, indent=2))
    finally:
        pet.root.destroy()


if __name__ == "__main__":
    main()
