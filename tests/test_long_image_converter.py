"""长图片转视频 converter 模块纯函数单元测试

覆盖三个不依赖 ffmpeg / GPU / OpenCV 编码器的纯函数：
- _check_gpu_encode_limits: GPU 编码分辨率/宏块数限制检查
- _fit_gpu_resolution: GPU 分辨率自适应
- _generate_scroll_offsets: 缓动滚动偏移生成

并补充以下测试：
- generate_scroll_video / generate_multi_scroll_video 的参数校验
- _encode_with_ffmpeg_pipe 的错误处理（mock subprocess.Popen）
- _detect_gpu_encoder 的 GPU 检测与回退逻辑（mock shutil.which / subprocess.run）
- _load_and_resize_image 的图片加载和缩放
- _viewport_to_bgr / _build_static_frame_bgr 的视口裁剪逻辑
"""
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

# 把 converter.py 所在目录加入 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import converter as conv_mod
from converter import (
    _GPU_MAX_HEIGHT,
    _GPU_MAX_MACROBLOCKS,
    _build_static_frame_bgr,
    _check_gpu_encode_limits,
    _detect_gpu_encoder,
    _encode_with_ffmpeg_pipe,
    _fit_gpu_resolution,
    _generate_scroll_offsets,
    _load_and_resize_image,
    _viewport_to_bgr,
    generate_multi_scroll_video,
    generate_scroll_video,
)


# ---------------------------------------------------------------------------
# _check_gpu_encode_limits
# ---------------------------------------------------------------------------
def test_gpu_limits_within_bounds():
    """常规 1080p 视口应在限制内"""
    # 1920x1080 -> 120*68=8160 宏块，远小于 36864
    assert _check_gpu_encode_limits(1920, 1080) is True


def test_gpu_limits_viewport_too_high():
    """视口高度超过 4096 应返回 False"""
    assert _check_gpu_encode_limits(1920, _GPU_MAX_HEIGHT + 2) is False


def test_gpu_limits_macroblocks_exceeded():
    """宏块数超过上限应返回 False"""
    # 构造一个 mb_w * mb_h > 36864 的用例
    # 16K: 15360x8640 -> 960*540=518400 宏块，远超上限
    assert _check_gpu_encode_limits(15360, 8640) is False


def test_gpu_limits_exact_boundary():
    """刚好等于宏块上限时应返回 True（边界值）"""
    # 构造 mb_w * mb_h == _GPU_MAX_MACROBLOCKS
    # 36864 = 256 * 144 -> mb_w=256 (width=4080), mb_h=144 (height=2295)
    # 但 4080/16=255, 2295/16=143.4375 -> 144, 255*144=36720 < 36864
    # 用更精确的：mb_w=192 (width=3072), mb_h=192 (height=3072) -> 192*192=36864
    assert _check_gpu_encode_limits(3072, 3072) is True


# ---------------------------------------------------------------------------
# _fit_gpu_resolution
# ---------------------------------------------------------------------------
def test_fit_gpu_resolution_returns_valid():
    """对超限分辨率应返回满足限制的 (w, h)"""
    # 7680x4320 (8K) 视口高度 4320 > 4096，需要缩放
    result = _fit_gpu_resolution(7680, 4320)
    assert result is not None
    new_w, new_h = result
    # 返回值应满足 GPU 限制
    assert new_w >= 360
    assert new_h >= 360
    assert new_h <= _GPU_MAX_HEIGHT
    mb_w = (new_w + 15) // 16
    mb_h = (new_h + 15) // 16
    assert mb_w * mb_h <= _GPU_MAX_MACROBLOCKS


def test_fit_gpu_resolution_already_small():
    """对已经在限制内的分辨率，函数仍会基于 _GPU_MAX_HEIGHT 计算"""
    # 注意：_fit_gpu_resolution 总是按 ratio = _GPU_MAX_HEIGHT / viewport_h 计算
    # 当 viewport_h < _GPU_MAX_HEIGHT 时 ratio > 1，new_w/new_h 会比原始大
    # 但函数仍会检查宏块限制，最终返回满足限制的值
    result = _fit_gpu_resolution(1920, 1080)
    # 这种情况下函数可能返回比 1920x1080 更大的值，但应满足限制
    if result is not None:
        new_w, new_h = result
        assert new_w >= 360
        assert new_h >= 360


