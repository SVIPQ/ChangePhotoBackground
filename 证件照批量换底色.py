# -*- coding: utf-8 -*-
"""
证件照批量换底色工具_Czq
功能：批量导入图片/文件夹（递归扫描子文件夹）、多线程并发 AI 抠图、统一替换底色、
保留子文件夹结构和文件主名称、支持导出 JPG 或 PNG、支持自定义 AI 模型存放目录、
支持精细边缘模式（加强头发丝细节）、白蓝清爽界面、无四角尖锐的圆润加载窗与平滑进度条。
"""
import os
import sys
import io
import math
import shutil
import threading
import queue
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, colorchooser, messagebox

APP_DISPLAY_NAME = "证件照批量换底色工具_Czq"
SPLASH_DISPLAY_NAME = "证件照批量换底色工具_czq"

if sys.stdout is None:
    sys.stdout = io.StringIO()
if sys.stderr is None:
    sys.stderr = io.StringIO()


def _read_installer_model_dir():
    """读取 Inno Setup 安装时写入的自定义模型目录（Windows 注册表）"""
    if os.name != "nt":
        return None
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\证件照批量换底色工具_Czq",
            0,
            winreg.KEY_READ,
        ) as key:
            val, _ = winreg.QueryValueEx(key, "ModelDir")
            if val and str(val).strip():
                return str(val).strip()
    except Exception:
        pass
    return None


def _detect_bundled_model_dir():
    """查找打包自带的 .u2net 模型目录（兼容 PyInstaller 6.x _internal 与同级目录）"""
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        for candidate in (exe_dir / "_internal" / ".u2net", exe_dir / ".u2net"):
            if candidate.exists():
                return candidate
    return None


_BUNDLED_MODEL_DIR = _detect_bundled_model_dir()
_INSTALLER_MODEL_DIR = _read_installer_model_dir()

if _INSTALLER_MODEL_DIR:
    os.environ.setdefault("U2NET_HOME", _INSTALLER_MODEL_DIR)
elif _BUNDLED_MODEL_DIR is not None:
    os.environ.setdefault("U2NET_HOME", str(_BUNDLED_MODEL_DIR))

Image = None
ImageOps = None
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

