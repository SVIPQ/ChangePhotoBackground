# -*- coding: utf-8 -*-
"""
证件照批量换底色工具_Czq
功能：批量导入图片/文件夹（递归扫描子文件夹）、顶部区域鼠标拖放文件夹后手动确认导入、
手动点击“开始批量处理”执行、多线程并发 AI 抠图（默认 birefnet-portrait 强化人像与发丝细节）、
支持在软件内选择模型并点击“下载模型”直接保存到软件目录的 .u2net 文件夹、
统一替换底色、保留子文件夹结构和文件主名称、支持导出 JPG 或 PNG、
支持精细边缘模式（加强头发丝与边缘去白边）、圆润胶囊白蓝界面与连贯进度条。
"""
import os
import sys
import io
import math
import traceback
import threading
import queue
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, colorchooser, messagebox

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    HAS_DND = True
except Exception:
    DND_FILES = None
    TkinterDnD = None
    HAS_DND = False

APP_DISPLAY_NAME = "证件照批量换底色工具_Czq"
SPLASH_DISPLAY_NAME = "证件照批量换底色工具_Czq"

if sys.stdout is None:
    sys.stdout = io.StringIO()
if sys.stderr is None:
    sys.stderr = io.StringIO()


def get_app_model_dir() -> Path:
    """获取软件自身目录内的模型存放文件夹（<软件目录>/.u2net）"""
    if getattr(sys, "frozen", False):
        base_dir = Path(sys.executable).resolve().parent
    else:
        base_dir = Path(__file__).resolve().parent
    model_dir = base_dir / ".u2net"
    model_dir.mkdir(parents=True, exist_ok=True)
    return model_dir


APP_MODEL_DIR = get_app_model_dir()
os.environ["U2NET_HOME"] = str(APP_MODEL_DIR)

Image = None
ImageOps = None
ImageFilter = None
remove = None
new_session = None

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}
PRESETS = {
    "蓝色（常用）": "#438EDB",
    "白色": "#FFFFFF",
    "红色": "#D9001B",
    "浅蓝色": "#ADD8E6",
    "灰色": "#D9D9D9",
}

MODEL_OPTIONS = {
    "人像发丝最佳 (birefnet-portrait)": "birefnet-portrait",
    "人像精细 (nrnet)": "nrnet",
    "高精度通用 (isnet-general-use)": "isnet-general-use",
    "经典通用 (u2net)": "u2net",
}

THEME = {
    "bg": "#F1F6FD",
    "card": "#FFFFFF",
    "border": "#D8E4F3",
    "text": "#0F172A",
    "muted": "#64748B",
    "accent": "#2563EB",
    "accent_hover": "#1D4ED8",
    "btn_sec": "#EBF3FF",
    "btn_sec_hover": "#DCEAFF",
    "btn_sec_fg": "#1E3A8A",
    "drop_bg": "#EFF6FF",
    "drop_hover": "#DBEAFE",
    "drop_border": "#93C5FD",
    "drop_fg": "#1E40AF",
    "list_bg": "#F8FAFC",
}


def hex_to_rgb(value):
    value = (value or "").strip().lstrip("#")
    if len(value) != 6:
        raise ValueError("颜色请使用 #RRGGBB 格式，例如 #438EDB")
    return tuple(int(value[i:i+2], 16) for i in (0, 2, 4))


def collect_images(paths):
    found = []
    for raw in paths:
        p = Path(raw)
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
            found.append(p)
        elif p.is_dir():
            for child in p.rglob("*"):
                if child.is_file() and child.suffix.lower() in IMAGE_EXTS:
                    found.append(child)
    seen, result = set(), []
    for p in found:
        key = str(p.resolve())
        if key not in seen:
            seen.add(key)
            result.append(p)
    return result


def encode_image_under_limit(image, fmt, max_kb):
    """按指定格式（JPG / PNG）编码图片；超限时尝试调整画质或逐步缩小尺寸。"""
    fmt_upper = (fmt or "JPG").upper()
    limit = max(1, int(max_kb)) * 1024
    img = image.convert("RGB")

    if fmt_upper == "PNG":
        for _ in range(12):
            buf = io.BytesIO()
            img.save(buf, format="PNG", optimize=True, compress_level=9)
            data = buf.getvalue()
            if len(data) <= limit:
                return data
            w, h = img.size
            if min(w, h) <= 160:
                return data
            img = img.resize((max(1, int(w * 0.85)), max(1, int(h * 0.85))), Image.Resampling.LANCZOS)
        raise ValueError(f"PNG 在可接受的最低尺寸下仍无法压缩到 {max_kb}KB")

    for _ in range(12):
        lo, hi = 25, 95
        best = None
        while lo <= hi:
            q = (lo + hi) // 2
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=q, optimize=True, progressive=True)
            data = buf.getvalue()
            if len(data) <= limit:
                best = data
                lo = q + 1
            else:
                hi = q - 1
        if best is not None:
            return best
        w, h = img.size
        if min(w, h) <= 160:
            break
        img = img.resize((max(1, int(w * 0.85)), max(1, int(h * 0.85))), Image.Resampling.LANCZOS)
    raise ValueError(f"JPG 在可接受的最低尺寸下仍无法压缩到 {max_kb}KB")


def _rounded_rect_points(x1, y1, x2, y2, r, segments=16):
    r = max(0.0, min(float(r), (x2 - x1) / 2.0, (y2 - y1) / 2.0))
    if r <= 0:
        return [x1, y1, x2, y1, x2, y2, x1, y2]

    pts = []
    corners = [
        (x2 - r, y1 + r, -math.pi / 2, 0.0),
        (x2 - r, y2 - r, 0.0, math.pi / 2),
        (x1 + r, y2 - r, math.pi / 2, math.pi),
        (x1 + r, y1 + r, math.pi, 1.5 * math.pi),
    ]
    for cx, cy, start_a, end_a in corners:
        for i in range(segments + 1):
            ang = start_a + (end_a - start_a) * (i / segments)
            pts.extend([cx + math.cos(ang) * r, cy + math.sin(ang) * r])
    return pts


def create_rounded_rect(canvas, x1, y1, x2, y2, r=18, **kwargs):
    pts = _rounded_rect_points(x1, y1, x2, y2, r=r, segments=16)
    return canvas.create_polygon(pts, smooth=False, **kwargs)