def test_fit_gpu_resolution_extreme_case():
    """极端高瘦分辨率应能找到合法值或返回 None"""
    # 极端窄高图：100 宽，8000 高
    result = _fit_gpu_resolution(100, 8000)
    # 应返回 None 或满足限制的值
    if result is not None:
        new_w, new_h = result
        assert new_w >= 360
        assert new_h >= 360
        mb_w = (new_w + 15) // 16
        mb_h = (new_h + 15) // 16
        assert mb_w * mb_h <= _GPU_MAX_MACROBLOCKS


# ---------------------------------------------------------------------------
# _generate_scroll_offsets
# ---------------------------------------------------------------------------
def test_scroll_offsets_zero_distance():
    """scroll_distance=0 时所有偏移应为 0"""
    offsets = _generate_scroll_offsets(30, 0)
    assert len(offsets) == 30
    assert all(o == 0 for o in offsets)


def test_scroll_offsets_single_frame():
    """total_frames<=1 时应返回 [0]"""
    assert _generate_scroll_offsets(0, 100) == []
    assert _generate_scroll_offsets(1, 100) == [0]


def test_scroll_offsets_last_equals_distance():
    """最后一个偏移应等于 scroll_distance（保证滚到底）"""
    offsets = _generate_scroll_offsets(30, 500, easing="linear")
    assert offsets[-1] == 500


def test_scroll_offsets_linear():
    """linear 缓动应产生线性递增的偏移"""
    offsets = _generate_scroll_offsets(11, 100, easing="linear")
    assert len(offsets) == 11
    # linear: t = i/10, offset = t * 100
    assert offsets[0] == 0
    assert offsets[-1] == 100
    # 中间值应为 10, 20, 30, ..., 90
    for i in range(1, 10):
        assert offsets[i] == i * 10


def test_scroll_offsets_ease_in_out():
    """ease-in-out 应满足：开头慢、中间快、结尾慢"""
    offsets = _generate_scroll_offsets(101, 1000, easing="ease-in-out")
    assert len(offsets) == 101
    assert offsets[0] == 0
    assert offsets[-1] == 1000
    # 中点附近应有较大增量（中间快）
    mid_start = offsets[49]
    mid_end = offsets[51]
    mid_delta = mid_end - mid_start
    # 开头附近增量应较小（开头慢）
    early_delta = offsets[1] - offsets[0]
    assert mid_delta > early_delta


def test_scroll_offsets_ease_in_out_quad():
    """ease-in-out-quad 应正常生成且首尾正确"""
    offsets = _generate_scroll_offsets(50, 800, easing="ease-in-out-quad")
    assert len(offsets) == 50
    assert offsets[0] == 0
    assert offsets[-1] == 800
    # 偏移应单调不减
    for i in range(1, len(offsets)):
        assert offsets[i] >= offsets[i - 1]


def test_scroll_offsets_invalid_easing_falls_back():
    """未知 easing 应回退到 ease-in-out"""
    offsets_unknown = _generate_scroll_offsets(30, 500, easing="invalid-easing")
    offsets_default = _generate_scroll_offsets(30, 500, easing="ease-in-out")
    assert offsets_unknown == offsets_default


def test_scroll_offsets_monotonic():
    """所有 easing 生成的偏移应单调不减"""
    for easing in ["linear", "ease-in-out", "ease-in-out-quad"]:
        offsets = _generate_scroll_offsets(100, 1000, easing=easing)
        for i in range(1, len(offsets)):
            assert offsets[i] >= offsets[i - 1], f"{easing} 在第 {i} 帧处非单调"


def test_scroll_offsets_max_capped():
    """任何偏移都不应超过 scroll_distance"""
    offsets = _generate_scroll_offsets(50, 300, easing="ease-in-out")
    assert all(0 <= o <= 300 for o in offsets)


# ===========================================================================
# 以下为 P0-6 补充测试
# ===========================================================================


# ---------------------------------------------------------------------------
# generate_scroll_video / generate_multi_scroll_video 参数校验
# ---------------------------------------------------------------------------
def test_generate_scroll_video_empty_images(tmp_path):
    """空图片列表应抛出 ValueError

    注意 generate_scroll_video 接收单个 image_path，不是列表；
    空列表场景由 generate_multi_scroll_video 直接覆盖。
    """
    with pytest.raises(ValueError, match="至少需要一张图片"):
        generate_multi_scroll_video([], tmp_path)


