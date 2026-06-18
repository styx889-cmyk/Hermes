# Hermes AI Product Video Factory

这套交付物把你的需求拆成一条可执行流水线：

抖音/本地参考视频 -> 自动分镜 -> 内容拆解 -> 锁定真实产品图 -> AI 逐镜头生成或本地合成 -> 原音频或泰语配音 -> 泰语字幕 -> 1080x1920、30fps TikTok MP4。

## 目录

- `ci/pipeline.yaml`: 可选 CI 流水线示例。
- `bin/video_factory.py`: 视频处理主程序。
- `adapters/hermes_ai_adapter.py`: Hermes AI HTTP 适配器。
- `adapters/provider-contract.md`: Hermes/AI 视频、字幕、配音服务的接入契约。
- `config/local.example.env`: 本地和 Hermes AI 变量示例。

## 核心设计

产品一致性是硬约束。默认策略不是让视频模型重新“画”产品，而是把提供的真实产品图作为锁定素材进入每个镜头。AI 服务可以生成场景、动作、背景、节奏，但产品包装、logo、颜色和形状必须来自你的产品图。

如果没有配置 Hermes AI 服务，脚本会使用内置 fallback：按参考视频分镜节奏生成可测试的竖屏产品视频草稿。上线时，把 `AI_VIDEO_COMMAND`、`VISION_COMMAND`、`CAPTION_COMMAND`、`TTS_COMMAND` 指向 `adapters/hermes_ai_adapter.py`，并设置 Hermes 地址和密钥即可。

## Hermes AI 使用

1. 把整个 `hermes-ai-video-factory` 目录放进一个 Git 仓库。
2. 准备参考视频：抖音/TikTok/直链视频 URL，或本地视频路径。
3. 准备真实产品图片，优先使用透明 PNG。
4. 设置 Hermes AI 变量：

```bash
export HERMES_BASE_URL="https://your-hermes-host.example"
export HERMES_API_KEY="..."
export HERMES_ASSET_MODE=base64
export VISION_COMMAND="python3 adapters/hermes_ai_adapter.py"
export AI_VIDEO_COMMAND="python3 adapters/hermes_ai_adapter.py"
export CAPTION_COMMAND="python3 adapters/hermes_ai_adapter.py"
export TTS_COMMAND="python3 adapters/hermes_ai_adapter.py"
```

5. 运行主程序生成成品 MP4。

## CI 变量

| 变量 | 说明 |
| --- | --- |
| `reference_source` | 抖音/TikTok/直链视频 URL，或仓库里的本地视频路径 |
| `product_image_source` | 真实产品图 URL，或仓库里的图片路径 |
| `audio_mode` | `original` 或 `thai_voice` |
| `thai_script` | 泰语字幕/配音文案 |
| `thai_srt_source` | 可选，已有泰语 SRT 文件 |
| `thai_voice_audio_source` | 可选，已有泰语配音音频 |
| `rights_confirmed` | 必须设为 `true`，确认你有权使用参考视频/音频 |
| `product_lock_mode` | 默认 `overlay`，强制把真实产品图放入每个 AI 镜头 |
| `product_position` | `center`、`bottom` 或 `top` |
| `product_scale` | 产品图最大高度占画面比例，默认 `0.60` |
| `ai_video_command` | 可选，AI 逐镜头视频生成适配器 |
| `vision_command` | 可选，视频内容拆解适配器 |
| `caption_command` | 可选，泰语字幕生成适配器 |
| `tts_command` | 可选，泰语 TTS 适配器 |
| `hermes_base_url` | Hermes AI 服务地址 |
| `hermes_api_key` | Hermes AI 密钥 |
| `hermes_asset_mode` | `base64` 适合远程 Hermes，`paths` 适合同机服务 |
| `artifact_upload_command` | 可选，把成品 MP4 上传到 S3/GCS/OSS/CDN 的命令前缀 |

成品默认写到：

```text
dist/tiktok_product_video.mp4
```

同目录还会生成：

```text
dist/tiktok_product_video.report.json
dist/tiktok_product_video.ffprobe.json
dist/tiktok_product_video.sha256
```

## 本地运行

需要 Python 3、FFmpeg、ffprobe。URL 导入需要 `yt-dlp`。如果你的 FFmpeg 没有 `subtitles/libass` 滤镜，还需要 `pillow`，脚本会自动用 Pillow 逐帧烧录字幕。

```bash
python3 bin/video_factory.py run \
  --reference ./reference.mp4 \
  --product-image ./product.png \
  --thai-script-file ./thai.txt \
  --audio-mode original \
  --rights-confirmed \
  --product-lock-mode overlay \
  --workdir ./build/video_factory \
  --output ./dist/tiktok_product_video.mp4
```

使用 Hermes AI 逐镜头生成：

```bash
HERMES_BASE_URL="https://your-hermes-host.example" \
HERMES_API_KEY="..." \
HERMES_ASSET_MODE=base64 \
VISION_COMMAND="python3 adapters/hermes_ai_adapter.py" \
AI_VIDEO_COMMAND="python3 adapters/hermes_ai_adapter.py" \
CAPTION_COMMAND="python3 adapters/hermes_ai_adapter.py" \
python3 bin/video_factory.py run \
  --reference ./reference.mp4 \
  --product-image ./product.png \
  --thai-script-file ./thai.txt \
  --audio-mode original \
  --rights-confirmed \
  --product-lock-mode overlay \
  --workdir ./build/video_factory \
  --output ./dist/tiktok_product_video.mp4
```

泰语配音模式：

```bash
TTS_COMMAND="python3 adapters/hermes_ai_adapter.py" \
python3 bin/video_factory.py run \
  --reference ./reference.mp4 \
  --product-image ./product.png \
  --thai-script-file ./thai.txt \
  --audio-mode thai_voice \
  --rights-confirmed \
  --workdir ./build/video_factory \
  --output ./dist/tiktok_product_video.mp4
```

## 音频和版权边界

- `audio_mode=original` 会抽取参考视频原音频并合入新视频。只在你拥有或已获得授权时使用。
- `audio_mode=thai_voice` 会使用已有泰语音频，或调用 `TTS_COMMAND` 生成泰语配音。
- 参考视频用于结构、节奏和镜头拆解；不建议复制原视频人物、品牌素材、独创画面或未经授权音乐。

## 输出规格

最终导出会校验：

- 宽高：1080x1920
- 帧率：30fps
- 视频：H.264 / yuv420p
- 音频：AAC
- 字幕：泰语硬字幕，适配 TikTok 竖屏底部安全区

## 实施链路

```text
抖音/本地视频
  -> 导入并转 1080x1920 30fps
  -> 自动检测分镜
  -> Hermes 视觉拆解每个镜头
  -> Hermes 逐镜头生成相似场景
  -> 叠加真实产品图锁定包装/logo/颜色/形状
  -> 恢复原音频，或调用 Hermes 生成泰语配音
  -> 生成/导入泰语 SRT 并硬字幕烧录
  -> 输出 TikTok MP4 和校验报告
```
