# Hermes AI Provider Contract

所有外部服务都按同一种方式接入：

```bash
provider_command request.json response.json
```

服务读取 `request.json`，处理完成后写入 `response.json`。命令退出码非 0 会让流水线失败。

本交付物已经提供 Hermes HTTP 适配器：

```bash
python3 adapters/hermes_ai_adapter.py request.json response.json
```

常用环境变量：

| 变量 | 说明 |
| --- | --- |
| `HERMES_BASE_URL` | Hermes AI 服务根地址 |
| `HERMES_API_KEY` | Hermes AI 密钥 |
| `HERMES_ASSET_MODE` | `paths` 或 `base64`；远程服务通常用 `base64` |
| `HERMES_VIDEO_ENDPOINT` | 视频生成端点，默认 `/v1/video/generations` |
| `HERMES_VISION_ENDPOINT` | 视频拆解端点，默认 `/v1/video/breakdown` |
| `HERMES_CAPTION_ENDPOINT` | 泰语字幕端点，默认 `/v1/captions/thai` |
| `HERMES_TTS_ENDPOINT` | 泰语配音端点，默认 `/v1/audio/speech` |
| `HERMES_RESULT_ENDPOINT` | 异步任务轮询端点，默认 `/v1/jobs/{job_id}` |

如果你的 Hermes 返回 `job_id` / `id` / `task_id`，适配器会自动轮询结果；如果直接返回 `video_url`、`srt_text` 或 `audio_url`，适配器会直接下载或写入目标文件。

## AI_VIDEO_COMMAND

用途：为单个分镜生成 AI 视频。

请求示例：

```json
{
  "task": "render_scene",
  "reference_video": "/path/reference_normalized.mp4",
  "reference_frame": "/path/scene_001.jpg",
  "product_image": "/path/product.png",
  "scene": {
    "index": 1,
    "start": 0.0,
    "end": 2.8,
    "duration": 2.8,
    "description": "Reference scene 1...",
    "prompt": "Create a vertical product ad scene..."
  },
  "output_video": "/path/scene_001_raw.mp4",
  "requirements": {
    "width": 1080,
    "height": 1920,
    "fps": 30,
    "product_identity_lock": true,
    "product_lock_mode": "overlay",
    "keep_product_logo_packaging_color_and_shape": true,
    "do_not_copy_reference_people_or_brand_assets": true
  }
}
```

响应示例：

```json
{
  "video": "/path/scene_001_raw.mp4",
  "notes": "Generated with product image locked as foreground asset."
}
```

## VISION_COMMAND

用途：把自动分镜进一步拆成内容说明和提示词。

响应示例：

```json
{
  "scenes": [
    {
      "index": 1,
      "description": "Close-up product reveal with hand motion.",
      "prompt": "Vertical beauty product reveal, warm store shelf background..."
    }
  ]
}
```

## CAPTION_COMMAND

用途：生成泰语 SRT。

响应示例：

```json
{
  "srt": "/path/thai_subtitles.srt"
}
```

## TTS_COMMAND

用途：生成泰语配音音频。

请求会包含：

```json
{
  "task": "thai_tts",
  "language": "th-TH",
  "script": "Thai script text...",
  "target_duration": 12.5,
  "output_audio": "/path/thai_voice_provider_audio.wav"
}
```

响应示例：

```json
{
  "audio": "/path/thai_voice_provider_audio.wav"
}
```

## 建议的适配器策略

- 视频模型优先使用 image-to-video 或 reference-to-video 模式。
- 产品图最好使用透明 PNG；如果只有 JPG，建议先在适配器里抠图再合成。
- 适配器应在输出前做产品一致性检查，比如 logo OCR、颜色采样、CLIP/embedding 相似度、关键区域差异检测。
- `PRODUCT_LOCK_MODE=overlay` 会在 AI 生成视频后再次叠加真实产品图，是默认的强一致策略。
- 如果 Hermes 端已经能严格保持产品包装、logo、颜色和形状，可以改成 `PRODUCT_LOCK_MODE=provider`。
- AI 生成失败时应返回非 0 退出码，让流水线标记失败。