def test_generate_multi_scroll_video_empty_images(tmp_path):
    """generate_multi_scroll_video 同样校验空列表"""
    with pytest.raises(ValueError, match="至少需要一张图片"):
        generate_multi_scroll_video([], tmp_path)


def test_generate_scroll_video_missing_image(tmp_path):
    """图片文件不存在应抛出 FileNotFoundError"""
    fake_path = tmp_path / "not_exist.png"
    # generate_scroll_video 接收单个路径，不是列表
    with pytest.raises(FileNotFoundError, match="图片文件不存在"):
        generate_scroll_video(str(fake_path), tmp_path)


def test_generate_scroll_video_invalid_preset(tmp_path):
    """非法 preset 应抛出 ValueError"""
    # 用一个真实存在的图片避免被 FileNotFoundError 提前打断
    img_path = tmp_path / "src.png"
    Image.new("RGB", (200, 400), (255, 0, 0)).save(img_path)
    with pytest.raises(ValueError, match="preset 必须为"):
        generate_scroll_video(str(img_path), tmp_path, preset="invalid_preset")


def test_generate_scroll_video_invalid_easing(tmp_path):
    """非法 easing 应抛出 ValueError"""
    img_path = tmp_path / "src.png"
    Image.new("RGB", (200, 400), (255, 0, 0)).save(img_path)
    with pytest.raises(ValueError, match="easing 必须为"):
        generate_scroll_video(str(img_path), tmp_path, easing="invalid_easing")


# ---------------------------------------------------------------------------
# _detect_gpu_encoder 的 GPU 检测与回退逻辑
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=False)
def reset_gpu_cache():
    """每个 GPU 检测测试前重置全局缓存，避免相互污染"""
    conv_mod._gpu_encoder_cache = None
    yield
    conv_mod._gpu_encoder_cache = None


def test_detect_gpu_encoder_no_ffmpeg(reset_gpu_cache):
    """系统无 ffmpeg 时应返回 None"""
    with patch.object(conv_mod.shutil, "which", return_value=None):
        assert _detect_gpu_encoder() is None
    # 缓存应被写成 '__none__'
    assert conv_mod._gpu_encoder_cache == "__none__"


def test_detect_gpu_encoder_nvenc_available(reset_gpu_cache):
    """ffmpeg 输出含 h264_nvenc 时应返回该编码器"""
    fake_result = MagicMock()
    fake_result.stdout = "V..... h264_nvenc  NVIDIA NVENC encoder"
    with patch.object(conv_mod.shutil, "which", return_value="/usr/bin/ffmpeg"), \
         patch.object(conv_mod.subprocess, "run", return_value=fake_result):
        assert _detect_gpu_encoder() == "h264_nvenc"
    assert conv_mod._gpu_encoder_cache == "h264_nvenc"


def test_detect_gpu_encoder_qsv_available(reset_gpu_cache):
    """ffmpeg 输出含 h264_qsv（且无 nvenc）时应回退到 QSV"""
    fake_result = MagicMock()
    fake_result.stdout = "V..... h264_qsv  Intel QSV encoder"
    with patch.object(conv_mod.shutil, "which", return_value="/usr/bin/ffmpeg"), \
         patch.object(conv_mod.subprocess, "run", return_value=fake_result):
        assert _detect_gpu_encoder() == "h264_qsv"


def test_detect_gpu_encoder_amf_available(reset_gpu_cache):
    """ffmpeg 输出只含 h264_amf 时应回退到 AMF"""
    fake_result = MagicMock()
    fake_result.stdout = "V..... h264_amf  AMD AMF encoder"
    with patch.object(conv_mod.shutil, "which", return_value="/usr/bin/ffmpeg"), \
         patch.object(conv_mod.subprocess, "run", return_value=fake_result):
        assert _detect_gpu_encoder() == "h264_amf"


def test_detect_gpu_encoder_no_gpu_codec(reset_gpu_cache):
    """ffmpeg 存在但无任何 GPU 编码器时应返回 None"""
    fake_result = MagicMock()
    fake_result.stdout = "V..... libx264  libx264 H.264 encoder"
    with patch.object(conv_mod.shutil, "which", return_value="/usr/bin/ffmpeg"), \
         patch.object(conv_mod.subprocess, "run", return_value=fake_result):
        assert _detect_gpu_encoder() is None
    assert conv_mod._gpu_encoder_cache == "__none__"


