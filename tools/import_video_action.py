"""Import a light-background reference video as a transparent pet action pack."""

from __future__ import annotations

import argparse
from collections import deque
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--fps", type=int, default=10)
    parser.add_argument("--width", type=int, default=160)
    parser.add_argument("--height", type=int, default=120)
    return parser.parse_args()


def extract_frames(source: Path, folder: Path, fps: int) -> list[Path]:
    pattern = folder / "raw_%04d.png"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(source),
            "-vf",
            f"fps={fps}",
            str(pattern),
        ],
        check=True,
    )
    return sorted(folder.glob("raw_*.png"))


def remove_connected_background(image: Image.Image) -> Image.Image:
    rgb = image.convert("RGB")
    width, height = rgb.size
    pixels = rgb.load()
    seeds = ((0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1))
    references = [pixels[x, y] for x, y in seeds]
    queue: deque[tuple[int, int]] = deque(seeds)
    visited = bytearray(width * height)
    background_mask = bytearray(width * height)

    def resembles_background(color: tuple[int, int, int]) -> bool:
        return any(
            sum((color[channel] - reference[channel]) ** 2 for channel in range(3)) <= 48**2
            for reference in references
        )

    while queue:
        x, y = queue.popleft()
        index = y * width + x
        if visited[index]:
            continue
        visited[index] = 1
        if not resembles_background(pixels[x, y]):
            continue
        background_mask[index] = 1
        if x > 0:
            queue.append((x - 1, y))
        if x + 1 < width:
            queue.append((x + 1, y))
        if y > 0:
            queue.append((x, y - 1))
        if y + 1 < height:
            queue.append((x, y + 1))

    alpha = Image.new("L", rgb.size, 255)
    alpha_pixels = alpha.load()
    for y in range(height):
        for x in range(width):
            if background_mask[y * width + x]:
                alpha_pixels[x, y] = 0

    result = image.convert("RGBA")
    result.putalpha(alpha)
    return result


def union_bbox(images: list[Image.Image]) -> tuple[int, int, int, int]:
    boxes = [image.getchannel("A").getbbox() for image in images]
    boxes = [box for box in boxes if box is not None]
    if not boxes:
        raise RuntimeError("No foreground subject was found in the video.")
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def fit_frame(image: Image.Image, bbox: tuple[int, int, int, int], size: tuple[int, int]) -> Image.Image:
    cropped = image.crop(bbox)
    max_width, max_height = size[0] - 8, size[1] - 8
    ratio = min(max_width / cropped.width, max_height / cropped.height)
    resized = cropped.resize(
        (max(1, round(cropped.width * ratio)), max(1, round(cropped.height * ratio))),
        Image.Resampling.NEAREST,
    )
    alpha = resized.getchannel("A").point(lambda value: 255 if value >= 96 else 0)
    resized.putalpha(alpha)
    canvas = Image.new("RGBA", size, (0, 0, 0, 0))
    x = (size[0] - resized.width) // 2
    y = size[1] - resized.height - 4
    canvas.paste(resized, (x, y), resized)
    return canvas


def save_transparent_gif(frames: list[Image.Image], path: Path, duration_ms: int) -> None:
    keyed_frames: list[Image.Image] = []
    for frame in frames:
        keyed = Image.new("RGB", frame.size, (255, 0, 255))
        keyed.paste(frame, (0, 0), frame)
        keyed_frames.append(keyed)

    atlas = Image.new("RGB", (frames[0].width * len(frames), frames[0].height), (255, 0, 255))
    for index, frame in enumerate(keyed_frames):
        atlas.paste(frame, (index * frames[0].width, 0))
    palette = atlas.quantize(colors=255, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)

    palette_values = palette.getpalette()
    palette_size = len(palette_values) // 3
    transparency = min(
        range(palette_size),
        key=lambda index: (
            (palette_values[index * 3] - 255) ** 2
            + palette_values[index * 3 + 1] ** 2
            + (palette_values[index * 3 + 2] - 255) ** 2
        ),
    )
    gif_frames = [
        frame.quantize(palette=palette, dither=Image.Dither.NONE)
        for frame in keyed_frames
    ]
    gif_frames[0].save(
        path,
        save_all=True,
        append_images=gif_frames[1:],
        duration=duration_ms,
        transparency=transparency,
        disposal=2,
        optimize=False,
    )


def main() -> int:
    args = parse_args()
    output = args.output.resolve()
    frames_dir = output / "frames"
    source_dir = output / "source"
    frames_dir.mkdir(parents=True, exist_ok=True)
    source_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="shorekeeper-action-") as tmp:
        raw_paths = extract_frames(args.source.resolve(), Path(tmp), args.fps)
        if not raw_paths:
            raise RuntimeError("FFmpeg extracted no frames.")
        transparent = [remove_connected_background(Image.open(path)) for path in raw_paths]
        bbox = union_bbox(transparent)
        frames = [fit_frame(image, bbox, (args.width, args.height)) for image in transparent]

    for index, frame in enumerate(frames):
        frame.save(frames_dir / f"frame_{index:03d}.png", optimize=True)

    frame_duration_ms = round(1000 / args.fps)
    frames[0].save(
        output / "preview.webp",
        save_all=True,
        append_images=frames[1:],
        duration=frame_duration_ms,
        loop=0,
        lossless=True,
        method=6,
    )
    save_transparent_gif(frames, output / "stand-tap-button-once.gif", frame_duration_ms)
    shutil.copy2(args.source, source_dir / "reference.mp4")

    manifest = {
        "schema_version": 1,
        "id": "stand_tap_button",
        "display_name": "起身敲按钮",
        "description": "守岸人从趴伏姿势撑起身体、敲击左侧按钮，然后回到趴伏姿势的一次性互动动作。",
        "state": "interaction",
        "pose": {"from": "lying", "action": "upright_tap", "to": "lying"},
        "mood": "calm",
        "energy": "medium",
        "loop": False,
        "interruptible": False,
        "fps": args.fps,
        "frame_duration_ms": frame_duration_ms,
        "frame_count": len(frames),
        "canvas": {"width": args.width, "height": args.height},
        "anchor": {"x": 0.5, "y": 1.0},
        "gif": "stand-tap-button-once.gif",
        "loop_preview_gif": "stand-tap-button.gif",
        "webp_preview": "preview.webp",
        "frames": [f"frames/frame_{index:03d}.png" for index in range(len(frames))],
        "lifecycle": {
            "suggested_trigger": "interaction.button_tap",
            "enter_from": ["awake.idle", "sleepy.idle"],
            "exit_to": ["awake.idle"],
            "playback": "once",
            "cooldown_ms": 8000,
            "priority": 70,
        },
        "source": {
            "reference": "source/reference.mp4",
            "note": "User-provided motion reference; background removed and normalized for desktop-pet use.",
        },
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Created {len(frames)} frames at {args.fps} fps in {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
