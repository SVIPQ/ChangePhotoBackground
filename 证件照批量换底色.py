# -*- coding: utf-8 -*-
"""
证件照批量换底色工具_Czq（竖屏紧凑布局 + 导出下方名称清单实时箭头映射 + 完成绿色大勾 ✔ 版）
"""
import gc
import io
import math
import os
import queue
import re
import shutil
import sys
import threading
import traceback
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox, ttk

try:
    import numpy as np
    HAS_NUMPY = True
except Exception:
    np = None
    HAS_NUMPY = False

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

DEFAULT_MODEL = "birefnet-portrait"
ALL_MODEL_NAMES = ["birefnet-portrait", "isnet-general-use", "u2net"]

MODEL_RELEASE_URLS = {
    "birefnet-portrait": "https://github.com/danielgatis/rembg/releases/download/v0.0.0/birefnet-portrait.onnx",
    "isnet-general-use": "https://github.com/danielgatis/rembg/releases/download/v0.0.0/isnet-general-use.onnx",
    "u2net": "https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2net.onnx",
}

CN_GH_MIRROR_PREFIXES = [
    "https://gh-proxy.com/",
    "https://ghproxy.net/",
    "https://gh.llkk.cc/",
    "https://ghproxy.cxkpro.top/",
]

ENSEMBLE_PRESETS = {
    "双模型叠加 (birefnet + isnet)": [
        ("birefnet-portrait", 0.75),
        ("isnet-general-use", 0.25),
    ],
    "三模型叠加 (birefnet + isnet + u2net)": [
        ("birefnet-portrait", 0.65),
        ("isnet-general-use", 0.20),
        ("u2net", 0.15),
    ],
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
    "ok_green": "#15803D",
    "ok_bg": "#DCFCE7",
    "err_red": "#DC2626",
}


def get_app_root_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def get_u2net_home_dir() -> Path:
    return get_app_root_dir() / "_internal" / ".u2net"


def get_fixed_models_root() -> Path:
    return get_u2net_home_dir() / "models"


def get_model_subdir(model_name: str) -> Path:
    return get_fixed_models_root() / model_name


def _find_existing_model_file(model_name: str) -> Path | None:
    models_root = get_fixed_models_root()
    model_dir = models_root / model_name
    model_dir.mkdir(parents=True, exist_ok=True)

    for f in model_dir.rglob("*"):
        if f.is_file() and f.suffix.lower() in MODEL_WEIGHT_EXTS and f.stat().st_size > 1024 * 100:
            return f

    wrong_nested_dir = models_root / "models" / model_name
    if wrong_nested_dir.exists() and wrong_nested_dir.is_dir():
        for f in wrong_nested_dir.rglob("*"):
            if f.is_file() and f.suffix.lower() in MODEL_WEIGHT_EXTS and f.stat().st_size > 1024 * 100:
                dst = model_dir / f.name
                if not dst.exists():
                    dst.write_bytes(f.read_bytes())
                return dst

    legacy_u2net = get_u2net_home_dir()
    if legacy_u2net.exists() and legacy_u2net.is_dir():
        key_token = model_name.lower().replace("-", "_")
        for f in legacy_u2net.iterdir():
            if f.is_file() and f.suffix.lower() in MODEL_WEIGHT_EXTS and f.stat().st_size > 1024 * 100:
                fname_norm = f.stem.lower().replace("-", "_")
                if model_name.lower() in f.stem.lower() or key_token in fname_norm:
                    dst = model_dir / f.name
                    if not dst.exists():
                        dst.write_bytes(f.read_bytes())
                    return dst

    return None


def prepare_models_env() -> Path:
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


def is_model_ready(model_name: str, custom_file: str = "") -> bool:
    if custom_file:
        p = Path(custom_file.strip())
        if p.is_file() and p.suffix.lower() == ".onnx" and p.stat().st_size > 1024 * 100:
            return True
        return False
    prepare_models_env()
    return _find_existing_model_file(model_name) is not None


def download_model_via_mirrors(model_name: str, status_cb=None) -> Path:
    prepare_models_env()
    existing = _find_existing_model_file(model_name)
    if existing is not None:
        return existing

    raw_url = MODEL_RELEASE_URLS.get(model_name)
    target_dir = get_model_subdir(model_name)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_file = target_dir / f"{model_name}.onnx"
    temp_file = target_dir / f"{model_name}.onnx.part"

    last_err = ""
    if raw_url:
        for idx, prefix in enumerate(CN_GH_MIRROR_PREFIXES, 1):
            mirror_url = f"{prefix}{raw_url}"
            if status_cb:
                status_cb(f"正在通过国内镜像 ({idx}/{len(CN_GH_MIRROR_PREFIXES)}) 下载 {model_name} ……")
            try:
                req = urllib.request.Request(
                    mirror_url,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                )
                with urllib.request.urlopen(req, timeout=60) as resp, open(temp_file, "wb") as out_f:
                    total_bytes = int(resp.headers.get("Content-Length") or 0)
                    downloaded = 0
                    last_pct = -1
                    while True:
                        chunk = resp.read(256 * 1024)
                        if not chunk:
                            break
                        out_f.write(chunk)
                        downloaded += len(chunk)
                        if status_cb and total_bytes > 0:
                            pct = int(downloaded * 100 / total_bytes)
                            if pct >= last_pct + 5:
                                last_pct = pct
                                status_cb(
                                    f"镜像下载 {model_name}：{pct}% ({downloaded // (1024 * 1024)}MB / {total_bytes // (1024 * 1024)}MB)"
                                )
                if temp_file.exists() and temp_file.stat().st_size > 1024 * 100:
                    if target_file.exists():
                        target_file.unlink()
                    temp_file.replace(target_file)
                    return target_file
            except Exception as e:
                last_err = f"{mirror_url} -> {e}"
                if temp_file.exists():
                    try:
                        temp_file.unlink()
                    except Exception:
                        pass

    if new_session is not None:
        if status_cb:
            status_cb(f"镜像源暂不可达，尝试默认通道获取 {model_name} ……")
        sess = new_session(model_name)
        del sess
        gc.collect()
        found = _find_existing_model_file(model_name)
        if found is not None:
            return found

    raise RuntimeError(f"模型 [{model_name}] 下载失败。最后错误：{last_err}")


APP_MODELS_ROOT = prepare_models_env()

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


def parse_name_lines(raw_text: str) -> list[str]:
    """按行拆分名称清单，清洗非法文件名字符并去掉多余后缀"""
    if not raw_text:
        return []
    lines = []
    for raw_line in raw_text.splitlines():
        s = raw_line.strip()
        if not s:
            continue
        # 若用户粘贴了带常见图片后缀的名称，去掉原后缀以便统一按输出格式拼接
        s_no_ext = re.sub(r"\.(jpg|jpeg|png|bmp|webp|tif|tiff)$", "", s, flags=re.IGNORECASE).strip()
        if not s_no_ext:
            s_no_ext = s
        safe = re.sub(r'[\\/:*?"<>|]+', "_", s_no_ext).strip(" .")
        if safe:
            lines.append(safe)
    return lines


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


