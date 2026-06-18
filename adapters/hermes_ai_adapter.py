#!/usr/bin/env python3
"""
Hermes AI provider adapter.

Usage:
    python3 adapters/hermes_ai_adapter.py request.json response.json

The main video factory invokes provider commands with exactly two positional
arguments. This adapter keeps that contract stable while letting the Hermes
HTTP endpoint shape be configured through environment variables.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional


TASK_ENDPOINT_ENV = {
    "break_down_reference_video": ("HERMES_VISION_ENDPOINT", "/v1/video/breakdown"),
    "render_scene": ("HERMES_VIDEO_ENDPOINT", "/v1/video/generations"),
    "thai_subtitles": ("HERMES_CAPTION_ENDPOINT", "/v1/captions/thai"),
    "thai_tts": ("HERMES_TTS_ENDPOINT", "/v1/audio/speech"),
}

TASK_MODEL_ENV = {
    "break_down_reference_video": "HERMES_VISION_MODEL",
    "render_scene": "HERMES_VIDEO_MODEL",
    "thai_subtitles": "HERMES_CAPTION_MODEL",
    "thai_tts": "HERMES_TTS_MODEL",
}

PENDING_STATUSES = {"created", "queued", "pending", "processing", "running", "submitted"}
SUCCESS_STATUSES = {"complete", "completed", "done", "success", "succeeded"}
FAILED_STATUSES = {"cancelled", "canceled", "error", "failed", "failure", "rejected"}


class HermesAdapterError(RuntimeError):
    pass


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def join_url(base_url: str, endpoint: str) -> str:
    if endpoint.startswith("http://") or endpoint.startswith("https://"):
        return endpoint
    if not base_url:
        raise HermesAdapterError("Set HERMES_BASE_URL or provide a full endpoint URL.")
    return f"{base_url.rstrip('/')}/{endpoint.lstrip('/')}"


def endpoint_for_task(task: str) -> str:
    if os.getenv("HERMES_ENDPOINT"):
        return join_url(os.getenv("HERMES_BASE_URL", ""), os.environ["HERMES_ENDPOINT"])
    try:
        env_name, default_endpoint = TASK_ENDPOINT_ENV[task]
    except KeyError as exc:
        raise HermesAdapterError(f"Unsupported Hermes task: {task}") from exc
    endpoint = os.getenv(env_name, default_endpoint)
    return join_url(os.getenv("HERMES_BASE_URL", ""), endpoint)


def auth_headers() -> Dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    api_key = os.getenv("HERMES_API_KEY", "")
    if api_key:
        header_name = os.getenv("HERMES_AUTH_HEADER", "Authorization")
        scheme = os.getenv("HERMES_AUTH_SCHEME", "Bearer")
        headers[header_name] = f"{scheme} {api_key}".strip()
    extra_headers = os.getenv("HERMES_EXTRA_HEADERS_JSON", "")
    if extra_headers:
        headers.update(json.loads(extra_headers))
    return headers


def file_to_data_uri(path_value: str) -> Dict[str, str]:
    path = Path(path_value)
    if not path.exists():
        raise HermesAdapterError(f"Asset does not exist for base64 upload: {path}")
    suffix = path.suffix.lower()
    mime = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".mp4": "video/mp4",
        ".mov": "video/quicktime",
        ".wav": "audio/wav",
        ".m4a": "audio/mp4",
        ".mp3": "audio/mpeg",
    }.get(suffix, "application/octet-stream")
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return {"filename": path.name, "mime_type": mime, "data": encoded}


def attach_assets(payload: Dict[str, Any]) -> Dict[str, Any]:
    mode = os.getenv("HERMES_ASSET_MODE", "paths").strip().lower()
    if mode != "base64":
        return payload

    assets: Dict[str, Any] = {}
    for key in ("product_image", "reference_frame"):
        if payload.get(key):
            assets[f"{key}_asset"] = file_to_data_uri(str(payload[key]))
    if env_bool("HERMES_EMBED_REFERENCE_VIDEO", False) and payload.get("reference_video"):
        assets["reference_video_asset"] = file_to_data_uri(str(payload["reference_video"]))
    if assets:
        payload = dict(payload)
        payload["assets"] = assets
    return payload


def add_model(payload: Dict[str, Any]) -> Dict[str, Any]:
    task = str(payload.get("task", ""))
    model_env = TASK_MODEL_ENV.get(task)
    model = os.getenv(model_env or "", "")
    if model:
        payload = dict(payload)
        payload["model"] = model
    return payload


def http_json(method: str, url: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    timeout = float(os.getenv("HERMES_TIMEOUT_SECONDS", "120"))
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=auth_headers(), method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise HermesAdapterError(f"Hermes HTTP {exc.code}: {body}") from exc
    if not body.strip():
        return {}
    return json.loads(body)


def pick(data: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    nested = data.get("data")
    if isinstance(nested, dict):
        for key in keys:
            value = nested.get(key)
            if value not in (None, ""):
                return value
    return None


def poll_if_needed(initial: Dict[str, Any]) -> Dict[str, Any]:
    status = str(pick(initial, "status", "state") or "").lower()
    job_id = pick(initial, "job_id", "id", "task_id")
    if not job_id or status in SUCCESS_STATUSES:
        return initial
    if status and status not in PENDING_STATUSES:
        if status in FAILED_STATUSES:
            raise HermesAdapterError(f"Hermes job failed: {initial}")
        return initial

    base_url = os.getenv("HERMES_BASE_URL", "")
    endpoint_template = os.getenv("HERMES_RESULT_ENDPOINT", "/v1/jobs/{job_id}")
    url = join_url(base_url, endpoint_template.format(job_id=urllib.parse.quote(str(job_id))))
    poll_seconds = float(os.getenv("HERMES_POLL_SECONDS", "5"))
    max_wait = float(os.getenv("HERMES_MAX_WAIT_SECONDS", "1800"))
    deadline = time.time() + max_wait

    while time.time() < deadline:
        time.sleep(poll_seconds)
        result = http_json("GET", url)
        status = str(pick(result, "status", "state") or "").lower()
        if status in SUCCESS_STATUSES or not status:
            return result
        if status in FAILED_STATUSES:
            raise HermesAdapterError(f"Hermes job failed: {result}")
    raise HermesAdapterError(f"Timed out waiting for Hermes job {job_id}")


def download(url: str, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers=auth_headers())
    with urllib.request.urlopen(request, timeout=float(os.getenv("HERMES_TIMEOUT_SECONDS", "120"))) as response:
        with destination.open("wb") as output:
            shutil.copyfileobj(response, output)
    return destination


def materialize_asset(value: str, destination: Path) -> Path:
    if value.startswith("http://") or value.startswith("https://"):
        return download(value, destination)
    source = Path(value)
    if not source.exists():
        raise HermesAdapterError(f"Hermes returned a missing file path: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() != destination.resolve():
        shutil.copy2(source, destination)
    return destination


def decode_base64_asset(value: str, destination: Path) -> Path:
    if "," in value and value.strip().startswith("data:"):
        value = value.split(",", 1)[1]
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(base64.b64decode(value))
    return destination


def normalize_response(task: str, request: Dict[str, Any], response: Dict[str, Any]) -> Dict[str, Any]:
    normalized = dict(response)

    if task == "render_scene":
        target = Path(request["output_video"])
        video = pick(response, "video", "video_url", "output_video", "url")
        video_b64 = pick(response, "video_base64", "output_video_base64")
        if video_b64:
            decode_base64_asset(str(video_b64), target)
        elif video:
            materialize_asset(str(video), target)
        elif not target.exists():
            raise HermesAdapterError("Hermes render response did not include a video or create output_video.")
        normalized["video"] = str(target)

    elif task == "thai_subtitles":
        target = Path(request["output_srt"])
        srt = pick(response, "srt", "srt_url", "subtitle", "subtitle_url")
        srt_text = pick(response, "srt_text", "subtitle_text")
        if srt_text:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(srt_text), encoding="utf-8")
        elif srt:
            materialize_asset(str(srt), target)
        elif not target.exists():
            raise HermesAdapterError("Hermes caption response did not include SRT content or create output_srt.")
        normalized["srt"] = str(target)

    elif task == "thai_tts":
        target = Path(request["output_audio"])
        audio = pick(response, "audio", "audio_url", "output_audio", "url")
        audio_b64 = pick(response, "audio_base64", "output_audio_base64")
        if audio_b64:
            decode_base64_asset(str(audio_b64), target)
        elif audio:
            materialize_asset(str(audio), target)
        elif not target.exists():
            raise HermesAdapterError("Hermes TTS response did not include audio or create output_audio.")
        normalized["audio"] = str(target)

    elif task == "break_down_reference_video":
        if "scenes" not in normalized:
            nested = response.get("data")
            if isinstance(nested, dict) and "scenes" in nested:
                normalized["scenes"] = nested["scenes"]
        if "scenes" not in normalized:
            raise HermesAdapterError("Hermes vision response must include scenes.")

    return normalized


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("Usage: hermes_ai_adapter.py request.json response.json", file=sys.stderr)
        return 2

    request_path = Path(argv[1])
    response_path = Path(argv[2])
    request = load_json(request_path)
    task = str(request.get("task", ""))
    if not task:
        raise HermesAdapterError("Provider request is missing task.")

    payload = attach_assets(add_model(dict(request)))
    response = http_json("POST", endpoint_for_task(task), payload)
    response = poll_if_needed(response)
    normalized = normalize_response(task, request, response)
    write_json(response_path, normalized)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv))
    except HermesAdapterError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
