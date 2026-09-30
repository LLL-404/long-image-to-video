"""
长图片转视频 - 核心转换逻辑（优化版）

优化:
1. 缓动曲线（ease-in/ease-out）代替线性滚动
2. 多图并发加载 (ThreadPoolExecutor)
3. 分块编码减少峰值内存
4. WebP 输入支持
"""

import logging
import os
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)


_gpu_encoder_cache: str | None = None


def _detect_gpu_encoder() -> str | None:
    global _gpu_encoder_cache
    if _gpu_encoder_cache is not None:
        return None if _gpu_encoder_cache == '__none__' else _gpu_encoder_cache
    if not shutil.which('ffmpeg'):
        _gpu_encoder_cache = '__none__'
        return None
    try:
        result = subprocess.run(
            ['ffmpeg', '-hide_banner', '-encoders'],
            capture_output=True, text=True, timeout=10, check=False
        )
        encoders = result.stdout
    except Exception as e:
        logger.warning(f"[GPU检测] 无法获取编码器列表: {e}")
        _gpu_encoder_cache = '__none__'
        return None
    for codec, name in [('h264_nvenc', 'NVIDIA NVENC'), ('h264_qsv', 'Intel QSV'), ('h264_amf', 'AMD AMF')]:
        if codec in encoders:
            logger.info(f"[GPU检测] 找到 {name} ({codec})")
            _gpu_encoder_cache = codec
            return codec
    _gpu_encoder_cache = '__none__'
    return None


_GPU_MAX_HEIGHT = 4096
_GPU_MAX_MACROBLOCKS = 36864


def _check_gpu_encode_limits(video_width: int, viewport_h: int) -> bool:
    if viewport_h > _GPU_MAX_HEIGHT:
        return False
    mb_w = (video_width + 15) // 16
    mb_h = (viewport_h + 15) // 16
    return mb_w * mb_h <= _GPU_MAX_MACROBLOCKS


def _fit_gpu_resolution(video_width: int, viewport_h: int) -> tuple[int, int] | None:
    ratio = _GPU_MAX_HEIGHT / viewport_h
    new_w = int(video_width * ratio) // 2 * 2
    new_h = _GPU_MAX_HEIGHT // 2 * 2
    while new_w >= 360 and new_h >= 360:
        mb_w = (new_w + 15) // 16
        mb_h = (new_h + 15) // 16
        if mb_w * mb_h <= _GPU_MAX_MACROBLOCKS:
            return (new_w, new_h)
        new_h -= 2
    return None


_EASING_FUNCS = {
    'linear': lambda t: t,
    'ease-in-out': lambda t: 4 * t * t * t if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2,
    'ease-in-out-quad': lambda t: 2 * t * t if t < 0.5 else 1 - (-2 * t + 2) ** 2 / 2,
    'ease-in-out-sine': lambda t: -(np.cos(np.pi * t) - 1) / 2,
}


def _generate_scroll_offsets(total_frames: int, scroll_distance: int, easing: str = 'ease-in-out') -> list[int]:
    if total_frames <= 1 or scroll_distance <= 0:
        return [0] * total_frames
    ease_func = _EASING_FUNCS.get(easing, _EASING_FUNCS['ease-in-out'])
    offsets = []
    for i in range(total_frames):
        t = i / (total_frames - 1)
        y_offset = min(int(round(ease_func(t) * scroll_distance)), scroll_distance)
        offsets.append(y_offset)
    offsets[-1] = scroll_distance
    return offsets


def _create_video_writer(output_video_path, fps, video_width, viewport_h):
    for codec in ['mp4v', 'avc1', 'h264']:
        fourcc = cv2.VideoWriter_fourcc(*codec)
        writer = cv2.VideoWriter(str(output_video_path), fourcc, fps, (video_width, viewport_h))
        if writer.isOpened():
            return writer
    raise RuntimeError("无法初始化视频编码器")


def _viewport_to_bgr(merged_img: Image.Image, y_offset: int, video_width: int,
                     viewport_h: int, merged_height: int) -> np.ndarray:
    """从长图中裁剪一帧视口区域，返回 BGR numpy 数组。

    使用 PIL.crop + np.asarray 流式取帧：
    - crop 返回的是新 PIL 图像（仅 viewport 大小），不复制整张长图
    - np.asarray 与 PIL 内部缓冲区共享内存，避免再次复制
    """
    # 边界对齐：当 y_offset + viewport_h 超过图片高度时，需要补黑色边
    end_y = min(y_offset + viewport_h, merged_height)
    crop_box = (0, y_offset, video_width, end_y)
    frame_rgb = merged_img.crop(crop_box)
    frame_np = np.asarray(frame_rgb)  # 共享 PIL 内部缓冲区，零拷贝
    # 若裁剪区域不足 viewport_h，需要在底部补黑色
    actual_h = frame_np.shape[0]
    if actual_h < viewport_h:
        canvas = np.zeros((viewport_h, video_width, 3), dtype=np.uint8)
        canvas[:actual_h, :, :] = frame_np
        frame_np = canvas
    # PIL 默认 RGB，OpenCV 需要 BGR
    return cv2.cvtColor(frame_np, cv2.COLOR_RGB2BGR)