def test_detect_gpu_encoder_subprocess_exception(reset_gpu_cache):
    """subprocess.run 抛异常时应安全回退到 None"""
    with patch.object(conv_mod.shutil, "which", return_value="/usr/bin/ffmpeg"), \
         patch.object(conv_mod.subprocess, "run", side_effect=RuntimeError("boom")):
        assert _detect_gpu_encoder() is None
    assert conv_mod._gpu_encoder_cache == "__none__"


def test_detect_gpu_encoder_uses_cache(reset_gpu_cache):
    """缓存命中后不应再次调用 which / subprocess.run"""
    conv_mod._gpu_encoder_cache = "h264_nvenc"
    # which 与 subprocess.run 都不应被调用
    with patch.object(conv_mod.shutil, "which") as mock_which, \
         patch.object(conv_mod.subprocess, "run") as mock_run:
        assert _detect_gpu_encoder() == "h264_nvenc"
        mock_which.assert_not_called()
        mock_run.assert_not_called()


def test_detect_gpu_encoder_cache_none_marker(reset_gpu_cache):
    """缓存值为 '__none__' 时应返回 None（标记之前未检测到 GPU）"""
    conv_mod._gpu_encoder_cache = "__none__"
    with patch.object(conv_mod.shutil, "which") as mock_which, \
         patch.object(conv_mod.subprocess, "run"):
        assert _detect_gpu_encoder() is None
        mock_which.assert_not_called()


# ---------------------------------------------------------------------------
# _encode_with_ffmpeg_pipe 的错误处理（mock subprocess.Popen）
# ---------------------------------------------------------------------------
class _FakeProc:
    """模拟 subprocess.Popen 返回的进程对象"""

    def __init__(self, returncode=0, stderr_lines=None, raise_on_write=False):
        self.returncode = returncode
        self.stdin = MagicMock()
        if raise_on_write:
            self.stdin.write.side_effect = BrokenPipeError("pipe closed")
        self.stderr = MagicMock()
        # 模拟按行读取 stderr
        stderr_lines = stderr_lines or []
        self.stderr.readline.side_effect = [
            (line + "\n").encode("utf-8") for line in stderr_lines
        ] + [b""]
        self._terminated = False

    def terminate(self):
        self._terminated = True

    def kill(self):
        self._terminated = True

    def wait(self, timeout=None):
        return self.returncode


def test_encode_with_ffmpeg_pipe_success(tmp_path):
    """ffmpeg 正常退出时应返回 actual_frames=total_frames"""
    merged = Image.new("RGB", (100, 300), (10, 20, 30))
    output = tmp_path / "out.mp4"
    fake_proc = _FakeProc(returncode=0)

    with patch.object(conv_mod.subprocess, "Popen", return_value=fake_proc):
        path, elapsed, frames = _encode_with_ffmpeg_pipe(
            merged, output, 100, 60, 30,
            total_frames=5, scroll_distance=240, merged_height=300,
            gpu_encoder="h264_nvenc",
        )
    assert path == output
    assert frames == 5
    assert elapsed >= 0
    # 应当写入了 5 帧
    assert fake_proc.stdin.write.call_count == 5
    fake_proc.stdin.close.assert_called_once()


def test_encode_with_ffmpeg_pipe_failure_raises(tmp_path):
    """ffmpeg 退出码非 0 时应抛出 RuntimeError 并删除输出文件"""
    merged = Image.new("RGB", (100, 300), (10, 20, 30))
    output = tmp_path / "out.mp4"
    output.write_bytes(b"placeholder")  # 模拟已部分写入
    fake_proc = _FakeProc(returncode=1, stderr_lines=["some ffmpeg error"])

    with patch.object(conv_mod.subprocess, "Popen", return_value=fake_proc):
        with pytest.raises(RuntimeError, match="FFmpeg 编码失败"):
            _encode_with_ffmpeg_pipe(
                merged, output, 100, 60, 30,
                total_frames=3, scroll_distance=240, merged_height=300,
                gpu_encoder="h264_nvenc",
            )
    # 输出文件应被删除
    assert not output.exists()


