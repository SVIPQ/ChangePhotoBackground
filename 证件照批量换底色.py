# -*- coding: utf-8 -*-
"""
证件照批量换底色工具_Czq
功能：
1. 批量导入图片/文件夹（递归扫描子文件夹），支持顶部区域鼠标拖放文件夹/图片并确认；
2. 固定以当前程序所在根目录为基准，统一从 <软件根目录>\_internal\.u2net\models\<模型名> 读取与下载模型；
3. 支持“三模型智能融合引擎”统一开关（同时调用 birefnet-portrait + isnet-general-use + u2net 加权平衡互补），
   也支持一键检测/下载补全全部三个离线模型，并支持命令行 `--download-all-models` 供打包阶段预下载；
4. 列表每处理完成一张图片，对应条目后方实时显示绿色对号（✓）；
5. 输出目录根据导入文件夹名称（或导入图片的上一级文件夹名称）自动命名并存放到所选导出位置；
6. 支持导出 JPG / PNG、大小上限控制、精细边缘模式（尽量保留白色/浅色肩膀与衣料）与及时释放内存。
"""
import gc
import io
import math
import os
import queue
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox, ttk

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

MODEL_WEIGHT_EXTS = {".onnx", ".pth", ".pt", ".bin", ".safetensors"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}

PRESETS = {
    "蓝色（常用）": "#438EDB",
    "白色": "#FFFFFF",
    "红色": "#D9001B",
    "浅蓝色": "#ADD8E6",
    "灰色": "#D9D9D9",
}

# 三模型统一配置与融合平衡权重（发丝细节 + 边缘轮廓 + 整体结构互补）
ENSEMBLE_MODELS = [
    ("birefnet-portrait", 0.50),
    ("isnet-general-use", 0.30),
    ("u2net", 0.20),
]
ALL_MODEL_NAMES = [m[0] for m in ENSEMBLE_MODELS]
PRIMARY_FALLBACK_MODEL = "birefnet-portrait"

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
    "ok_green": "#16A34A",
    "err_red": "#DC2626",
}


