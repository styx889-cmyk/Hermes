#!/usr/bin/env python3
"""
Asset-locked vertical video factory.

This script orchestrates:
1. Reference import from a local file or URL.
2. Automatic scene detection and storyboard creation.
3. Optional provider calls for vision breakdown, AI scene rendering, captions, and TTS.
4. Product-locked Hermes AI rendering or local compositing.
5. Thai subtitle burn-in.
6. Final 1080x1920, 30fps, H.264/AAC MP4 export.

The built-in renderer is a deterministic fallback for pipeline testing. For
Hermes AI or any other video provider, set AI_VIDEO_COMMAND or pass
--ai-video-command. Provider commands are invoked as:

    provider_command request.json response.json

Each provider writes JSON to response.json.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


WIDTH = 1080
HEIGHT = 1920
FPS = 30


class VideoFactoryError(RuntimeError):
    pass


@dataclass
class Scene:
    index: int
    start: float
    end: float
    duration: float
    thumbnail: str
    description: str = ""
    prompt: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "duration": round(self.duration, 3),
            "thumbnail": self.thumbnail,
            "description": self.description,
            "prompt": self.prompt,
        }


@dataclass
class CaptionEntry:
    start: float
    end: float
    text: str


def log(message: str) -> None:
    print(f"[video-factory] {message}", flush=True)


def truthy(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def run(cmd: List[str], *, cwd: Optional[Path] = None, capture: bool = False, check: bool = True) -> subprocess.CompletedProcess:
    printable = " ".join(shlex.quote(part) for part in cmd)
    log(printable)
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        check=check,
    )


def ensure_tool(name: str, required: bool = True) -> bool:
    if shutil.which(name):
        return True
    if required:
        raise VideoFactoryError(f"Required tool not found on PATH: {name}")
    return False


def ffmpeg_has_filter(name: str) -> bool:
    completed = run(["ffmpeg", "-hide_banner", "-filters"], capture=True, check=False)
    combined = f"{completed.stdout or ''}\n{completed.stderr or ''}"
    return bool(re.search(rf"\b{name}\b", combined))


def is_url(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://")


def safe_name(path_or_url: str, fallback: str) -> str:
    raw = path_or_url.rstrip("/").split("/")[-1] or fallback
    raw = re.sub(r"[^A-Za-z0-9._-]+", "_", raw)
    return raw[:120] or fallback


def download_file(url: str, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    log(f"Downloading asset: {url}")
    with urllib.request.urlopen(url, timeout=120) as response, destination.open("wb") as output:
        shutil.copyfileobj(response, output)
    return destination


def resolve_asset(source: str, workdir: Path, label: str) -> Path:
    if not source:
        raise VideoFactoryError(f"Missing source for {label}")
    if is_url(source):
        suffix = Path(safe_name(source, f"{label}.asset")).suffix or ".asset"
        return download_file(source, workdir / "inputs" / f"{label}{suffix}")
    path = Path(source).expanduser()
    if not path.exists():
        raise VideoFactoryError(f"{label} does not exist: {source}")
    return path.resolve()


def ingest_reference(source: str, workdir: Path) -> Path:
    inputs_dir = workdir / "inputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    if is_url(source):
        ensure_tool("yt-dlp", required=True)
        output_template = str(inputs_dir / "reference.%(ext)s")
        run(
            [
                "yt-dlp",
                "--no-playlist",
                "--restrict-filenames",
                "-f",
                "bv*+ba/b",
                "--merge-output-format",
                "mp4",
                "-o",
                output_template,
                source,
            ]
        )
        candidates = sorted(inputs_dir.glob("reference.*"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not candidates:
            raise VideoFactoryError("yt-dlp completed but no reference video was created")
        return candidates[0].resolve()
    path = Path(source).expanduser()
    if not path.exists():
        raise VideoFactoryError(f"Reference video does not exist: {source}")
    copied = inputs_dir / f"reference{path.suffix or '.mp4'}"
    if path.resolve() != copied.resolve():
        shutil.copy2(path, copied)
    return copied.resolve()


def ffprobe_json(path: Path, args: List[str]) -> Dict[str, Any]:
    completed = run(["ffprobe", "-v", "error", *args, "-of", "json", str(path)], capture=True)
    return json.loads(completed.stdout or "{}")


def get_duration(path: Path) -> float:
    data = ffprobe_json(path, ["-show_entries", "format=duration"])
    try:
        return float(data["format"]["duration"])
    except (KeyError, TypeError, ValueError) as exc:
        raise VideoFactoryError(f"Could not read duration for {path}") from exc


def has_audio(path: Path) -> bool:
    data = ffprobe_json(path, ["-select_streams", "a:0", "-show_entries", "stream=codec_type"])
    return bool(data.get("streams"))


def normalize_reference(reference: Path, output: Path, duration_limit: Optional[float]) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-i", str(reference)]
    if duration_limit and duration_limit > 0:
        cmd += ["-t", f"{duration_limit:.3f}"]
    cmd += [
        "-map",
        "0:v:0",
        "-vf",
        f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,crop={WIDTH}:{HEIGHT},fps={FPS},setsar=1,format=yuv420p",
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "18",
        str(output),
    ]
    run(cmd)
    return output


def parse_scene_times(metadata_path: Path) -> List[float]:
    if not metadata_path.exists():
        return []
    times: List[float] = []
    for line in metadata_path.read_text(errors="ignore").splitlines():
        match = re.search(r"pts_time[:=]([0-9.]+)", line)
        if match:
            try:
                times.append(float(match.group(1)))
            except ValueError:
                pass
    return times


def reduce_cuts(cuts: List[float], duration: float, max_scenes: int, min_scene: float = 0.85) -> List[float]:
    filtered: List[float] = []
    last = 0.0
    for cut in sorted(set(round(c, 3) for c in cuts)):
        if cut <= min_scene or cut >= duration - min_scene:
            continue
        if cut - last >= min_scene:
            filtered.append(cut)
            last = cut
    max_cuts = max(0, max_scenes - 1)
    if len(filtered) <= max_cuts:
        return filtered
    step = len(filtered) / max_cuts
    return [filtered[min(len(filtered) - 1, int(i * step))] for i in range(max_cuts)]


def fallback_cuts(duration: float, max_scenes: int) -> List[float]:
    scene_count = max(1, min(max_scenes, math.ceil(duration / 3.0)))
    return [duration * i / scene_count for i in range(1, scene_count)]


def extract_thumbnail(video: Path, timestamp: float, output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    run(
        [
            "ffmpeg",
            "-y",
            "-ss",
            f"{timestamp:.3f}",
            "-i",
            str(video),
            "-frames:v",
            "1",
            "-vf",
            "scale=540:-2",
            str(output),
        ]
    )
    return output


def detect_scenes(video: Path, workdir: Path, threshold: float, max_scenes: int) -> List[Scene]:
    duration = get_duration(video)
    scenes_dir = workdir / "scenes"
    scenes_dir.mkdir(parents=True, exist_ok=True)
    metadata = scenes_dir / "scene_metadata.txt"
    cut_pattern = scenes_dir / "cut_%04d.jpg"
    filter_expr = f"select='gt(scene,{threshold})',metadata=print:file={metadata},scale=360:-2"
    run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(video),
            "-vf",
            filter_expr,
            "-vsync",
            "vfr",
            str(cut_pattern),
        ],
        check=False,
    )
    cuts = reduce_cuts(parse_scene_times(metadata), duration, max_scenes)
    if not cuts and duration > 1.5:
        cuts = fallback_cuts(duration, max_scenes)
    boundaries = [0.0, *cuts, duration]
    scenes: List[Scene] = []
    for idx, (start, end) in enumerate(zip(boundaries, boundaries[1:]), start=1):
        if end - start <= 0.25:
            continue
        thumb = extract_thumbnail(video, start + ((end - start) / 2), scenes_dir / f"scene_{idx:03d}.jpg")
        scenes.append(
            Scene(
                index=idx,
                start=start,
                end=end,
                duration=end - start,
                thumbnail=str(thumb),
                description=f"Reference scene {idx}: match the pacing and composition without copying protected creative assets.",
                prompt=(
                    f"Create a vertical product ad scene matching the reference timing from {start:.2f}s "
                    f"to {end:.2f}s. Keep the supplied product image visually unchanged."
                ),
            )
        )
    if not scenes:
        raise VideoFactoryError("No usable scenes were detected")
    return scenes


def call_json_provider(command: str, request: Dict[str, Any], workdir: Path, name: str) -> Dict[str, Any]:
    if not command:
        return {}
    provider_dir = workdir / "providers" / name
    provider_dir.mkdir(parents=True, exist_ok=True)
    request_path = provider_dir / "request.json"
    response_path = provider_dir / "response.json"
    request_path.write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")
    if response_path.exists():
        response_path.unlink()
    cmd = shlex.split(command) + [str(request_path), str(response_path)]
    run(cmd)
    if not response_path.exists():
        raise VideoFactoryError(f"{name} provider did not write response JSON: {response_path}")
    return json.loads(response_path.read_text(encoding="utf-8"))


def enrich_storyboard_with_vision(
    scenes: List[Scene],
    vision_command: str,
    reference_video: Path,
    product_image: Path,
    workdir: Path,
) -> List[Scene]:
    if not vision_command:
        return scenes
    response = call_json_provider(
        vision_command,
        {
            "task": "break_down_reference_video",
            "reference_video": str(reference_video),
            "product_image": str(product_image),
            "requirements": {
                "language": "en",
                "avoid_copying": True,
                "product_must_match_supplied_image": True,
            },
            "scenes": [scene.as_dict() for scene in scenes],
        },
        workdir,
        "vision_breakdown",
    )
    by_index = {int(item.get("index", 0)): item for item in response.get("scenes", [])}
    for scene in scenes:
        item = by_index.get(scene.index, {})
        scene.description = item.get("description") or scene.description
        scene.prompt = item.get("prompt") or scene.prompt
    return scenes


def write_storyboard(scenes: List[Scene], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "storyboard.json").write_text(
        json.dumps({"scenes": [scene.as_dict() for scene in scenes]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    lines = ["# Storyboard", ""]
    for scene in scenes:
        lines.append(f"## Scene {scene.index:03d} ({scene.start:.2f}s - {scene.end:.2f}s)")
        lines.append("")
        lines.append(scene.description)
        lines.append("")
        lines.append(f"Prompt: {scene.prompt}")
        lines.append("")
        lines.append(f"Thumbnail: {scene.thumbnail}")
        lines.append("")
    (output_dir / "storyboard.md").write_text("\n".join(lines), encoding="utf-8")


def product_overlay_geometry(product_scale: float, product_position: str) -> tuple[int, int, str]:
    scale = min(max(product_scale, 0.15), 0.85)
    max_w = max(1, int(WIDTH * 0.78))
    max_h = max(1, int(HEIGHT * scale))
    if product_position == "top":
        y_expr = "260"
    elif product_position == "bottom":
        y_expr = "H-h-360"
    else:
        y_expr = "(H-h)/2"
    return max_w, max_h, y_expr


def render_fallback_scene(
    scene: Scene,
    product_image: Path,
    output: Path,
    product_scale: float,
    product_position: str,
) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    product_w, product_h, product_y = product_overlay_geometry(product_scale, product_position)
    filter_complex = (
        f"[0:v]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={WIDTH}:{HEIGHT},boxblur=luma_radius=18:luma_power=1,"
        "eq=saturation=0.85:brightness=-0.03[bg];"
        f"[1:v]format=rgba,scale={product_w}:{product_h}:force_original_aspect_ratio=decrease[prod];"
        "[bg][prod]overlay=x=(W-w)/2:y=(H-h)/2:format=auto,"
        f"fps={FPS},setsar=1,format=yuv420p[v]"
    )
    filter_complex = filter_complex.replace("y=(H-h)/2", f"y={product_y}")
    run(
        [
            "ffmpeg",
            "-y",
            "-loop",
            "1",
            "-t",
            f"{scene.duration:.3f}",
            "-i",
            scene.thumbnail,
            "-loop",
            "1",
            "-t",
            f"{scene.duration:.3f}",
            "-i",
            str(product_image),
            "-filter_complex",
            filter_complex,
            "-map",
            "[v]",
            "-r",
            str(FPS),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            str(output),
        ]
    )
    return output


def overlay_product_on_scene(
    source: Path,
    product_image: Path,
    output: Path,
    duration: float,
    product_scale: float,
    product_position: str,
) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    product_w, product_h, product_y = product_overlay_geometry(product_scale, product_position)
    filter_complex = (
        f"[0:v]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={WIDTH}:{HEIGHT},fps={FPS},setsar=1[bg];"
        f"[1:v]format=rgba,scale={product_w}:{product_h}:force_original_aspect_ratio=decrease[prod];"
        f"[bg][prod]overlay=x=(W-w)/2:y={product_y}:format=auto,"
        "format=yuv420p[v]"
    )
    run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(source),
            "-loop",
            "1",
            "-t",
            f"{duration:.3f}",
            "-i",
            str(product_image),
            "-filter_complex",
            filter_complex,
            "-map",
            "[v]",
            "-t",
            f"{duration:.3f}",
            "-r",
            str(FPS),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            str(output),
        ]
    )
    return output


def normalize_scene_video(source: Path, output: Path, duration: float) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(source),
            "-t",
            f"{duration:.3f}",
            "-vf",
            f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,crop={WIDTH}:{HEIGHT},fps={FPS},setsar=1,format=yuv420p",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            str(output),
        ]
    )
    return output


def render_ai_or_fallback_scenes(
    scenes: List[Scene],
    reference_video: Path,
    product_image: Path,
    ai_video_command: str,
    product_lock_mode: str,
    product_scale: float,
    product_position: str,
    workdir: Path,
) -> List[Path]:
    rendered_dir = workdir / "rendered_scenes"
    rendered_dir.mkdir(parents=True, exist_ok=True)
    normalized: List[Path] = []
    for scene in scenes:
        raw_output = rendered_dir / f"scene_{scene.index:03d}_raw.mp4"
        final_output = rendered_dir / f"scene_{scene.index:03d}.mp4"
        if ai_video_command:
            response = call_json_provider(
                ai_video_command,
                {
                    "task": "render_scene",
                    "reference_video": str(reference_video),
                    "reference_frame": scene.thumbnail,
                    "product_image": str(product_image),
                    "scene": scene.as_dict(),
                    "output_video": str(raw_output),
                    "requirements": {
                        "width": WIDTH,
                        "height": HEIGHT,
                        "fps": FPS,
                        "product_identity_lock": True,
                        "product_lock_mode": product_lock_mode,
                        "keep_product_logo_packaging_color_and_shape": True,
                        "do_not_copy_reference_people_or_brand_assets": True,
                    },
                },
                workdir,
                f"ai_scene_{scene.index:03d}",
            )
            provider_video = Path(response.get("video") or raw_output)
            if not provider_video.exists():
                raise VideoFactoryError(f"AI provider did not create scene video: {provider_video}")
            if product_lock_mode == "overlay":
                overlay_product_on_scene(
                    provider_video,
                    product_image,
                    final_output,
                    scene.duration,
                    product_scale,
                    product_position,
                )
            else:
                normalize_scene_video(provider_video, final_output, scene.duration)
        else:
            render_fallback_scene(scene, product_image, final_output, product_scale, product_position)
        normalized.append(final_output)
    return normalized


def concatenate_segments(segments: List[Path], output: Path, workdir: Path) -> Path:
    list_path = workdir / "concat.txt"
    list_path.write_text("".join(f"file '{segment.as_posix()}'\n" for segment in segments), encoding="utf-8")
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_path), "-c", "copy", str(output)])
    return output


def read_text_arg(value: Optional[str], file_path: Optional[str]) -> str:
    parts: List[str] = []
    if file_path:
        parts.append(Path(file_path).expanduser().read_text(encoding="utf-8"))
    if value:
        parts.append(value)
    return "\n".join(part for part in parts if part).strip()


def split_script(text: str, target_count: int) -> List[str]:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned:
        return []
    pieces = [p.strip() for p in re.split(r"(?<=[.!?。！？])\s+|\n+", cleaned) if p.strip()]
    if len(pieces) >= target_count:
        return pieces
    max_len = max(35, math.ceil(len(cleaned) / max(1, target_count)))
    chunks: List[str] = []
    remaining = cleaned
    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break
        split_at = remaining.rfind(" ", 0, max_len)
        if split_at < max_len * 0.45:
            split_at = max_len
        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    return chunks


def srt_time(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    hours, ms = divmod(ms, 3_600_000)
    minutes, ms = divmod(ms, 60_000)
    secs, ms = divmod(ms, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def parse_srt_timestamp(value: str) -> float:
    match = re.match(r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})", value.strip())
    if not match:
        raise VideoFactoryError(f"Invalid SRT timestamp: {value}")
    hours, minutes, seconds, millis = (int(part) for part in match.groups())
    return (hours * 3600) + (minutes * 60) + seconds + (millis / 1000)


def parse_srt(path: Path) -> List[CaptionEntry]:
    text = path.read_text(encoding="utf-8-sig")
    entries: List[CaptionEntry] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        if lines[0].isdigit():
            lines = lines[1:]
        if not lines or "-->" not in lines[0]:
            continue
        start_raw, end_raw = [part.strip() for part in lines[0].split("-->", 1)]
        entries.append(CaptionEntry(parse_srt_timestamp(start_raw), parse_srt_timestamp(end_raw), "\n".join(lines[1:])))
    return entries


def create_srt_from_script(script: str, scenes: List[Scene], output: Path) -> Path:
    chunks = split_script(script, len(scenes))
    if not chunks:
        raise VideoFactoryError("Thai subtitles require --thai-srt, --thai-script, --thai-script-file, or CAPTION_COMMAND")
    entries: List[str] = []
    for idx, scene in enumerate(scenes, start=1):
        text = chunks[min(idx - 1, len(chunks) - 1)]
        entries.append(f"{idx}\n{srt_time(scene.start)} --> {srt_time(scene.end)}\n{text}\n")
    output.write_text("\n".join(entries), encoding="utf-8")
    return output


def active_caption(entries: List[CaptionEntry], timestamp: float) -> str:
    for entry in entries:
        if entry.start <= timestamp <= entry.end:
            return entry.text
    return ""


def text_width(draw: Any, text: str, font: Any) -> int:
    bbox = draw.textbbox((0, 0), text, font=font, stroke_width=0)
    return bbox[2] - bbox[0]


def wrap_caption_text(draw: Any, text: str, font: Any, max_width: int) -> List[str]:
    lines: List[str] = []
    for paragraph in text.splitlines():
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        words = paragraph.split()
        if len(words) > 1:
            current = ""
            for word in words:
                candidate = word if not current else f"{current} {word}"
                if text_width(draw, candidate, font) <= max_width:
                    current = candidate
                else:
                    if current:
                        lines.append(current)
                    current = word
            if current:
                lines.append(current)
        else:
            current = ""
            for char in paragraph:
                candidate = current + char
                if text_width(draw, candidate, font) <= max_width:
                    current = candidate
                else:
                    if current:
                        lines.append(current)
                    current = char
            if current:
                lines.append(current)
    return lines[:3]


def find_thai_font() -> Optional[Path]:
    env_path = os.getenv("THAI_FONT_PATH")
    candidates = [
        env_path,
        "/usr/share/fonts/truetype/noto/NotoSansThai-Regular.ttf",
        "/usr/share/fonts/truetype/tlwg/Garuda.ttf",
        "/usr/share/fonts/truetype/tlwg/Loma.ttf",
        "/System/Library/Fonts/Supplemental/Thonburi.ttf",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/Library/Fonts/Arial Unicode.ttf",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return Path(candidate)
    if shutil.which("fc-match"):
        completed = run(["fc-match", "-f", "%{file}", "Noto Sans Thai"], capture=True, check=False)
        font_path = (completed.stdout or "").strip()
        if font_path and Path(font_path).exists():
            return Path(font_path)
    return None


def burn_subtitles_with_pillow(video: Path, audio: Path, subtitles: Path, output: Path, workdir: Path) -> Path:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        raise VideoFactoryError(
            "FFmpeg has no subtitles filter and Pillow is not installed. Install Pillow or use an FFmpeg build with libass."
        ) from exc

    frames_dir = workdir / "pillow_frames"
    burned_dir = workdir / "pillow_frames_burned"
    for directory in (frames_dir, burned_dir):
        if directory.exists():
            shutil.rmtree(directory)
        directory.mkdir(parents=True, exist_ok=True)

    run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(video),
            "-vf",
            f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,crop={WIDTH}:{HEIGHT},fps={FPS}",
            str(frames_dir / "frame_%06d.png"),
        ]
    )

    entries = parse_srt(subtitles)
    font_path = find_thai_font()
    if font_path:
        font = ImageFont.truetype(str(font_path), 52)
    else:
        font = ImageFont.load_default()
        log("Warning: no Thai font found; subtitle rendering may be incomplete.")

    frame_paths = sorted(frames_dir.glob("frame_*.png"))
    max_text_width = WIDTH - 160
    for index, frame_path in enumerate(frame_paths):
        timestamp = index / FPS
        text = active_caption(entries, timestamp)
        image = Image.open(frame_path).convert("RGBA")
        if text:
            overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
            draw = ImageDraw.Draw(overlay)
            lines = wrap_caption_text(draw, text, font, max_text_width)
            line_boxes = [draw.textbbox((0, 0), line, font=font, stroke_width=3) for line in lines]
            line_heights = [box[3] - box[1] for box in line_boxes]
            total_height = sum(line_heights) + max(0, len(lines) - 1) * 14
            y = HEIGHT - 220 - total_height
            box_top = max(0, y - 22)
            box_bottom = min(HEIGHT, y + total_height + 28)
            draw.rounded_rectangle(
                (70, box_top, WIDTH - 70, box_bottom),
                radius=24,
                fill=(0, 0, 0, 115),
            )
            for line, line_height in zip(lines, line_heights):
                x = WIDTH / 2
                draw.text(
                    (x, y),
                    line,
                    font=font,
                    fill=(255, 255, 255, 255),
                    anchor="ma",
                    stroke_width=3,
                    stroke_fill=(0, 0, 0, 220),
                )
                y += line_height + 14
            image = Image.alpha_composite(image, overlay)
        image.convert("RGB").save(burned_dir / frame_path.name, quality=95)

    run(
        [
            "ffmpeg",
            "-y",
            "-framerate",
            str(FPS),
            "-i",
            str(burned_dir / "frame_%06d.png"),
            "-i",
            str(audio),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-shortest",
            "-r",
            str(FPS),
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(output),
        ]
    )
    return output


def srt_to_plain_text(path: Path) -> str:
    lines: List[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.isdigit() or "-->" in stripped:
            continue
        lines.append(stripped)
    return " ".join(lines)


def prepare_subtitles(
    scenes: List[Scene],
    reference_video: Path,
    thai_srt: Optional[str],
    thai_script: str,
    caption_command: str,
    workdir: Path,
) -> Path:
    output_srt = workdir / "thai_subtitles.srt"
    if thai_srt:
        source = resolve_asset(thai_srt, workdir, "thai_srt")
        shutil.copy2(source, output_srt)
        return output_srt
    if caption_command:
        response = call_json_provider(
            caption_command,
            {
                "task": "thai_subtitles",
                "reference_video": str(reference_video),
                "scenes": [scene.as_dict() for scene in scenes],
                "thai_script": thai_script,
                "output_srt": str(output_srt),
                "language": "th-TH",
            },
            workdir,
            "thai_captions",
        )
        provider_srt = Path(response.get("srt") or output_srt)
        if not provider_srt.exists():
            raise VideoFactoryError(f"Caption provider did not create SRT: {provider_srt}")
        if provider_srt.resolve() != output_srt.resolve():
            shutil.copy2(provider_srt, output_srt)
        return output_srt
    return create_srt_from_script(thai_script, scenes, output_srt)


def create_silent_audio(duration: float, output: Path) -> Path:
    run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-t",
            f"{duration:.3f}",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            str(output),
        ]
    )
    return output


def prepare_original_audio(reference: Path, duration: float, output: Path) -> Path:
    if not has_audio(reference):
        log("Reference has no audio stream; creating silent audio track.")
        return create_silent_audio(duration, output)
    run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(reference),
            "-t",
            f"{duration:.3f}",
            "-vn",
            "-ac",
            "2",
            "-ar",
            "48000",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            str(output),
        ]
    )
    return output


def prepare_thai_voice_audio(
    script: str,
    srt_path: Path,
    provided_audio: Optional[str],
    tts_command: str,
    duration: float,
    workdir: Path,
    output: Path,
) -> Path:
    source_audio: Optional[Path] = None
    if provided_audio:
        source_audio = resolve_asset(provided_audio, workdir, "thai_voice_audio")
    elif tts_command:
        voice_script = script or srt_to_plain_text(srt_path)
        if not voice_script:
            raise VideoFactoryError("Thai voice mode requires Thai script text or subtitles")
        response = call_json_provider(
            tts_command,
            {
                "task": "thai_tts",
                "language": "th-TH",
                "script": voice_script,
                "target_duration": duration,
                "output_audio": str(workdir / "thai_voice_provider_audio.wav"),
            },
            workdir,
            "thai_tts",
        )
        source_audio = Path(response.get("audio") or workdir / "thai_voice_provider_audio.wav")
    else:
        raise VideoFactoryError("audio-mode=thai_voice requires --thai-voice-audio or TTS_COMMAND")
    if not source_audio.exists():
        raise VideoFactoryError(f"Thai voice audio was not created: {source_audio}")
    run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(source_audio),
            "-t",
            f"{duration:.3f}",
            "-ac",
            "2",
            "-ar",
            "48000",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            str(output),
        ]
    )
    return output


def burn_subtitles_and_mux(video: Path, audio: Path, subtitles: Path, output: Path, workdir: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    if not ffmpeg_has_filter("subtitles"):
        log("FFmpeg subtitles filter is unavailable; using Pillow subtitle burn-in fallback.")
        return burn_subtitles_with_pillow(video, audio, subtitles, output, workdir)

    local_srt = workdir / "thai_subtitles.srt"
    if subtitles.resolve() != local_srt.resolve():
        shutil.copy2(subtitles, local_srt)
    style = "\\,".join(
        [
            "FontName=Noto Sans Thai",
            "FontSize=52",
            "PrimaryColour=&H00FFFFFF",
            "OutlineColour=&H80000000",
            "BackColour=&H40000000",
            "BorderStyle=1",
            "Outline=3",
            "Shadow=1",
            "Alignment=2",
            "MarginV=220",
        ]
    )
    vf = (
        f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={WIDTH}:{HEIGHT},fps={FPS},setsar=1,"
        f"subtitles=filename=thai_subtitles.srt:force_style='{style}',format=yuv420p"
    )
    run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(video),
            "-i",
            str(audio),
            "-vf",
            vf,
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-shortest",
            "-r",
            str(FPS),
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(output),
        ],
        cwd=workdir,
    )
    return output


def validate_output(path: Path) -> Dict[str, Any]:
    data = ffprobe_json(
        path,
        [
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,avg_frame_rate,r_frame_rate,codec_name:format=duration,size",
        ],
    )
    stream = data.get("streams", [{}])[0]
    report = {
        "path": str(path),
        "width": stream.get("width"),
        "height": stream.get("height"),
        "avg_frame_rate": stream.get("avg_frame_rate"),
        "r_frame_rate": stream.get("r_frame_rate"),
        "video_codec": stream.get("codec_name"),
        "duration": data.get("format", {}).get("duration"),
        "size": data.get("format", {}).get("size"),
    }
    if report["width"] != WIDTH or report["height"] != HEIGHT:
        raise VideoFactoryError(f"Output dimensions are not {WIDTH}x{HEIGHT}: {report}")
    if report["avg_frame_rate"] not in {"30/1", "30000/1000", "30000/1001"}:
        log(f"Warning: output avg_frame_rate is {report['avg_frame_rate']}")
    return report


def run_pipeline(args: argparse.Namespace) -> Path:
    if truthy(os.getenv("RIGHTS_CONFIRMED")):
        args.rights_confirmed = True
    if not args.rights_confirmed:
        raise VideoFactoryError(
            "Set --rights-confirmed or RIGHTS_CONFIRMED=true after confirming you have rights to use the reference video/audio."
        )

    ensure_tool("ffmpeg")
    ensure_tool("ffprobe")
    if is_url(args.reference):
        ensure_tool("yt-dlp")

    workdir = Path(args.workdir).expanduser().resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    output = Path(args.output).expanduser().resolve()

    reference = ingest_reference(args.reference, workdir)
    product_image = resolve_asset(args.product_image, workdir, "product_image")
    normalized_reference = normalize_reference(reference, workdir / "reference_normalized.mp4", args.duration_limit)
    scenes = detect_scenes(normalized_reference, workdir, args.scene_threshold, args.max_scenes)
    scenes = enrich_storyboard_with_vision(
        scenes,
        args.vision_command,
        normalized_reference,
        product_image,
        workdir,
    )
    write_storyboard(scenes, workdir / "storyboard")

    rendered_segments = render_ai_or_fallback_scenes(
        scenes,
        normalized_reference,
        product_image,
        args.ai_video_command,
        args.product_lock_mode,
        args.product_scale,
        args.product_position,
        workdir,
    )
    silent_video = concatenate_segments(rendered_segments, workdir / "video_no_audio.mp4", workdir)
    duration = get_duration(silent_video)

    thai_script = read_text_arg(args.thai_script, args.thai_script_file)
    subtitles = prepare_subtitles(
        scenes,
        normalized_reference,
        args.thai_srt,
        thai_script,
        args.caption_command,
        workdir,
    )

    audio_output = workdir / "final_audio.m4a"
    if args.audio_mode == "original":
        audio = prepare_original_audio(reference, duration, audio_output)
    elif args.audio_mode == "thai_voice":
        audio = prepare_thai_voice_audio(
            thai_script,
            subtitles,
            args.thai_voice_audio,
            args.tts_command,
            duration,
            workdir,
            audio_output,
        )
    else:
        raise VideoFactoryError(f"Unsupported audio mode: {args.audio_mode}")

    burn_subtitles_and_mux(silent_video, audio, subtitles, output, workdir)
    report = validate_output(output)
    report.update(
        {
            "audio_mode": args.audio_mode,
            "product_lock_mode": args.product_lock_mode,
            "product_position": args.product_position,
            "product_scale": args.product_scale,
        }
    )
    report_path = output.with_suffix(".report.json")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"Created {output}")
    log(f"Wrote report {report_path}")
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create a product-locked 1080x1920 30fps TikTok MP4 from a reference video.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """
            Provider command environment variables:
              AI_VIDEO_COMMAND    Render each scene from JSON request.
              VISION_COMMAND      Produce scene descriptions/prompts.
              CAPTION_COMMAND     Produce Thai SRT.
              TTS_COMMAND         Produce Thai voice audio.
            """
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    run_parser = sub.add_parser("run", help="Run the full pipeline")
    run_parser.add_argument("--reference", required=True, help="Douyin/TikTok/direct URL or local reference video path")
    run_parser.add_argument("--product-image", required=True, help="URL or local path to the real product image")
    run_parser.add_argument("--output", required=True, help="Output MP4 path")
    run_parser.add_argument("--workdir", default="work/video_factory_run", help="Temporary working directory")
    run_parser.add_argument("--audio-mode", choices=["original", "thai_voice"], default="original")
    run_parser.add_argument("--thai-script", default="")
    run_parser.add_argument("--thai-script-file", default="")
    run_parser.add_argument("--thai-srt", default="")
    run_parser.add_argument("--thai-voice-audio", default="")
    run_parser.add_argument("--duration-limit", type=float, default=None, help="Optional max seconds from the reference")
    run_parser.add_argument("--scene-threshold", type=float, default=0.32)
    run_parser.add_argument("--max-scenes", type=int, default=18)
    run_parser.add_argument(
        "--product-lock-mode",
        choices=["overlay", "provider", "none"],
        default=os.getenv("PRODUCT_LOCK_MODE", "overlay"),
        help="overlay keeps the supplied product image visible in every generated scene",
    )
    run_parser.add_argument(
        "--product-position",
        choices=["center", "bottom", "top"],
        default=os.getenv("PRODUCT_POSITION", "center"),
    )
    run_parser.add_argument("--product-scale", type=float, default=float(os.getenv("PRODUCT_SCALE", "0.60")))
    run_parser.add_argument("--ai-video-command", default=os.getenv("AI_VIDEO_COMMAND", ""))
    run_parser.add_argument("--vision-command", default=os.getenv("VISION_COMMAND", ""))
    run_parser.add_argument("--caption-command", default=os.getenv("CAPTION_COMMAND", ""))
    run_parser.add_argument("--tts-command", default=os.getenv("TTS_COMMAND", ""))
    run_parser.add_argument("--rights-confirmed", action="store_true")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            run_pipeline(args)
        return 0
    except subprocess.CalledProcessError as exc:
        if exc.stdout:
            print(exc.stdout, file=sys.stderr)
        if exc.stderr:
            print(exc.stderr, file=sys.stderr)
        print(f"Command failed with exit code {exc.returncode}: {exc.cmd}", file=sys.stderr)
        return exc.returncode or 1
    except VideoFactoryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
