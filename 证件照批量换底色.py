# -*- coding: utf-8 -*-
"""
证件照批量换底色工具_Czq
功能：批量导入图片/文件夹（递归扫描子文件夹）、AI抠图、统一替换底色、
保留子文件夹结构和文件主名称、支持导出 JPG 或 PNG、支持自定义 AI 模型存放目录、
启动加载进度条与卡片式界面。
"""
import os
import sys
import io
import shutil
import threading
import queue
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, colorchooser, messagebox

APP_DISPLAY_NAME = "证件照批量换底色工具_Czq"
SPLASH_DISPLAY_NAME = "证件照批量换底色工具_czq"

# 1. 修复 PyInstaller --windowed 模式下 sys.stdout / sys.stderr 为 None 导致进度条库报错
if sys.stdout is None:
    sys.stdout = io.StringIO()
if sys.stderr is None:
    sys.stderr = io.StringIO()


def _detect_bundled_model_dir():
    """查找打包自带的 .u2net 模型目录（兼容 PyInstaller 6.x _internal 与同级目录）"""
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        for candidate in (exe_dir / "_internal" / ".u2net", exe_dir / ".u2net"):
            if candidate.exists():
                return candidate
    return None


_BUNDLED_MODEL_DIR = _detect_bundled_model_dir()
if _BUNDLED_MODEL_DIR is not None:
    os.environ.setdefault("U2NET_HOME", str(_BUNDLED_MODEL_DIR))

# 延迟在启动加载窗口中导入的重型依赖
Image = None
ImageOps = None
remove = None
new_session = None
TkinterDnD = None
DND_FILES = None

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}
PRESETS = {
    "蓝色（常用）": "#438EDB",
    "白色": "#FFFFFF",
    "红色": "#D9001B",
    "浅蓝色": "#ADD8E6",
    "灰色": "#D9D9D9",
}

# 界面美化配色方案
THEME = {
    "bg": "#F4F7FB",
    "card": "#FFFFFF",
    "border": "#DCE4EE",
    "text": "#0F172A",
    "muted": "#64748B",
    "accent": "#2563EB",
    "accent_hover": "#1D4ED8",
    "drop_bg": "#EFF6FF",
    "drop_border": "#93C5FD",
    "drop_fg": "#1E40AF",
    "list_bg": "#F8FAFC",
}


def hex_to_rgb(value):
    value = value.strip().lstrip("#")
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

    # 默认 JPG：先二分查找最高画质，必要时逐步缩小尺寸
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


def prepare_model_directory(target_dir_str, status_cb=None):
    """
    准备自定义模型存放目录：
    1. 支持用户手输或点选任意目录（如 D:\\AI模型\\.u2net）
    2. 若目标目录尚无模型且打包自带了模型，则自动复制过去，避免重复下载并省出 C 盘空间
    """
    raw = (target_dir_str or "").strip()
    if not raw:
        raise ValueError("请先填写或选择一个 AI 模型存放目录。")

    target_dir = Path(raw).expanduser()
    target_dir.mkdir(parents=True, exist_ok=True)

    if _BUNDLED_MODEL_DIR is not None and _BUNDLED_MODEL_DIR.resolve() != target_dir.resolve():
        has_existing_files = any(target_dir.iterdir())
        if not has_existing_files:
            if status_cb:
                status_cb(f"正在复制模型到 {target_dir} ……")
            for item in _BUNDLED_MODEL_DIR.iterdir():
                dest = target_dir / item.name
                if item.is_dir():
                    shutil.copytree(item, dest, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, dest)

    os.environ["U2NET_HOME"] = str(target_dir)
    return target_dir


class LoadingWindow:
    """启动时的加载提示窗口，避免长时间无响应错觉"""
    def __init__(self, root):
        self.root = root
        self.win = tk.Toplevel(root)
        self.win.title(SPLASH_DISPLAY_NAME)
        self.win.geometry("420x168")
        self.win.resizable(False, False)
        self.win.overrideredirect(True)
        self._center()

        frame = tk.Frame(
            self.win,
            bg="#FFFFFF",
            highlightbackground="#2563EB",
            highlightthickness=2,
        )
        frame.pack(fill="both", expand=True)

        tk.Label(
            frame,
            text=SPLASH_DISPLAY_NAME,
            bg="#FFFFFF",
            fg="#0F172A",
            font=("Microsoft YaHei UI", 12, "bold"),
        ).pack(pady=(20, 4))

        self.status_var = tk.StringVar(value="正在初始化程序环境，请稍候...")
        tk.Label(
            frame,
            textvariable=self.status_var,
            bg="#FFFFFF",
            fg="#475569",
            font=("Microsoft YaHei UI", 9),
        ).pack(pady=(0, 12))

        self.bar = ttk.Progressbar(frame, mode="determinate", maximum=100, length=340)
        self.bar.pack(pady=(0, 8))

        tk.Label(
            frame,
            text="首次加载 AI 抠图组件可能需要几十秒，请勿关闭",
            bg="#FFFFFF",
            fg="#94A3B8",
            font=("Microsoft YaHei UI", 8),
        ).pack()

    def _center(self):
        self.win.update_idletasks()
        sw = self.win.winfo_screenwidth()
        sh = self.win.winfo_screenheight()
        x = (sw - 420) // 2
        y = (sh - 168) // 2
        self.win.geometry(f"420x168+{x}+{y}")

    def update_progress(self, value, text):
        self.bar["value"] = value
        self.status_var.set(text)
        self.win.update_idletasks()

    def close(self):
        try:
            self.win.destroy()
        except Exception:
            pass


class App:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_DISPLAY_NAME)
        self.root.geometry("620x780")
        self.root.minsize(560, 680)
        self.root.configure(bg=THEME["bg"])

        default_model_path = (
            os.environ.get("U2NET_HOME")
            or str(Path.home() / "Documents" / "证件照模型_.u2net")
        )

        self.msgq = queue.Queue()
        self.input_paths = []
        self._images = []
        self.output_dir = tk.StringVar(value=str(Path.home() / "Pictures" / "证件照换底色结果"))
        self.model_dir = tk.StringVar(value=default_model_path)
        self.color = tk.StringVar(value=PRESETS["蓝色（常用）"])
        self.output_format = tk.StringVar(value="JPG")