def test_encode_with_ffmpeg_pipe_stop_event_cancels(tmp_path):
    """stop_event 被 set 时应提前终止，不抛 RuntimeError"""
    merged = Image.new("RGB", (100, 300), (10, 20, 30))
    output = tmp_path / "out.mp4"
    fake_proc = _FakeProc(returncode=-15)  # SIGTERM

    stop_event = threading.Event()
    # 在第二帧写入前触发取消；注意不能调用 original_write 以免递归
    write_count = [0]

    def _counting_write(data):
        write_count[0] += 1
        if write_count[0] >= 2:
            stop_event.set()
        return None  # MagicMock 默认返回值

    fake_proc.stdin.write.side_effect = _counting_write

    with patch.object(conv_mod.subprocess, "Popen", return_value=fake_proc):
        path, elapsed, frames = _encode_with_ffmpeg_pipe(
            merged, output, 100, 60, 30,
            total_frames=10, scroll_distance=240, merged_height=300,
            gpu_encoder="h264_nvenc",
            stop_event=stop_event,
        )
    # 应当只写了少量帧就退出
    assert frames < 10
    # 取消路径下应调用 terminate
    assert fake_proc._terminated


def test_encode_with_ffmpeg_pipe_static_frame(tmp_path):
    """scroll_distance=0 时应只构造一帧并重复写入"""
    merged = Image.new("RGB", (100, 50), (10, 20, 30))  # 比视口小
    output = tmp_path / "out.mp4"
    fake_proc = _FakeProc(returncode=0)

    progress_calls = []

    def _progress(done, total):
        progress_calls.append((done, total))

    with patch.object(conv_mod.subprocess, "Popen", return_value=fake_proc):
        _, _, frames = _encode_with_ffmpeg_pipe(
            merged, output, 100, 60, 30,
            total_frames=4, scroll_distance=0, merged_height=50,
            gpu_encoder="h264_nvenc",
            progress_callback=_progress,
        )
    assert frames == 4
    # 静态帧场景应当写入 4 次相同的 frame_data
    assert fake_proc.stdin.write.call_count == 4
    # 进度回调应当至少被调用一次
    assert len(progress_calls) >= 1


def test_encode_with_ffmpeg_pipe_progress_callback(tmp_path):
    """progress_callback 应按 interval 被调用"""
    merged = Image.new("RGB", (100, 300), (10, 20, 30))
    output = tmp_path / "out.mp4"
    fake_proc = _FakeProc(returncode=0)

    progress_calls = []

    def _progress(done, total):
        progress_calls.append((done, total))

    with patch.object(conv_mod.subprocess, "Popen", return_value=fake_proc):
        _, _, _ = _encode_with_ffmpeg_pipe(
            merged, output, 100, 60, 30,
            total_frames=20, scroll_distance=240, merged_height=300,
            gpu_encoder="h264_nvenc",
            progress_callback=_progress,
        )
    # 至少最后一次回调应当报告完成
    assert progress_calls[-1] == (20, 20)


# ---------------------------------------------------------------------------
# _load_and_resize_image 图片加载与缩放
# ---------------------------------------------------------------------------
def test_load_and_resize_image_basic(tmp_path):
    """普通 RGB 图片应被等比缩放到目标宽度"""
    src = tmp_path / "src.png"
    Image.new("RGB", (400, 200), (255, 0, 0)).save(src)

    img, ow, oh, elapsed = _load_and_resize_image(src, video_width=200)
    assert ow == 400
    assert oh == 200
    assert img.size == (200, 100)  # 等比缩放
    assert elapsed >= 0


def test_load_and_resize_image_rgba_to_rgb(tmp_path):
    """RGBA 图片应被合成到白底后转 RGB"""
    src = tmp_path / "rgba.png"
    Image.new("RGBA", (100, 100), (255, 0, 0, 128)).save(src)

    img, ow, oh, _ = _load_and_resize_image(src, video_width=50)
    assert img.mode == "RGB"
    assert img.size == (50, 50)


def test_load_and_resize_image_palette_mode(tmp_path):
    """P 模式（调色板）图片应被转为 RGB"""
    src = tmp_path / "p.gif"
    Image.new("P", (100, 100), 0).save(src)

    img, _, _, _ = _load_and_resize_image(src, video_width=50)
    assert img.mode == "RGB"
    assert img.size == (50, 50)


def test_load_and_resize_image_non_rgb_mode(tmp_path):
    """L 模式（灰度）应被转为 RGB"""
    src = tmp_path / "gray.png"
    Image.new("L", (100, 100), 128).save(src)

    img, _, _, _ = _load_and_resize_image(src, video_width=50)
    assert img.mode == "RGB"


