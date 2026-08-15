"""Build deterministic transparent GIF action packs for Hermes events."""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageSequence


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "assets" / "shorekeeper-laying.png"
OUTPUT = ROOT / "actions" / "hermes_events"
CANVAS = (160, 120)
KEY = (255, 0, 255)


@dataclass(frozen=True)
class Action:
    id: str
    display_name: str
    event: str
    source: str
    effect: str
    state: str
    playback: str
    frames: int
    fps: int
    priority: int
    fallback: str
    interruptible: bool = True
    coalesce_ms: int = 0


ACTIONS = (
    Action("gateway_startup", "网关启动", "gateway:startup", "hook", "startup", "system.starting", "once", 18, 10, 20, "awake.idle", False),
    Action("session_start", "会话开始", "session:start", "hook", "hello", "session.starting", "once", 16, 10, 35, "awake.idle", False),
    Action("session_end", "会话结束", "session:end", "hook", "sleep", "session.ending", "once", 18, 10, 35, "sleeping.idle", False),
    Action("session_reset", "会话重置", "session:reset", "hook", "reset", "session.resetting", "once", 18, 10, 45, "awake.idle", False),
    Action("agent_start", "开始思考", "agent:start", "hook", "thinking", "agent.thinking", "hold", 12, 8, 50, "awake.idle", True, 250),
    Action("agent_step", "思考推进", "agent:step", "hook", "step", "agent.thinking.step", "once", 12, 10, 55, "agent.thinking", True, 350),
    Action("agent_end", "回答结束", "agent:end", "hook", "settle", "agent.finishing", "once", 16, 10, 60, "awake.idle", False),
    Action("command_any", "执行命令", "command:*", "hook", "command", "command.running", "once", 14, 10, 65, "awake.idle", True, 200),
    Action("response_created", "响应创建", "response.created", "sse", "attention", "response.starting", "hold", 12, 8, 52, "agent.thinking", True, 250),
    Action("response_text_delta", "正在输出文字", "response.output_text.delta", "sse", "typing", "response.streaming_text", "hold", 12, 10, 58, "agent.thinking", True, 300),
    Action("response_text_done", "文字输出完成", "response.output_text.done", "sse", "text_done", "response.text_done", "once", 12, 10, 62, "agent.thinking", True, 200),
    Action("output_item_added", "输出项加入", "response.output_item.added", "sse", "item_add", "response.item_added", "once", 12, 10, 61, "agent.thinking", True, 150),
    Action("output_item_done", "输出项完成", "response.output_item.done", "sse", "item_done", "response.item_done", "once", 12, 10, 64, "agent.thinking", True, 150),
    Action("tool_started", "工具开始", "tool.started", "sse", "busy", "tool.running", "hold", 12, 10, 80, "agent.thinking", True, 150),
    Action("tool_completed", "工具完成", "tool.completed", "sse", "success", "tool.succeeded", "once", 14, 10, 85, "agent.thinking", False, 150),
    Action("tool_failed", "工具失败", "tool.failed", "sse", "tool_error", "tool.failed", "once", 18, 10, 95, "agent.thinking", False, 150),
    Action("response_completed", "响应完成", "response.completed", "sse", "celebrate", "response.completed", "once", 18, 10, 90, "awake.idle", False, 250),
    Action("response_failed", "响应失败", "response.failed", "sse", "response_error", "response.failed", "once", 20, 10, 100, "awake.idle", False, 250),
)


def load_base() -> Image.Image:
    image = Image.open(SOURCE).convert("RGBA")
    bbox = image.getchannel("A").getbbox()
    if not bbox:
        raise RuntimeError("Source sprite has no visible pixels")
    image = image.crop(bbox)
    ratio = min(116 / image.width, 82 / image.height)
    image = image.resize((round(image.width * ratio), round(image.height * ratio)), Image.Resampling.NEAREST)
    image.putalpha(image.getchannel("A").point(lambda value: 255 if value >= 88 else 0))
    return image


def line(draw: ImageDraw.ImageDraw, points: list[tuple[int, int]], fill: str, width: int = 3) -> None:
    draw.line(points, fill=fill, width=width, joint="curve")