THEME = {
    "bg": "#F2F6FC",
    "card": "#FFFFFF",
    "border": "#CBD5E1",
    "text": "#0F172A",
    "muted": "#64748B",
    "accent": "#2563EB",
    "accent_hover": "#1D4ED8",
    "btn_sec": "#EBF2FF",
    "btn_sec_fg": "#1E3A8A",
    "drop_bg": "#EFF6FF",
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
    """准备自定义模型存放目录，若目标为空且打包自带模型则自动复制。"""
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


def _rounded_rect_points(x1, y1, x2, y2, r, segments=18):
    r = max(0, min(r, (x2 - x1) / 2.0, (y2 - y1) / 2.0))
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


def create_rounded_rect(canvas, x1, y1, x2, y2, r=20, **kwargs):
    pts = _rounded_rect_points(x1, y1, x2, y2, r=r, segments=18)
    return canvas.create_polygon(pts, smooth=False, **kwargs)


class LoadingWindow:
    """无四角尖锐感、带平滑绿色进度条的启动加载窗"""
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

        # 绘制纯圆角卡片底板，外围四角透明，彻底去掉尖锐边角
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
        self.status_var = tk.StringVar(value="正在初始化程序环境，请稍候...")
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
                self.bar["value"] = max(0, min(100, float(value)))
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
        self.root.geometry("680x860")
        self.root.minsize(600, 760)
        self.root.configure(bg=THEME["bg"])

        default_model_path = (
            os.environ.get("U2NET_HOME")
            or str(Path.home() / "Documents" / "证件照模型_.u2net")
        )
        default_output_path = str(Path.home() / "Desktop" / "证件照换底色结果")
        cpu_count = os.cpu_count() or 4
        default_threads = max(2, min(4, cpu_count // 2))

        self.msgq = queue.Queue()
        self.input_paths = []
        self._images = []
        self.output_dir = tk.StringVar(value=default_output_path)
        self.model_dir = tk.StringVar(value=default_model_path)
        self.color = tk.StringVar(value=PRESETS["蓝色（常用）"])
        self.output_format = tk.StringVar(value="JPG")
        self.max_kb = tk.IntVar(value=50)
        self.thread_count = tk.IntVar(value=default_threads)
        self.fine_edge = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="等待导入照片或文件夹")

        self._setup_styles()
        self._build()
        self._poll()

    def _setup_styles(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(
            "Blue.Horizontal.TProgressbar",
            troughcolor="#E2E8F0",
            background=THEME["accent"],
            thickness=10,
        )

    def _make_card(self, parent, pady=(0, 10)):
        outer = tk.Frame(
            parent,
            bg=THEME["card"],
            highlightbackground=THEME["border"],
            highlightthickness=1,
        )
        outer.pack(fill="x", padx=16, pady=pady)
        inner = tk.Frame(outer, bg=THEME["card"])
        inner.pack(fill="both", expand=True, padx=14, pady=10)
        return inner

    def _make_sec_btn(self, parent, text, command, width=10):
        return tk.Button(
            parent,
            text=text,
            command=command,
            width=width,
            bg=THEME["btn_sec"],
            fg=THEME["btn_sec_fg"],
            activebackground="#DBEAFE",
            activeforeground=THEME["btn_sec_fg"],
            relief="groove",
            bd=1,
            cursor="hand2",
            font=("Microsoft YaHei UI", 9, "bold"),
        )

    def _build(self):
        header = tk.Frame(self.root, bg=THEME["bg"])
        header.pack(fill="x", padx=16, pady=(12, 8))
        tk.Label(
            header,
            text=APP_DISPLAY_NAME,
            bg=THEME["bg"],
            fg=THEME["text"],
            font=("Microsoft YaHei UI", 16, "bold"),
        ).pack(anchor="w")
        tk.Label(
            header,
            text="多线程并发 AI 抠图 · 精细边缘可选 · 保留子文件夹层级与文件名 · 支持 JPG / PNG",
            bg=THEME["bg"],
            fg=THEME["muted"],
            font=("Microsoft YaHei UI", 9),
        ).pack(anchor="w", pady=(2, 0))

        import_card = self._make_card(self.root)
        drop_box = tk.Frame(
            import_card,
            bg=THEME["drop_bg"],
            highlightbackground="#93C5FD",
            highlightthickness=1,
        )
        drop_box.pack(fill="x", pady=(0, 8))
        tk.Label(
            drop_box,
            text="点击下方“添加照片”或“添加文件夹”选择待处理图片",
            bg=THEME["drop_bg"],
            fg=THEME["drop_fg"],
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(pady=(8, 2))
        tk.Label(
            drop_box,
            text="支持整批照片、总文件夹及多层子文件夹递归扫描",
            bg=THEME["drop_bg"],
            fg="#3B82F6",
            font=("Microsoft YaHei UI", 8),
        ).pack(pady=(0, 8))

        btn_row = tk.Frame(import_card, bg=THEME["card"])
        btn_row.pack(fill="x", pady=(0, 8))
        self._make_sec_btn(btn_row, "添加照片", self.add_files, width=11).pack(side="left", padx=(0, 8))
        self._make_sec_btn(btn_row, "添加文件夹", self.add_folder, width=12).pack(side="left")
        self._make_sec_btn(btn_row, "清空", self.clear, width=9).pack(side="right")

        list_frame = tk.Frame(
            import_card,
            bg=THEME["list_bg"],
            highlightbackground=THEME["border"],
            highlightthickness=1,
        )
        list_frame.pack(fill="x")
        self.listbox = tk.Listbox(
            list_frame,
            height=5,
            bg=THEME["list_bg"],
            fg=THEME["text"],
            selectbackground=THEME["accent"],
            selectforeground="#FFFFFF",
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            font=("Microsoft YaHei UI", 9),
        )
        self.listbox.pack(fill="x", padx=6, pady=6)

        set_card = self._make_card(self.root)
        tk.Label(
            set_card,
            text="处理参数设置",
            bg=THEME["card"],
            fg=THEME["text"],
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(anchor="w", pady=(0, 8))

        r1 = tk.Frame(set_card, bg=THEME["card"])
        r1.pack(fill="x", pady=(0, 8))
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

        self.swatch = tk.Label(
            r1,
            text="   ",
            bg=self.color.get(),
            relief="solid",
            bd=1,
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
        self._make_sec_btn(r1, "自定义颜色", self.choose_color, width=12).pack(side="right")

        r2 = tk.Frame(set_card, bg=THEME["card"])
        r2.pack(fill="x", pady=(0, 8))
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

        tk.Label(r2, text="大小上限：", bg=THEME["card"], fg=THEME["text"], font=("Microsoft YaHei UI", 9)).pack(side="left")
        ttk.Spinbox(
            r2,
            from_=10,
            to=2000,
            textvariable=self.max_kb,
            width=7,
            font=("Microsoft YaHei UI", 9),
        ).pack(side="left", padx=(4, 4))
        tk.Label(r2, text="KB", bg=THEME["card"], fg=THEME["muted"], font=("Microsoft YaHei UI", 9)).pack(side="left", padx=(0, 12))

        tk.Label(r2, text="并发线程：", bg=THEME["card"], fg=THEME["text"], font=("Microsoft YaHei UI", 9)).pack(side="left")
        ttk.Spinbox(
            r2,
            from_=1,
            to=8,
            textvariable=self.thread_count,
            width=5,
            font=("Microsoft YaHei UI", 9),
        ).pack(side="left", padx=(4, 4))
        tk.Label(r2, text="线程", bg=THEME["card"], fg=THEME["muted"], font=("Microsoft YaHei UI", 9)).pack(side="left")

        r_fine = tk.Frame(set_card, bg=THEME["card"])
        r_fine.pack(fill="x")
        tk.Checkbutton(
            r_fine,
            text="精细边缘模式（加强头发丝与边缘过渡，耗时更长、CPU 占用更高）",
            variable=self.fine_edge,
            bg=THEME["card"],
            fg=THEME["text"],
            selectcolor="#EFF6FF",
            activebackground=THEME["card"],
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(anchor="w")

        dir_card = self._make_card(self.root)
        tk.Label(
            dir_card,
            text="输出文件夹（默认桌面，可点击“浏览”或“原目录”修改）",
            bg=THEME["card"],
            fg=THEME["text"],
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(anchor="w", pady=(0, 4))

        r3 = tk.Frame(dir_card, bg=THEME["card"])
        r3.pack(fill="x", pady=(0, 8))
        self.out_entry = tk.Entry(
            r3,
            textvariable=self.output_dir,
            font=("Microsoft YaHei UI", 9),
            bg="#F8FAFC",
            fg=THEME["text"],
            relief="solid",
            bd=1,
        )
        self.out_entry.pack(side="left", fill="x", expand=True, padx=(0, 6), ipady=3)
        self._make_sec_btn(r3, "原目录", self.use_source_dir_as_output, width=8).pack(side="left", padx=(0, 6))
        self._make_sec_btn(r3, "浏览", self.choose_output, width=8).pack(side="right")

        tk.Label(
            dir_card,
            text="AI 模型存放位置（跟随安装程序设置，可鼠标点选目录或键盘输入/粘贴路径）：",
            bg=THEME["card"],
            fg=THEME["text"],
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(anchor="w", pady=(0, 4))

        r4 = tk.Frame(dir_card, bg=THEME["card"])
        r4.pack(fill="x")
        self.model_entry = tk.Entry(
            r4,
            textvariable=self.model_dir,
            font=("Microsoft YaHei UI", 9),
            bg="#F8FAFC",
            fg=THEME["text"],
            relief="solid",
            bd=1,
        )
        self.model_entry.pack(side="left", fill="x", expand=True, padx=(0, 8), ipady=3)
        self.model_entry.bind("<Return>", self._on_model_entry_confirm)
        self.model_entry.bind("<FocusOut>", self._on_model_entry_confirm)
        self._make_sec_btn(r4, "选择目录", self.choose_model_dir, width=10).pack(side="right")

        bottom = tk.Frame(self.root, bg=THEME["bg"])
        bottom.pack(fill="x", padx=16, pady=(0, 12))

        self.progress = ttk.Progressbar(
            bottom,
            mode="determinate",
            style="Blue.Horizontal.TProgressbar",
        )
        self.progress.pack(fill="x", pady=(0, 4))
        tk.Label(
            bottom,
            textvariable=self.status,
            bg=THEME["bg"],
            fg=THEME["muted"],
            font=("Microsoft YaHei UI", 9),
        ).pack(anchor="w", pady=(0, 8))

        self.start_btn = tk.Button(
            bottom,
            text="开始批量处理",
            command=self.start,
            bg=THEME["accent"],
            fg="#FFFFFF",
            activebackground=THEME["accent_hover"],
            activeforeground="#FFFFFF",
            disabledforeground="#E2E8F0",
            relief="flat",
            bd=0,
            cursor="hand2",
            font=("Microsoft YaHei UI", 11, "bold"),
            height=2,
        )
        self.start_btn.pack(fill="x")

        tk.Label(
            bottom,
            text="提示：默认输出在桌面；模型目录与安装时一致；支持多线程并发处理，原图不会被修改。",
            bg=THEME["bg"],
            fg=THEME["muted"],
            font=("Microsoft YaHei UI", 8),
            wraplength=640,
            justify="left",
        ).pack(anchor="w", pady=(6, 0))

    def _add_paths(self, paths):
        valid = [p for p in paths if Path(p).exists()]
        self.input_paths.extend(valid)
        images = collect_images(self.input_paths)
        self.listbox.delete(0, tk.END)
        for p in images[:1000]:
            self.listbox.insert(tk.END, str(p))
        self.status.set(f"已找到 {len(images)} 张图片")
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

    def choose_model_dir(self):
        initial = self.model_dir.get().strip()
        init_path = Path(initial).expanduser()
        while init_path != init_path.parent and not init_path.exists():
            init_path = init_path.parent
        p = filedialog.askdirectory(
            title="选择存放 AI 抠图模型的文件夹（支持逐级点击选择，建议选非 C 盘）",
            initialdir=str(init_path) if init_path.exists() else None,
        )
        if p:
            self.model_dir.set(p)
            self.status.set(f"已选择模型目录：{p}")

    def _on_model_entry_confirm(self, _=None):
        typed = self.model_dir.get().strip()
        if typed:
            normalized = str(Path(typed).expanduser())
            self.model_dir.set(normalized)

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

    def start(self):
        images = getattr(self, "_images", [])
        if not images:
            messagebox.showwarning("提示", "请先添加照片或文件夹。")
            return
        out = Path(self.output_dir.get().strip())
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

        chosen_model_str = self.model_dir.get().strip()
        if not chosen_model_str:
            messagebox.showwarning("提示", "请输入或选择一个 AI 模型存放目录。")
            return

        try:
            workers = max(1, min(8, int(self.thread_count.get())))
        except Exception:
            workers = 2

        fmt = (self.output_format.get() or "JPG").upper()
        fine_edge = bool(self.fine_edge.get())
        self.start_btn.configure(state="disabled")
        self.progress.configure(maximum=len(images), value=0)
        threading.Thread(
            target=self._worker,
            args=(images, out, rgb, fmt, self.max_kb.get(), chosen_model_str, fine_edge, workers),
            daemon=True,
        ).start()

    def _process_single_image(self, src, session, out, rgb, fmt, max_kb, fine_edge, root_for, ext):
        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im).convert("RGBA")
            if fine_edge:
                cut = remove(
                    im,
                    session=session,
                    alpha_matting=True,
                    alpha_matting_foreground_threshold=240,
                    alpha_matting_background_threshold=10,
                    alpha_matting_erode_size=10,
                    post_process_mask=True,
                )
            else:
                cut = remove(im, session=session)
            if not isinstance(cut, Image.Image):
                cut = Image.open(io.BytesIO(cut)).convert("RGBA")
            else:
                cut = cut.convert("RGBA")

        bg = Image.new("RGBA", cut.size, (*rgb, 255))
        bg.alpha_composite(cut)
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

    def _worker(self, images, out, rgb, fmt, max_kb, chosen_model_str, fine_edge, workers):
        if remove is None or new_session is None:
            self.msgq.put(("fatal", "缺少 rembg 组件，请检查打包是否完整。"))
            return
        try:
            prepare_model_directory(
                chosen_model_str,
                status_cb=lambda text: self.msgq.put(("status", text)),
            )
            self.msgq.put(("status", f"正在加载 AI 抠图模型（{workers} 线程并发）……"))
            session = new_session("u2net")
        except Exception as e:
            self.msgq.put(("fatal", f"AI 模型加载失败：{e}"))
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
                elif kind == "progress":
                    _, v, total, msg = item
                    self.progress.configure(value=v)
                    self.status.set(msg)
                elif kind == "fatal":
                    self.start_btn.configure(state="normal")
                    messagebox.showerror("无法处理", item[1])
                elif kind == "done":
                    _, ok, total, errors, out = item
                    self.start_btn.configure(state="normal")
                    self.status.set(f"完成：成功 {ok} 张，失败 {len(errors)} 张")
                    msg = f"处理完成！\n成功：{ok}/{total} 张\n输出位置：{out}"
                    if errors:
                        msg += "\n\n部分失败原因（最多显示 5 条）：\n" + "\n".join(errors[:5])
                    messagebox.showinfo("处理完成", msg)
        except queue.Empty:
            pass
        self.root.after(150, self._poll)


def main():
    global Image, ImageOps, remove, new_session

    try:
        root = tk.Tk()
        root.withdraw()
        splash = LoadingWindow(root)
        load_q = queue.Queue()

        def load_deps():
            global Image, ImageOps, remove, new_session
            try:
                load_q.put((25, "正在加载图像处理组件 (Pillow)..."))
                from PIL import Image as _Image, ImageOps as _ImageOps
                Image, ImageOps = _Image, _ImageOps

                load_q.put((65, "正在加载 AI 抠图引擎 (rembg / onnxruntime)..."))
                from rembg import remove as _remove, new_session as _new_session
                remove, new_session = _remove, _new_session

                load_q.put((100, "加载完成，正在打开主界面..."))
                load_q.put(("DONE", None))
            except Exception as e:
                load_q.put(("ERROR", str(e)))

        def finish_load(err_msg=None):
            splash.close()
            if err_msg:
                messagebox.showerror("启动失败", f"组件加载出错：\n{err_msg}")
                root.destroy()
                return
            root.deiconify()
            App(root)

        state = {"target": 12.0, "current": 12.0}

        def poll_load():
            try:
                while True:
                    step, payload = load_q.get_nowait()
                    if step == "DONE":
                        splash.update_progress(100, "加载完成，正在打开主界面...")
                        root.after(120, lambda: finish_load(None))
                        return
                    elif step == "ERROR":
                        finish_load(payload)
                        return
                    else:
                        state["target"] = max(state["target"], float(step))
                        splash.update_progress(int(state["current"]), payload)
            except queue.Empty:
                pass

            cap = 94.0 if state["target"] < 100.0 else 100.0
            if state["current"] < cap:
                state["current"] = min(cap, state["current"] + 1.5)
                splash.update_progress(int(state["current"]))

            if root.winfo_exists():
                root.after(80, poll_load)

        threading.Thread(target=load_deps, daemon=True).start()
        root.after(80, poll_load)
        root.mainloop()
    except Exception as e:
        try:
            messagebox.showerror("启动异常", f"程序启动出错：\n{e}")
        except Exception:
            pass


if __name__ == "__main__":
    main()