def compute_target_output_path(
    src: Path,
    input_paths: list,
    base_out: Path,
    ext: str,
    custom_stem: str | None = None,
) -> tuple[Path, Path]:
    out_folder, relative = resolve_output_root_for_image(src, input_paths, base_out)
    target_dir = out_folder / relative.parent
    stem_to_use = custom_stem if custom_stem else src.stem
    target = target_dir / (stem_to_use + ext)
    if target.resolve() == src.resolve():
        target = target_dir / (stem_to_use + f"_converted{ext}")
    return out_folder, target


def encode_image_output(image, fmt, limit_enabled: bool, max_kb: int):
    fmt_upper = (fmt or "JPG").upper()
    img = image.convert("RGB")

    if not limit_enabled:
        buf = io.BytesIO()
        if fmt_upper == "PNG":
            img.save(buf, format="PNG", optimize=True)
        else:
            img.save(buf, format="JPEG", quality=95, optimize=True)
        return buf.getvalue()

    limit = max(1, int(max_kb)) * 1024
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
        radius=22,
        bg_color="#FFFFFF",
        border_color="#D8E4F3",
        parent_bg="#F1F6FD",
        padx=16,
        pady=8,
        outer_pady=(0, 6),
    ):
        self.pill_r = float(radius)
        self.bg_color = bg_color
        self.border_color = border_color
        self.card_w = 580.0
        self.card_h = 85.0

        self.canvas = tk.Canvas(
            parent,
            width=580,
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
            width=548,
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
        self.field_w = 340.0
        self.field_h = float(height)
        self.padx = padx
        self.pady = pady

        self.canvas = tk.Canvas(
            parent,
            width=340,
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
            width=320,
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
        width=112,
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

    def set_text(self, new_text):
        self.btn_text = new_text
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
        height=40,
        radius=20,
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
    TRANSPARENT_KEY = "#FF00FF"

    def __init__(self, root):
        self.root = root
        self.win = tk.Toplevel(root)
        self.win.title(SPLASH_DISPLAY_NAME)
        self.win.geometry("440x168")
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
            width=440,
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
            438,
            166,
            r=24,
            fill="#FFFFFF",
            outline="#2563EB",
            width=2,
        )

        canvas.create_text(
            220,
            36,
            text=SPLASH_DISPLAY_NAME,
            fill="#0F172A",
            font=("Microsoft YaHei UI", 12, "bold"),
        )
        self.status_var = tk.StringVar(value="正在初始化程序环境...")
        self.status_id = canvas.create_text(
            220,
            64,
            text=self.status_var.get(),
            fill="#475569",
            font=("Microsoft YaHei UI", 9),
        )

        self.bar = ttk.Progressbar(
            canvas,
            style="SplashGreen.Horizontal.TProgressbar",
            mode="determinate",
            maximum=100,
            length=360,
        )
        canvas.create_window(220, 100, window=self.bar)

        canvas.create_text(
            220,
            136,
            text="首次加载 AI 抠图引擎可能需要几十秒，请勿关闭",
            fill="#94A3B8",
            font=("Microsoft YaHei UI", 8),
        )
        self._canvas = canvas

    def _center(self):
        self.win.update_idletasks()
        sw = self.win.winfo_screenwidth()
        sh = self.win.winfo_screenheight()
        x = max(0, (sw - 440) // 2)
        y = max(0, (sh - 168) // 2)
        self.win.geometry(f"440x168+{x}+{y}")

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
    RIGHT_BTN_W = 112

    def __init__(self, root):
        self.root = root
        self.root.title(APP_DISPLAY_NAME)
        win_w, win_h = 630, 910
        self.root.minsize(590, 840)
        self.root.configure(bg=THEME["bg"])
        center_window_on_screen(self.root, win_w, win_h)

        default_output_path = str(Path.home() / "Desktop")
        default_threads = 1

        self.msgq = queue.Queue()
        self.input_paths = []
        self._images = []
        self._img_index_map = {}
        self._img_states = {}
        self._name_lines = []
        self._is_downloading_model = False
        self._is_running = False
        self._closing = False
        self._poll_job = None
        self._name_sync_job = None

        self.output_dir = tk.StringVar(value=default_output_path)
        self.mode_choice = tk.StringVar(value="single")
        self.single_model = tk.StringVar(value=DEFAULT_MODEL)
        self.use_custom_model_path = tk.BooleanVar(value=False)
        self.custom_model_file = tk.StringVar(value="")
        self.ensemble_choice = tk.StringVar(value="双模型叠加 (birefnet + isnet)")

        self.fine_edge = tk.BooleanVar(value=True)
        self.color = tk.StringVar(value=PRESETS["蓝色（常用）"])
        self.output_format = tk.StringVar(value="JPG")
        self.limit_size_enabled = tk.BooleanVar(value=True)
        self.max_kb = tk.IntVar(value=50)
        self.thread_count = tk.IntVar(value=default_threads)
        self.status = tk.StringVar(value="拖入文件夹确认后，再点击下方“开始批量处理”")

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self._setup_styles()
        try:
            self._build()
        except Exception:
            messagebox.showerror("界面构建错误", traceback.format_exc())
        self._sync_mode_panels()
        self._sync_size_limit_state()
        self._poll()

    def on_close(self):
        self._closing = True
        if self._poll_job is not None:
            try:
                self.root.after_cancel(self._poll_job)
            except Exception:
                pass
            self._poll_job = None
        if self._name_sync_job is not None:
            try:
                self.root.after_cancel(self._name_sync_job)
            except Exception:
                pass
            self._name_sync_job = None
        try:
            self.root.withdraw()
            self.root.quit()
            self.root.destroy()
        except Exception:
            pass
        os._exit(0)

    def _setup_styles(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass

    def _make_pill_card(self, parent, radius=22, outer_pady=(0, 6)):
        card = SoftPillCard(
            parent,
            radius=radius,
            bg_color=THEME["card"],
            border_color=THEME["border"],
            parent_bg=THEME["bg"],
            padx=16,
            pady=8,
            outer_pady=outer_pady,
        )
        return card.canvas, card.inner

    def _make_sec_btn(self, parent, text, command, width=112, height=28):
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

    def _current_ext(self) -> str:
        return ".png" if (self.output_format.get() or "JPG").upper() == "PNG" else ".jpg"

    def _preview_target_name(self, idx: int, src: Path) -> str:
        ext = self._current_ext()
        if idx < len(self._name_lines):
            return f"{self._name_lines[idx]}{ext}"
        if self._name_lines:
            return f"{src.stem}{ext} [超行沿用原名]"
        return f"{src.stem}{ext}"

    def _format_list_row(self, idx: int, p: Path, state: str) -> str:
        mapped_name = self._preview_target_name(idx, p)
        base_str = f"#{idx + 1}  {p}   -->   {mapped_name}"
        if state == "ok":
            return f"{base_str}   ✔"
        if state == "err":
            return f"{base_str}   ✗"
        return base_str

    def _refresh_list_view(self, images):
        self.listbox.delete(0, tk.END)
        self._img_index_map = {}
        for idx, p in enumerate(images):
            key = str(p.resolve())
            self._img_index_map[key] = idx
            state = self._img_states.get(key, "pending")
            if idx < 2000:
                self.listbox.insert(tk.END, self._format_list_row(idx, p, state))
                if state == "ok":
                    self.listbox.itemconfig(idx, fg=THEME["ok_green"], bg=THEME["ok_bg"])
                elif state == "err":
                    self.listbox.itemconfig(idx, fg=THEME["err_red"], bg=THEME["list_bg"])
                else:
                    self.listbox.itemconfig(idx, fg=THEME["text"], bg=THEME["list_bg"])

    def _mark_image_state(self, src: Path, state: str):
        key = str(src.resolve())
        self._img_states[key] = state
        idx = self._img_index_map.get(key)
        if idx is not None and idx < self.listbox.size():
            self.listbox.delete(idx)
            self.listbox.insert(idx, self._format_list_row(idx, src, state))
            if state == "ok":
                self.listbox.itemconfig(idx, fg=THEME["ok_green"], bg=THEME["ok_bg"])
            elif state == "err":
                self.listbox.itemconfig(idx, fg=THEME["err_red"], bg=THEME["list_bg"])

    def _on_name_text_modified(self, _event=None):
        try:
            self.name_text.edit_modified(False)
        except Exception:
            pass
        if self._name_sync_job is not None:
            try:
                self.root.after_cancel(self._name_sync_job)
            except Exception:
                pass
        self._name_sync_job = self.root.after(120, self._apply_name_lines_preview)

    def _apply_name_lines_preview(self):
        self._name_sync_job = None
        raw = self.name_text.get("1.0", "end-1c")
        self._name_lines = parse_name_lines(raw)
        name_cnt = len(self._name_lines)
        img_cnt = len(self._images)
        if name_cnt == 0:
            self.name_count_lbl.configure(text="已输入 0 行名称（未填则沿用原名）", fg=THEME["muted"])
        elif img_cnt > 0 and name_cnt != img_cnt:
            self.name_count_lbl.configure(
                text=f"名称 {name_cnt} 行 / 图片 {img_cnt} 张（数量不一致，请核对是否串行）",
                fg=THEME["err_red"],
            )
        else:
            self.name_count_lbl.configure(
                text=f"已输入 {name_cnt} 行名称（已按顺序一一对应箭头）",
                fg=THEME["ok_green"],
            )
        if self._images:
            self._refresh_list_view(self._images)

    def import_names_txt(self):
        txt_path = filedialog.askopenfilename(
            title="选择名称清单 TXT 文件",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
        )
        if txt_path:
            try:
                content = Path(txt_path).read_text(encoding="utf-8-sig")
            except Exception:
                content = Path(txt_path).read_text(encoding="gbk", errors="ignore")
            self.name_text.delete("1.0", tk.END)
            self.name_text.insert("1.0", content)
            self._apply_name_lines_preview()

    def clear_names_text(self):
        self.name_text.delete("1.0", tk.END)
        self._apply_name_lines_preview()

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
        old_states = dict(self._img_states)
        self.input_paths = new_input_paths
        self._images = images
        self._img_states = {
            str(p.resolve()): old_states.get(str(p.resolve()), "pending")
            for p in images
        }
        self._apply_name_lines_preview()
        done_cnt = sum(1 for s in self._img_states.values() if s == "ok")
        if done_cnt > 0:
            self.status.set(f"共 {len(images)} 张图片（其中 {done_cnt} 张已完成 ✔），点击“开始批量处理”")
        else:
            self.status.set(f"已找到 {len(images)} 张图片，请点击“开始批量处理”")

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
        tk.Frame(header, height=4, bg=THEME["bg"], bd=0, highlightthickness=0).pack(fill="x")

        # 卡片 1：导入列表（实时显示原图 --> 目标名称）
        import_outer, import_card = self._make_pill_card(self.root, radius=22)

        main_hint = (
            "将整个文件夹或图片拖入此处，确认后手动点击开始"
            if HAS_DND
            else "点击下方“添加照片”或“添加文件夹”选择待处理图片"
        )
        drop_banner = SoftPillBanner(
            import_card,
            main_text=main_hint,
            height=36,
            radius=18,
            bg_color=THEME["drop_bg"],
            hover_color=THEME["drop_hover"],
            border_color=THEME["drop_border"],
            main_fg=THEME["drop_fg"],
            parent_bg=THEME["card"],
        )
        drop_banner.pack(fill="x", pady=(0, 6))

        btn_row = tk.Frame(import_card, bg=THEME["card"])
        btn_row.pack(fill="x", pady=(0, 6))
        self._make_sec_btn(btn_row, "添加照片", self.add_files, width=106, height=28).pack(side="left", padx=(0, 8))
        self._make_sec_btn(btn_row, "添加文件夹", self.add_folder, width=114, height=28).pack(side="left")
        self._make_sec_btn(btn_row, "清空", self.clear, width=92, height=28).pack(side="right")

        list_pill = SoftPillField(
            import_card,
            height=104,
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
            height=4,
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

        # 卡片 2：处理参数与模型设置
        _, set_card = self._make_pill_card(self.root, radius=22)
        tk.Label(
            set_card,
            text="处理参数与模型设置",
            bg=THEME["card"],
            fg=THEME["text"],
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(anchor="w", pady=(0, 4))

        r_mode = tk.Frame(set_card, bg=THEME["card"])
        r_mode.pack(fill="x", pady=(0, 4))
        tk.Radiobutton(
            r_mode,
            text="选项一：选择单个模型",
            variable=self.mode_choice,
            value="single",
            command=self._sync_mode_panels,
            bg=THEME["card"],
            fg=THEME["text"],
            selectcolor="#EFF6FF",
            activebackground=THEME["card"],
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(side="left", padx=(0, 18))

        tk.Radiobutton(
            r_mode,
            text="选项二：模型叠加引擎",
            variable=self.mode_choice,
            value="ensemble",
            command=self._sync_mode_panels,
            bg=THEME["card"],
            fg=THEME["text"],
            selectcolor="#EFF6FF",
            activebackground=THEME["card"],
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(side="left")

        self.single_container = tk.Frame(set_card, bg=THEME["card"])

        r_models = tk.Frame(self.single_container, bg=THEME["card"])
        r_models.pack(fill="x", pady=(0, 4))

        models_left = tk.Frame(r_models, bg=THEME["card"])
        models_left.pack(side="left", fill="x", expand=True)

        m_radio_row = tk.Frame(models_left, bg=THEME["card"])
        m_radio_row.pack(fill="x")
        for m_name in ALL_MODEL_NAMES:
            tk.Radiobutton(
                m_radio_row,
                text=m_name,
                variable=self.single_model,
                value=m_name,
                command=self._sync_mode_panels,
                bg=THEME["card"],
                fg=THEME["text"],
                selectcolor="#EFF6FF",
                activebackground=THEME["card"],
                font=("Microsoft YaHei UI", 8),
            ).pack(side="left", padx=(6, 4))

        self.model_state_lbl = tk.Label(
            models_left,
            text="",
            bg=THEME["card"],
            fg=THEME["muted"],
            font=("Microsoft YaHei UI", 8, "bold"),
        )
        self.model_state_lbl.pack(anchor="w", padx=(10, 0), pady=(1, 0))

        self.download_model_btn = self._make_sec_btn(
            r_models,
            "下载模型",
            self.download_selected_model_ui,
            width=self.RIGHT_BTN_W,
            height=28,
        )
        self.download_model_btn.pack(side="right", anchor="n")

        r_custom_chk = tk.Frame(self.single_container, bg=THEME["card"])
        r_custom_chk.pack(fill="x", pady=(2, 2))
        tk.Checkbutton(
            r_custom_chk,
            text="自定义路径（指定电脑中其他位置的 .onnx 模型文件）",
            variable=self.use_custom_model_path,
            command=self._sync_mode_panels,
            bg=THEME["card"],
            fg=THEME["text"],
            selectcolor="#EFF6FF",
            activebackground=THEME["card"],
            font=("Microsoft YaHei UI", 9),
        ).pack(anchor="w", padx=(6, 0))

        r_custom_path = tk.Frame(self.single_container, bg=THEME["card"])
        r_custom_path.pack(fill="x", pady=(0, 4))
        custom_pill = SoftPillField(
            r_custom_path,
            height=28,
            radius=14,
            bg_color="#F8FAFC",
            border_color="#CBD5E1",
            parent_bg=THEME["card"],
            padx=10,
            pady=3,
        )
        custom_pill.canvas.pack(side="left", fill="x", expand=True, padx=(10, 8))
        self.custom_path_entry = tk.Entry(
            custom_pill.inner,
            textvariable=self.custom_model_file,
            font=("Microsoft YaHei UI", 8),
            bg="#F8FAFC",
            fg=THEME["text"],
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
        )
        self.custom_path_entry.pack(fill="both", expand=True)

        self.pick_onnx_btn = self._make_sec_btn(
            r_custom_path,
            "选择 .onnx",
            self.choose_custom_onnx_file,
            width=self.RIGHT_BTN_W,
            height=28,
        )
        self.pick_onnx_btn.pack(side="right")

        self.ensemble_container = tk.Frame(set_card, bg=THEME["card"])
        tk.Label(
            self.ensemble_container,
            text="叠加方案：",
            bg=THEME["card"],
            fg=THEME["text"],
            font=("Microsoft YaHei UI", 9),
        ).pack(side="left", padx=(8, 6))
        self.ensemble_combo = ttk.Combobox(
            self.ensemble_container,
            textvariable=self.ensemble_choice,
            values=list(ENSEMBLE_PRESETS.keys()),
            state="readonly",
            width=36,
            font=("Microsoft YaHei UI", 9),
        )
        self.ensemble_combo.pack(side="left")

        self.mode_anchor = tk.Frame(set_card, bg=THEME["card"])
        self.mode_anchor.pack(fill="x")

        r_fine = tk.Frame(set_card, bg=THEME["card"])
        r_fine.pack(fill="x", pady=(2, 5))
        tk.Checkbutton(
            r_fine,
            text="精细发丝去白边（清理发丝与浅色衣领杂边，保留浅色外套轮廓）",
            variable=self.fine_edge,
            bg=THEME["card"],
            fg=THEME["text"],
            selectcolor="#EFF6FF",
            activebackground=THEME["card"],
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(anchor="w")

        r1 = tk.Frame(set_card, bg=THEME["card"])
        r1.pack(fill="x", pady=(0, 5))
        tk.Label(r1, text="背景颜色：", bg=THEME["card"], fg=THEME["text"], font=("Microsoft YaHei UI", 9)).pack(side="left")
        self.preset = ttk.Combobox(
            r1,
            values=list(PRESETS.keys()) + ["自定义颜色"],
            state="readonly",
            width=15,
            font=("Microsoft YaHei UI", 9),
        )
        self.preset.set("蓝色（常用）")
        self.preset.pack(side="left", padx=(4, 8))
        self.preset.bind("<<ComboboxSelected>>", self._preset_changed)

        self.swatch = tk.Label(r1, text="    ", bg=self.color.get(), relief="flat", bd=0)
        self.swatch.pack(side="left", padx=(0, 6))

        self.hex_label = tk.Label(
            r1,
            text=self.color.get(),
            bg=THEME["card"],
            fg=THEME["text"],
            font=("Microsoft YaHei UI", 9),
        )
        self.hex_label.pack(side="left")

        self._make_sec_btn(
            r1,
            "自定义颜色",
            self.choose_color,
            width=self.RIGHT_BTN_W,
            height=28,
        ).pack(side="right")

        r2 = tk.Frame(set_card, bg=THEME["card"])
        r2.pack(fill="x", pady=(0, 4))
        tk.Label(r2, text="输出格式：", bg=THEME["card"], fg=THEME["text"], font=("Microsoft YaHei UI", 9)).pack(side="left")
        self.format_combo = ttk.Combobox(
            r2,
            textvariable=self.output_format,
            values=["JPG", "PNG"],
            state="readonly",
            width=7,
            font=("Microsoft YaHei UI", 9),
        )
        self.format_combo.pack(side="left", padx=(4, 12))
        self.format_combo.bind("<<ComboboxSelected>>", lambda _e: self._apply_name_lines_preview())

        tk.Checkbutton(
            r2,
            text="限制大小",
            variable=self.limit_size_enabled,
            command=self._sync_size_limit_state,
            bg=THEME["card"],
            fg=THEME["text"],
            selectcolor="#EFF6FF",
            activebackground=THEME["card"],
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(side="left", padx=(0, 4))

        self.max_kb_spin = ttk.Spinbox(
            r2,
            from_=10,
            to=5000,
            textvariable=self.max_kb,
            width=7,
            font=("Microsoft YaHei UI", 9),
        )
        self.max_kb_spin.pack(side="left", padx=(2, 4))
        self.max_kb_unit_lbl = tk.Label(
            r2,
            text="KB（超出自动压缩）",
            bg=THEME["card"],
            fg=THEME["muted"],
            font=("Microsoft YaHei UI", 8),
        )
        self.max_kb_unit_lbl.pack(side="left")

        r_th = tk.Frame(set_card, bg=THEME["card"])
        r_th.pack(fill="x")
        tk.Label(r_th, text="并发线程：", bg=THEME["card"], fg=THEME["text"], font=("Microsoft YaHei UI", 9)).pack(side="left")
        ttk.Spinbox(
            r_th,
            from_=1,
            to=4,
            textvariable=self.thread_count,
            width=6,
            font=("Microsoft YaHei UI", 9),
        ).pack(side="left", padx=(4, 4))
        tk.Label(r_th, text="线程（建议 1）", bg=THEME["card"], fg=THEME["muted"], font=("Microsoft YaHei UI", 8)).pack(side="left")

        # 卡片 3：导出位置
        _, dir_card = self._make_pill_card(self.root, radius=22, outer_pady=(0, 6))
        tk.Label(
            dir_card,
            text="导出位置（自动生成同名文件夹，如导入 16 则导出为 <所选位置>\\16）",
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
        self._make_sec_btn(r3, "原目录", self.use_source_dir_as_output, width=78, height=28).pack(side="left", padx=(0, 6))
        self._make_sec_btn(r3, "浏览", self.choose_output, width=78, height=28).pack(side="right")

        # 卡片 4：导出位置下方的“名称清单”输入框（按行对应导入列表箭头映射）
        _, name_card = self._make_pill_card(self.root, radius=22, outer_pady=(0, 2))
        name_top = tk.Frame(name_card, bg=THEME["card"])
        name_top.pack(fill="x", pady=(0, 4))
        tk.Label(
            name_top,
            text="名称清单（每行一个新名称，支持直接粘贴表格一列或导入 TXT）",
            bg=THEME["card"],
            fg=THEME["text"],
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(side="left")

        self._make_sec_btn(name_top, "清空", self.clear_names_text, width=68, height=24).pack(side="right")
        self._make_sec_btn(name_top, "导入 TXT", self.import_names_txt, width=84, height=24).pack(side="right", padx=(0, 6))

        name_pill = SoftPillField(
            name_card,
            height=78,
            radius=16,
            bg_color="#F8FAFC",
            border_color="#CBD5E1",
            parent_bg=THEME["card"],
            padx=10,
            pady=4,
        )
        name_pill.canvas.pack(fill="x")
        self.name_text = tk.Text(
            name_pill.inner,
            height=3,
            bg="#F8FAFC",
            fg=THEME["text"],
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            font=("Microsoft YaHei UI", 9),
            undo=True,
        )
        self.name_text.pack(fill="both", expand=True)
        self.name_text.bind("<<Modified>>", self._on_name_text_modified)

        self.name_count_lbl = tk.Label(
            name_card,
            text="已输入 0 行名称（未填则沿用原名）",
            bg=THEME["card"],
            fg=THEME["muted"],
            font=("Microsoft YaHei UI", 8),
        )
        self.name_count_lbl.pack(anchor="w", pady=(3, 0))

        # 底部进度与主按钮
        bottom = tk.Frame(self.root, bg=THEME["bg"])
        bottom.pack(fill="x", padx=14, pady=(0, 10))

        tk.Frame(bottom, height=8, bg=THEME["bg"], bd=0, highlightthickness=0).pack(fill="x")

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
            width=590,
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

    def _sync_size_limit_state(self):
        enabled = bool(self.limit_size_enabled.get())
        self.max_kb_spin.configure(state="normal" if enabled else "disabled")
        if enabled:
            self.max_kb_unit_lbl.configure(text="KB（超出自动压缩）", fg=THEME["muted"])
        else:
            self.max_kb_unit_lbl.configure(text="KB（已关闭：原像素尺寸无限制输出）", fg=THEME["accent"])

    def _sync_mode_panels(self):
        is_single = (self.mode_choice.get() == "single")
        if is_single:
            self.ensemble_container.pack_forget()
            self.single_container.pack(fill="x", before=self.mode_anchor)
        else:
            self.single_container.pack_forget()
            self.ensemble_container.pack(fill="x", before=self.mode_anchor)

        use_custom = bool(self.use_custom_model_path.get())
        self.custom_path_entry.configure(state="normal" if (is_single and use_custom) else "disabled")
        self.pick_onnx_btn.set_enabled(is_single and use_custom and not self._is_downloading_model and not self._is_running)

        chosen = self.single_model.get()
        custom_p = self.custom_model_file.get().strip() if use_custom else ""
        ready = is_model_ready(chosen, custom_file=custom_p)

        if use_custom:
            if ready:
                self.model_state_lbl.configure(text="自定义 .onnx：已找到", fg=THEME["ok_green"])
            else:
                self.model_state_lbl.configure(text="自定义 .onnx：未选择或无效", fg=THEME["err_red"])
            self.download_model_btn.set_text("下载模型")
            self.download_model_btn.set_enabled(False)
        else:
            if ready:
                self.model_state_lbl.configure(text=f"{chosen}：已下载", fg=THEME["ok_green"])
                self.download_model_btn.set_text("已就绪")
                self.download_model_btn.set_enabled(False)
            else:
                self.model_state_lbl.configure(text=f"{chosen}：未下载", fg=THEME["err_red"])
                self.download_model_btn.set_text("下载模型")
                self.download_model_btn.set_enabled(not self._is_downloading_model and not self._is_running)

    def choose_custom_onnx_file(self):
        file_path = filedialog.askopenfilename(
            title="选择 .onnx 模型文件",
            filetypes=[("ONNX 模型文件", "*.onnx"), ("所有文件", "*.*")],
        )
        if file_path:
            self.custom_model_file.set(str(Path(file_path)))
            self._sync_mode_panels()

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
        self._apply_name_lines_preview()
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

    def download_selected_model_ui(self):
        if self._is_downloading_model or self._is_running:
            return
        target_model = self.single_model.get()
        if is_model_ready(target_model):
            self._sync_mode_panels()
            return

        self._is_downloading_model = True
        self.download_model_btn.set_enabled(False)
        self.start_btn.set_enabled(False)
        self.status.set(f"正在通过国内镜像下载模型 [{target_model}] ……")

        def _dl_worker():
            try:
                saved_path = download_model_via_mirrors(
                    target_model,
                    status_cb=lambda msg: self.msgq.put(("status", msg)) if not self._closing else None,
                )
                if not self._closing:
                    self.msgq.put(("model_dl_done", True, target_model, str(saved_path)))
            except Exception:
                if not self._closing:
                    self.msgq.put(("model_dl_done", False, target_model, traceback.format_exc()))

        threading.Thread(target=_dl_worker, daemon=True).start()

    def _ask_conflict_or_done_policy(self, done_count: int, disk_exist_count: int, overlap_total: int, total_count: int) -> str | None:
        dlg = tk.Toplevel(self.root)
        dlg.title("已存在 / 已处理文件确认")
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.resizable(False, False)
        dlg.configure(bg=THEME["bg"])
        center_window_on_screen(dlg, 410, 210)

        card = SoftPillCard(
            dlg,
            radius=22,
            bg_color=THEME["card"],
            border_color=THEME["border"],
            parent_bg=THEME["bg"],
            padx=18,
            pady=14,
            outer_pady=(10, 10),
        )
        box = card.inner

        choice = {"val": None}

        detail_parts = []
        if done_count > 0:
            detail_parts.append(f"列表已处理(✔) {done_count} 张")
        if disk_exist_count > 0:
            detail_parts.append(f"导出目录已有同名文件 {disk_exist_count} 张")
        detail_str = "，".join(detail_parts)

        tk.Label(
            box,
            text=f"共 {total_count} 张待处理（涉及重复/已完成 {overlap_total} 张）",
            bg=THEME["card"],
            fg=THEME["text"],
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(pady=(2, 4))

        tk.Label(
            box,
            text=f"{detail_str}\n请选择统一处理方式：",
            bg=THEME["card"],
            fg=THEME["muted"],
            font=("Microsoft YaHei UI", 9),
            justify="center",
        ).pack(pady=(0, 14))

        btn_row = tk.Frame(box, bg=THEME["card"])
        btn_row.pack(pady=(0, 4))

        def _pick(v):
            choice["val"] = v
            dlg.destroy()

        SoftPillButton(
            btn_row,
            text="替换重新生成",
            command=lambda: _pick("regen"),
            width=118,
            height=32,
            radius=16,
            bg_color=THEME["accent"],
            hover_color=THEME["accent_hover"],
            fg_color="#FFFFFF",
            wave_color="#BFDBFE",
            font=("Microsoft YaHei UI", 9, "bold"),
            parent_bg=THEME["card"],
        ).pack(side="left", padx=5)

        SoftPillButton(
            btn_row,
            text="跳过不生成",
            command=lambda: _pick("skip"),
            width=108,
            height=32,
            radius=16,
            bg_color=THEME["btn_sec"],
            hover_color=THEME["btn_sec_hover"],
            fg_color=THEME["btn_sec_fg"],
            wave_color="#93C5FD",
            font=("Microsoft YaHei UI", 9, "bold"),
            parent_bg=THEME["card"],
        ).pack(side="left", padx=5)

        SoftPillButton(
            btn_row,
            text="取消",
            command=lambda: _pick(None),
            width=74,
            height=32,
            radius=16,
            bg_color="#F1F5F9",
            hover_color="#E2E8F0",
            fg_color=THEME["muted"],
            wave_color="#CBD5E1",
            font=("Microsoft YaHei UI", 9, "bold"),
            parent_bg=THEME["card"],
        ).pack(side="left", padx=5)

        dlg.protocol("WM_DELETE_WINDOW", lambda: _pick(None))
        self.root.wait_window(dlg)
        return choice["val"]

    def _get_target_model_specs(self):
        if self.mode_choice.get() == "single":
            m_name = self.single_model.get()
            custom_p = self.custom_model_file.get().strip() if self.use_custom_model_path.get() else ""
            return [(m_name, 1.0, custom_p)]
        specs = ENSEMBLE_PRESETS.get(self.ensemble_choice.get(), ENSEMBLE_PRESETS["双模型叠加 (birefnet + isnet)"])
        return [(m_name, w, "") for m_name, w in specs]

    def _build_session_for_spec(self, m_name: str, custom_p: str):
        if custom_p:
            src_onnx = Path(custom_p)
            if not src_onnx.is_file():
                raise FileNotFoundError(f"自定义模型文件不存在：{src_onnx}")
            target_dir = get_model_subdir(m_name)
            target_dir.mkdir(parents=True, exist_ok=True)
            linked_onnx = target_dir / f"{m_name}.onnx"
            if src_onnx.resolve() != linked_onnx.resolve():
                shutil.copy2(src_onnx, linked_onnx)
        return new_session(m_name)

    def start(self):
        if self._is_running or self._is_downloading_model or self._closing:
            return
        all_images = list(getattr(self, "_images", []))
        if not all_images:
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
            workers = max(1, min(4, int(self.thread_count.get())))
        except Exception:
            workers = 1

        target_specs = self._get_target_model_specs()
        missing_models = [
            m_name for m_name, _, custom_p in target_specs
            if not is_model_ready(m_name, custom_file=custom_p)
        ]
        if missing_models:
            messagebox.showwarning(
                "模型尚未就绪",
                f"当前任务需要模型：{', '.join(missing_models)}\n\n"
                f"请先在“选项一：选择单个模型”中下载该模型，或勾选“自定义路径”选择对应的 .onnx 文件。",
            )
            return

        self._apply_name_lines_preview()
        name_snapshot = list(self._name_lines)
        ext = self._current_ext()
        input_paths_snapshot = list(self.input_paths)

        # 建立每张图的任务元组：(idx, src, custom_stem)
        task_entries = []
        for idx, p in enumerate(all_images):
            c_stem = name_snapshot[idx] if idx < len(name_snapshot) else None
            task_entries.append((idx, p, c_stem))

        done_set = set()
        disk_exist_set = set()
        for _idx, p, c_stem in task_entries:
            key = str(p.resolve())
            if self._img_states.get(key) == "ok":
                done_set.add(key)
            _, target_file = compute_target_output_path(p, input_paths_snapshot, base_out, ext, c_stem)
            if target_file.exists():
                disk_exist_set.add(key)

        overlap_keys = done_set | disk_exist_set
        if overlap_keys:
            policy = self._ask_conflict_or_done_policy(
                done_count=len(done_set),
                disk_exist_count=len(disk_exist_set),
                overlap_total=len(overlap_keys),
                total_count=len(all_images),
            )
            if policy is None:
                return
            if policy == "skip":
                tasks_to_run = [t for t in task_entries if str(t[1].resolve()) not in overlap_keys]
                for _idx, p, _c in task_entries:
                    key = str(p.resolve())
                    self._img_states[key] = "ok" if key in overlap_keys else "pending"
                self._refresh_list_view(all_images)
                if not tasks_to_run:
                    self.status.set("所有重复/已完成文件均已跳过")
                    messagebox.showinfo("提示", "所选文件均已存在或已完成，已全部跳过。")
                    return
            else:
                tasks_to_run = list(task_entries)
                for _idx, p, _c in tasks_to_run:
                    self._img_states[str(p.resolve())] = "pending"
                self._refresh_list_view(all_images)
        else:
            tasks_to_run = list(task_entries)
            for _idx, p, _c in tasks_to_run:
                self._img_states[str(p.resolve())] = "pending"
            self._refresh_list_view(all_images)

        prepare_models_env()
        fmt = (self.output_format.get() or "JPG").upper()
        fine_edge = bool(self.fine_edge.get())
        limit_enabled = bool(self.limit_size_enabled.get())
        max_kb_val = int(self.max_kb.get())

        self._is_running = True
        mode_desc = "+".join(m[0] for m in target_specs)
        self.status.set(f"正在加载模型 [{mode_desc}] ……")
        self.start_btn.set_enabled(False)
        self.download_model_btn.set_enabled(False)
        self.pick_onnx_btn.set_enabled(False)
        self.progress.configure(maximum=len(tasks_to_run), value=0)

        threading.Thread(
            target=self._worker,
            args=(tasks_to_run, input_paths_snapshot, base_out, rgb, fmt, limit_enabled, max_kb_val, target_specs, fine_edge, workers, ext),
            daemon=True,
        ).start()

    def _run_model_alpha(self, im_rgba, session):
        cut = remove(im_rgba, session=session)
        if not isinstance(cut, Image.Image):
            cut_img = Image.open(io.BytesIO(cut)).convert("RGBA")
        else:
            cut_img = cut.convert("RGBA")
        alpha = cut_img.split()[3]
        if cut_img is not cut:
            cut_img.close()
        return alpha

    def _fuse_alphas(self, im_rgba, weighted_sessions):
        if len(weighted_sessions) == 1:
            return self._run_model_alpha(im_rgba, weighted_sessions[0][2])

        chops = ImageChops
        if chops is None:
            from PIL import ImageChops as _chops
            chops = _chops

        portrait_alpha = None
        support_alphas = []

        for m_name, _weight, sess in weighted_sessions:
            a = self._run_model_alpha(im_rgba, sess)
            if m_name == DEFAULT_MODEL and portrait_alpha is None:
                portrait_alpha = a
            else:
                support_alphas.append(a)

        if portrait_alpha is None and support_alphas:
            portrait_alpha = support_alphas.pop(0)

        if not support_alphas:
            return portrait_alpha

        support_max = support_alphas[0]
        for other in support_alphas[1:]:
            next_max = chops.lighter(support_max, other)
            if support_max is not support_alphas[0]:
                support_max.close()
            other.close()
            support_max = next_max

        flt = ImageFilter
        if flt is None:
            from PIL import ImageFilter as _flt
            flt = _flt
        support_soft = support_max.filter(flt.GaussianBlur(radius=1.0))
        if support_max is not support_alphas[0]:
            support_max.close()

        if HAS_NUMPY:
            p_arr = np.asarray(portrait_alpha, dtype=np.float32)
            s_arr = np.asarray(support_soft, dtype=np.float32)
            weighted = 0.78 * p_arr + 0.22 * s_arr
            fused_arr = np.clip(np.maximum(weighted, s_arr), 0, 255).astype(np.uint8)
            fused = Image.fromarray(fused_arr, mode="L")
        else:
            w, h = im_rgba.size
            fused = Image.new("L", (w, h), 0)
            p_px = portrait_alpha.load()
            s_px = support_soft.load()
            f_px = fused.load()
            for y in range(h):
                for x in range(w):
                    pv = p_px[x, y]
                    sv = s_px[x, y]
                    val = max(sv, int(0.78 * pv + 0.22 * sv + 0.5))
                    f_px[x, y] = 255 if val > 255 else (0 if val < 0 else val)

        try:
            portrait_alpha.close()
        except Exception:
            pass
        try:
            support_soft.close()
        except Exception:
            pass
        return fused

    def _clean_fringe_and_composite(self, im_rgba, alpha_img, rgb, fine_edge):
        if HAS_NUMPY:
            rgba_arr = np.asarray(im_rgba, dtype=np.float32)
            a_arr = np.asarray(alpha_img, dtype=np.float32)
            r = rgba_arr[:, :, 0]
            g = rgba_arr[:, :, 1]
            b = rgba_arr[:, :, 2]

            edge_mask = (a_arr > 0.0) & (a_arr < 240.0)
            if np.any(edge_mask):
                luma = 0.299 * r + 0.587 * g + 0.114 * b
                edge_ratio = np.clip((240.0 - a_arr) / 240.0, 0.0, 1.0)
                bright_mask = edge_mask & (luma > 165.0)
                if np.any(bright_mask):
                    base_str = 0.45 if fine_edge else 0.28
                    strength = np.minimum(0.85, base_str + 0.40 * edge_ratio)
                    r = np.where(bright_mask, r * (1.0 - strength) + rgb[0] * strength, r)
                    g = np.where(bright_mask, g * (1.0 - strength) + rgb[1] * strength, g)
                    b = np.where(bright_mask, b * (1.0 - strength) + rgb[2] * strength, b)

                if fine_edge:
                    soft_mask = edge_mask & (~bright_mask) & (a_arr < 120.0)
                    if np.any(soft_mask):
                        strength2 = np.minimum(0.45, 0.22 + 0.22 * edge_ratio)
                        r = np.where(soft_mask, r * (1.0 - strength2) + rgb[0] * strength2, r)
                        g = np.where(soft_mask, g * (1.0 - strength2) + rgb[1] * strength2, g)
                        b = np.where(soft_mask, b * (1.0 - strength2) + rgb[2] * strength2, b)

            alpha_norm = a_arr / 255.0
            out_r = r * alpha_norm + rgb[0] * (1.0 - alpha_norm)
            out_g = g * alpha_norm + rgb[1] * (1.0 - alpha_norm)
            out_b = b * alpha_norm + rgb[2] * (1.0 - alpha_norm)
            out_arr = np.stack([out_r, out_g, out_b], axis=-1)
            return Image.fromarray(np.clip(out_arr, 0, 255).astype(np.uint8), mode="RGB")

        r_ch, g_ch, b_ch, _ = im_rgba.split()
        a_px = alpha_img.load()
        r_px = r_ch.load()
        g_px = g_ch.load()
        b_px = b_ch.load()
        w, h = im_rgba.size
        for y in range(h):
            for x in range(w):
                alpha_val = a_px[x, y]
                if 0 < alpha_val < 240:
                    rv, gv, bv = r_px[x, y], g_px[x, y], b_px[x, y]
                    luma = 0.299 * rv + 0.587 * gv + 0.114 * bv
                    edge_ratio = (240.0 - alpha_val) / 240.0
                    if luma > 165.0:
                        strength = min(0.85, (0.45 if fine_edge else 0.28) + 0.40 * edge_ratio)
                        r_px[x, y] = int(rv * (1.0 - strength) + rgb[0] * strength)
                        g_px[x, y] = int(gv * (1.0 - strength) + rgb[1] * strength)
                        b_px[x, y] = int(bv * (1.0 - strength) + rgb[2] * strength)
        cut_cleaned = Image.merge("RGBA", (r_ch, g_ch, b_ch, alpha_img))
        bg = Image.new("RGBA", cut_cleaned.size, (*rgb, 255))
        bg.alpha_composite(cut_cleaned)
        res = bg.convert("RGB")
        bg.close()
        cut_cleaned.close()
        return res

    def _process_single_image(
        self,
        src: Path,
        c_stem: str | None,
        input_paths: list,
        weighted_sessions,
        base_out: Path,
        rgb,
        fmt,
        limit_enabled: bool,
        max_kb: int,
        fine_edge: bool,
        ext: str,
    ):
        im = None
        a_ch = None
        result = None
        try:
            im = ImageOps.exif_transpose(Image.open(src)).convert("RGBA")
            a_ch = self._fuse_alphas(im, weighted_sessions)
            result = self._clean_fringe_and_composite(im, a_ch, rgb, fine_edge)

            out_folder, target = compute_target_output_path(src, input_paths, base_out, ext, c_stem)
            target.parent.mkdir(parents=True, exist_ok=True)
            data = encode_image_output(result, fmt, limit_enabled, max_kb)
            target.write_bytes(data)
            return out_folder, target.name
        finally:
            for obj in (result, a_ch, im):
                if obj is not None:
                    try:
                        obj.close()
                    except Exception:
                        pass

    def _worker(self, tasks_to_run, input_paths, base_out, rgb, fmt, limit_enabled, max_kb, target_specs, fine_edge, workers, ext):
        weighted_sessions = []
        executor = None
        try:
            if remove is None or new_session is None:
                self.msgq.put(("fatal", "缺少 rembg 组件，请检查打包是否完整。"))
                return

            prepare_models_env()

            for idx, (m_name, weight, custom_p) in enumerate(target_specs, 1):
                if self._closing:
                    return
                self.msgq.put(("status", f"正在加载模型 ({idx}/{len(target_specs)})：{m_name} ……"))
                try:
                    sess = self._build_session_for_spec(m_name, custom_p)
                    weighted_sessions.append((m_name, weight, sess))
                except Exception as e:
                    self.msgq.put((
                        "fatal",
                        f"AI 模型 [{m_name}] 加载失败：{e}\n模型目录：{get_model_subdir(m_name)}",
                    ))
                    return

            if self._closing:
                return

            engine_tip = "+".join(m[0] for m in target_specs)
            total_tasks = len(tasks_to_run)
            self.msgq.put(("status", f"[{engine_tip}] 已就绪，开始处理 {total_tasks} 张图片……"))

            effective_workers = max(1, min(workers, total_tasks))
            ok = 0
            done_count = 0
            errors = []
            exported_folders = set()
            lock = threading.Lock()

            executor = ThreadPoolExecutor(max_workers=effective_workers)
            future_map = {
                executor.submit(
                    self._process_single_image,
                    src,
                    c_stem,
                    input_paths,
                    weighted_sessions,
                    base_out,
                    rgb,
                    fmt,
                    limit_enabled,
                    max_kb,
                    fine_edge,
                    ext,
                ): (idx, src, c_stem)
                for idx, src, c_stem in tasks_to_run
            }

            for fut in as_completed(future_map):
                if self._closing:
                    break
                _idx, src, _c_stem = future_map[fut]
                try:
                    out_folder, out_fname = fut.result()
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
                        total_tasks,
                        f"处理中 {cur_done}/{total_tasks}（成功 {cur_ok}）：{src.name} -> {out_fname} ✔",
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
                        total_tasks,
                        f"处理中 {cur_done}/{total_tasks}（失败）：{src.name} ✗",
                    ))
                if done_count % 3 == 0:
                    gc.collect()

            if not self._closing:
                summary_out = "；".join(sorted(exported_folders)) if exported_folders else str(base_out)
                self.msgq.put(("done", ok, total_tasks, errors, summary_out))
        except Exception:
            if not self._closing:
                self.msgq.put(("fatal", f"后台处理线程发生未捕获异常：\n{traceback.format_exc()}"))
        finally:
            if executor is not None:
                try:
                    executor.shutdown(wait=False, cancel_futures=True)
                except Exception:
                    pass
            for _, _, sess in weighted_sessions:
                try:
                    del sess
                except Exception:
                    pass
            weighted_sessions.clear()
            gc.collect()

    def _poll(self):
        if self._closing:
            return
        try:
            while True:
                item = self.msgq.get_nowait()
                kind = item[0]
                if kind == "status":
                    self.status.set(item[1])
                elif kind == "model_dl_done":
                    _, ok_flag, m_name, info_str = item
                    self._is_downloading_model = False
                    self.start_btn.set_enabled(True)
                    self._sync_mode_panels()
                    if ok_flag:
                        self.status.set(f"模型 [{m_name}] 已下载就绪")
                        messagebox.showinfo("下载完成", f"模型 [{m_name}] 已下载并准备就绪：\n{info_str}")
                    else:
                        self.status.set(f"模型 [{m_name}] 下载失败")
                        messagebox.showerror("下载失败", f"模型 [{m_name}] 下载出错：\n{info_str}")
                elif kind == "item_done":
                    _, src, state, v, total, msg = item
                    self._mark_image_state(src, state)
                    self.progress.configure(value=v)
                    self.status.set(msg)
                elif kind == "fatal":
                    self._is_running = False
                    self.start_btn.set_enabled(True)
                    self._sync_mode_panels()
                    self.status.set("处理中止，请查看错误提示")
                    messagebox.showerror("无法处理", item[1])
                elif kind == "done":
                    _, ok, total, errors, out_summary = item
                    self._is_running = False
                    self.start_btn.set_enabled(True)
                    self._sync_mode_panels()
                    self.status.set(f"完成：成功 {ok} 张，失败 {len(errors)} 张")
                    msg = f"处理完成！\n成功：{ok}/{total} 张\n导出文件夹：{out_summary}"
                    if errors:
                        msg += "\n\n部分失败原因（最多显示 5 条）：\n" + "\n".join(errors[:5])
                    messagebox.showinfo("处理完成", msg)
        except queue.Empty:
            pass
        if not self._closing and self.root.winfo_exists():
            self._poll_job = self.root.after(100, self._poll)


def cli_download_default_model() -> int:
    try:
        prepare_models_env()
        saved = download_model_via_mirrors(DEFAULT_MODEL, status_cb=lambda msg: print(f"[INFO] {msg}"))
        print(f"[SUCCESS] 默认模型 {DEFAULT_MODEL} 已就绪 -> {saved}")
        return 0
    except Exception as e:
        print(f"[ERROR] 默认模型下载失败: {e}")
        traceback.print_exc()
        return 1


def main():
    if "--download-default-model" in sys.argv:
        sys.exit(cli_download_default_model())

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
                prepare_models_env()
                load_q.put(("STAGE", 20.0, "正在准备图像解码组件 (Pillow)..."))
                from PIL import (
                    Image as _Image,
                    ImageOps as _ImageOps,
                    ImageFilter as _ImageFilter,
                    ImageChops as _ImageChops,
                )
                Image, ImageOps, ImageFilter, ImageChops = _Image, _ImageOps, _ImageFilter, _ImageChops

                load_q.put(("STAGE", 55.0, "正在初始化 AI 抠图引擎 (rembg / onnxruntime)..."))
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
                os._exit(1)
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
        os._exit(1)


if __name__ == "__main__":
    main()