def sparkle(draw: ImageDraw.ImageDraw, x: int, y: int, color: str, size: int = 5) -> None:
    draw.rectangle((x - 1, y - size, x + 1, y + size), fill=color)
    draw.rectangle((x - size, y - 1, x + size, y + 1), fill=color)
    draw.point((x, y), fill="#ffffff")


def check(draw: ImageDraw.ImageDraw, x: int, y: int, color: str = "#60e8b2") -> None:
    line(draw, [(x - 8, y), (x - 2, y + 7), (x + 11, y - 8)], color, 4)


def cross(draw: ImageDraw.ImageDraw, x: int, y: int, color: str = "#ff7188") -> None:
    line(draw, [(x - 8, y - 8), (x + 8, y + 8)], color, 4)
    line(draw, [(x + 8, y - 8), (x - 8, y + 8)], color, 4)


def draw_z(draw: ImageDraw.ImageDraw, x: int, y: int, scale: int, color: str) -> None:
    width = 5 * scale
    draw.rectangle((x, y, x + width, y + scale), fill=color)
    draw.rectangle((x, y + 4 * scale, x + width, y + 5 * scale), fill=color)
    for offset in range(4):
        draw.rectangle((x + (4 - offset) * scale, y + (offset + 1) * scale, x + (5 - offset) * scale, y + (offset + 2) * scale), fill=color)


def draw_gear(draw: ImageDraw.ImageDraw, x: int, y: int, phase: float) -> None:
    color = "#68ddff"
    radius = 10
    draw.ellipse((x - radius, y - radius, x + radius, y + radius), outline=color, width=3)
    draw.ellipse((x - 3, y - 3, x + 3, y + 3), fill="#0f376b")
    for index in range(8):
        angle = phase * math.tau + index * math.pi / 4
        px = round(x + math.cos(angle) * 13)
        py = round(y + math.sin(angle) * 13)
        draw.rectangle((px - 2, py - 2, px + 2, py + 2), fill=color)


def transform_sprite(base: Image.Image, effect: str, index: int, count: int) -> tuple[Image.Image, int, int]:
    phase = index / count
    wave = math.sin(phase * math.tau)
    x_shift = 0
    y_shift = round(-1.5 * wave)
    scale = 1.0

    if effect == "startup":
        progress = min(1.0, index / max(1, count * 0.65))
        scale = 0.68 + 0.32 * progress
        y_shift = round(10 * (1 - progress) - 2 * math.sin(progress * math.pi))
    elif effect == "hello":
        y_shift = -round(max(0, math.sin(index / (count - 1) * math.pi)) * 8)
    elif effect == "sleep":
        y_shift = round(index / (count - 1) * 5)
        scale = 1.0 - 0.05 * index / (count - 1)
    elif effect == "reset":
        y_shift = -round(abs(math.sin(phase * math.tau)) * 4)
        x_shift = round(math.sin(phase * math.tau) * 3)
    elif effect in {"thinking", "step", "attention", "typing", "text_done", "item_add", "item_done", "busy"}:
        y_shift = -round((wave + 1) * 1.2)
    elif effect in {"settle", "success", "celebrate"}:
        y_shift = -round(max(0, math.sin(index / (count - 1) * math.pi)) * (5 if effect == "settle" else 8))
    elif effect in {"tool_error", "response_error"}:
        x_shift = (-2, 2, -2, 2, 0)[index % 5]
        y_shift = 0
    elif effect == "command":
        y_shift = -round(abs(wave) * 3)

    if scale != 1:
        sprite = base.resize((max(1, round(base.width * scale)), max(1, round(base.height * scale))), Image.Resampling.NEAREST)
    else:
        sprite = base
    return sprite, x_shift, y_shift


