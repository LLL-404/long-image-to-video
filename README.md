# Long Image to Video

[![CI](https://github.com/LLL-404/long-image-to-video/actions/workflows/ci.yml/badge.svg)](https://github.com/LLL-404/long-image-to-video/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

Turn one or more tall screenshots into a single smooth-scrolling MP4 — with automatic GPU-accelerated encoding and a lightweight desktop GUI.

<p align="center">
  <img src="docs/demo.gif" alt="Scrolling video demo" width="480">
</p>

Fed up with sharing a 10,000-pixel-tall screenshot as 20 separate images? Drop it in here and get one video that scrolls through it at a comfortable pace — handy for chat logs, articles, code reviews, or feed captures.

## Features

- **Single or multiple images** — several images are stitched vertically (with EXIF-aware orientation and alpha flattening) and scrolled as one
- **Smooth easing** — `linear`, `ease-in-out`, `ease-in-out-quad`, `ease-in-out-sine` scroll curves instead of constant-velocity panning
- **GPU-accelerated encoding** — auto-detects NVIDIA NVENC / Intel QSV / AMD AMF; falls back to CPU (OpenCV) when no GPU encoder is available or the resolution exceeds GPU limits
- **Full control over the output** — video width (360–3840), aspect ratio (16:9 / 9:16 / 3:4 / 1:1 / 4:3 / 21:9), frame rate, scroll duration, and x264 speed preset
- **Desktop GUI** — reorderable image list, live progress, and cancel support
- **Python API** — the converter core is importable for scripting and automation

## Requirements

- Python **3.10+**
- **FFmpeg** available on `PATH` (checked at runtime; used for pipe encoding)

## Installation

```bash
git clone https://github.com/LLL-404/long-image-to-video.git
cd long-image-to-video
pip install .
```

Or without installing, just for the GUI:

```bash
pip install -r requirements.txt
python main_gui.py
```

## Usage

### GUI

```bash
long-image-to-video        # if installed via pip
python main_gui.py         # from the repo root
```

1. Add one or more images and arrange the stitching order
2. Pick an output directory and tune the parameters (width, aspect ratio, fps, duration, preset, easing)
3. Start the conversion — progress is shown live and can be cancelled

### Python API

```python
from pathlib import Path
from converter import generate_multi_scroll_video

out = generate_multi_scroll_video(
    image_paths=[Path("screen-1.png"), Path("screen-2.png")],
    output_dir=Path("./outputs"),
    video_width=1080,
    fps=30,
    scroll_duration=10.0,
    aspect_ratio="16:9",
    preset="medium",
    easing="ease-in-out",
)
print(f"Saved to {out}")
```

## Parameters

| Parameter | Range | Default | Description |
|-----------|-------|---------|-------------|
| `video_width` | 360–3840 | 1080 | Output video width in px (odd heights are rounded down) |
| `aspect_ratio` | 16:9, 9:16, 3:4, 1:1, 4:3, 21:9 | 16:9 | Viewport aspect ratio |
| `fps` | 1–60 | 30 | Frame rate (24 / 30 / 60 recommended) |
| `scroll_duration` | 1–300 s | 10 | Wall-clock time to scroll through the whole image |
| `preset` | ultrafast … veryslow | medium | x264/NVENC speed preset — slower means better compression |
| `easing` | linear, ease-in-out, ease-in-out-quad, ease-in-out-sine | ease-in-out | Scroll curve |

## How encoding works

1. Images are loaded (EXIF-transposed, flattened onto white, resized to the target width) in a thread pool and stitched vertically.
2. Scroll offsets are generated from the chosen easing curve.
3. Frames are streamed to the encoder — no full-length numpy copy of the merged image is ever materialized, so very tall images stay memory-friendly.
4. Encoding prefers a detected GPU encoder (NVENC / QSV / AMF). If the resolution exceeds GPU limits (height > 4096 or > 36,864 macroblocks), it first tries downscaling (≤ 30% reduction), otherwise falls back to CPU encoding.

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest     # run the test suite
ruff check .         # lint
```

## Contributing

Issues and pull requests are welcome. For larger changes, please open an issue first to discuss what you would like to change.

## License

[MIT](LICENSE) © 2026 LLL-404

## 中文文档

参见 [README.zh-CN.md](README.zh-CN.md)。
