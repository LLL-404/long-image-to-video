"""
长图片转视频 - GUI 桌面程序

基于 tkinter 的图形界面，支持将单张或多张长图片纵向拼接后
转换为平滑滚动的 MP4 视频。
主题: Modern Minimalist（极简黑白灰）
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import logging
import threading
import time
import tkinter as tk
from pathlib import Path
from queue import Queue
from tkinter import filedialog, messagebox, ttk
from typing import cast

from converter import (  # pyright: ignore[reportImplicitRelativeImport, reportUnknownVariableType]
    generate_multi_scroll_video,
    generate_scroll_video,
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)


# 进度消息格式（用于 Queue 线程间通信）：
#   {'type': 'progress', 'value': <float>}            - 进度更新
#   {'type': 'status', 'text': <str>}                 - 状态文本
#   {'type': 'complete', 'text': <str>}               - 转换完成
#   {'type': 'error', 'text': <str>}                  - 错误
#   {'type': 'cancelled', 'text': <str>}              - 已取消


class LongImageToVideoGUI:
    """长图片转视频 GUI 工具（支持多图拼接）"""

    # Modern Minimalist 主题配色
    _C_CHARCOAL: str = '#36454f'
    _C_SLATE: str = '#708090'
    _C_LIGHT_GRAY: str = '#d3d3d3'
    _C_WHITE: str = '#ffffff'
    _C_DANGER: str = '#c0392b'
    _C_DANGER_ACTIVE: str = '#a93226'
    _C_SLATE_ACTIVE: str = '#5a6e7e'
    _F_HEADER: 'tuple[str, int, str]' = ('Segoe UI', 16, 'bold')
    _F_SUBTITLE: 'tuple[str, int]' = ('Segoe UI', 9)
    _F_LABEL: 'tuple[str, int]' = ('Segoe UI', 10)
    _F_BUTTON: 'tuple[str, int]' = ('Segoe UI', 9)
    _F_TITLE: 'tuple[str, int, str]' = ('Segoe UI', 11, 'bold')

    def __init__(self, root: tk.Tk) -> None:
        """初始化 GUI，绑定变量并创建控件。

        Args:
            root: tkinter 根窗口对象。
        """
        self.root: tk.Tk = root
        self.root.title("长图片转视频")
        self.root.geometry("640x480")
        self.root.resizable(True, True)
        _ = self.root.configure(bg=self._C_LIGHT_GRAY)

        # ---- ttk 主题样式 ----
        style = ttk.Style()
        try:
            style.theme_use('clam')  # 更可定制的主题引擎
        except tk.TclError:
            logger.warning("[主题] 'clam' 不可用，使用系统默认主题")

        # 全局字体
        style.configure('.', font=self._F_LABEL, background=self._C_LIGHT_GRAY)

        # TFrame
        style.configure('TFrame', background=self._C_LIGHT_GRAY)
        style.configure('Card.TFrame', background=self._C_WHITE, relief='flat')

        # TLabel（默认）
        style.configure('TLabel', background=self._C_LIGHT_GRAY, foreground=self._C_CHARCOAL)
        # 标题 Label
        style.configure('Title.TLabel', font=self._F_HEADER, foreground=self._C_CHARCOAL,
                        background=self._C_LIGHT_GRAY)
        # 副标题 Label
        style.configure('Subtitle.TLabel', font=self._F_SUBTITLE, foreground=self._C_SLATE,
                        background=self._C_LIGHT_GRAY)
        # 区块标题 Label
        style.configure('Section.TLabel', font=self._F_TITLE, foreground=self._C_CHARCOAL,
                        background=self._C_LIGHT_GRAY)
        # 白色背景卡片内的 Label
        style.configure('Card.TLabel', background=self._C_WHITE, foreground=self._C_CHARCOAL)

        # 按钮样式
        style.configure('Primary.TButton', font=self._F_BUTTON,
                        background=self._C_CHARCOAL, foreground=self._C_WHITE,
                        borderwidth=0, relief='flat', padding=(16, 6))
        style.map('Primary.TButton',
                  background=[('active', self._C_SLATE), ('disabled', self._C_LIGHT_GRAY)],
                  foreground=[('disabled', self._C_SLATE)])

        style.configure('Secondary.TButton', font=self._F_BUTTON,
                        background=self._C_SLATE, foreground=self._C_WHITE,
                        borderwidth=0, relief='flat', padding=(10, 4))
        style.map('Secondary.TButton',
                  background=[('active', self._C_SLATE_ACTIVE), ('disabled', self._C_LIGHT_GRAY)])

        style.configure('Outline.TButton', font=self._F_BUTTON,
                        background=self._C_WHITE, foreground=self._C_CHARCOAL,
                        borderwidth=1, relief='solid', padding=(10, 4))
        style.map('Outline.TButton',
                  background=[('active', self._C_LIGHT_GRAY)])

        # 危险按钮（取消/退出）
        style.configure('Danger.TButton', font=self._F_BUTTON,
                        background=self._C_DANGER, foreground=self._C_WHITE,
                        borderwidth=0, relief='flat', padding=(10, 4))
        style.map('Danger.TButton', background=[('active', self._C_DANGER_ACTIVE), ('disabled', self._C_LIGHT_GRAY)])

        # Entry
        style.configure('TEntry', fieldbackground=self._C_WHITE, foreground=self._C_CHARCOAL,
                        borderwidth=1, relief='solid')

        # Spinbox
        style.configure('TSpinbox', fieldbackground=self._C_WHITE, foreground=self._C_CHARCOAL)

        # Progressbar
        style.configure('TProgressbar', troughcolor=self._C_LIGHT_GRAY,
                        background=self._C_CHARCOAL, thickness=8)

        # 状态标签
        style.configure('Status.TLabel', font=self._F_SUBTITLE, foreground=self._C_SLATE,
                        background=self._C_LIGHT_GRAY)

        # 变量绑定
        self.image_paths: list[str] = []  # 存储完整路径列表
        self.output_dir: tk.StringVar = tk.StringVar()
        self.video_width: tk.IntVar = tk.IntVar(value=1080)
        self.fps: tk.IntVar = tk.IntVar(value=30)
        self.scroll_duration: tk.DoubleVar = tk.DoubleVar(value=10.0)
        self.aspect_ratio: tk.StringVar = tk.StringVar(value="16:9")
        self.preset: tk.StringVar = tk.StringVar(value="medium")  # 编码速度：越慢资源消耗越低
        # 滚动曲线（缓动函数）：必须在被 Combobox 引用前定义
        self.easing: tk.StringVar = tk.StringVar(value="ease-in-out")
        self.progress: tk.DoubleVar = tk.DoubleVar()

        # 状态管理
        self.is_converting: bool = False
        self.stop_event: threading.Event = threading.Event()
        self.progress_queue: Queue[dict[str, float | str]] = Queue()
        self.worker_thread: threading.Thread | None = None

        self.create_widgets()
        self.setup_progress_updater()

    def create_widgets(self) -> None:
        """构建 UI 组件（Modern Minimalist 主题）"""
        main_frame = ttk.Frame(self.root, padding="24")
        main_frame.pack(fill=tk.BOTH, expand=True)

        # 标题
        ttk.Label(main_frame, text="长图片转视频工具", style='Title.TLabel').pack(pady=(0, 4))
        ttk.Label(main_frame, text="选择多张长图片 → 纵向拼接 → 整体滚动生成 MP4 视频",
                  style='Subtitle.TLabel').pack(pady=(0, 16))

        # ---- 文件列表卡片 ----
        card_frame = ttk.Frame(main_frame, style='Card.TFrame', padding="12")
        card_frame.pack(fill=tk.X, pady=(0, 12))

        ttk.Label(card_frame, text="选择长图片（支持多选、排序）", style='Section.TLabel').pack(anchor=tk.W, pady=(0, 8))
        file_row = ttk.Frame(card_frame, style='Card.TFrame')
        file_row.pack(fill=tk.X)
        self.image_listbox: tk.Listbox = tk.Listbox(
            file_row, height=5, width=50,
            bg=self._C_WHITE, fg=self._C_CHARCOAL,
            selectbackground=self._C_CHARCOAL, selectforeground=self._C_WHITE,
            font=('Segoe UI', 9), borderwidth=1, relief='solid',
            highlightthickness=0, activestyle='none',
        )
        self.image_listbox.pack(side=tk.LEFT, padx=(0, 8), fill=tk.BOTH, expand=True)

        list_btn_frame = ttk.Frame(file_row, style='Card.TFrame')
        list_btn_frame.pack(side=tk.RIGHT)
        ttk.Button(list_btn_frame, text="添加", style='Secondary.TButton',
                   command=self.select_images).pack(pady=2, fill=tk.X)
        ttk.Button(list_btn_frame, text="移除", style='Secondary.TButton',
                   command=self.remove_selected).pack(pady=2, fill=tk.X)
        ttk.Button(list_btn_frame, text="清空", style='Secondary.TButton',
                   command=self.clear_list).pack(pady=2, fill=tk.X)
        ttk.Button(list_btn_frame, text="↑ 上移", style='Secondary.TButton',
                   command=self.move_up).pack(pady=2, fill=tk.X)
        ttk.Button(list_btn_frame, text="↓ 下移", style='Secondary.TButton',
                   command=self.move_down).pack(pady=2, fill=tk.X)

        # ---- 输出目录 & 参数卡片 ----
        params_card = ttk.Frame(main_frame, style='Card.TFrame', padding="12")
        params_card.pack(fill=tk.X, pady=(0, 12))

        # 输出目录
        ttk.Label(params_card, text="输出目录", style='Section.TLabel').pack(anchor=tk.W, pady=(0, 6))
        dir_row = ttk.Frame(params_card, style='Card.TFrame')
        dir_row.pack(fill=tk.X, pady=(0, 10))
        ttk.Entry(dir_row, textvariable=self.output_dir, state='readonly', width=45).pack(
            side=tk.LEFT, padx=(0, 8), fill=tk.X, expand=True)
        ttk.Button(dir_row, text="浏览", style='Secondary.TButton',
                   command=self.select_output_dir).pack(side=tk.RIGHT)

        # 视频宽度
        ttk.Label(params_card, text="视频宽度", style='Section.TLabel').pack(anchor=tk.W, pady=(0, 4))
        width_row = ttk.Frame(params_card, style='Card.TFrame')
        width_row.pack(fill=tk.X, pady=(0, 6))
        ttk.Spinbox(width_row, from_=360, to=3840, textvariable=self.video_width, width=8).pack(side=tk.LEFT)
        ttk.Label(width_row, text=" px  (高度按比例自动)", style='Card.TLabel').pack(side=tk.LEFT, padx=6)

        # 预设按钮行
        presets_row = ttk.Frame(params_card, style='Card.TFrame')
        presets_row.pack(fill=tk.X, pady=(0, 10))
        ttk.Button(presets_row, text="720p", width=6, style='Outline.TButton',
                   command=lambda: self.video_width.set(720)).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(presets_row, text="1080p", width=6, style='Outline.TButton',
                   command=lambda: self.video_width.set(1080)).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(presets_row, text="2K", width=6, style='Outline.TButton',
                   command=lambda: self.video_width.set(2560)).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(presets_row, text="4K", width=6, style='Outline.TButton',
                   command=lambda: self.video_width.set(3840)).pack(side=tk.LEFT)

        # 宽高比
        ttk.Label(params_card, text="视频比例", style='Section.TLabel').pack(anchor=tk.W, pady=(6, 4))
        ratio_row = ttk.Frame(params_card, style='Card.TFrame')
        ratio_row.pack(fill=tk.X, pady=(0, 10))
        ttk.Combobox(ratio_row, textvariable=self.aspect_ratio, width=10,
                     values=["16:9", "9:16", "3:4", "1:1", "4:3", "21:9"],
                     state='readonly').pack(side=tk.LEFT)
        ttk.Button(ratio_row, text="16:9", width=6, style='Outline.TButton',
                   command=lambda: self.aspect_ratio.set("16:9")).pack(side=tk.LEFT, padx=4)
        ttk.Button(ratio_row, text="9:16", width=6, style='Outline.TButton',
                   command=lambda: self.aspect_ratio.set("9:16")).pack(side=tk.LEFT, padx=4)
        ttk.Button(ratio_row, text="3:4", width=6, style='Outline.TButton',
                   command=lambda: self.aspect_ratio.set("3:4")).pack(side=tk.LEFT)

        # FPS + 滚动时长 同行
        params_row = ttk.Frame(params_card, style='Card.TFrame')
        params_row.pack(fill=tk.X)

        # FPS
        fps_col = ttk.Frame(params_row, style='Card.TFrame')
        fps_col.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 12))
        ttk.Label(fps_col, text="帧率 (FPS)", style='Section.TLabel').pack(anchor=tk.W, pady=(0, 4))
        fps_inner = ttk.Frame(fps_col, style='Card.TFrame')
        fps_inner.pack(fill=tk.X)
        ttk.Spinbox(fps_inner, from_=1, to=60, textvariable=self.fps, width=8).pack(side=tk.LEFT)
        ttk.Label(fps_inner, text="  推荐 24/30/60", style='Card.TLabel').pack(side=tk.LEFT)

        # 滚动时长
        dur_col = ttk.Frame(params_row, style='Card.TFrame')
        dur_col.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Label(dur_col, text="滚动时长", style='Section.TLabel').pack(anchor=tk.W, pady=(0, 4))
        dur_inner = ttk.Frame(dur_col, style='Card.TFrame')
        dur_inner.pack(fill=tk.X)
        ttk.Spinbox(dur_inner, from_=1, to=300, increment=1,
                    textvariable=self.scroll_duration, width=8).pack(side=tk.LEFT)
        ttk.Label(dur_inner, text="  秒", style='Card.TLabel').pack(side=tk.LEFT)

        # ---- 编码速度控制 ----
        ttk.Label(params_card, text="编码速度", style='Section.TLabel').pack(anchor=tk.W, pady=(10, 4))
        preset_row = ttk.Frame(params_card, style='Card.TFrame')
        preset_row.pack(fill=tk.X, pady=(0, 4))
        ttk.Combobox(preset_row, textvariable=self.preset, width=10,
                     values=["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"],
                     state='readonly').pack(side=tk.LEFT)

        # 速度预设按钮
        preset_presets = ttk.Frame(params_card, style='Card.TFrame')
        preset_presets.pack(fill=tk.X, pady=(0, 10))
        ttk.Button(preset_presets, text="快速 (ultrafast)", width=14, style='Outline.TButton',
                   command=lambda: self.preset.set("ultrafast")).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(preset_presets, text="标准 (medium)", width=14, style='Outline.TButton',
                   command=lambda: self.preset.set("medium")).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(preset_presets, text="省资源 (slow)", width=14, style='Outline.TButton',
                   command=lambda: self.preset.set("slow")).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(preset_presets, text="极省 (veryslow)", width=14, style='Outline.TButton',
                   command=lambda: self.preset.set("veryslow")).pack(side=tk.LEFT)

        # ---- 滚动曲线 ----
        ttk.Label(params_card, text="滚动曲线", style='Section.TLabel').pack(anchor=tk.W, pady=(10, 4))
        easing_row = ttk.Frame(params_card, style='Card.TFrame')
        easing_row.pack(fill=tk.X, pady=(0, 10))
        ttk.Combobox(easing_row, textvariable=self.easing, width=12,
                     values=["linear", "ease-in-out", "ease-in-out-quad", "ease-in-out-sine"],
                     state='readonly').pack(side=tk.LEFT)
        ttk.Label(easing_row, text="ease-in-out = 两头慢中间快, linear = 匀速", style='Card.TLabel').pack(side=tk.LEFT, padx=8)

        # ---- 进度条 ----
        self.progress_bar: ttk.Progressbar = ttk.Progressbar(
            main_frame, variable=self.progress, maximum=100, style='TProgressbar')
        self.progress_bar.pack(fill=tk.X, pady=(6, 4))

        # 状态标签
        self.status_label: ttk.Label = ttk.Label(main_frame, text="就绪", style='Status.TLabel')
        self.status_label.pack(pady=(0, 12))

        # ---- 操作按钮 ----
        button_frame = ttk.Frame(main_frame)
        button_frame.pack()

        self.start_btn: ttk.Button = ttk.Button(
            button_frame, text="▶ 开始转换", style='Primary.TButton', command=self.start_convert)
        self.start_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.stop_btn: ttk.Button = ttk.Button(
            button_frame, text="■ 取消", style='Danger.TButton', command=self.stop_convert, state='disabled')
        self.stop_btn.pack(side=tk.LEFT, padx=(0, 8))

        ttk.Button(button_frame, text="打开输出目录", style='Outline.TButton',
                   command=self.open_output_dir).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(button_frame, text="退出", style='Outline.TButton',
                   command=self.quit_app).pack(side=tk.LEFT)

    def setup_progress_updater(self):
        """设置进度更新器，通过 root.after 轮询 Queue"""
        def update_progress():
            while not self.progress_queue.empty():
                msg = self.progress_queue.get()
                if msg['type'] == 'progress':
                    self.progress.set(float(msg['value']))
                elif msg['type'] == 'status':
                    _ = self.status_label.config(text=str(msg['text']))
                elif msg['type'] == 'complete':
                    self.is_converting = False
                    _ = self.start_btn.config(state='normal')
                    _ = self.stop_btn.config(state='disabled')
                    self.progress.set(0)
                    _ = messagebox.showinfo("成功", str(msg['text']))
                elif msg['type'] == 'error':
                    self.is_converting = False
                    _ = self.start_btn.config(state='normal')
                    _ = self.stop_btn.config(state='disabled')
                    self.progress.set(0)
                    _ = messagebox.showerror("错误", str(msg['text']))
                elif msg['type'] == 'cancelled':
                    self.is_converting = False
                    _ = self.start_btn.config(state='normal')
                    _ = self.stop_btn.config(state='disabled')
                    self.progress.set(0)
            # 仅在转换中继续轮询，空闲时降低频率
            if self.is_converting:
                _ = self.root.after(100, update_progress)
            else:
                _ = self.root.after(500, update_progress)
        _ = self.root.after(100, update_progress)

    # ============================================================
    # 文件列表操作
    # ============================================================

    def _refresh_listbox(self) -> None:
        """根据 self.image_paths 刷新 Listbox 显示"""
        self.image_listbox.delete(0, tk.END)
        for path in self.image_paths:
            self.image_listbox.insert(tk.END, Path(path).name)

    def select_images(self) -> None:
        """添加图片文件（支持多选）"""
        paths = filedialog.askopenfilenames(
            filetypes=[("图片文件", "*.png *.jpg *.jpeg *.webp *.bmp *.gif *.tiff")]
        )
        if paths:
            for path in paths:
                if path not in self.image_paths:
                    self.image_paths.append(path)
            self._refresh_listbox()
            # 自动设置输出目录为第一张图片所在目录下的 output
            if not self.output_dir.get() and self.image_paths:
                default_dir = Path(self.image_paths[0]).parent / "output"
                self.output_dir.set(str(default_dir))

    def remove_selected(self) -> None:
        """移除选中的图片（支持多选，倒序移除避免索引偏移）"""
        selection = cast("tuple[int, ...]", self.image_listbox.curselection())  # pyright: ignore[reportUnknownMemberType]
        if selection:
            for index in sorted(selection, reverse=True):
                _ = self.image_paths.pop(index)
            self._refresh_listbox()

    def clear_list(self) -> None:
        """清空文件列表"""
        self.image_paths = []
        self._refresh_listbox()

    def move_up(self) -> None:
        """上移选中项"""
        selection = cast("tuple[int, ...]", self.image_listbox.curselection())  # pyright: ignore[reportUnknownMemberType]
        if selection and selection[0] > 0:
            idx = selection[0]
            self.image_paths[idx], self.image_paths[idx - 1] = \
                self.image_paths[idx - 1], self.image_paths[idx]
            self._refresh_listbox()
            self.image_listbox.selection_set(idx - 1)

    def move_down(self) -> None:
        """下移选中项"""
        selection = cast("tuple[int, ...]", self.image_listbox.curselection())  # pyright: ignore[reportUnknownMemberType]
        if selection and selection[0] < len(self.image_paths) - 1:
            idx = selection[0]
            self.image_paths[idx], self.image_paths[idx + 1] = \
                self.image_paths[idx + 1], self.image_paths[idx]
            self._refresh_listbox()
            self.image_listbox.selection_set(idx + 1)

    # ============================================================
    # 输出目录 & 转换逻辑
    # ============================================================

    def select_output_dir(self) -> None:
        """选择输出目录"""
        path = filedialog.askdirectory()
        if path:
            self.output_dir.set(path)

    def start_convert(self) -> None:
        """开始转换（支持多图拼接）"""
        if not self.image_paths:
            _ = messagebox.showerror("错误", "请至少选择一张长图片")
            return

        if self.is_converting:
            return

        output_dir = self.output_dir.get()
        if not output_dir:
            _ = messagebox.showerror("错误", "请先选择输出目录")
            return

        video_width = self.video_width.get()
        fps = self.fps.get()
        scroll_duration = self.scroll_duration.get()
        aspect_ratio = self.aspect_ratio.get()
        preset = self.preset.get()  # 编码速度

        if video_width < 360 or video_width > 3840:
            _ = messagebox.showerror("错误", "视频宽度必须在 360-3840 之间")
            return
        if fps < 1 or fps > 60:
            _ = messagebox.showerror("错误", "FPS 必须在 1-60 之间")
            return
        if scroll_duration < 1:
            _ = messagebox.showerror("错误", "滚动时长必须 >= 1 秒")
            return
        valid_presets = ['ultrafast', 'superfast', 'veryfast', 'faster', 'fast', 'medium', 'slow', 'slower', 'veryslow']
        if preset not in valid_presets:
            _ = messagebox.showerror("错误", f"编码速度必须是 {valid_presets} 之一")
            return

        # 更新 UI 状态
        self.is_converting = True
        self.stop_event.clear()
        _ = self.start_btn.config(state='disabled')
        _ = self.stop_btn.config(state='normal')
        self.progress.set(0)

        image_paths = list(self.image_paths)  # 快照

        logger.info("=" * 60)
        logger.info(f"[开始转换] 图片数量: {len(image_paths)} | 输出目录: {output_dir}")
        if len(image_paths) > 1:
            logger.info(f"[拼接顺序] {' → '.join(Path(p).name for p in image_paths)}")
        logger.info(f"[参数] 视频宽度: {video_width} | 比例: {aspect_ratio} | FPS: {fps} | 滚动时长: {scroll_duration}s | 编码速度: {preset}")

        def progress_callback(current: int, total: int) -> None:
            """帧编码进度回调，将进度推入 Queue 供主线程轮询。"""
            progress_pct = (current / total) * 100
            self.progress_queue.put({'type': 'progress', 'value': progress_pct})
            self.progress_queue.put({
                'type': 'status',
                'text': f"正在编码第 {current}/{total} 帧..."
            })

        def conversion_worker():
            batch_start = time.time()
            try:
                if len(image_paths) == 1:
                    output_path = generate_scroll_video(
                        image_path=image_paths[0],
                        output_dir=output_dir,
                        video_width=video_width,
                        fps=fps,
                        scroll_duration=scroll_duration,
                        aspect_ratio=aspect_ratio,
                        preset=preset,
                        easing=self.easing.get(),
                        stop_event=self.stop_event,
                        progress_callback=progress_callback,
                    )
                else:
                    output_path = generate_multi_scroll_video(
                        image_paths=image_paths,
                        output_dir=output_dir,
                        video_width=video_width,
                        fps=fps,
                        scroll_duration=scroll_duration,
                        aspect_ratio=aspect_ratio,
                        preset=preset,
                        easing=self.easing.get(),
                        stop_event=self.stop_event,
                        progress_callback=progress_callback,
                    )

                batch_elapsed = (time.time() - batch_start) * 1000
                logger.info(f"[总耗时] {batch_elapsed:.2f} ms")
                logger.info("=" * 60)

                if self.stop_event.is_set():
                    self.progress_queue.put({'type': 'status', 'text': '已取消'})
                    self.progress_queue.put({'type': 'cancelled', 'text': '转换已取消'})
                else:
                    self.progress_queue.put({'type': 'progress', 'value': 100})
                    self.progress_queue.put({
                        'type': 'status',
                        'text': f'转换完成！输出: {output_path.name}'
                    })
                    self.progress_queue.put({
                        'type': 'complete',
                        'text': (
                            f'转换完成！\n\n输出目录: {output_dir}\n'
                            f'图片数量: {len(image_paths)}\n'
                            f'视频文件: {output_path.name}\n'
                            f'总耗时: {batch_elapsed:.2f} ms'
                        )
                    })

            except InterruptedError:
                logger.info("[用户取消] 在准备阶段取消")
                self.progress_queue.put({'type': 'status', 'text': '已取消'})
                self.progress_queue.put({'type': 'cancelled', 'text': '转换已取消'})
            except Exception as e:
                batch_elapsed = (time.time() - batch_start) * 1000
                logger.error(f"[转换失败] 耗时: {batch_elapsed:.2f} ms | 错误: {str(e)}")
                self.progress_queue.put({
                    'type': 'error',
                    'text': f'转换失败: {str(e)}'
                })

        self.worker_thread = threading.Thread(target=conversion_worker, daemon=True)
        self.worker_thread.start()

    def stop_convert(self) -> None:
        """取消转换，设置停止标志通知工作线程。"""
        if self.is_converting:
            self.stop_event.set()
            _ = self.status_label.config(text="正在取消...")

    def open_output_dir(self) -> None:
        """在资源管理器中打开输出目录。"""
        output_dir = self.output_dir.get()
        if output_dir and os.path.exists(output_dir):
            os.startfile(output_dir)
        else:
            _ = messagebox.showinfo("提示", "请先选择输出目录或完成转换")

    def quit_app(self) -> None:
        """退出程序，等待工作线程结束以确保视频文件正确写入。"""
        if self.is_converting:
            self.stop_event.set()
            _ = self.status_label.config(text="正在停止转换...")
            # 等待工作线程结束（最多等待 5 秒）以确保视频文件正确写入
            if self.worker_thread and self.worker_thread.is_alive():
                self.worker_thread.join(timeout=5)
        self.root.quit()


def main() -> None:
    """启动 GUI 应用（作为包入口点使用）。"""
    root = tk.Tk()
    _ = LongImageToVideoGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