def draw_effect(canvas: Image.Image, effect: str, index: int, count: int, behind: bool) -> None:
    draw = ImageDraw.Draw(canvas)
    phase = index / count
    wave = math.sin(phase * math.tau)
    cyan, pale, navy = "#44dcff", "#d9f8ff", "#163b7c"

    if behind:
        if effect in {"startup", "attention", "response_created"}:
            radius = 27 + round((wave + 1) * 3)
            draw.ellipse((80 - radius, 79 - radius, 80 + radius, 79 + radius), outline="#73e7ff", width=3)
        if effect == "reset":
            draw.ellipse((26, 31, 137, 113), outline="#55dfff", width=3)
        return

    if effect == "startup":
        for x, y, delay in ((29, 33, 0), (133, 28, 4), (145, 73, 8), (20, 83, 12)):
            if (index + delay) % 12 < 7:
                sparkle(draw, x, y, pale, 4)
    elif effect == "hello":
        offset = round(wave * 2)
        line(draw, [(124, 34 + offset), (130, 27 + offset), (136, 34 + offset)], cyan, 3)
        line(draw, [(134, 39 - offset), (142, 31 - offset), (148, 38 - offset)], pale, 2)
    elif effect == "sleep":
        if index > 3:
            draw_z(draw, 119, 28 - index // 5, 2, "#8aaeff")
        if index > 8:
            draw_z(draw, 139, 15 - index // 7, 1, pale)
    elif effect == "reset":
        angle = phase * math.tau * 2
        x, y = round(81 + math.cos(angle) * 57), round(72 + math.sin(angle) * 42)
        sparkle(draw, x, y, pale, 4)
    elif effect == "thinking":
        active = index // 3 % 3
        for dot in range(3):
            radius = 4 if dot == active else 2
            draw.ellipse((119 + dot * 12 - radius, 28 - radius, 119 + dot * 12 + radius, 28 + radius), fill=cyan if dot == active else "#7a9ccf")
    elif effect == "step":
        active = index // 2 % 4
        for dot in range(4):
            color = pale if dot == active else "#4778bd"
            draw.rectangle((116 + dot * 9, 24, 121 + dot * 9, 29), fill=color)
    elif effect == "settle":
        if 4 <= index <= 12:
            sparkle(draw, 132, 31, pale, 5 - abs(8 - index) // 2)
    elif effect == "command":
        draw.rectangle((111, 13, 154, 43), fill="#102a55", outline=cyan, width=2)
        line(draw, [(119, 22), (126, 28), (119, 34)], pale, 3)
        cursor_on = index % 4 < 2
        if cursor_on:
            draw.rectangle((133, 32, 145, 35), fill=cyan)
    elif effect == "attention":
        draw.rectangle((132, 12, 137, 28), fill=cyan)
        draw.rectangle((132, 34, 137, 39), fill=pale)
    elif effect == "typing":
        for row in range(3):
            length = 10 + ((index + row * 3) % 7) * 3
            draw.rectangle((116, 18 + row * 9, 116 + length, 22 + row * 9), fill=(pale, cyan, "#80a8e8")[row])
        draw.rectangle((145 if index % 4 < 2 else 141, 38, 149, 42), fill=cyan)
    elif effect == "text_done":
        draw.rectangle((112, 14, 151, 43), fill="#e9fbff", outline=cyan, width=2)
        for row, width in enumerate((28, 22, 16)):
            draw.rectangle((118, 20 + row * 7, 118 + width, 22 + row * 7), fill="#4c76b9")
        check(draw, 145, 38, "#3ccf9a")
    elif effect in {"item_add", "item_done"}:
        grow = min(1.0, index / max(1, count // 2))
        half_w, half_h = round(19 * grow), round(14 * grow)
        draw.rectangle((134 - half_w, 28 - half_h, 134 + half_w, 28 + half_h), fill="#eefcff", outline=cyan, width=2)
        if effect == "item_add":
            draw.rectangle((131, 20, 137, 36), fill="#4bbfe8")
            draw.rectangle((126, 25, 142, 31), fill="#4bbfe8")
        elif index > count // 3:
            check(draw, 134, 28)
    elif effect == "busy":
        draw_gear(draw, 133, 27, phase)
        draw.rectangle((112, 43, 152, 47), fill=navy)
        progress = 4 + round((index / (count - 1)) * 34)
        draw.rectangle((113, 44, 113 + progress, 46), fill=cyan)
    elif effect == "success":
        draw.ellipse((115, 10, 153, 48), fill="#163b65", outline="#71f0bd", width=3)
        check(draw, 134, 29)
        if 3 < index < 11:
            sparkle(draw, 109, 16, pale, 3)
    elif effect == "tool_error":
        draw.ellipse((115, 10, 153, 48), fill="#4a213a", outline="#ff7188", width=3)
        cross(draw, 134, 29)
        draw.polygon(((105, 20), (110, 31), (100, 31)), fill="#7de8ff")
    elif effect == "celebrate":
        positions = ((23, 27), (136, 24), (148, 61), (30, 72))
        for number, (x, y) in enumerate(positions):
            if (index + number * 3) % 10 < 7:
                sparkle(draw, x, y, ("#fff2a6", pale, cyan, "#a7b9ff")[number], 4)
        if index > count // 3:
            check(draw, 134, 32, "#69edb7")
    elif effect == "response_error":
        draw.rectangle((112, 12, 154, 48), fill="#3f263d", outline="#ff7188", width=3)
        draw.rectangle((131, 19, 136, 34), fill="#ff8ba0")
        draw.rectangle((131, 39, 136, 44), fill="#ffd2d8")
        draw.polygon(((104, 19), (109, 31), (99, 31)), fill="#7de8ff")


def render_frames(base: Image.Image, action: Action) -> list[Image.Image]:
    frames: list[Image.Image] = []
    for index in range(action.frames):
        canvas = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
        draw_effect(canvas, action.effect, index, action.frames, behind=True)
        sprite, x_shift, y_shift = transform_sprite(base, action.effect, index, action.frames)
        x = (CANVAS[0] - sprite.width) // 2 + x_shift
        y = CANVAS[1] - sprite.height - 4 + y_shift
        canvas.alpha_composite(sprite, (x, y))
        draw_effect(canvas, action.effect, index, action.frames, behind=False)
        frames.append(canvas)
    return frames


def palette_frames(frames: list[Image.Image]) -> tuple[list[Image.Image], int]:
    rgb_frames: list[Image.Image] = []
    for frame in frames:
        rgb = Image.new("RGB", frame.size, KEY)
        rgb.paste(frame, (0, 0), frame)
        rgb_frames.append(rgb)
    atlas = Image.new("RGB", (frames[0].width * len(frames), frames[0].height), KEY)
    for index, rgb in enumerate(rgb_frames):
        atlas.paste(rgb, (index * frames[0].width, 0))
    palette = atlas.quantize(colors=255, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    values = palette.getpalette()
    palette_size = len(values) // 3
    transparent = min(
        range(palette_size),
        key=lambda item: (values[item * 3] - 255) ** 2 + values[item * 3 + 1] ** 2 + (values[item * 3 + 2] - 255) ** 2,
    )
    return [rgb.quantize(palette=palette, dither=Image.Dither.NONE) for rgb in rgb_frames], transparent


def save_gif(frames: list[Image.Image], target: Path, duration_ms: int, loop: bool) -> None:
    paletted, transparent = palette_frames(frames)
    loop_target = target if loop else target.with_name(target.stem + "-loop-source.gif")
    paletted[0].save(
        loop_target,
        save_all=True,
        append_images=paletted[1:],
        duration=duration_ms,
        loop=0,
        transparency=transparent,
        disposal=2,
        optimize=False,
    )
    if not loop:
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(loop_target), "-map", "0:v:0", "-c:v", "copy", "-loop", "-1", str(target)],
            check=True,
        )
        loop_target.unlink(missing_ok=True)


def write_action(base: Image.Image, action: Action) -> dict[str, object]:
    folder = OUTPUT / action.id
    if folder.exists():
        shutil.rmtree(folder)
    frames_folder = folder / "frames"
    frames_folder.mkdir(parents=True)
    frames = render_frames(base, action)
    for index, frame in enumerate(frames):
        frame.save(frames_folder / f"frame_{index:03d}.png", optimize=True)
    gif_name = f"{action.id}.gif"
    save_gif(frames, folder / gif_name, round(1000 / action.fps), action.playback == "hold")
    encoded_gif = Image.open(folder / gif_name)
    gif_frame_count = encoded_gif.n_frames
    gif_duration_ms = sum(
        int(frame.info.get("duration", round(1000 / action.fps)))
        for frame in ImageSequence.Iterator(encoded_gif)
    )
    frames[0].save(
        folder / "preview.webp",
        save_all=True,
        append_images=frames[1:],
        duration=round(1000 / action.fps),
        loop=0,
        lossless=True,
        method=6,
    )
    manifest = {
        "schema_version": 1,
        "id": action.id,
        "display_name": action.display_name,
        "description": f"Hermes 事件 {action.event} 的守岸人像素桌宠动作。",
        "event": {"name": action.event, "source": action.source},
        "state": action.state,
        "playback": action.playback,
        "loop": action.playback == "hold",
        "interruptible": action.interruptible,
        "fps": action.fps,
        "frame_duration_ms": round(1000 / action.fps),
        "frame_count": len(frames),
        "gif_frame_count": gif_frame_count,
        "duration_ms": gif_duration_ms,
        "canvas": {"width": CANVAS[0], "height": CANVAS[1]},
        "anchor": {"x": 0.5, "y": 1.0},
        "gif": gif_name,
        "webp_preview": "preview.webp",
        "frames": [f"frames/frame_{index:03d}.png" for index in range(len(frames))],
        "lifecycle": {
            "priority": action.priority,
            "fallback_state": action.fallback,
            "coalesce_ms": action.coalesce_ms,
            "restart_on_repeat": False if action.playback == "hold" else True,
        },
    }
    (folder / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def write_contact_sheet(manifests: list[dict[str, object]]) -> None:
    columns, cell_w, cell_h = 3, 185, 151
    rows = math.ceil(len(manifests) / columns)
    sheet = Image.new("RGB", (columns * cell_w, rows * cell_h), "#eef5f9")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    for index, manifest in enumerate(manifests):
        col, row = index % columns, index // columns
        x, y = col * cell_w, row * cell_h
        frame = Image.open(OUTPUT / str(manifest["id"]) / "frames" / "frame_000.png").convert("RGBA")
        tile = Image.new("RGB", CANVAS, "#ffffff")
        tile.paste(frame, (0, 0), frame)
        sheet.paste(tile, (x + 12, y + 6))
        draw.rectangle((x + 11, y + 5, x + 172, y + 126), outline="#9ab5c9", width=1)
        draw.text((x + 13, y + 132), str(manifest["id"]), fill="#17365d", font=font)
    sheet.save(OUTPUT / "inventory.png", optimize=True)


def main() -> int:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    base = load_base()
    manifests = [write_action(base, action) for action in ACTIONS]
    catalog = {
        "schema_version": 1,
        "name": "Hermes 守岸人事件动作库存",
        "canvas": {"width": CANVAS[0], "height": CANVAS[1]},
        "actions": [
            {
                "event": manifest["event"],
                "action_id": manifest["id"],
                "manifest": f"{manifest['id']}/manifest.json",
                "gif": f"{manifest['id']}/{manifest['gif']}",
                "state": manifest["state"],
                "playback": manifest["playback"],
                "priority": manifest["lifecycle"]["priority"],
                "fallback_state": manifest["lifecycle"]["fallback_state"],
                "coalesce_ms": manifest["lifecycle"]["coalesce_ms"],
            }
            for manifest in manifests
        ],
        "policy": {
            "event_preemption": "higher_priority_wins",
            "hold_actions": "do not restart on repeated events; keep playing until a terminal event changes state",
            "once_actions": "play once, then transition to fallback_state unless a newer higher-priority event arrived",
            "deduplication": "if hook and SSE represent the same transition, deduplicate within 300ms using session_id/run_id/tool id when available",
        },
    }
    (OUTPUT / "catalog.json").write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_contact_sheet(manifests)
    print(f"Built {len(manifests)} Hermes event action packs in {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