def _build_static_frame_bgr(merged_img: Image.Image, video_width: int,
                            viewport_h: int, merged_height: int) -> np.ndarray:
    """构造静态（scroll_distance=0）居中显示的单帧 BGR numpy 数组。

    只生成一个 viewport 大小的画布，避免整张长图转 numpy。
    """
    canvas = Image.new('RGB', (video_width, viewport_h), (0, 0, 0))
    y_padding = max(0, (viewport_h - merged_height) // 2)
    # 只粘贴长图与视口重叠的部分，避免 PIL 在画布外浪费内存
    paste_top = y_padding
    paste_bottom = min(y_padding + merged_height, viewport_h)
    src_top = max(0, -y_padding)
    src_bottom = src_top + (paste_bottom - paste_top)
    if paste_bottom > paste_top:
        canvas.paste(
            merged_img.crop((0, src_top, video_width, src_bottom)),
            (0, paste_top),
        )
    return cv2.cvtColor(np.asarray(canvas), cv2.COLOR_RGB2BGR)


def _encode_with_ffmpeg_pipe(
    merged_img: Image.Image, output_video_path: Path,
    video_width: int, viewport_h: int, fps: int,
    total_frames: int, scroll_distance: int, merged_height: int,
    gpu_encoder: str, preset: str = "fast", easing: str = 'ease-in-out',
    stop_event: threading.Event | None = None, progress_callback: callable = None,
) -> tuple[Path, float, int]:
    encode_start = time.time()
    preset_time_mult = {'ultrafast': 0.5, 'superfast': 0.6, 'veryfast': 0.8, 'faster': 1.0,
                        'fast': 1.2, 'medium': 1.5, 'slow': 2.0, 'slower': 3.0, 'veryslow': 5.0}
    est_time = max(30, total_frames * 0.1 * preset_time_mult.get(preset, 1.5))
    timeout_seconds = int(est_time * 2) + 60

    cmd = [
        'ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
        '-f', 'rawvideo', '-vcodec', 'rawvideo',
        '-s', f'{video_width}x{viewport_h}',
        '-pix_fmt', 'bgr24', '-r', str(fps), '-i', 'pipe:0',
        '-c:v', gpu_encoder, '-preset', preset,
        '-level', 'auto', '-pix_fmt', 'yuv420p',
        '-movflags', '+faststart', '-threads', '1',
        str(output_video_path),
    ]

    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    progress_interval = max(1, total_frames // 20)
    scroll_offsets = _generate_scroll_offsets(total_frames, scroll_distance, easing)
    actual_frames = 0
    stderr_lines = []

    def read_stderr():
        try:
            if proc.stderr:
                for line in iter(proc.stderr.readline, b''):
                    stderr_lines.append(line.decode('utf-8', errors='replace').strip())
        except Exception:
            pass

    stderr_thread = threading.Thread(target=read_stderr, daemon=True)
    stderr_thread.start()

    try:
        if scroll_distance == 0:
            # 静态居中显示：只需构造一帧，重复写入
            frame_bgr = _build_static_frame_bgr(merged_img, video_width, viewport_h, merged_height)
            frame_data = frame_bgr.tobytes()
            for i in range(total_frames):
                if stop_event and stop_event.is_set():
                    break
                proc.stdin.write(frame_data)
                actual_frames += 1
                if progress_callback and ((i + 1) % progress_interval == 0 or i == total_frames - 1):
                    progress_callback(i + 1, total_frames)
        else:
            # 流式滚动：每帧只裁剪 viewport 大小，避免整张长图转 numpy
            for i, y_offset in enumerate(scroll_offsets):
                if stop_event and stop_event.is_set():
                    break
                frame_bgr = _viewport_to_bgr(merged_img, y_offset, video_width, viewport_h, merged_height)
                proc.stdin.write(frame_bgr.tobytes())
                actual_frames += 1
                if progress_callback and ((i + 1) % progress_interval == 0 or i == total_frames - 1):
                    progress_callback(i + 1, total_frames)
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        if stop_event and stop_event.is_set():
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        try:
            proc.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=10)
            except Exception:
                pass
        try:
            stderr_thread.join(timeout=5)
        except Exception:
            pass
        rc = proc.returncode
        if rc is not None and rc != 0 and not (stop_event and stop_event.is_set()):
            if stderr_lines:
                logger.warning(f"[GPU] FFmpeg 错误: {' | '.join(stderr_lines[-5:])}")
            try:
                output_video_path.unlink(missing_ok=True)
            except Exception:
                pass
            raise RuntimeError(f"FFmpeg 编码失败，退出码: {rc}")

    encode_time = (time.time() - encode_start) * 1000
    return output_video_path, encode_time, actual_frames


def _encode_scroll_video(
    merged_img: Image.Image, merged_height: int,
    output_video_path: Path, video_width: int, viewport_h: int,
    fps: int, total_frames: int, scroll_distance: int,
    preset: str = "fast", easing: str = 'ease-in-out',
    stop_event: threading.Event | None = None, progress_callback: callable = None,
) -> tuple[Path, float, int]:
    encode_start = time.time()

    gpu_encoder = _detect_gpu_encoder()
    if gpu_encoder:
        gpu_w, gpu_vh, gpu_img, gpu_sd = video_width, viewport_h, merged_img, scroll_distance
        gpu_merged_h = merged_height
        if not _check_gpu_encode_limits(gpu_w, gpu_vh):
            fitted = _fit_gpu_resolution(gpu_w, gpu_vh)
            if fitted is not None:
                new_w, new_vh = fitted
                reduction = (gpu_w - new_w) / gpu_w * 100
                if reduction <= 30:
                    # 直接基于 PIL 缩放，避免把整张长图转 numpy 再 resize
                    new_img_h = int(new_w / gpu_w * merged_height)
                    gpu_img = merged_img.resize((new_w, new_img_h), Image.LANCZOS)
                    gpu_w, gpu_vh = new_w, new_vh
                    gpu_merged_h = new_img_h
                    gpu_sd = max(0, new_img_h - gpu_vh)
                else:
                    gpu_encoder = None
            else:
                gpu_encoder = None

        if gpu_encoder:
            try:
                return _encode_with_ffmpeg_pipe(
                    gpu_img, output_video_path, gpu_w, gpu_vh, fps,
                    total_frames, gpu_sd, gpu_merged_h, gpu_encoder, preset, easing,
                    stop_event, progress_callback,
                )
            except RuntimeError as e:
                logger.warning(f"[GPU回退] {e}，切换至 CPU")

    logger.info("[CPU编码] 使用 OpenCV")
    video_writer = _create_video_writer(output_video_path, fps, video_width, viewport_h)
    progress_interval = max(1, total_frames // 20)
    scroll_offsets = _generate_scroll_offsets(total_frames, scroll_distance, easing)
    actual_frames = 0

    try:
        if scroll_distance == 0:
            # 静态居中显示：只需构造一帧，重复写入
            frame_bgr = _build_static_frame_bgr(merged_img, video_width, viewport_h, merged_height)
            for i in range(total_frames):
                if stop_event and stop_event.is_set():
                    break
                video_writer.write(frame_bgr)
                actual_frames += 1
                if progress_callback and ((i + 1) % progress_interval == 0 or i == total_frames - 1):
                    progress_callback(i + 1, total_frames)
        else:
            # 流式滚动：每帧只裁剪 viewport 大小，避免整张长图转 numpy
            for i, y_offset in enumerate(scroll_offsets):
                if stop_event and stop_event.is_set():
                    break
                frame_bgr = _viewport_to_bgr(merged_img, y_offset, video_width, viewport_h, merged_height)
                video_writer.write(frame_bgr)
                actual_frames += 1
                if progress_callback and ((i + 1) % progress_interval == 0 or i == total_frames - 1):
                    progress_callback(i + 1, total_frames)
    finally:
        video_writer.release()

    encode_time = (time.time() - encode_start) * 1000
    return output_video_path, encode_time, actual_frames


def _load_and_resize_image(image_path: Path, video_width: int) -> tuple:
    load_start = time.time()
    img = Image.open(image_path)
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass
    if img.mode in ('RGBA', 'P'):
        if img.mode == 'P':
            img = img.convert('RGBA')
        bg = Image.new('RGBA', img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(bg, img).convert('RGB')
    elif img.mode != 'RGB':
        img = img.convert('RGB')
    ow, oh = img.size
    rh = int(video_width / ow * oh)
    img_resized = img.resize((video_width, rh), Image.LANCZOS)
    elapsed = (time.time() - load_start) * 1000
    return img_resized, ow, oh, elapsed


def generate_scroll_video(
    image_path, output_dir, video_width=1080, fps=30,
    scroll_duration=10.0, aspect_ratio="16:9",
    preset="medium", easing="ease-in-out",
    stop_event=None, progress_callback=None,
):
    return generate_multi_scroll_video(
        image_paths=[image_path], output_dir=output_dir,
        video_width=video_width, fps=fps,
        scroll_duration=scroll_duration, aspect_ratio=aspect_ratio,
        preset=preset, easing=easing,
        stop_event=stop_event, progress_callback=progress_callback,
    )


def generate_multi_scroll_video(
    image_paths, output_dir, video_width=1080, fps=30,
    scroll_duration=10.0, aspect_ratio="16:9",
    preset="medium", easing="ease-in-out",
    stop_event=None, progress_callback=None,
):
    if not image_paths:
        raise ValueError("至少需要一张图片")

    valid_presets = ['ultrafast', 'superfast', 'veryfast', 'faster', 'fast', 'medium', 'slow', 'slower', 'veryslow']
    if preset not in valid_presets:
        raise ValueError(f"preset 必须为 {valid_presets} 之一")
    if easing not in _EASING_FUNCS:
        raise ValueError(f"easing 必须为 {list(_EASING_FUNCS.keys())} 之一")

    logger.info(f"[设置] preset={preset}, easing={easing}")

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    total_start = time.time()

    merge_height = 0
    image_resize_heights = []
    for img_path in image_paths:
        img_path = Path(img_path)
        if not img_path.exists():
            raise FileNotFoundError(f"图片文件不存在: {img_path}")
        with Image.open(img_path) as preview:
            ow, oh = preview.size
            rh = int(video_width / ow * oh)
            image_resize_heights.append(rh)
            merge_height += rh

    logger.info(f"[多图] {len(image_paths)} 张, 拼后总高 {merge_height}")
    stitch_start = time.time()
    merged_img = Image.new('RGB', (video_width, merge_height), (0, 0, 0))
    total_prep_time = 0

    max_workers = min(len(image_paths), os.cpu_count() or 4)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_load_and_resize_image, Path(p), video_width): i for i, p in enumerate(image_paths)}
        results = [(futures[f], f.result()) for f in as_completed(futures)]
    results.sort(key=lambda x: x[0])

    y_cursor = 0
    for idx, (img_resized, _, _, prep_time) in results:
        if stop_event and stop_event.is_set():
            raise InterruptedError("用户取消")
        merged_img.paste(img_resized, (0, y_cursor))
        y_cursor += img_resized.height
        total_prep_time += prep_time
        del img_resized

    stitch_time = (time.time() - stitch_start) * 1000
    logger.info(f"[拼接] {video_width}x{merge_height}, {stitch_time:.0f}ms")

    ratio_parts = aspect_ratio.split(':')
    ratio_w, ratio_h = int(ratio_parts[0]), int(ratio_parts[1])
    viewport_h = int(video_width * ratio_h / ratio_w)
    if viewport_h % 2 != 0:
        viewport_h -= 1

    scroll_distance = max(0, merge_height - viewport_h)
    total_frames = int(scroll_duration * fps)
    logger.info(f"[视频] {video_width}x{viewport_h}, {fps}fps, {scroll_duration}s")

    first_stem = Path(image_paths[0]).stem
    output_video_path = output_dir / f"{first_stem}.mp4"
    counter = 1
    while output_video_path.exists():
        output_video_path = output_dir / f"{first_stem}_{counter}.mp4"
        counter += 1

    # 不再把整张长图转为 numpy（1080x100000 长图转 np 会占用 324MB+ 并复制一份）
    # 直接把 PIL Image 传给编码器，由编码器按 viewport 流式切片
    _, encode_time, actual_encoded = _encode_scroll_video(
        merged_img, merge_height,
        output_video_path, video_width, viewport_h, fps,
        total_frames, scroll_distance, preset, easing,
        stop_event, progress_callback,
    )
    del merged_img

    total_time = (time.time() - total_start) * 1000
    logger.info(f"[完成] {output_video_path.name} | 准备 {total_prep_time:.0f}ms + "
                f"拼接 {stitch_time:.0f}ms + 编码 {encode_time:.0f}ms = {total_time:.0f}ms")

    return output_video_path