class SoftPillCard:
    """胶囊圆角卡片容器：用画布绘制大圆角背景，承载内部内容"""
    def __init__(
        self,
        parent,
        radius=24,
        bg_color="#FFFFFF",
        border_color="#D8E4F3",
        parent_bg="#F1F6FD",
        padx=15,
        pady=8,
        outer_pady=(0, 6),
    ):
        self.pill_r = float(radius)
        self.bg_color = bg_color
        self.border_color = border_color
        self.card_w = 600.0
        self.card_h = 85.0

        self.canvas = tk.Canvas(
            parent,
            width=600,
            height=85,
            bg=parent_bg,
            highlightthickness=0,
            bd=0,
        )
        self.canvas.pack(fill="x", padx=14, pady=outer_pady)

        self.inner = tk.Frame(self.canvas, bg=bg_color, bd=0, highlightthickness=0)
        self.win_id = self.canvas.create_window(
            padx,
            pady,
            window=self.inner,
            anchor="nw",
            width=570,
        )
        self.padx = padx
        self.pady = pady

        self.canvas.bind("<Configure>", self._on_canvas_resize)
        self.inner.bind("<Configure>", self._on_inner_resize)

    def _on_canvas_resize(self, event):
        if event.width > 20:
            self.card_w = float(event.width)
            inner_w = max(80, int(event.width - self.padx * 2))
            self.canvas.itemconfigure(self.win_id, width=inner_w)
            self._redraw()

    def _on_inner_resize(self, event):
        if event.height > 10:
            needed_h = float(event.height + self.pady * 2)
            if abs(needed_h - self.card_h) > 1.0:
                self.card_h = needed_h
                self.canvas.configure(height=int(needed_h))
                self._redraw()

    def _redraw(self):
        self.canvas.delete("card_bg")
        w, h = self.card_w, self.card_h
        if w <= 8 or h <= 8:
            return
        r = min(self.pill_r, (w - 4) / 2.0, (h - 4) / 2.0)
        create_rounded_rect(
            self.canvas,
            2,
            2,
            w - 2,
            h - 2,
            r=r,
            fill=self.bg_color,
            outline=self.border_color,
            width=1.5,
            tags="card_bg",
        )
        self.canvas.tag_lower("card_bg")


class SoftPillField:
    """圆润胶囊形输入框/列表外框"""
    def __init__(
        self,
        parent,
        height=28,
        radius=14,
        bg_color="#F8FAFC",
        border_color="#CBD5E1",
        parent_bg="#FFFFFF",
        padx=10,
        pady=3,
    ):
        self.pill_r = float(radius)
        self.bg_color = bg_color
        self.border_color = border_color
        self.field_w = 300.0
        self.field_h = float(height)
        self.padx = padx
        self.pady = pady

        self.canvas = tk.Canvas(
            parent,
            width=300,
            height=int(height),
            bg=parent_bg,
            highlightthickness=0,
            bd=0,
        )
        self.inner = tk.Frame(self.canvas, bg=bg_color, bd=0, highlightthickness=0)
        self.win_id = self.canvas.create_window(
            padx,
            pady,
            window=self.inner,
            anchor="nw",
            width=280,
            height=max(14, int(height - pady * 2)),
        )
        self.canvas.bind("<Configure>", self._on_resize)
        self._redraw()

    def _on_resize(self, event):
        if event.width > 12 and event.height > 8:
            self.field_w = float(event.width)
            self.field_h = float(event.height)
            self.canvas.itemconfigure(
                self.win_id,
                width=max(40, int(event.width - self.padx * 2)),
                height=max(14, int(event.height - self.pady * 2)),
            )
            self._redraw()

    def _redraw(self):
        self.canvas.delete("field_bg")
        w, h = self.field_w, self.field_h
        if w <= 6 or h <= 6:
            return
        r = min(self.pill_r, (w - 3) / 2.0, (h - 3) / 2.0)
        create_rounded_rect(
            self.canvas,
            1.5,
            1.5,
            w - 1.5,
            h - 1.5,
            r=r,
            fill=self.bg_color,
            outline=self.border_color,
            width=1.2,
            tags="field_bg",
        )
        self.canvas.tag_lower("field_bg")


class SoftPillProgressBar(tk.Canvas):
    """圆润胶囊形进度条"""
    def __init__(
        self,
        parent,
        height=12,
        track_color="#DCEAFF",
        fill_color="#2563EB",
        parent_bg="#F1F6FD",
    ):
        super().__init__(
            parent,
            width=580,
            height=int(height),
            bg=parent_bg,
            highlightthickness=0,
            bd=0,
        )
        self.bar_w = 580.0
        self.bar_h = float(height)
        self.track_color = track_color
        self.fill_color = fill_color
        self._max = 100.0
        self._val = 0.0

        self.bind("<Configure>", self._on_resize)
        self._redraw()

    def configure(self, **kwargs):
        if "maximum" in kwargs and kwargs["maximum"] is not None:
            self._max = max(1.0, float(kwargs["maximum"]))
        if "value" in kwargs and kwargs["value"] is not None:
            self._val = max(0.0, float(kwargs["value"]))
        super().configure(**kwargs)
        self._redraw()

    def _on_resize(self, event):
        if event.width > 12 and event.height > 6:
            self.bar_w = float(event.width)
            self.bar_h = float(event.height)
            self._redraw()

    def _redraw(self):
        self.delete("all")
        w, h = self.bar_w, self.bar_h
        if w <= 6 or h <= 4:
            return
        r = h / 2.0
        create_rounded_rect(self, 0, 0, w, h, r=r, fill=self.track_color, outline="", width=0)

        ratio = max(0.0, min(1.0, self._val / self._max))
        if ratio > 0.0:
            fill_w = max(h, w * ratio)
            create_rounded_rect(self, 0, 0, fill_w, h, r=r, fill=self.fill_color, outline="", width=0)