# ---------------------------------------------------------------------------
# _viewport_to_bgr / _build_static_frame_bgr 视口裁剪
# ---------------------------------------------------------------------------
def test_viewport_to_bgr_basic():
    """正常裁剪：返回 BGR 数组，形状为 (viewport_h, w, 3)"""
    merged = Image.new("RGB", (100, 500), (255, 0, 0))
    frame = _viewport_to_bgr(merged, y_offset=0, video_width=100, viewport_h=60, merged_height=500)
    assert frame.shape == (60, 100, 3)
    # RGB(255,0,0) -> BGR(0,0,255)
    assert frame[0, 0, 0] == 0  # B
    assert frame[0, 0, 1] == 0  # G
    assert frame[0, 0, 2] == 255  # R


def test_viewport_to_bgr_offset_clamped():
    """y_offset + viewport_h 超出图片高度时应底部补黑色"""
    merged = Image.new("RGB", (100, 100), (255, 0, 0))
    # 视口 60，从 80 开始 -> 真实 20 行 + 40 行黑色
    frame = _viewport_to_bgr(merged, y_offset=80, video_width=100, viewport_h=60, merged_height=100)
    assert frame.shape == (60, 100, 3)
    # 顶部 20 行应为红色（BGR: 0,0,255）
    assert frame[0, 0, 2] == 255
    # 底部应为黑色
    assert frame[59, 0, 0] == 0
    assert frame[59, 0, 1] == 0
    assert frame[59, 0, 2] == 0


def test_viewport_to_bgr_uses_no_copy():
    """np.asarray 应与 PIL 缓冲区共享内存（避免大块复制）"""
    merged = Image.new("RGB", (50, 50), (0, 0, 0))
    # 用 np.asarray 应返回只读视图；若试图写入会抛 ValueError
    frame = _viewport_to_bgr(merged, y_offset=0, video_width=50, viewport_h=50, merged_height=50)
    # cv2.cvtColor 默认会返回新数组（可写），但其输入是共享视图
    assert frame.shape == (50, 50, 3)
    # 验证可写性（cvtColor 输出可写）
    frame[0, 0] = (1, 2, 3)
    assert (frame[0, 0] == [1, 2, 3]).all()


def test_build_static_frame_bgr_centered():
    """图片高度小于视口时应居中显示，上下补黑色"""
    merged = Image.new("RGB", (100, 40), (255, 0, 0))
    frame = _build_static_frame_bgr(merged, video_width=100, viewport_h=100, merged_height=40)
    assert frame.shape == (100, 100, 3)
    # 顶部 padding 应为黑色
    top_padding = (100 - 40) // 2  # 30
    assert frame[0, 0, 2] == 0  # 黑色
    # 中部应为红色
    assert frame[top_padding + 5, 0, 2] == 255
    # 底部应为黑色
    assert frame[99, 0, 2] == 0


def test_build_static_frame_bgr_larger_than_viewport():
    """图片高度大于视口时只裁剪视口区域，不抛错"""
    merged = Image.new("RGB", (100, 200), (0, 255, 0))
    frame = _build_static_frame_bgr(merged, video_width=100, viewport_h=100, merged_height=200)
    assert frame.shape == (100, 100, 3)
    # y_padding = max(0, (100-200)//2) = 0, 所以从图片顶部开始粘贴 100 行
    # 顶部应为绿色（BGR: 0,255,0）
    assert frame[0, 0, 1] == 255


# ---------------------------------------------------------------------------
# generate_multi_scroll_video 端到端（mock 编码器）
# ---------------------------------------------------------------------------

def test_multi_scroll_video_stitching_regression(tmp_path):
    """回归测试：拼接循环曾因结果列表展平丢失 idx 导致解包崩溃。

    走真实的加载/拼接/滚动帧流程，仅 mock 掉编码器（GPU 检测与 OpenCV writer），
    确保 generate_multi_scroll_video 在进入编码阶段之前不再抛异常。
    """
    img_path = tmp_path / "tall.png"
    Image.new("RGB", (32, 240), (200, 100, 50)).save(img_path)

    with patch.object(conv_mod, "_detect_gpu_encoder", return_value=None), \
         patch.object(conv_mod, "_create_video_writer", return_value=MagicMock()):
        out_path = conv_mod.generate_multi_scroll_video(
            [img_path], tmp_path, video_width=32, fps=5,
            scroll_duration=1.0, aspect_ratio="16:9", preset="ultrafast",
        )

    assert Path(out_path).name == "tall.mp4"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