def get_app_root_dir() -> Path:
    """获取当前软件所在根目录（兼容打包后 EXE 目录与源码目录）"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def get_u2net_home_dir() -> Path:
    """
    U2NET_HOME 固定为：<软件根目录>\_internal\.u2net
    rembg 内部会自动在此目录下拼接 models\<model_name>\，从而指向：
    <软件根目录>\_internal\.u2net\models\<model_name>\
    """
    return get_app_root_dir() / "_internal" / ".u2net"


def get_fixed_models_root() -> Path:
    """固定模型存放根目录：<软件根目录>\_internal\.u2net\models"""
    return get_u2net_home_dir() / "models"


def get_model_subdir(model_name: str) -> Path:
    """获取指定模型的专属目录：<软件根目录>\_internal\.u2net\models\<model_name>"""
    return get_fixed_models_root() / model_name


def _find_existing_model_file(model_name: str) -> Path | None:
    """
    在固定目录 <软件根目录>\_internal\.u2net\models\<model_name> 下查找模型权重文件；
    若误落在误多拼一层 models\models\<model_name> 或扁平 _internal\.u2net 下，自动归整过来。
    """
    models_root = get_fixed_models_root()
    model_dir = models_root / model_name
    model_dir.mkdir(parents=True, exist_ok=True)

    for f in model_dir.rglob("*"):
        if f.is_file() and f.suffix.lower() in MODEL_WEIGHT_EXTS:
            return f

    # 兼容之前误多拼一层 models 的目录：<软件根目录>\_internal\.u2net\models\models\<model_name>
    wrong_nested_dir = models_root / "models" / model_name
    if wrong_nested_dir.exists() and wrong_nested_dir.is_dir():
        for f in wrong_nested_dir.rglob("*"):
            if f.is_file() and f.suffix.lower() in MODEL_WEIGHT_EXTS:
                dst = model_dir / f.name
                if not dst.exists():
                    dst.write_bytes(f.read_bytes())
                return dst

    legacy_u2net = get_u2net_home_dir()
    if legacy_u2net.exists() and legacy_u2net.is_dir():
        key_token = model_name.lower().replace("-", "_")
        for f in legacy_u2net.iterdir():
            if f.is_file() and f.suffix.lower() in MODEL_WEIGHT_EXTS:
                fname_norm = f.stem.lower().replace("-", "_")
                if model_name.lower() in f.stem.lower() or key_token in fname_norm:
                    dst = model_dir / f.name
                    if not dst.exists():
                        dst.write_bytes(f.read_bytes())
                    return dst

    return None


def prepare_all_models_env() -> Path:
    """
    将 U2NET_HOME 固定设为 <软件根目录>\_internal\.u2net，
    并确保三个模型的专属子目录均已创建并归整好已有模型文件。
    """
    u2net_home = get_u2net_home_dir()
    models_root = get_fixed_models_root()
    u2net_home.mkdir(parents=True, exist_ok=True)
    models_root.mkdir(parents=True, exist_ok=True)
    os.environ["U2NET_HOME"] = str(u2net_home)

    for m_name in ALL_MODEL_NAMES:
        m_dir = get_model_subdir(m_name)
        m_dir.mkdir(parents=True, exist_ok=True)
        _find_existing_model_file(m_name)
    return models_root


def check_all_models_status() -> tuple[list[str], list[str]]:
    """返回 (已就绪模型列表, 缺失模型列表)"""
    prepare_all_models_env()
    ready, missing = [], []
    for m_name in ALL_MODEL_NAMES:
        if _find_existing_model_file(m_name) is not None:
            ready.append(m_name)
        else:
            missing.append(m_name)
    return ready, missing


APP_MODELS_ROOT = prepare_all_models_env()

Image = None
ImageOps = None
ImageFilter = None
ImageChops = None
remove = None
new_session = None


def hex_to_rgb(value):
    value = (value or "").strip().lstrip("#")
    if len(value) != 6:
        raise ValueError("颜色请使用 #RRGGBB 格式，例如 #438EDB")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


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


def resolve_output_root_for_image(src: Path, input_paths: list, base_export_dir: Path) -> tuple[Path, Path]:
    roots = [Path(p).resolve() for p in input_paths if Path(p).exists() and Path(p).is_dir()]
    src_resolved = src.resolve()
    matched_roots = [r for r in roots if r == src_resolved or r in src_resolved.parents]

    if matched_roots:
        chosen_root = max(matched_roots, key=lambda x: len(x.parts))
        folder_name = chosen_root.name or "输出照片"
        rel = src_resolved.relative_to(chosen_root)
        return base_export_dir / folder_name, rel

    parent_dir = src_resolved.parent
    folder_name = parent_dir.name or "输出照片"
    return base_export_dir / folder_name, Path(src_resolved.name)


def encode_image_under_limit(image, fmt, max_kb):
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
    def __init__(
        self,
        parent,
        radius=24,
        bg_color="#FFFFFF",
        border_color="#D8E4F3",
        parent_bg="#F1F6FD",
        padx=18,
        pady=8,
        outer_pady=(0, 6),
    ):
        self.pill_r = float(radius)
        self.bg_color = bg_color
        self.border_color = border_color
        self.card_w = 720.0
        self.card_h = 85.0

        self.canvas = tk.Canvas(
            parent,
            width=720,
            height=85,
            bg=parent_bg,
            highlightthickness=0,
            bd=0,
        )
        self.canvas.pack(fill="x", padx=16, pady=outer_pady)

        self.inner = tk.Frame(self.canvas, bg=bg_color, bd=0, highlightthickness=0)
        self.win_id = self.canvas.create_window(
            padx,
            pady,
            window=self.inner,
            anchor="nw",
            width=680,
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
        self.field_w = 360.0
        self.field_h = float(height)
        self.padx = padx
        self.pady = pady

        self.canvas = tk.Canvas(
            parent,
            width=360,
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
            width=340,
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
            width=720,
            height=int(height),
            bg=parent_bg,
            highlightthickness=0,
            bd=0,
        )
        self.bar_w = 720.0
        self.bar_h = float(height)
        self.track_color = track_color
        self.fill_color = fill_color
        self._max = 100.0
        self._val = 0.0

        self.bind("<Configure>", self._on_resize)
        self._redraw()

    def configure(self, **kwargs):
        if "maximum" in kwargs and kwargs["maximum"] is not None:
            self._max = max(1.0, float(kwargs.pop("maximum")))
        else:
            kwargs.pop("maximum", None)

        if "value" in kwargs and kwargs["value"] is not None:
            self._val = max(0.0, float(kwargs.pop("value")))
        else:
            kwargs.pop("value", None)

        if kwargs:
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
    def __init__(
        self,
        parent,
        text,
        command=None,
        width=118,
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
            self.after(30, self.btn_command)

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
            width=660,
            height=int(height),
            bg=parent_bg,
            highlightthickness=0,
            bd=0,
        )
        self.banner_main = main_text
        self.pill_w = 660.0
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
    TRANSPARENT_KEY = "#FF00FF"

    def __init__(self, root):
        self.root = root
        self.win = tk.Toplevel(root)
        self.win.title(SPLASH_DISPLAY_NAME)
        self.win.geometry("460x172")
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
            width=460,
            height=172,
            bg=self.TRANSPARENT_KEY,
            highlightthickness=0,
            bd=0,
        )
        canvas.pack(fill="both", expand=True)

        create_rounded_rect(
            canvas,
            2,
            2,
            458,
            170,
            r=24,
            fill="#FFFFFF",
            outline="#2563EB",
            width=2,
        )

        canvas.create_text(
            230,
            38,
            text=SPLASH_DISPLAY_NAME,
            fill="#0F172A",
            font=("Microsoft YaHei UI", 12, "bold"),
        )
        self.status_var = tk.StringVar(value="正在初始化程序环境...")
        self.status_id = canvas.create_text(
            230,
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
            length=380,
        )
        canvas.create_window(230, 102, window=self.bar)

        canvas.create_text(
            230,
            138,
            text="首次加载三模型 AI 抠图引擎可能需要几十秒，请勿关闭",
            fill="#94A3B8",
            font=("Microsoft YaHei UI", 8),
        )
        self._canvas = canvas

    def _center(self):
        self.win.update_idletasks()
        sw = self.win.winfo_screenwidth()
        sh = self.win.winfo_screenheight()
        x = max(0, (sw - 460) // 2)
        y = max(0, (sh - 172) // 2)
        self.win.geometry(f"460x172+{x}+{y}")

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


def center_window_on_screen(win, width: int, height: int):
    win.update_idletasks()
    sw = win.winfo_screenwidth()
    sh = win.winfo_screenheight()
    x = max(0, (sw - width) // 2)
    y = max(0, (sh - height) // 2)
    win.geometry(f"{width}x{height}+{x}+{y}")


class App:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_DISPLAY_NAME)
        win_w, win_h = 780, 690
        self.root.minsize(720, 640)
        self.root.configure(bg=THEME["bg"])
        center_window_on_screen(self.root, win_w, win_h)

        default_output_path = str(Path.home() / "Desktop")
        default_threads = 2

        self.msgq = queue.Queue()
        self.input_paths = []
        self._images = []
        self._img_index_map = {}
        self._img_states = {}
        self._is_downloading_model = False
        self._is_running = False

        self.output_dir = tk.StringVar(value=default_output_path)
        self.use_triple_ensemble = tk.BooleanVar(value=True)
        self.model_status_var = tk.StringVar(value=self._build_model_status_text())
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

    def _build_model_status_text(self) -> str:
        ready, missing = check_all_models_status()
        if not missing:
            return f"离线模型包已完整就绪（{len(ready)}/3）"
        return f"已就绪 {len(ready)}/3（缺失：{', '.join(missing)}）"

    def _refresh_model_status_label(self):
        txt = self._build_model_status_text()
        self.model_status_var.set(txt)
        _, missing = check_all_models_status()
        if hasattr(self, "model_status_lbl"):
            self.model_status_lbl.configure(fg=THEME["ok_green"] if not missing else THEME["err_red"])

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
            padx=18,
            pady=8,
            outer_pady=outer_pady,
        )
        return card.canvas, card.inner

    def _make_sec_btn(self, parent, text, command, width=116, height=28):
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

    def _format_list_row(self, p: Path, state: str) -> str:
        if state == "ok":
            return f"{p}   ✓"
        if state == "err":
            return f"{p}   ✗"
        return str(p)

    def _refresh_list_view(self, images):
        self.listbox.delete(0, tk.END)
        self._img_index_map = {}
        for idx, p in enumerate(images):
            key = str(p.resolve())
            self._img_index_map[key] = idx
            state = self._img_states.get(key, "pending")
            if idx < 2000:
                self.listbox.insert(tk.END, self._format_list_row(p, state))
                if state == "ok":
                    self.listbox.itemconfig(idx, fg=THEME["ok_green"])
                elif state == "err":
                    self.listbox.itemconfig(idx, fg=THEME["err_red"])
                else:
                    self.listbox.itemconfig(idx, fg=THEME["text"])

    def _mark_image_state(self, src: Path, state: str):
        key = str(src.resolve())
        self._img_states[key] = state
        idx = self._img_index_map.get(key)
        if idx is not None and idx < self.listbox.size():
            self.listbox.delete(idx)
            self.listbox.insert(idx, self._format_list_row(src, state))
            if state == "ok":
                self.listbox.itemconfig(idx, fg=THEME["ok_green"])
            elif state == "err":
                self.listbox.itemconfig(idx, fg=THEME["err_red"])

    def _enable_full_drop_zone(self, root_container, banner):
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

            self._set_images_list(candidate_paths, candidate_images)

        for w in self._collect_all_children(root_container):
            try:
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<DragEnter>>", lambda _e: _set_hover(True))
                w.dnd_bind("<<DragLeave>>", lambda _e: _set_hover(False))
                w.dnd_bind("<<Drop>>", _on_drop)
            except Exception:
                pass

    def _set_images_list(self, new_input_paths, images):
        self.input_paths = new_input_paths
        self._images = images
        self._img_states = {str(p.resolve()): "pending" for p in images}
        self._refresh_list_view(images)
        self.status.set(f"已找到 {len(images)} 张图片，请点击“开始批量处理”")

    def _build(self):
        header = tk.Frame(self.root, bg=THEME["bg"])
        header.pack(fill="x", padx=16, pady=(8, 2))
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
            "将整个文件夹或图片拖入此处（整块区域任意位置），确认后手动点击开始"
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
        self._make_sec_btn(btn_row, "添加照片", self.add_files, width=116, height=28).pack(side="left", padx=(0, 10))
        self._make_sec_btn(btn_row, "添加文件夹", self.add_folder, width=128, height=28).pack(side="left")
        self._make_sec_btn(btn_row, "清空", self.clear, width=96, height=28).pack(side="right")

        list_pill = SoftPillField(
            import_card,
            height=96,
            radius=16,
            bg_color=THEME["list_bg"],
            border_color=THEME["border"],
            parent_bg=THEME["card"],
            padx=12,
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
            width=20,
            font=("Microsoft YaHei UI", 9),
        )
        self.preset.set("蓝色（常用）")
        self.preset.pack(side="left", padx=(6, 10))
        self.preset.bind("<<ComboboxSelected>>", self._preset_changed)

        self.swatch = tk.Label(
            r1,
            text="     ",
            bg=self.color.get(),
            relief="flat",
            bd=0,
        )
        self.swatch.pack(side="left", padx=(0, 6))

        self.hex_label = tk.Label(
            r1,
            text=self.color.get(),
            bg=THEME["card"],
            fg=THEME["text"],
            font=("Microsoft YaHei UI", 9),
        )
        self.hex_label.pack(side="left", padx=(0, 8))
        self._make_sec_btn(r1, "自定义颜色", self.choose_color, width=116, height=28).pack(side="right")

        # 统一三模型融合总开关行（替代原三模型下拉单选框）
        r_model = tk.Frame(set_card, bg=THEME["card"])
        r_model.pack(fill="x", pady=(0, 6))
        tk.Checkbutton(
            r_model,
            text="三模型融合平衡引擎（birefnet-portrait + isnet-general-use + u2net 协同加权）",
            variable=self.use_triple_ensemble,
            bg=THEME["card"],
            fg=THEME["text"],
            selectcolor="#EFF6FF",
            activebackground=THEME["card"],
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(side="left")

        _, missing_init = check_all_models_status()
        self.model_status_lbl = tk.Label(
            r_model,
            textvariable=self.model_status_var,
            bg=THEME["card"],
            fg=THEME["ok_green"] if not missing_init else THEME["err_red"],
            font=("Microsoft YaHei UI", 8, "bold"),
        )
        self.model_status_lbl.pack(side="left", padx=(6, 6))

        self.download_model_btn = self._make_sec_btn(
            r_model,
            "补全三模型",
            self.download_all_models_ui,
            width=108,
            height=28,
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
            width=10,
            font=("Microsoft YaHei UI", 9),
        )
        self.format_combo.pack(side="left", padx=(6, 16))

        tk.Label(r2, text="大小上限：", bg=THEME["card"], fg=THEME["text"], font=("Microsoft YaHei UI", 9)).pack(side="left")
        ttk.Spinbox(
            r2,
            from_=10,
            to=2000,
            textvariable=self.max_kb,
            width=10,
            font=("Microsoft YaHei UI", 9),
        ).pack(side="left", padx=(6, 4))
        tk.Label(r2, text="KB", bg=THEME["card"], fg=THEME["muted"], font=("Microsoft YaHei UI", 9)).pack(side="left", padx=(0, 16))

        tk.Label(r2, text="并发线程：", bg=THEME["card"], fg=THEME["text"], font=("Microsoft YaHei UI", 9)).pack(side="left")
        ttk.Spinbox(
            r2,
            from_=1,
            to=8,
            textvariable=self.thread_count,
            width=8,
            font=("Microsoft YaHei UI", 9),
        ).pack(side="left", padx=(6, 4))
        tk.Label(r2, text="线程", bg=THEME["card"], fg=THEME["muted"], font=("Microsoft YaHei UI", 9)).pack(side="left")

        r_fine = tk.Frame(set_card, bg=THEME["card"])
        r_fine.pack(fill="x")
        tk.Checkbutton(
            r_fine,
            text="精细边缘模式（保留发丝与浅色衣料/肩膀轮廓，耗时更长、内存与 CPU 占用更高）",
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
            text="导出位置（将自动在所选位置下生成同名文件夹，如导入 16 则导出为 <所选位置>\\16）",
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
            padx=12,
            pady=3,
        )
        out_pill.canvas.pack(side="left", fill="x", expand=True, padx=(0, 8))
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
        self._make_sec_btn(r3, "原目录", self.use_source_dir_as_output, width=92, height=28).pack(side="left", padx=(0, 8))
        self._make_sec_btn(r3, "浏览", self.choose_output, width=92, height=28).pack(side="right")

        bottom = tk.Frame(self.root, bg=THEME["bg"])
        bottom.pack(fill="x", padx=16, pady=(0, 10))

        tk.Frame(bottom, height=18, bg=THEME["bg"], bd=0, highlightthickness=0).pack(fill="x")
        tk.Frame(bottom, height=16, bg=THEME["bg"], bd=0, highlightthickness=0).pack(fill="x")

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
            width=740,
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
        candidate_paths = self.input_paths + valid
        images = collect_images(candidate_paths)
        self._set_images_list(candidate_paths, images)

    def use_source_dir_as_output(self):
        if self._images:
            src_dir = str(Path(self._images[0]).resolve().parent)
            self.output_dir.set(src_dir)
            self.status.set(f"导出基准位置已设为：{src_dir}")
        elif self.input_paths:
            p = Path(self.input_paths[0]).resolve()
            src_dir = str(p if p.is_dir() else p.parent)
            self.output_dir.set(src_dir)
            self.status.set(f"导出基准位置已设为：{src_dir}")
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
        self._img_index_map = {}
        self._img_states = {}
        self.listbox.delete(0, tk.END)
        self.status.set("列表已清空")

    def choose_output(self):
        p = filedialog.askdirectory(title="选择导出位置（将在此位置下生成同名文件夹）")
        if p:
            self.output_dir.set(str(Path(p)))

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

    def download_all_models_ui(self):
        """统一检测并下载全部 3 个模型到固定目录 <软件根目录>\_internal\.u2net\models\<model_name>"""
        if self._is_downloading_model or self._is_running:
            messagebox.showinfo("提示", "当前有任务正在执行，请稍候。")
            return
        if new_session is None:
            messagebox.showerror("下载失败", "缺少 rembg 组件，请检查打包是否完整。")
            return

        ready, missing = check_all_models_status()
        models_root = get_fixed_models_root()
        if not missing:
            self._refresh_model_status_label()
            messagebox.showinfo(
                "模型已完备",
                f"全部 3 个模型均已内置并就绪，可直接离线使用：\n"
                f"• birefnet-portrait\n• isnet-general-use\n• u2net\n\n存放目录：{models_root}",
            )
            return

        self._is_downloading_model = True
        self.download_model_btn.set_enabled(False)
        self.start_btn.set_enabled(False)
        self.status.set(f"正在统一下载全部 3 个模型到固定目录 ({models_root}) ……")

        def _dl_worker():
            try:
                prepare_all_models_env()
                for idx, m_name in enumerate(ALL_MODEL_NAMES, 1):
                    self.msgq.put(("status", f"正在检查/下载模型 ({idx}/3)：[{m_name}] ……"))
                    sess = new_session(m_name)
                    del sess
                    gc.collect()
                self.msgq.put(("model_dl_done", True, "全部 3 个模型", str(models_root)))
            except Exception:
                self.msgq.put(("model_dl_done", False, "三模型集合", traceback.format_exc()))

        threading.Thread(target=_dl_worker, daemon=True).start()

    def start(self):
        if self._is_running or self._is_downloading_model:
            return
        images = list(getattr(self, "_images", []))
        if not images:
            messagebox.showwarning("提示", "请先添加照片或文件夹。")
            return
        out_str = self.output_dir.get().strip()
        if not out_str:
            messagebox.showwarning("提示", "请填写导出位置路径。")
            return
        base_out = Path(out_str).expanduser()
        if base_out.exists() and not base_out.is_dir():
            messagebox.showerror("导出位置错误", f"导出路径指向的是文件：{base_out}")
            return
        try:
            base_out.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            messagebox.showerror("导出位置错误", str(e))
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

        self._img_states = {str(p.resolve()): "pending" for p in images}
        self._refresh_list_view(images)

        models_root = prepare_all_models_env()
        ensemble_enabled = bool(self.use_triple_ensemble.get())
        fmt = (self.output_format.get() or "JPG").upper()
        fine_edge = bool(self.fine_edge.get())
        max_kb_val = int(self.max_kb.get())
        input_paths_snapshot = list(self.input_paths)

        self._is_running = True
        mode_desc = "三模型融合平衡引擎" if ensemble_enabled else f"单模型极速模式 ({PRIMARY_FALLBACK_MODEL})"
        self.status.set(f"正在加载 {mode_desc}（{models_root}）……")
        self.start_btn.set_enabled(False)
        self.download_model_btn.set_enabled(False)
        self.progress.configure(maximum=len(images), value=0)

        threading.Thread(
            target=self._worker,
            args=(images, input_paths_snapshot, base_out, rgb, fmt, max_kb_val, ensemble_enabled, fine_edge, workers),
            daemon=True,
        ).start()

    def _run_model_alpha(self, im_rgba, session):
        """运行单个 rembg 会话并返回同尺寸灰度 Alpha 通道"""
        cut = remove(im_rgba, session=session)
        if not isinstance(cut, Image.Image):
            cut_img = Image.open(io.BytesIO(cut)).convert("RGBA")
        else:
            cut_img = cut.convert("RGBA")
        alpha = cut_img.split()[3]
        if cut_img is not cut:
            cut_img.close()
        return alpha

    def _fuse_triple_alphas(self, im_rgba, weighted_sessions):
        """
        同时调用三个模型得到各自 Alpha 蒙版并按权重加权融合：
        - birefnet-portrait (0.50)：精细发丝与半透明边缘
        - isnet-general-use (0.30)：衣领/肩膀干净轮廓
        - u2net (0.20)：主体结构完整性兜底，防止局部漏抠
        """
        alpha_list = []
        for _m_name, weight, sess in weighted_sessions:
            a = self._run_model_alpha(im_rgba, sess)
            alpha_list.append((weight, a))

        if len(alpha_list) == 1:
            return alpha_list[0][1]

        w, h = im_rgba.size
        fused = Image.new("L", (w, h), 0)
        fused_px = fused.load()
        loaded = [(wt, a.load()) for wt, a in alpha_list]

        for y in range(h):
            for x in range(w):
                acc = 0.0
                for wt, px in loaded:
                    acc += wt * px[x, y]
                val = int(acc + 0.5)
                fused_px[x, y] = 255 if val > 255 else (0 if val < 0 else val)

        for _, a in alpha_list:
            try:
                a.close()
            except Exception:
                pass
        return fused

    def _process_single_image(self, src: Path, input_paths: list, weighted_sessions, base_out: Path, rgb, fmt, max_kb, fine_edge, ext):
        im = None
        a_ch = None
        a_clean = None
        cut_cleaned = None
        bg = None
        result = None
        try:
            im = ImageOps.exif_transpose(Image.open(src)).convert("RGBA")
            r_ch, g_ch, b_ch, _ = im.split()

            # 三模型加权融合（或单模型）生成平衡后的 Alpha 通道
            a_ch = self._fuse_triple_alphas(im, weighted_sessions)

            flt = ImageFilter
            if flt is None:
                from PIL import ImageFilter as _flt
                flt = _flt

            if fine_edge:
                a_clean = a_ch.filter(flt.GaussianBlur(radius=0.35))
            else:
                a_clean = a_ch

            a_px = a_clean.load()
            r_px = r_ch.load()
            g_px = g_ch.load()
            b_px = b_ch.load()
            w, h = im.size
            blend = 0.28 if fine_edge else 0.18
            for y in range(h):
                for x in range(w):
                    alpha_val = a_px[x, y]
                    if 0 < alpha_val < 210:
                        if r_px[x, y] > 228 and g_px[x, y] > 228 and b_px[x, y] > 228:
                            r_px[x, y] = int(r_px[x, y] * (1 - blend) + rgb[0] * blend)
                            g_px[x, y] = int(g_px[x, y] * (1 - blend) + rgb[1] * blend)
                            b_px[x, y] = int(b_px[x, y] * (1 - blend) + rgb[2] * blend)

            cut_cleaned = Image.merge("RGBA", (r_ch, g_ch, b_ch, a_clean))
            bg = Image.new("RGBA", cut_cleaned.size, (*rgb, 255))
            bg.alpha_composite(cut_cleaned)
            result = bg.convert("RGB")

            out_folder, relative = resolve_output_root_for_image(src, input_paths, base_out)
            target_dir = out_folder / relative.parent
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / (src.stem + ext)
            if target.exists() and src.suffix.lower() != ext:
                target = target_dir / (src.stem + f"_converted{ext}")
            data = encode_image_under_limit(result, fmt, max_kb)
            target.write_bytes(data)
            return out_folder
        finally:
            for obj in (result, bg, cut_cleaned, a_clean, a_ch, im):
                if obj is not None:
                    try:
                        obj.close()
                    except Exception:
                        pass

    def _worker(self, images, input_paths, base_out, rgb, fmt, max_kb, ensemble_enabled, fine_edge, workers):
        weighted_sessions = []
        try:
            if remove is None or new_session is None:
                self.msgq.put(("fatal", "缺少 rembg 组件，请检查打包是否完整。"))
                return

            models_root = prepare_all_models_env()
            target_specs = ENSEMBLE_MODELS if ensemble_enabled else [(PRIMARY_FALLBACK_MODEL, 1.0)]

            for idx, (m_name, weight) in enumerate(target_specs, 1):
                m_dir = get_model_subdir(m_name)
                existing_file = _find_existing_model_file(m_name)
                if existing_file is not None:
                    self.msgq.put(("status", f"正在加载内置模型 ({idx}/{len(target_specs)})：{m_name} ……"))
                else:
                    self.msgq.put(("status", f"未在本地找到 {m_name}，正在自动下载 ({idx}/{len(target_specs)})……"))
                try:
                    sess = new_session(m_name)
                    weighted_sessions.append((m_name, weight, sess))
                except Exception as e:
                    self.msgq.put((
                        "fatal",
                        f"AI 模型 [{m_name}] 加载或下载失败：{e}\n固定模型目录：{m_dir}\n（若处于离线环境，请确认安装包已完整包含 {models_root}）",
                    ))
                    return

            self.msgq.put(("refresh_model_status",))
            engine_tip = "三模型融合引擎" if ensemble_enabled else PRIMARY_FALLBACK_MODEL
            self.msgq.put(("status", f"{engine_tip} 已就绪，开始 {workers} 线程并发处理 {len(images)} 张图片……"))

            ext = ".png" if fmt == "PNG" else ".jpg"
            effective_workers = max(1, min(workers, len(images)))
            ok = 0
            done_count = 0
            errors = []
            exported_folders = set()
            lock = threading.Lock()

            with ThreadPoolExecutor(max_workers=effective_workers) as executor:
                future_map = {
                    executor.submit(
                        self._process_single_image,
                        src,
                        input_paths,
                        weighted_sessions,
                        base_out,
                        rgb,
                        fmt,
                        max_kb,
                        fine_edge,
                        ext,
                    ): src
                    for src in images
                }

                for fut in as_completed(future_map):
                    src = future_map[fut]
                    try:
                        out_folder = fut.result()
                        with lock:
                            ok += 1
                            done_count += 1
                            cur_ok = ok
                            cur_done = done_count
                            exported_folders.add(str(out_folder))
                        self.msgq.put((
                            "item_done",
                            src,
                            "ok",
                            cur_done,
                            len(images),
                            f"处理中 {cur_done}/{len(images)}（成功 {cur_ok}）：{src.name} ✓",
                        ))
                    except Exception as e:
                        with lock:
                            errors.append(f"{src}: {e}")
                            done_count += 1
                            cur_done = done_count
                        self.msgq.put((
                            "item_done",
                            src,
                            "err",
                            cur_done,
                            len(images),
                            f"处理中 {cur_done}/{len(images)}（失败）：{src.name} ✗",
                        ))
                    if done_count % 5 == 0:
                        gc.collect()

            summary_out = "；".join(sorted(exported_folders)) if exported_folders else str(base_out)
            self.msgq.put(("done", ok, len(images), errors, summary_out))
        except Exception:
            self.msgq.put(("fatal", f"后台处理线程发生未捕获异常：\n{traceback.format_exc()}"))
        finally:
            for _, _, sess in weighted_sessions:
                try:
                    del sess
                except Exception:
                    pass
            weighted_sessions.clear()
            gc.collect()

    def _poll(self):
        try:
            while True:
                item = self.msgq.get_nowait()
                kind = item[0]
                if kind == "status":
                    self.status.set(item[1])
                elif kind == "refresh_model_status":
                    self._refresh_model_status_label()
                elif kind == "model_dl_done":
                    _, ok_flag, m_name, info_str = item
                    self._is_downloading_model = False
                    self.download_model_btn.set_enabled(True)
                    self.start_btn.set_enabled(True)
                    self._refresh_model_status_label()
                    if ok_flag:
                        self.status.set(f"{m_name} 已全部就绪（存放于 {info_str}）")
                        messagebox.showinfo("下载完成", f"{m_name} 已全部准备就绪，可离线使用：\n{info_str}")
                    else:
                        self.status.set(f"{m_name} 下载失败")
                        messagebox.showerror("下载失败", f"{m_name} 下载出错：\n{info_str}")
                elif kind == "item_done":
                    _, src, state, v, total, msg = item
                    self._mark_image_state(src, state)
                    self.progress.configure(value=v)
                    self.status.set(msg)
                elif kind == "fatal":
                    self._is_running = False
                    self.start_btn.set_enabled(True)
                    self.download_model_btn.set_enabled(True)
                    self.status.set("处理中止，请查看错误提示")
                    messagebox.showerror("无法处理", item[1])
                elif kind == "done":
                    _, ok, total, errors, out_summary = item
                    self._is_running = False
                    self.start_btn.set_enabled(True)
                    self.download_model_btn.set_enabled(True)
                    self.status.set(f"完成：成功 {ok} 张，失败 {len(errors)} 张")
                    msg = f"处理完成！\n成功：{ok}/{total} 张\n导出文件夹：{out_summary}"
                    if errors:
                        msg += "\n\n部分失败原因（最多显示 5 条）：\n" + "\n".join(errors[:5])
                    messagebox.showinfo("处理完成", msg)
        except queue.Empty:
            pass
        if self.root.winfo_exists():
            self.root.after(100, self._poll)


def cli_download_all_models() -> int:
    """
    供打包阶段（如 GitHub Actions 或本地构建脚本）静默调用：
    python 证件照批量换底色_2.py --download-all-models
    将全部 3 个模型下载到 <软件根目录>\_internal\.u2net\models\<模型名>\ 中，方便随安装包一起打包。
    """
    try:
        models_root = prepare_all_models_env()
        print(f"[INFO] 固定模型根目录: {models_root}")
        from rembg import new_session as _new_session
        for idx, m_name in enumerate(ALL_MODEL_NAMES, 1):
            print(f"[INFO] ({idx}/3) 正在准备模型: {m_name} ...")
            sess = _new_session(m_name)
            del sess
            gc.collect()
            found = _find_existing_model_file(m_name)
            print(f"[OK]   模型 {m_name} 已就绪 -> {found}")
        print("[SUCCESS] 全部 3 个离线模型已下载并归整完成！")
        return 0
    except Exception as e:
        print(f"[ERROR] 模型预下载失败: {e}")
        traceback.print_exc()
        return 1


def main():
    if "--download-all-models" in sys.argv:
        sys.exit(cli_download_all_models())

    global Image, ImageOps, ImageFilter, ImageChops, remove, new_session

    try:
        if HAS_DND and TkinterDnD is not None:
            root = TkinterDnD.Tk()
        else:
            root = tk.Tk()
        root.withdraw()
        splash = LoadingWindow(root)
        load_q = queue.Queue()

        def load_deps():
            global Image, ImageOps, ImageFilter, ImageChops, remove, new_session
            try:
                prepare_all_models_env()
                load_q.put(("STAGE", 20.0, "正在准备图像解码组件 (Pillow)..."))
                from PIL import (
                    Image as _Image,
                    ImageOps as _ImageOps,
                    ImageFilter as _ImageFilter,
                    ImageChops as _ImageChops,
                )
                Image, ImageOps, ImageFilter, ImageChops = _Image, _ImageOps, _ImageFilter, _ImageChops

                load_q.put(("STAGE", 55.0, "正在初始化三模型 AI 抠图引擎 (rembg / onnxruntime)..."))
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