class SoftPillButton(tk.Canvas):
    """圆润胶囊按钮：支持悬停变色、按压微缩回弹与轻柔波动"""
    def __init__(
        self,
        parent,
        text,
        command=None,
        width=104,
        height=28,
        radius=14,
        bg_color="#EBF3FF",
        hover_color="#DCEAFF",
        fg_color="#1E3A8A",
        wave_color="#93C5FD",
        font=("Microsoft YaHei UI", 9, "bold"),
        parent_bg="#FFFFFF",
    ):
        super().__init__(
            parent,
            width=int(width),
            height=int(height),
            bg=parent_bg,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )
        self.btn_text = text
        self.btn_command = command
        self.pill_w = float(width)
        self.pill_h = float(height)
        self.pill_r = float(radius)
        self.bg_color = bg_color
        self.hover_color = hover_color
        self.fg_color = fg_color
        self.wave_color = wave_color
        self.btn_font = font
        self.btn_enabled = True
        self._hovered = False
        self._pressed = False
        self._wave_r = 0.0
        self._wave_job = None

        self.bind("<Configure>", self._on_resize)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self._redraw()

    def set_enabled(self, enabled):
        self.btn_enabled = bool(enabled)
        super().configure(cursor="hand2" if self.btn_enabled else "arrow")
        self._redraw()

    def set_width(self, new_width):
        if new_width and new_width > 10:
            self.pill_w = float(new_width)
            super().configure(width=int(new_width))
            self._redraw()

    def _on_resize(self, event):
        if event.width > 8 and event.height > 8:
            self.pill_w = float(event.width)
            self.pill_h = float(event.height)
            self._redraw()

    def _on_enter(self, _e):
        if self.btn_enabled:
            self._hovered = True
            self._redraw()

    def _on_leave(self, _e):
        self._hovered = False
        self._pressed = False
        self._redraw()

    def _on_press(self, _e):
        if not self.btn_enabled:
            return
        self._pressed = True
        self._wave_r = 6.0
        self._start_wave()
        self._redraw()

    def _on_release(self, event):
        if not self.btn_enabled:
            return
        was_pressed = self._pressed
        self._pressed = False
        self._redraw()
        inside = (0 <= event.x <= self.pill_w) and (0 <= event.y <= self.pill_h)
        if was_pressed and inside and callable(self.btn_command):
            self.after(55, self.btn_command)

    def _start_wave(self):
        if self._wave_job is not None:
            try:
                self.after_cancel(self._wave_job)
            except Exception:
                pass
            self._wave_job = None

        max_r = max(self.pill_w, self.pill_h) * 0.65

        def step():
            self._wave_r += 5.5
            self._redraw()
            if self._wave_r < max_r:
                self._wave_job = self.after(18, step)
            else:
                self._wave_r = 0.0
                self._wave_job = None
                self._redraw()

        self._wave_job = self.after(18, step)

    def _redraw(self):
        self.delete("all")
        w, h = self.pill_w, self.pill_h
        if w <= 4 or h <= 4:
            return

        if not self.btn_enabled:
            fill = "#93C5FD"
            fg = "#FFFFFF"
            inset = 1.0
        else:
            fill = self.hover_color if self._hovered else self.bg_color
            fg = self.fg_color
            inset = 2.2 if self._pressed else 1.0

        x1, y1 = inset, inset
        x2, y2 = max(x1 + 4, w - inset), max(y1 + 4, h - inset)
        r = min(self.pill_r, (x2 - x1) / 2.0, (y2 - y1) / 2.0)

        create_rounded_rect(self, x1, y1, x2, y2, r=r, fill=fill, outline="", width=0)

        if self._wave_r > 0:
            create_rounded_rect(
                self,
                max(x1 + 1, w / 2.0 - self._wave_r * 1.4),
                max(y1 + 1, h / 2.0 - self._wave_r * 0.7),
                min(x2 - 1, w / 2.0 + self._wave_r * 1.4),
                min(y2 - 1, h / 2.0 + self._wave_r * 0.7),
                r=r,
                fill="",
                outline=self.wave_color,
                width=1.5,
            )

        self.create_text(w / 2.0, h / 2.0, text=self.btn_text, fill=fg, font=self.btn_font)


class SoftPillBanner(tk.Canvas):
    """圆润胶囊形提示框，支持悬停高亮与自适应宽度"""
    def __init__(
        self,
        parent,
        main_text,
        height=44,
        radius=22,
        bg_color="#EFF6FF",
        hover_color="#DBEAFE",
        border_color="#93C5FD",
        main_fg="#1E40AF",
        parent_bg="#FFFFFF",
    ):
        super().__init__(
            parent,
            width=540,
            height=int(height),
            bg=parent_bg,
            highlightthickness=0,
            bd=0,
        )
        self.banner_main = main_text
        self.pill_w = 540.0
        self.pill_h = float(height)
        self.pill_r = float(radius)
        self.bg_color = bg_color
        self.hover_color = hover_color
        self.border_color = border_color
        self.main_fg = main_fg
        self._hovered = False

        self.bind("<Configure>", self._on_resize)
        self._redraw()

    def set_hovered(self, active):
        self._hovered = bool(active)
        self._redraw()

    def _on_resize(self, event):
        if event.width > 12 and event.height > 12:
            self.pill_w = float(event.width)
            self.pill_h = float(event.height)
            self._redraw()

    def _redraw(self):
        self.delete("all")
        w, h = self.pill_w, self.pill_h
        if w <= 8 or h <= 8:
            return
        fill = self.hover_color if self._hovered else self.bg_color
        r = min(self.pill_r, (w - 4) / 2.0, (h - 4) / 2.0)
        create_rounded_rect(
            self,
            2,
            2,
            w - 2,
            h - 2,
            r=r,
            fill=fill,
            outline=self.border_color,
            width=1.5,
        )
        self.create_text(
            w / 2.0,
            h / 2.0,
            text=self.banner_main,
            fill=self.main_fg,
            font=("Microsoft YaHei UI", 9, "bold"),
        )


