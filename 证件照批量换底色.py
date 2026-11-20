# -*- coding: utf-8 -*-
"""
证件照批量换底色工具_Czq
功能：批量导入图片/文件夹（递归扫描子文件夹）、AI抠图、统一替换底色、
保留子文件夹结构和文件主名称、支持导出 JPG 或 PNG、支持自定义 AI 模型存放目录、
支持精细边缘模式（加强头发丝细节）、白蓝圆润卡片与胶囊控件、点击回弹与波动动画。
"""
import os
import sys
import io
import math
import shutil
import threading
import queue
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

# 白蓝清爽配色 + 高圆润模块层次
THEME = {
    "bg": "#F2F6FC",
    "card": "#FFFFFF",
    "card_soft": "#F8FAFC",
    "border": "#D8E3F0",
    "text": "#0F172A",
    "muted": "#64748B",
    "accent": "#2563EB",
    "accent_hover": "#1D4ED8",
    "accent_disabled": "#93C5FD",
    "btn_sec": "#EBF2FF",
    "btn_sec_hover": "#DBEAFE",
    "btn_sec_fg": "#1E3A8A",
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
    1. 支持用户手输或点选任意目录（如 D:\\证件照模型\\.u2net）
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


def _rounded_rect_points(x1, y1, x2, y2, r, segments=20):
    """高密度角采样生成顺滑圆角/胶囊矩形顶点"""
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


def create_rounded_rect(canvas, x1, y1, x2, y2, r=18, **kwargs):
    pts = _rounded_rect_points(x1, y1, x2, y2, r=r, segments=20)
    return canvas.create_polygon(pts, smooth=False, **kwargs)


class RoundedCard(tk.Canvas):
    """高圆润模块卡片容器"""
    def __init__(self, parent, bg_color="#F2F6FC", card_bg="#FFFFFF", border_color="#D8E3F0", radius=22, **kwargs):
        super().__init__(parent, bg=bg_color, highlightthickness=0, bd=0, **kwargs)
        self.card_bg = card_bg
        self.border_color = border_color
        self.radius = radius
        self.inner = tk.Frame(self, bg=card_bg, bd=0, highlightthickness=0)
        self._win_id = self.create_window(14, 12, window=self.inner, anchor="nw")
        self.bind("<Configure>", self._on_resize)
        self.inner.bind("<Configure>", self._on_inner_resize)

    def _on_inner_resize(self, _event=None):
        req_w = self.inner.winfo_reqwidth()
        req_h = self.inner.winfo_reqheight()
        target_h = req_h + 24
        if int(float(self.cget("height") or 0)) != target_h:
            self.configure(height=target_h)
        cur_w = max(self.winfo_width(), req_w + 28)
        self.itemconfigure(self._win_id, width=max(10, cur_w - 28))
        self._redraw(cur_w, target_h)

    def _on_resize(self, event):
        w = max(40, event.width)
        h = max(40, event.height)
        self.itemconfigure(self._win_id, width=max(10, w - 28))
        self._redraw(w, h)

    def _redraw(self, w, h):
        self.delete("card_bg")
        create_rounded_rect(
            self,
            1,
            1,
            w - 1,
            h - 1,
            r=self.radius,
            fill=self.card_bg,
            outline=self.border_color,
            width=1.4,
            tags="card_bg",
        )
        self.tag_lower("card_bg")


class SmoothAnimatedButton(tk.Canvas):
    """圆润胶囊按钮：支持弹性缩放回弹、悬停呼吸与多层扩散波动动画"""
    def __init__(
        self,
        parent,
        text,
        command=None,
        width=120,
        height=34,
        radius=17,
        bg_color="#EBF2FF",
        hover_color="#DBEAFE",
        fg_color="#1E3A8A",
        wave_color="#60A5FA",
        font=("Microsoft YaHei UI", 9, "bold"),
        parent_bg="#FFFFFF",
    ):
        super().__init__(
            parent,
            width=width,
            height=height,
            bg=parent_bg,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )
        self.text = text
        self.command = command
        self._w = width
        self._h = height
        self.radius = radius
        self.bg_color = bg_color
        self.hover_color = hover_color
        self.fg_color = fg_color
        self.wave_color = wave_color
        self.font = font
        self.enabled = True
        self._hovered = False

        # 回弹与波动动画状态
        self._scale = 1.0
        self._vel = 0.0
        self._spring_job = None
        self._wave_job = None
        self._waves = []  # [cx, cy, radius_val, max_r, tick]

        self.bind("<Configure>", self._on_resize)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self._render()

    def set_state(self, state):
        self.enabled = (state != "disabled")
        self.configure(cursor="hand2" if self.enabled else "arrow")
        self._render()

    def _on_resize(self, event):
        if event.width > 10 and event.height > 10:
            self._w = event.width
            self._h = event.height
            self._render()

    def _on_enter(self, _event):
        if not self.enabled:
            return
        self._hovered = True
        self._animate_spring(target_scale=1.025)
        self._render()

    def _on_leave(self, _event):
        self._hovered = False
        self._animate_spring(target_scale=1.0)
        self._render()

    def _on_press(self, event):
        if not self.enabled:
            return
        self._scale = 0.93
        self._vel = 0.0
        max_r = max(self._w, self._h) * 0.85
        # 触发双层波动圈，模拟涟漪扩散感
        self._waves.append([event.x, event.y, 5.0, max_r, 0])
        self._waves.append([event.x, event.y, 1.0, max_r * 0.72, -3])
        self._start_wave_loop()
        self._render()

    def _on_release(self, event):
        if not self.enabled:
            return
        self._vel = 0.038
        self._animate_spring(target_scale=1.035 if self._hovered else 1.0)
        inside = (0 <= event.x <= self._w) and (0 <= event.y <= self._h)
        if inside and callable(self.command):
            self.after(70, self.command)

    def _animate_spring(self, target_scale):
        if self._spring_job is not None:
            try:
                self.after_cancel(self._spring_job)
            except Exception:
                pass
            self._spring_job = None

        stiffness = 0.30
        damping = 0.70

        def step():
            force = (target_scale - self._scale) * stiffness
            self._vel = (self._vel + force) * damping
            self._scale += self._vel
            self._render()
            if abs(target_scale - self._scale) > 0.002 or abs(self._vel) > 0.002:
                self._spring_job = self.after(16, step)
            else:
                self._scale = target_scale
                self._vel = 0.0
                self._spring_job = None
                self._render()

        self._spring_job = self.after(16, step)

    def _start_wave_loop(self):
        if self._wave_job is not None:
            return

        def wave_step():
            alive = []
            for cx, cy, r_val, max_r, tick in self._waves:
                next_tick = tick + 1
                if next_tick < 0:
                    alive.append([cx, cy, r_val, max_r, next_tick])
                    continue
                next_r = r_val + max(4.5, (max_r - r_val) * 0.24)
                if next_r < max_r and next_tick < 20:
                    alive.append([cx, cy, next_r, max_r, next_tick])
            self._waves = alive
            self._render()
            if self._waves:
                self._wave_job = self.after(20, wave_step)
            else:
                self._wave_job = None

        self._wave_job = self.after(20, wave_step)

    def _render(self):
        self.delete("all")
        w, h = self._w, self._h
        if w <= 4 or h <= 4:
            return

        if not self.enabled:
            fill = THEME["accent_disabled"]
            fg = "#F8FAFC"
            scale = 1.0
        else:
            fill = self.hover_color if self._hovered else self.bg_color
            fg = self.fg_color
            scale = self._scale

        box_w = w * scale
        box_h = h * scale
        pad_x = (w - box_w) / 2.0 + 1.5
        pad_y = (h - box_h) / 2.0 + 1.5
        x1 = pad_x
        y1 = pad_y
        x2 = max(x1 + 4, w - pad_x - 1.5)
        y2 = max(y1 + 4, h - pad_y - 1.5)
        r = min(self.radius, (x2 - x1) / 2.0, (y2 - y1) / 2.0)

        create_rounded_rect(self, x1, y1, x2, y2, r=r, fill=fill, outline="", width=0)

        for cx, cy, r_val, max_r, tick in self._waves:
            if tick < 0:
                continue
            progress = r_val / max(1.0, max_r)
            ring_w = max(1.0, 2.6 * (1.0 - progress))
            create_rounded_rect(
                self,
                max(x1 + 1, cx - r_val * 1.35),
                max(y1 + 1, cy - r_val * 0.75),
                min(x2 - 1, cx + r_val * 1.35),
                min(y2 - 1, cy + r_val * 0.75),
                r=r,
                fill="",
                outline=self.wave_color if progress < 0.75 else "#BFDBFE",
                width=ring_w,
            )

        self.create_text(w / 2.0, h / 2.0, text=self.text, fill=fg, font=self.font)


class PillToggle(tk.Canvas):
    """胶囊滑动开关组件（用于精细边缘模式，视觉更圆润现代）"""
    def __init__(self, parent, variable, text, parent_bg="#FFFFFF"):
        super().__init__(
            parent,
            width=52,
            height=26,
            bg=parent_bg,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )
        self.variable = variable
        self.text = text
        self._pos = 1.0 if bool(variable.get()) else 0.0
        self._job = None
        self.bind("<Button-1>", self._toggle)
        self.variable.trace_add("write", lambda *_: self._start_anim())
        self._draw()

    def _toggle(self, _event=None):
        self.variable.set(not bool(self.variable.get()))
        self._start_anim()

    def _start_anim(self):
        if self._job is not None:
            try:
                self.after_cancel(self._job)
            except Exception:
                pass
            self._job = None
        target = 1.0 if bool(self.variable.get()) else 0.0

        def step():
            diff = target - self._pos
            if abs(diff) > 0.04:
                self._pos += diff * 0.35
                self._draw()
                self._job = self.after(16, step)
            else:
                self._pos = target
                self._draw()
                self._job = None

        self._job = self.after(16, step)

    def _draw(self):
        self.delete("all")
        active = bool(self.variable.get()) or self._pos > 0.5
        track_fill = THEME["accent"] if active else "#CBD5E1"
        create_rounded_rect(self, 2, 2, 50, 24, r=11, fill=track_fill, outline="", width=0)
        knob_x = 4 + self._pos * 24
        create_rounded_rect(self, knob_x, 4, knob_x + 20, 22, r=9, fill="#FFFFFF", outline="", width=0)


class LoadingWindow:
    def __init__(self, root):
        self.root = root
        self.win = tk.Toplevel(root)
        self.win.title(SPLASH_DISPLAY_NAME)
        self.win.geometry("420x168")
        self.win.resizable(False, False)
        self.win.overrideredirect(True)
        self._center()

        canvas = tk.Canvas(self.win, width=420, height=168, bg="#F2F6FC", highlightthickness=0, bd=0)
        canvas.pack(fill="both", expand=True)
        create_rounded_rect(canvas, 3, 3, 417, 165, r=22, fill="#FFFFFF", outline="#2563EB", width=2)

        canvas.create_text(210, 38, text=SPLASH_DISPLAY_NAME, fill="#0F172A", font=("Microsoft YaHei UI", 12, "bold"))
        self.status_var = tk.StringVar(value="正在初始化程序环境，请稍候...")
        self.status_id = canvas.create_text(210, 66, text=self.status_var.get(), fill="#475569", font=("Microsoft YaHei UI", 9))

        self.bar = ttk.Progressbar(canvas, mode="determinate", maximum=100, length=340)
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

    def update_progress(self, value, text):
        try:
            if self.win.winfo_exists():
                self.bar["value"] = value
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
        self.root.geometry("620x780")
        self.root.minsize(560, 680)
        self.root.configure(bg=THEME["bg"])

        default_model_path = (
            os.environ.get("U2NET_HOME")
            or str(Path.home() / "Documents" / "证件照模型_.u2net")
        )
        default_output_path = str(Path.home() / "Desktop" / "证件照换底色结果")

        self.msgq = queue.Queue()
        self.input_paths = []
        self._images = []
        self.output_dir = tk.StringVar(value=default_output_path)
        self.model_dir = tk.StringVar(value=default_model_path)
        self.color = tk.StringVar(value=PRESETS["蓝色（常用）"])
        self.output_format = tk.StringVar(value="JPG")
        self.max_kb = tk.IntVar(value=50)
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

        style.configure("App.TFrame", background=THEME["bg"])
        style.configure("Card.TFrame", background=THEME["card"])
        style.configure(
            "Title.TLabel",
            background=THEME["bg"],
            foreground=THEME["text"],
            font=("Microsoft YaHei UI", 16, "bold"),
        )
        style.configure(
            "Subtitle.TLabel",
            background=THEME["bg"],
            foreground=THEME["muted"],
            font=("Microsoft YaHei UI", 9),
        )
        style.configure(
            "CardTitle.TLabel",
            background=THEME["card"],
            foreground=THEME["text"],
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        style.configure(
            "CardText.TLabel",
            background=THEME["card"],
            foreground=THEME["text"],
            font=("Microsoft YaHei UI", 9),
        )
        style.configure(
            "MutedCard.TLabel",
            background=THEME["card"],
            foreground=THEME["muted"],
            font=("Microsoft YaHei UI", 8),
        )
        style.configure(
            "Status.TLabel",
            background=THEME["bg"],
            foreground=THEME["muted"],
            font=("Microsoft YaHei UI", 9),
        )
        style.configure(
            "Blue.Horizontal.TProgressbar",
            troughcolor="#E2E8F0",
            background=THEME["accent"],
            thickness=9,
        )

    def _make_card(self, parent, pady=(0, 8), radius=22):
        card = RoundedCard(
            parent,
            bg_color=THEME["bg"],
            card_bg=THEME["card"],
            border_color=THEME["border"],
            radius=radius,
            height=100,
        )
        card.pack(fill="x", padx=16, pady=pady)
        return card, card.inner

    def _make_rounded_entry_box(self, parent, textvariable, on_confirm=None, radius=15):
        wrap = tk.Canvas(parent, height=34, bg=THEME["card"], highlightthickness=0, bd=0)
        entry = tk.Entry(
            wrap,
            textvariable=textvariable,
            font=("Microsoft YaHei UI", 9),
            bg="#F8FAFC",
            fg=THEME["text"],
            relief="flat",
            bd=0,
            highlightthickness=0,
            insertbackground=THEME["accent"],
        )
        win_id = wrap.create_window(12, 6, window=entry, anchor="nw")

        def redraw(_e=None):
            w = max(40, wrap.winfo_width())
            h = max(24, wrap.winfo_height())
            wrap.delete("box_bg")
            create_rounded_rect(
                wrap,
                1,
                1,
                w - 1,
                h - 1,
                r=radius,
                fill="#F8FAFC",
                outline=THEME["border"],
                width=1.3,
                tags="box_bg",
            )
            wrap.tag_lower("box_bg")
            wrap.itemconfigure(win_id, width=max(20, w - 24), height=max(16, h - 12))

        wrap.bind("<Configure>", redraw)
        if callable(on_confirm):
            entry.bind("<Return>", on_confirm)
            entry.bind("<FocusOut>", on_confirm)
        return wrap, entry

    def _make_sec_btn(self, parent, text, command, width=96, height=32):
        return SmoothAnimatedButton(
            parent,
            text=text,
            command=command,
            width=width,
            height=height,
            radius=height / 2.0,
            bg_color=THEME["btn_sec"],
            hover_color=THEME["btn_sec_hover"],
            fg_color=THEME["btn_sec_fg"],
            wave_color="#60A5FA",
            font=("Microsoft YaHei UI", 9, "bold"),
            parent_bg=THEME["card"],
        )

    def _build(self):
        header = ttk.Frame(self.root, style="App.TFrame")
        header.pack(fill="x", padx=16, pady=(12, 6))
        ttk.Label(header, text=APP_DISPLAY_NAME, style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text="AI 智能抠图 · 精细边缘可选 · 保留子文件夹层级与文件名 · 支持 JPG / PNG",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(2, 0))

        # 卡片 1：圆润导入区域 + 胶囊按钮 + 圆润列表框
        _, import_card = self._make_card(self.root, pady=(0, 8), radius=22)

        drop_canvas = tk.Canvas(import_card, height=64, bg=THEME["card"], highlightthickness=0, bd=0)
        drop_canvas.pack(fill="x", pady=(0, 8))

        def draw_drop(_e=None):
            w = max(60, drop_canvas.winfo_width())
            h = max(44, drop_canvas.winfo_height())
            drop_canvas.delete("all")
            create_rounded_rect(
                drop_canvas,
                1,
                1,
                w - 1,
                h - 1,
                r=18,
                fill=THEME["drop_bg"],
                outline=THEME["drop_border"],
                width=1.4,
            )
            drop_canvas.create_text(
                w / 2.0,
                h / 2.0 - 8,
                text="点击下方“添加照片”或“添加文件夹”选择待处理图片",
                fill=THEME["drop_fg"],
                font=("Microsoft YaHei UI", 10, "bold"),
            )
            drop_canvas.create_text(
                w / 2.0,
                h / 2.0 + 11,
                text="支持整批照片、总文件夹及多层子文件夹递归扫描",
                fill="#3B82F6",
                font=("Microsoft YaHei UI", 8),
            )

        drop_canvas.bind("<Configure>", draw_drop)

        btn_row = tk.Frame(import_card, bg=THEME["card"])
        btn_row.pack(fill="x", pady=(0, 8))
        self._make_sec_btn(btn_row, "添加照片", self.add_files, width=98, height=32).pack(side="left", padx=(0, 8))
        self._make_sec_btn(btn_row, "添加文件夹", self.add_folder, width=110, height=32).pack(side="left")
        self._make_sec_btn(btn_row, "清空", self.clear, width=78, height=32).pack(side="right")

        list_canvas = tk.Canvas(import_card, height=84, bg=THEME["card"], highlightthickness=0, bd=0)
        list_canvas.pack(fill="x")
        self.listbox = tk.Listbox(
            list_canvas,
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
        list_win = list_canvas.create_window(12, 7, window=self.listbox, anchor="nw")

        def draw_list(_e=None):
            w = max(60, list_canvas.winfo_width())
            h = max(48, list_canvas.winfo_height())
            list_canvas.delete("list_bg_shape")
            create_rounded_rect(
                list_canvas,
                1,
                1,
                w - 1,
                h - 1,
                r=16,
                fill=THEME["list_bg"],
                outline=THEME["border"],
                width=1.3,
                tags="list_bg_shape",
            )
            list_canvas.tag_lower("list_bg_shape")
            list_canvas.itemconfigure(list_win, width=max(20, w - 24), height=max(24, h - 14))

        list_canvas.bind("<Configure>", draw_list)

        # 卡片 2：颜色、格式、大小上限与胶囊滑动“精细边缘模式”
        _, set_card = self._make_card(self.root, pady=(0, 8), radius=22)
        ttk.Label(set_card, text="处理参数设置", style="CardTitle.TLabel").pack(anchor="w", pady=(0, 6))

        r1 = tk.Frame(set_card, bg=THEME["card"])
        r1.pack(fill="x", pady=(0, 6))
        ttk.Label(r1, text="背景颜色：", style="CardText.TLabel").pack(side="left")
        self.preset = ttk.Combobox(
            r1,
            values=list(PRESETS.keys()) + ["自定义颜色"],
            state="readonly",
            width=14,
            font=("Microsoft YaHei UI", 9),
        )
        self.preset.set("蓝色（常用）")
        self.preset.pack(side="left", padx=(4, 8))
        self.preset.bind("<<ComboboxSelected>>", self._preset_changed)

        swatch_c = tk.Canvas(r1, width=26, height=24, bg=THEME["card"], highlightthickness=0, bd=0)
        swatch_c.pack(side="left", padx=(0, 6))
        self._swatch_id = create_rounded_rect(
            swatch_c,
            1,
            1,
            25,
            23,
            r=8,
            fill=self.color.get(),
            outline="#94A3B8",
            width=1,
        )
        self._swatch_canvas = swatch_c

        self.hex_label = ttk.Label(r1, text=self.color.get(), style="CardText.TLabel")
        self.hex_label.pack(side="left", padx=(0, 8))
        self._make_sec_btn(r1, "自定义颜色", self.choose_color, width=104, height=30).pack(side="right")

        r2 = tk.Frame(set_card, bg=THEME["card"])
        r2.pack(fill="x", pady=(0, 8))
        ttk.Label(r2, text="输出格式：", style="CardText.TLabel").pack(side="left")
        self.format_combo = ttk.Combobox(
            r2,
            textvariable=self.output_format,
            values=["JPG", "PNG"],
            state="readonly",
            width=7,
            font=("Microsoft YaHei UI", 9),
        )
        self.format_combo.pack(side="left", padx=(4, 12))

        ttk.Label(r2, text="大小上限：", style="CardText.TLabel").pack(side="left")
        ttk.Spinbox(
            r2,
            from_=10,
            to=2000,
            textvariable=self.max_kb,
            width=6,
            font=("Microsoft YaHei UI", 9),
        ).pack(side="left", padx=(4, 6))
        ttk.Label(r2, text="KB（超限时尝试降质/缩放）", style="MutedCard.TLabel").pack(side="left")

        r_fine = tk.Frame(set_card, bg=THEME["card"])
        r_fine.pack(fill="x")
        self.fine_toggle = PillToggle(r_fine, self.fine_edge, "精细边缘模式", parent_bg=THEME["card"])
        self.fine_toggle.pack(side="left", padx=(0, 8))
        lbl_fine = ttk.Label(
            r_fine,
            text="精细边缘模式（加强头发丝与边缘过渡，耗时更长、CPU 占用更高）",
            style="CardTitle.TLabel",
            cursor="hand2",
        )
        lbl_fine.pack(side="left")
        lbl_fine.bind("<Button-1>", lambda _e: self.fine_toggle._toggle())

        # 卡片 3：输出目录（默认桌面 + 原目录）与自定义 AI 模型目录
        _, dir_card = self._make_card(self.root, pady=(0, 8), radius=22)
        ttk.Label(dir_card, text="输出文件夹（默认桌面，可点击“浏览”或“原目录”修改）", style="CardTitle.TLabel").pack(anchor="w", pady=(0, 4))

        r3 = tk.Frame(dir_card, bg=THEME["card"])
        r3.pack(fill="x", pady=(0, 8))
        out_box, self.out_entry = self._make_rounded_entry_box(r3, self.output_dir, radius=15)
        out_box.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self._make_sec_btn(r3, "原目录", self.use_source_dir_as_output, width=76, height=32).pack(side="left", padx=(0, 6))
        self._make_sec_btn(r3, "浏览", self.choose_output, width=70, height=32).pack(side="right")

        ttk.Label(
            dir_card,
            text="AI 模型存放位置（跟随安装程序设置，可鼠标点选目录或键盘输入/粘贴路径）：",
            style="CardTitle.TLabel",
        ).pack(anchor="w", pady=(0, 4))

        r4 = tk.Frame(dir_card, bg=THEME["card"])
        r4.pack(fill="x")
        model_box, self.model_entry = self._make_rounded_entry_box(
            r4,
            self.model_dir,
            on_confirm=self._on_model_entry_confirm,
            radius=15,
        )
        model_box.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self._make_sec_btn(r4, "选择目录", self.choose_model_dir, width=94, height=32).pack(side="right")

        # 底部进度、胶囊回弹波动主按钮与说明
        bottom = ttk.Frame(self.root, style="App.TFrame")
        bottom.pack(fill="x", padx=16, pady=(0, 10))

        self.progress = ttk.Progressbar(
            bottom,
            mode="determinate",
            style="Blue.Horizontal.TProgressbar",
        )
        self.progress.pack(fill="x", pady=(0, 4))
        ttk.Label(bottom, textvariable=self.status, style="Status.TLabel").pack(anchor="w", pady=(0, 8))

        self.start_btn = SmoothAnimatedButton(
            bottom,
            text="开始批量处理",
            command=self.start,
            width=580,
            height=48,
            radius=24,
            bg_color=THEME["accent"],
            hover_color=THEME["accent_hover"],
            fg_color="#FFFFFF",
            wave_color="#BFDBFE",
            font=("Microsoft YaHei UI", 11, "bold"),
            parent_bg=THEME["bg"],
        )
        self.start_btn.pack(fill="x")

        def sync_start_btn_width(event):
            if event.width > 20:
                self.start_btn.configure(width=event.width)

        bottom.bind("<Configure>", sync_start_btn_width)

        ttk.Label(
            bottom,
            text="提示：默认输出在桌面；模型目录与安装时一致；点击“原目录”可将输出设为原图所在文件夹，原图不会被修改。",
            style="Subtitle.TLabel",
            wraplength=580,
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
        self._swatch_canvas.itemconfigure(self._swatch_id, fill=value)
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

        fmt = (self.output_format.get() or "JPG").upper()
        fine_edge = bool(self.fine_edge.get())
        self.start_btn.set_state("disabled")
        self.progress.configure(maximum=len(images), value=0)
        threading.Thread(
            target=self._worker,
            args=(images, out, rgb, fmt, self.max_kb.get(), chosen_model_str, fine_edge),
            daemon=True,
        ).start()

    def _worker(self, images, out, rgb, fmt, max_kb, chosen_model_str, fine_edge):
        if remove is None or new_session is None:
            self.msgq.put(("fatal", "缺少 rembg 组件，请检查打包是否完整。"))
            return
        try:
            prepare_model_directory(
                chosen_model_str,
                status_cb=lambda text: self.msgq.put(("status", text)),
            )
            self.msgq.put(("status", "正在加载 AI 抠图模型……"))
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
        ok = 0
        errors = []
        for idx, src in enumerate(images, 1):
            try:
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
                ok += 1
                self.msgq.put(("progress", idx, len(images), f"处理中 {idx}/{len(images)}：{src.name}"))
            except Exception as e:
                errors.append(f"{src}: {e}")
                self.msgq.put(("progress", idx, len(images), f"失败 {idx}/{len(images)}：{src.name}"))
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
                    self.start_btn.set_state("normal")
                    messagebox.showerror("无法处理", item[1])
                elif kind == "done":
                    _, ok, total, errors, out = item
                    self.start_btn.set_state("normal")
                    self.status.set(f"完成：成功 {ok} 张，失败 {len(errors)} 张")
                    msg = f"处理完成！\n成功：{ok}/{total} 张\n输出位置：{out}"
                    if errors:
                        msg += "\n\n部分失败原因（最多显示 5 条）：\n" + "\n".join(errors[:5])
                    messagebox.showinfo("处理完成", msg)
        except queue.Empty:
            pass
        self.root.after(150, self._poll)


def main():
    global Image, ImageOps, remove, new_session, TkinterDnD, DND_FILES

    try:
        root = tk.Tk()
        root.withdraw()
        splash = LoadingWindow(root)
        load_q = queue.Queue()

        def load_deps():
            global Image, ImageOps, remove, new_session, TkinterDnD, DND_FILES
            try:
                load_q.put((25, "正在加载图像处理组件 (Pillow)..."))
                from PIL import Image as _Image, ImageOps as _ImageOps
                Image, ImageOps = _Image, _ImageOps

                load_q.put((60, "正在加载 AI 抠图引擎 (rembg / onnxruntime)..."))
                from rembg import remove as _remove, new_session as _new_session
                remove, new_session = _remove, _new_session

                TkinterDnD, DND_FILES = None, None

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

        def poll_load():
            try:
                while True:
                    step, payload = load_q.get_nowait()
                    if step == "DONE":
                        finish_load(None)
                        return
                    elif step == "ERROR":
                        finish_load(payload)
                        return
                    else:
                        splash.update_progress(step, payload)
            except queue.Empty:
                pass
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