class LoadingWindow:
    """无四角尖锐感、带连贯绿色进度条的启动加载窗"""
    TRANSPARENT_KEY = "#FF00FF"

    def __init__(self, root):
        self.root = root
        self.win = tk.Toplevel(root)
        self.win.title(SPLASH_DISPLAY_NAME)
        self.win.geometry("420x168")
        self.win.resizable(False, False)
        self.win.overrideredirect(True)
        self.win.configure(bg=self.TRANSPARENT_KEY)
        try:
            self.win.wm_attributes("-transparentcolor", self.TRANSPARENT_KEY)
        except Exception:
            pass
        self._center()

        style = ttk.Style(self.win)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(
            "SplashGreen.Horizontal.TProgressbar",
            troughcolor="#E5E7EB",
            background="#16A34A",
            thickness=14,
        )

        canvas = tk.Canvas(
            self.win,
            width=420,
            height=168,
            bg=self.TRANSPARENT_KEY,
            highlightthickness=0,
            bd=0,
        )
        canvas.pack(fill="both", expand=True)

        create_rounded_rect(
            canvas,
            2,
            2,
            418,
            166,
            r=24,
            fill="#FFFFFF",
            outline="#2563EB",
            width=2,
        )

        canvas.create_text(
            210,
            38,
            text=SPLASH_DISPLAY_NAME,
            fill="#0F172A",
            font=("Microsoft YaHei UI", 12, "bold"),
        )
        self.status_var = tk.StringVar(value="正在初始化程序环境...")
        self.status_id = canvas.create_text(
            210,
            66,
            text=self.status_var.get(),
            fill="#475569",
            font=("Microsoft YaHei UI", 9),
        )

        self.bar = ttk.Progressbar(
            canvas,
            style="SplashGreen.Horizontal.TProgressbar",
            mode="determinate",
            maximum=100,
            length=340,
        )
        canvas.create_window(210, 102, window=self.bar)

        canvas.create_text(
            210,
            136,
            text="首次加载 AI 抠图组件可能需要几十秒，请勿关闭",
            fill="#94A3B8",
            font=("Microsoft YaHei UI", 8),
        )
        self._canvas = canvas

    def _center(self):
        self.win.update_idletasks()
        sw = self.win.winfo_screenwidth()
        sh = self.win.winfo_screenheight()
        x = (sw - 420) // 2
        y = (sh - 168) // 2
        self.win.geometry(f"420x168+{x}+{y}")

    def update_progress(self, value, text=None):
        try:
            if self.win.winfo_exists():
                self.bar["value"] = max(0.0, min(100.0, float(value)))
                if text:
                    self.status_var.set(text)
                    self._canvas.itemconfigure(self.status_id, text=text)
                self.win.update_idletasks()
        except Exception:
            pass

    def close(self):
        try:
            if self.win.winfo_exists():
                self.win.destroy()
        except Exception:
            pass


class App:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_DISPLAY_NAME)
        self.root.geometry("640x670")
        self.root.minsize(580, 630)
        self.root.configure(bg=THEME["bg"])

        default_output_path = str(Path.home() / "Desktop" / "证件照换底色结果")
        cpu_count = os.cpu_count() or 4
        default_threads = max(2, min(4, cpu_count // 2))

        self.msgq = queue.Queue()
        self.input_paths = []
        self._images = []
        self._is_downloading_model = False
        self.output_dir = tk.StringVar(value=default_output_path)
        self.model_label_var = tk.StringVar(value="人像发丝最佳 (birefnet-portrait)")
        self.color = tk.StringVar(value=PRESETS["蓝色（常用）"])
        self.output_format = tk.StringVar(value="JPG")
        self.max_kb = tk.IntVar(value=50)
        self.thread_count = tk.IntVar(value=default_threads)
        self.fine_edge = tk.BooleanVar(value=True)
        self.status = tk.StringVar(value="拖入文件夹确认后，再点击下方“开始批量处理”")

        self._setup_styles()
        try:
            self._build()
        except Exception:
            messagebox.showerror("界面构建错误", traceback.format_exc())
        self._poll()

    def _setup_styles(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass

    def _make_pill_card(self, parent, radius=24, outer_pady=(0, 6)):
        card = SoftPillCard(
            parent,
            radius=radius,
            bg_color=THEME["card"],
            border_color=THEME["border"],
            parent_bg=THEME["bg"],
            padx=15,
            pady=8,
            outer_pady=outer_pady,
        )
        return card.canvas, card.inner

    def _make_sec_btn(self, parent, text, command, width=98, height=28):
        return SoftPillButton(
            parent,
            text=text,
            command=command,
            width=width,
            height=height,
            radius=height / 2.0,
            bg_color=THEME["btn_sec"],
            hover_color=THEME["btn_sec_hover"],
            fg_color=THEME["btn_sec_fg"],
            wave_color="#93C5FD",
            font=("Microsoft YaHei UI", 9, "bold"),
            parent_bg=THEME["card"],
        )

    def _parse_dnd_paths(self, raw_data):
        """解析拖放进来的路径字符串（兼容带空格花括号路径 {D:\\My Photos}）"""
        if not raw_data:
            return []
        items = []
        if isinstance(raw_data, (list, tuple)):
            items = list(raw_data)
        elif isinstance(raw_data, str):
            try:
                split_items = list(self.root.tk.splitlist(raw_data))
                if split_items:
                    items = split_items
            except Exception:
                items = []
            if not items:
                s = raw_data.strip()
                parts, cur, in_brace = [], [], False
                for ch in s:
                    if ch == "{":
                        in_brace = True
                    elif ch == "}":
                        in_brace = False
                        parts.append("".join(cur))
                        cur = []
                    elif ch == " " and not in_brace:
                        if cur:
                            parts.append("".join(cur))
                            cur = []
                    else:
                        cur.append(ch)
                if cur:
                    parts.append("".join(cur))
                items = parts
        return [str(p).strip() for p in items if p and str(p).strip()]

    def _collect_all_children(self, widget):
        result = [widget]
        for child in widget.winfo_children():
            result.extend(self._collect_all_children(child))
        return result

    def _refresh_list_view(self, images):
        self.listbox.delete(0, tk.END)
        for p in images[:1000]:
            self.listbox.insert(tk.END, str(p))

    def _enable_full_drop_zone(self, root_container, banner):
        """拖入文件夹/图片后先扫描并弹窗确认，确认后留在列表，需手动点击“开始批量处理”"""
        if not HAS_DND or DND_FILES is None:
            return

        def _set_hover(active):
            banner.set_hovered(active)

        def _on_drop(event):
            _set_hover(False)
            paths = self._parse_dnd_paths(getattr(event, "data", ""))
            valid = [p for p in paths if Path(p).exists()]
            if not valid:
                messagebox.showwarning("提示", "未检测到有效的文件夹或图片路径。")
                return

            prev_paths = list(self.input_paths)
            prev_images = list(self._images)
            prev_status = self.status.get()

            candidate_paths = prev_paths + valid
            candidate_images = collect_images(candidate_paths)
            if not candidate_images:
                messagebox.showinfo("提示", "拖入的路径中没有找到支持的图片格式。")
                return

            preview_names = "、".join(Path(p).name for p in valid[:3])
            if len(valid) > 3:
                preview_names += " 等"

            confirmed = messagebox.askyesno(
                "确认导入",
                f"检测到拖入：{preview_names}\n共扫描到 {len(candidate_images)} 张待处理图片。\n\n是否确认加入列表？（加入后需手动点击“开始批量处理”）",
            )
            if not confirmed:
                self.input_paths = prev_paths
                self._images = prev_images
                self._refresh_list_view(prev_images)
                self.status.set(prev_status)
                return

            self.input_paths = candidate_paths
            self._images = candidate_images
            self._refresh_list_view(candidate_images)
            self.status.set(f"已确认导入 {len(candidate_images)} 张图片，请点击“开始批量处理”")

        for w in self._collect_all_children(root_container):
            try:
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<DragEnter>>", lambda _e: _set_hover(True))
                w.dnd_bind("<<DragLeave>>", lambda _e: _set_hover(False))
                w.dnd_bind("<<Drop>>", _on_drop)
            except Exception:
                pass

    def _build(self):
        header = tk.Frame(self.root, bg=THEME["bg"])
        header.pack(fill="x", padx=14, pady=(8, 2))
        tk.Label(
            header,
            text=APP_DISPLAY_NAME,
            bg=THEME["bg"],
            fg=THEME["text"],
            font=("Microsoft YaHei UI", 14, "bold"),
        ).pack(anchor="w")
        tk.Frame(header, height=6, bg=THEME["bg"], bd=0, highlightthickness=0).pack(fill="x")

        import_outer, import_card = self._make_pill_card(self.root, radius=24)

        main_hint = (
            "将整个文件夹拖入此处（整块区域任意位置），确认后手动点击开始"
            if HAS_DND
            else "点击下方“添加照片”或“添加文件夹”选择待处理图片"
        )
        drop_banner = SoftPillBanner(
            import_card,
            main_text=main_hint,
            height=42,
            radius=21,
            bg_color=THEME["drop_bg"],
            hover_color=THEME["drop_hover"],
            border_color=THEME["drop_border"],
            main_fg=THEME["drop_fg"],
            parent_bg=THEME["card"],
        )
        drop_banner.pack(fill="x", pady=(0, 7))

        btn_row = tk.Frame(import_card, bg=THEME["card"])
        btn_row.pack(fill="x", pady=(0, 7))
        self._make_sec_btn(btn_row, "添加照片", self.add_files, width=96, height=27).pack(side="left", padx=(0, 8))
        self._make_sec_btn(btn_row, "添加文件夹", self.add_folder, width=108, height=27).pack(side="left")
        self._make_sec_btn(btn_row, "清空", self.clear, width=76, height=27).pack(side="right")

        list_pill = SoftPillField(
            import_card,
            height=92,
            radius=16,
            bg_color=THEME["list_bg"],
            border_color=THEME["border"],
            parent_bg=THEME["card"],
            padx=10,
            pady=4,
        )
        list_pill.canvas.pack(fill="x")
        self.listbox = tk.Listbox(
            list_pill.inner,
            height=2,
            bg=THEME["list_bg"],
            fg=THEME["text"],
            selectbackground=THEME["accent"],
            selectforeground="#FFFFFF",
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            font=("Microsoft YaHei UI", 9),
        )
        self.listbox.pack(fill="both", expand=True)

        self._enable_full_drop_zone(import_outer, drop_banner)

        _, set_card = self._make_pill_card(self.root, radius=22)
        tk.Label(
            set_card,
            text="处理参数设置",
            bg=THEME["card"],
            fg=THEME["text"],
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(anchor="w", pady=(0, 5))

        r1 = tk.Frame(set_card, bg=THEME["card"])
        r1.pack(fill="x", pady=(0, 6))
        tk.Label(r1, text="背景颜色：", bg=THEME["card"], fg=THEME["text"], font=("Microsoft YaHei UI", 9)).pack(side="left")
        self.preset = ttk.Combobox(
            r1,
            values=list(PRESETS.keys()) + ["自定义颜色"],
            state="readonly",
            width=13,
            font=("Microsoft YaHei UI", 9),
        )
        self.preset.set("蓝色（常用）")
        self.preset.pack(side="left", padx=(4, 6))
        self.preset.bind("<<ComboboxSelected>>", self._preset_changed)

        self.swatch = tk.Label(
            r1,
            text="   ",
            bg=self.color.get(),
            relief="flat",
            bd=0,
        )
        self.swatch.pack(side="left", padx=(0, 4))

        self.hex_label = tk.Label(
            r1,
            text=self.color.get(),
            bg=THEME["card"],
            fg=THEME["text"],
            font=("Microsoft YaHei UI", 9),
        )
        self.hex_label.pack(side="left", padx=(0, 6))
        self._make_sec_btn(r1, "自定义颜色", self.choose_color, width=96, height=26).pack(side="right")

        r_model = tk.Frame(set_card, bg=THEME["card"])
        r_model.pack(fill="x", pady=(0, 6))
        tk.Label(r_model, text="抠图模型：", bg=THEME["card"], fg=THEME["text"], font=("Microsoft YaHei UI", 9)).pack(side="left")
        self.model_combo = ttk.Combobox(
            r_model,
            textvariable=self.model_label_var,
            values=list(MODEL_OPTIONS.keys()),
            state="readonly",
            width=31,
            font=("Microsoft YaHei UI", 9),
        )
        self.model_combo.pack(side="left", padx=(4, 8))
        self.download_model_btn = self._make_sec_btn(
            r_model,
            "下载模型",
            self.download_selected_model,
            width=96,
            height=26,
        )
        self.download_model_btn.pack(side="right")

        r2 = tk.Frame(set_card, bg=THEME["card"])
        r2.pack(fill="x", pady=(0, 6))
        tk.Label(r2, text="输出格式：", bg=THEME["card"], fg=THEME["text"], font=("Microsoft YaHei UI", 9)).pack(side="left")
        self.format_combo = ttk.Combobox(
            r2,
            textvariable=self.output_format,
            values=["JPG", "PNG"],
            state="readonly",
            width=6,
            font=("Microsoft YaHei UI", 9),
        )
        self.format_combo.pack(side="left", padx=(4, 10))

        tk.Label(r2, text="大小上限：", bg=THEME["card"], fg=THEME["text"], font=("Microsoft YaHei UI", 9)).pack(side="left")
        ttk.Spinbox(
            r2,
            from_=10,
            to=2000,
            textvariable=self.max_kb,
            width=6,
            font=("Microsoft YaHei UI", 9),
        ).pack(side="left", padx=(4, 3))
        tk.Label(r2, text="KB", bg=THEME["card"], fg=THEME["muted"], font=("Microsoft YaHei UI", 9)).pack(side="left", padx=(0, 10))

        tk.Label(r2, text="并发线程：", bg=THEME["card"], fg=THEME["text"], font=("Microsoft YaHei UI", 9)).pack(side="left")
        ttk.Spinbox(
            r2,
            from_=1,
            to=8,
            textvariable=self.thread_count,
            width=4,
            font=("Microsoft YaHei UI", 9),
        ).pack(side="left", padx=(4, 3))
        tk.Label(r2, text="线程", bg=THEME["card"], fg=THEME["muted"], font=("Microsoft YaHei UI", 9)).pack(side="left")

        r_fine = tk.Frame(set_card, bg=THEME["card"])
        r_fine.pack(fill="x")
        tk.Checkbutton(
            r_fine,
            text="精细边缘模式（加强头发丝分离与边缘去白边，耗时更长、CPU 占用更高）",
            variable=self.fine_edge,
            bg=THEME["card"],
            fg=THEME["text"],
            selectcolor="#EFF6FF",
            activebackground=THEME["card"],
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(anchor="w")

        _, dir_card = self._make_pill_card(self.root, radius=22, outer_pady=(0, 2))
        tk.Label(
            dir_card,
            text="输出文件夹",
            bg=THEME["card"],
            fg=THEME["text"],
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(anchor="w", pady=(0, 3))

        r3 = tk.Frame(dir_card, bg=THEME["card"])
        r3.pack(fill="x")
        out_pill = SoftPillField(
            r3,
            height=28,
            radius=14,
            bg_color="#F8FAFC",
            border_color="#CBD5E1",
            parent_bg=THEME["card"],
            padx=10,
            pady=3,
        )
        out_pill.canvas.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.out_entry = tk.Entry(
            out_pill.inner,
            textvariable=self.output_dir,
            font=("Microsoft YaHei UI", 9),
            bg="#F8FAFC",
            fg=THEME["text"],
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
        )
        self.out_entry.pack(fill="both", expand=True)
        self._make_sec_btn(r3, "原目录", self.use_source_dir_as_output, width=72, height=28).pack(side="left", padx=(0, 6))
        self._make_sec_btn(r3, "浏览", self.choose_output, width=68, height=28).pack(side="right")

        bottom = tk.Frame(self.root, bg=THEME["bg"])
        bottom.pack(fill="x", padx=14, pady=(0, 10))

        tk.Frame(bottom, height=20, bg=THEME["bg"], bd=0, highlightthickness=0).pack(fill="x")
        tk.Frame(bottom, height=18, bg=THEME["bg"], bd=0, highlightthickness=0).pack(fill="x")

        self.progress = SoftPillProgressBar(
            bottom,
            height=12,
            track_color="#DCEAFF",
            fill_color=THEME["accent"],
            parent_bg=THEME["bg"],
        )
        self.progress.pack(fill="x", pady=(0, 4))
        tk.Label(
            bottom,
            textvariable=self.status,
            bg=THEME["bg"],
            fg=THEME["muted"],
            font=("Microsoft YaHei UI", 9),
        ).pack(anchor="w", pady=(0, 6))

        self.start_btn = SoftPillButton(
            bottom,
            text="开始批量处理",
            command=self.start,
            width=610,
            height=38,
            radius=19,
            bg_color=THEME["accent"],
            hover_color=THEME["accent_hover"],
            fg_color="#FFFFFF",
            wave_color="#BFDBFE",
            font=("Microsoft YaHei UI", 10, "bold"),
            parent_bg=THEME["bg"],
        )
        self.start_btn.pack(fill="x")

        def _sync_start_width(event):
            if event.width > 20:
                self.start_btn.set_width(event.width)

        bottom.bind("<Configure>", _sync_start_width)

    def _add_paths(self, paths):
        valid = [p for p in paths if Path(p).exists()]
        self.input_paths.extend(valid)
        images = collect_images(self.input_paths)
        self._refresh_list_view(images)
        self.status.set(f"已找到 {len(images)} 张图片，请点击“开始批量处理”")
        self._images = images

    def use_source_dir_as_output(self):
        if self._images:
            src_dir = str(Path(self._images[0]).resolve().parent)
            self.output_dir.set(src_dir)
            self.status.set(f"输出目录已设为原目录：{src_dir}")
        elif self.input_paths:
            p = Path(self.input_paths[0]).resolve()
            src_dir = str(p if p.is_dir() else p.parent)
            self.output_dir.set(src_dir)
            self.status.set(f"输出目录已设为原目录：{src_dir}")
        else:
            messagebox.showinfo("提示", "请先添加照片或文件夹，再使用“原目录”。")

    def add_files(self):
        paths = filedialog.askopenfilenames(
            title="选择照片",
            filetypes=[("图片", "*.jpg *.jpeg *.png *.bmp *.webp *.tif *.tiff"), ("所有文件", "*.*")],
        )
        if paths:
            self._add_paths(paths)

    def add_folder(self):
        p = filedialog.askdirectory(title="选择包含照片的总文件夹")
        if p:
            self._add_paths([p])

    def clear(self):
        self.input_paths = []
        self._images = []
        self.listbox.delete(0, tk.END)
        self.status.set("列表已清空")

    def choose_output(self):
        p = filedialog.askdirectory(title="选择输出文件夹")
        if p:
            self.output_dir.set(p)

    def _preset_changed(self, _=None):
        name = self.preset.get()
        if name in PRESETS:
            self._set_color(PRESETS[name])
        elif name == "自定义颜色":
            self.choose_color()

    def choose_color(self):
        result = colorchooser.askcolor(color=self.color.get(), title="选择证件照背景颜色")
        if result and result[1]:
            self._set_color(result[1])

    def _set_color(self, value):
        self.color.set(value.upper())
        self.swatch.configure(bg=value)
        self.hex_label.configure(text=value.upper())
        if value.upper() not in {v.upper() for v in PRESETS.values()}:
            self.preset.set("自定义颜色")

    def _selected_model_name(self):
        label = self.model_label_var.get()
        return MODEL_OPTIONS.get(label, "birefnet-portrait")

    def download_selected_model(self):
        """将当前下拉框选中的模型下载到软件自身目录的 .u2net 文件夹中"""
        if self._is_downloading_model:
            messagebox.showinfo("提示", "正在下载模型，请稍候。")
            return
        if new_session is None:
            messagebox.showerror("下载失败", "缺少 rembg 组件，请检查打包是否完整。")
            return

        model_name = self._selected_model_name()
        os.environ["U2NET_HOME"] = str(APP_MODEL_DIR)
        APP_MODEL_DIR.mkdir(parents=True, exist_ok=True)

        self._is_downloading_model = True
        self.download_model_btn.set_enabled(False)
        self.start_btn.set_enabled(False)
        self.status.set(f"正在下载模型 [{model_name}] 到软件目录 ({APP_MODEL_DIR}) ……")

        def _dl_worker():
            try:
                new_session(model_name)
                self.msgq.put(("model_dl_done", True, model_name, str(APP_MODEL_DIR)))
            except Exception as e:
                self.msgq.put(("model_dl_done", False, model_name, str(e)))

        threading.Thread(target=_dl_worker, daemon=True).start()

    def start(self):
        images = getattr(self, "_images", [])
        if not images:
            messagebox.showwarning("提示", "请先添加照片或文件夹。")
            return
        out_str = self.output_dir.get().strip()
        if not out_str:
            messagebox.showwarning("提示", "请填写输出文件夹路径。")
            return
        out = Path(out_str).expanduser()
        if out.exists() and not out.is_dir():
            messagebox.showerror("输出目录错误", f"输出路径指向的是文件：{out}")
            return
        try:
            out.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            messagebox.showerror("输出目录错误", str(e))
            return
        try:
            rgb = hex_to_rgb(self.color.get())
        except Exception as e:
            messagebox.showerror("颜色设置错误", str(e))
            return

        try:
            workers = max(1, min(8, int(self.thread_count.get())))
        except Exception:
            workers = 2

        os.environ["U2NET_HOME"] = str(APP_MODEL_DIR)
        APP_MODEL_DIR.mkdir(parents=True, exist_ok=True)

        model_name = self._selected_model_name()
        fmt = (self.output_format.get() or "JPG").upper()
        fine_edge = bool(self.fine_edge.get())
        self.start_btn.set_enabled(False)
        self.download_model_btn.set_enabled(False)
        self.progress.configure(maximum=len(images), value=0)
        threading.Thread(
            target=self._worker,
            args=(images, out, rgb, fmt, self.max_kb.get(), model_name, fine_edge, workers),
            daemon=True,
        ).start()

    def _process_single_image(self, src, session, out, rgb, fmt, max_kb, fine_edge, root_for, ext):
        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im).convert("RGBA")
            cut = remove(im, session=session)
            if not isinstance(cut, Image.Image):
                cut = Image.open(io.BytesIO(cut)).convert("RGBA")
            else:
                cut = cut.convert("RGBA")

        r_ch, g_ch, b_ch, a_ch = cut.split()
        flt = ImageFilter
        if flt is None:
            from PIL import ImageFilter as _flt
            flt = _flt

        if fine_edge:
            a_clean = a_ch.filter(flt.MinFilter(size=3))
            a_clean = a_clean.filter(flt.GaussianBlur(radius=0.5))
        else:
            a_clean = a_ch.filter(flt.MinFilter(size=3))

        a_px = a_clean.load()
        r_px = r_ch.load()
        g_px = g_ch.load()
        b_px = b_ch.load()
        w, h = cut.size
        for y in range(h):
            for x in range(w):
                alpha_val = a_px[x, y]
                if 0 < alpha_val < 238:
                    if r_px[x, y] > 190 and g_px[x, y] > 190 and b_px[x, y] > 190:
                        blend = 0.55 if fine_edge else 0.35
                        r_px[x, y] = int(r_px[x, y] * (1 - blend) + rgb[0] * blend)
                        g_px[x, y] = int(g_px[x, y] * (1 - blend) + rgb[1] * blend)
                        b_px[x, y] = int(b_px[x, y] * (1 - blend) + rgb[2] * blend)

        cut_cleaned = Image.merge("RGBA", (r_ch, g_ch, b_ch, a_clean))
        bg = Image.new("RGBA", cut_cleaned.size, (*rgb, 255))
        bg.alpha_composite(cut_cleaned)
        result = bg.convert("RGB")

        root = root_for[src]
        try:
            relative = src.relative_to(root)
        except ValueError:
            relative = Path(src.name)
        target_dir = out / (relative.parent if root.is_dir() else Path())
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / (src.stem + ext)
        if target.exists() and src.suffix.lower() != ext:
            target = target_dir / (src.stem + f"_converted{ext}")
        data = encode_image_under_limit(result, fmt, max_kb)
        target.write_bytes(data)

    def _worker(self, images, out, rgb, fmt, max_kb, model_name, fine_edge, workers):
        if remove is None or new_session is None:
            self.msgq.put(("fatal", "缺少 rembg 组件，请检查打包是否完整。"))
            return
        try:
            os.environ["U2NET_HOME"] = str(APP_MODEL_DIR)
            APP_MODEL_DIR.mkdir(parents=True, exist_ok=True)
            self.msgq.put(("status", f"正在加载模型 {model_name}（{workers} 线程并发）……"))
            session = new_session(model_name)
        except Exception as e:
            self.msgq.put((
                "fatal",
                f"AI 模型 [{model_name}] 加载失败：{e}\n若尚未下载该模型，请先在下拉框选中后点击“下载模型”。",
            ))
            return

        roots = [Path(p) for p in self.input_paths if Path(p).is_dir()]
        root_for = {}
        for f in images:
            candidates = [r for r in roots if r == f or r in f.parents]
            root_for[f] = max(candidates, key=lambda x: len(x.parts)) if candidates else f.parent

        ext = ".png" if fmt == "PNG" else ".jpg"
        effective_workers = min(workers, len(images))
        ok = 0
        done_count = 0
        errors = []
        lock = threading.Lock()

        with ThreadPoolExecutor(max_workers=effective_workers) as executor:
            future_map = {
                executor.submit(
                    self._process_single_image,
                    src,
                    session,
                    out,
                    rgb,
                    fmt,
                    max_kb,
                    fine_edge,
                    root_for,
                    ext,
                ): src
                for src in images
            }

            for fut in as_completed(future_map):
                src = future_map[fut]
                try:
                    fut.result()
                    with lock:
                        ok += 1
                        done_count += 1
                        cur_ok = ok
                        cur_done = done_count
                    self.msgq.put((
                        "progress",
                        cur_done,
                        len(images),
                        f"多线程处理中 {cur_done}/{len(images)}（成功 {cur_ok}）：{src.name}",
                    ))
                except Exception as e:
                    with lock:
                        errors.append(f"{src}: {e}")
                        done_count += 1
                        cur_done = done_count
                    self.msgq.put((
                        "progress",
                        cur_done,
                        len(images),
                        f"失败 {cur_done}/{len(images)}：{src.name}",
                    ))

        self.msgq.put(("done", ok, len(images), errors, str(out)))

    def _poll(self):
        try:
            while True:
                item = self.msgq.get_nowait()
                kind = item[0]
                if kind == "status":
                    self.status.set(item[1])
                elif kind == "model_dl_done":
                    _, ok_flag, m_name, info_str = item
                    self._is_downloading_model = False
                    self.download_model_btn.set_enabled(True)
                    self.start_btn.set_enabled(True)
                    if ok_flag:
                        self.status.set(f"模型 [{m_name}] 已下载并就绪（存放于软件目录 .u2net）")
                        messagebox.showinfo("下载完成", f"模型 [{m_name}] 已下载到：\n{info_str}")
                    else:
                        self.status.set(f"模型 [{m_name}] 下载失败")
                        messagebox.showerror("下载失败", f"模型 [{m_name}] 下载出错：\n{info_str}")
                elif kind == "progress":
                    _, v, total, msg = item
                    self.progress.configure(value=v)
                    self.status.set(msg)
                elif kind == "fatal":
                    self.start_btn.set_enabled(True)
                    self.download_model_btn.set_enabled(True)
                    messagebox.showerror("无法处理", item[1])
                elif kind == "done":
                    _, ok, total, errors, out = item
                    self.start_btn.set_enabled(True)
                    self.download_model_btn.set_enabled(True)
                    self.status.set(f"完成：成功 {ok} 张，失败 {len(errors)} 张")
                    msg = f"处理完成！\n成功：{ok}/{total} 张\n输出位置：{out}"
                    if errors:
                        msg += "\n\n部分失败原因（最多显示 5 条）：\n" + "\n".join(errors[:5])
                    messagebox.showinfo("处理完成", msg)
        except queue.Empty:
            pass
        self.root.after(150, self._poll)


def main():
    global Image, ImageOps, ImageFilter, remove, new_session

    try:
        if HAS_DND and TkinterDnD is not None:
            root = TkinterDnD.Tk()
        else:
            root = tk.Tk()
        root.withdraw()
        splash = LoadingWindow(root)
        load_q = queue.Queue()

        def load_deps():
            global Image, ImageOps, ImageFilter, remove, new_session
            try:
                os.environ["U2NET_HOME"] = str(APP_MODEL_DIR)
                load_q.put(("STAGE", 20.0, "正在准备图像解码组件 (Pillow)..."))
                from PIL import Image as _Image, ImageOps as _ImageOps, ImageFilter as _ImageFilter
                Image, ImageOps, ImageFilter = _Image, _ImageOps, _ImageFilter

                load_q.put(("STAGE", 50.0, "正在初始化 AI 抠图引擎 (rembg / onnxruntime)..."))
                from rembg import remove as _remove, new_session as _new_session
                remove, new_session = _remove, _new_session

                load_q.put(("DONE", 100.0, "加载完成，正在进入主界面..."))
            except Exception as e:
                load_q.put(("ERROR", 0.0, str(e)))

        def finish_load(err_msg=None):
            splash.close()
            if err_msg:
                messagebox.showerror("启动失败", f"组件加载出错：\n{err_msg}")
                root.destroy()
                return
            root.deiconify()
            App(root)

        state = {"current": 4.0, "target": 18.0, "finished": False, "err": None}

        def poll_load():
            try:
                while True:
                    kind, val, payload = load_q.get_nowait()
                    if kind == "DONE":
                        state["finished"] = True
                        state["target"] = 100.0
                        splash.update_progress(state["current"], payload)
                    elif kind == "ERROR":
                        state["err"] = payload
                        finish_load(payload)
                        return
                    else:
                        state["target"] = max(state["target"], float(val))
                        splash.update_progress(state["current"], payload)
            except queue.Empty:
                pass

            if state["finished"]:
                state["current"] = min(100.0, state["current"] + 6.0)
                splash.update_progress(state["current"])
                if state["current"] >= 100.0:
                    splash.update_progress(100.0, "加载完成，正在进入主界面...")
                    root.after(220, lambda: finish_load(None))
                    return
            else:
                cap = 96.0
                if state["current"] < state["target"]:
                    state["current"] = min(state["target"], state["current"] + 1.4)
                elif state["current"] < cap:
                    step_inc = 0.75 if state["current"] < 75.0 else 0.35
                    state["current"] = min(cap, state["current"] + step_inc)
                splash.update_progress(state["current"])

            if root.winfo_exists():
                root.after(70, poll_load)

        threading.Thread(target=load_deps, daemon=True).start()
        root.after(70, poll_load)
        root.mainloop()
    except Exception as e:
        try:
            messagebox.showerror("启动异常", f"程序启动出错：\n{e}")
        except Exception:
            pass


if __name__ == "__main__":
    main()