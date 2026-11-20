# -*- coding: utf-8 -*-
"""
证件照批量换底色工具
功能：批量导入图片/文件夹（递归扫描子文件夹）、AI抠图、统一替换底色、
保留子文件夹结构和文件主名称、导出 JPG 并尽量压缩到指定 KB 以内、
支持自定义 AI 模型存放目录（防占 C 盘）。
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


def encode_jpeg_under_limit(image, max_kb):
    """输出 RGB JPEG；先降低质量，必要时再缩小尺寸，确保不超过上限。"""
    limit = max(1, int(max_kb)) * 1024
    img = image.convert("RGB")
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
    raise ValueError(f"在可接受的最低尺寸下仍无法压缩到 {max_kb}KB")


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
        self.win.title("正在启动")
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
            text="证件照批量换底色工具-Czq",
            bg="#FFFFFF",
            fg="#0F172A",
            font=("Microsoft YaHei UI", 13, "bold"),
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
        self.root.title("证件照批量换底色工具-Czq")
        # 竖长比例布局，确保首次打开即可完整看到“开始批量处理”按钮
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
        self.max_kb = tk.IntVar(value=50)
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
            font=("Microsoft YaHei UI", 17, "bold"),
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
            "Action.TButton",
            font=("Microsoft YaHei UI", 9),
            padding=(10, 4),
        )
        style.configure(
            "Primary.TButton",
            font=("Microsoft YaHei UI", 11, "bold"),
            foreground="#FFFFFF",
            background=THEME["accent"],
            padding=(18, 9),
        )
        style.map(
            "Primary.TButton",
            background=[("active", THEME["accent_hover"]), ("disabled", "#93C5FD")],
            foreground=[("disabled", "#F8FAFC")],
        )
        style.configure(
            "Blue.Horizontal.TProgressbar",
            troughcolor="#E2E8F0",
            background=THEME["accent"],
            thickness=8,
        )

    def _make_card(self, parent, pady=(0, 8)):
        outer = tk.Frame(
            parent,
            bg=THEME["card"],
            highlightbackground=THEME["border"],
            highlightthickness=1,
        )
        outer.pack(fill="x", padx=16, pady=pady)
        inner = ttk.Frame(outer, style="Card.TFrame")
        inner.pack(fill="both", expand=True, padx=12, pady=8)
        return outer, inner

    def _build(self):
        # 顶部标题区
        header = ttk.Frame(self.root, style="App.TFrame")
        header.pack(fill="x", padx=16, pady=(12, 6))
        ttk.Label(header, text="证件照批量换底色工具", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text="AI 智能抠图 · 统一底色 · 保留子文件夹层级与文件名 · 控制 JPG 大小上限",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(2, 0))

        # 卡片 1：导入与文件列表
        _, import_card = self._make_card(self.root, pady=(0, 8))

        self.drop = tk.Label(
            import_card,
            text="将照片或文件夹拖放到此处\n或使用下方按钮选择照片 / 总文件夹",
            relief="flat",
            bg=THEME["drop_bg"],
            fg=THEME["drop_fg"],
            font=("Microsoft YaHei UI", 10, "bold"),
            height=3,
            highlightbackground=THEME["drop_border"],
            highlightthickness=1,
        )
        self.drop.pack(fill="x", pady=(0, 6))

        if TkinterDnD is not None and DND_FILES is not None:
            try:
                self.drop.drop_target_register(DND_FILES)
                self.drop.dnd_bind("<<Drop>>", self._on_drop)
            except Exception:
                self.drop.configure(text="拖放功能未启用 · 请使用下方按钮选择照片或文件夹")
        else:
            self.drop.configure(text="支持点击下方按钮选择单张/多张照片或整个文件夹")

        btn_row = ttk.Frame(import_card, style="Card.TFrame")
        btn_row.pack(fill="x", pady=(0, 6))
        ttk.Button(btn_row, text="添加照片", style="Action.TButton", command=self.add_files).pack(side="left", padx=(0, 6))
        ttk.Button(btn_row, text="添加文件夹", style="Action.TButton", command=self.add_folder).pack(side="left")
        ttk.Button(btn_row, text="清空", style="Action.TButton", command=self.clear).pack(side="right")

        list_wrap = tk.Frame(
            import_card,
            bg=THEME["list_bg"],
            highlightbackground=THEME["border"],
            highlightthickness=1,
        )
        list_wrap.pack(fill="x")
        self.listbox = tk.Listbox(
            list_wrap,
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
        self.listbox.pack(fill="x", padx=6, pady=4)

        # 卡片 2：背景颜色与大小上限设置
        _, set_card = self._make_card(self.root, pady=(0, 8))
        ttk.Label(set_card, text="处理参数设置", style="CardTitle.TLabel").pack(anchor="w", pady=(0, 6))

        r1 = ttk.Frame(set_card, style="Card.TFrame")
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

        self.color_swatch = tk.Label(
            r1,
            text="    ",
            bg=self.color.get(),
            relief="solid",
            bd=1,
        )
        self.color_swatch.pack(side="left", padx=(0, 6))
        self.hex_label = ttk.Label(r1, text=self.color.get(), style="CardText.TLabel")
        self.hex_label.pack(side="left", padx=(0, 8))
        ttk.Button(r1, text="自定义颜色", style="Action.TButton", command=self.choose_color).pack(side="right")

        r2 = ttk.Frame(set_card, style="Card.TFrame")
        r2.pack(fill="x")
        ttk.Label(r2, text="单张大小上限：", style="CardText.TLabel").pack(side="left")
        ttk.Spinbox(
            r2,
            from_=10,
            to=500,
            textvariable=self.max_kb,
            width=6,
            font=("Microsoft YaHei UI", 9),
        ).pack(side="left", padx=(4, 6))
        ttk.Label(r2, text="KB（超限时自动降质/缩放）", style="MutedCard.TLabel").pack(side="left")

        # 卡片 3：输出目录与自定义 AI 模型目录（支持鼠标点选 + 键盘直接输入）
        _, dir_card = self._make_card(self.root, pady=(0, 10))
        ttk.Label(dir_card, text="输出文件夹", style="CardTitle.TLabel").pack(anchor="w", pady=(0, 4))

        r3 = ttk.Frame(dir_card, style="Card.TFrame")
        r3.pack(fill="x", pady=(0, 8))
        self.out_entry = ttk.Entry(r3, textvariable=self.output_dir, font=("Microsoft YaHei UI", 9))
        self.out_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        ttk.Button(r3, text="浏览", style="Action.TButton", command=self.choose_output).pack(side="right")

        ttk.Label(
            dir_card,
            text="AI 模型存放位置（可鼠标点选目录，也可直接键盘输入/粘贴路径，建议选非 C 盘）：",
            style="CardTitle.TLabel",
        ).pack(anchor="w", pady=(0, 4))

        r4 = ttk.Frame(dir_card, style="Card.TFrame")
        r4.pack(fill="x")
        self.model_entry = ttk.Entry(r4, textvariable=self.model_dir, font=("Microsoft YaHei UI", 9))
        self.model_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.model_entry.bind("<Return>", self._on_model_entry_confirm)
        self.model_entry.bind("<FocusOut>", self._on_model_entry_confirm)
        ttk.Button(r4, text="选择目录", style="Action.TButton", command=self.choose_model_dir).pack(side="right")

        # 底部进度、主按钮与说明（确保在 620x780 首屏完整可见）
        bottom = ttk.Frame(self.root, style="App.TFrame")
        bottom.pack(fill="x", padx=16, pady=(0, 10))

        self.progress = ttk.Progressbar(
            bottom,
            mode="determinate",
            style="Blue.Horizontal.TProgressbar",
        )
        self.progress.pack(fill="x", pady=(0, 4))
        ttk.Label(bottom, textvariable=self.status, style="Status.TLabel").pack(anchor="w", pady=(0, 8))

        self.start_btn = ttk.Button(
            bottom,
            text="开始批量处理",
            style="Primary.TButton",
            command=self.start,
        )
        self.start_btn.pack(fill="x", ipady=2)

        ttk.Label(
            bottom,
            text="提示：输出统一为 JPG 格式；PNG 等格式会自动转为 JPG，原图不会被修改。",
            style="Subtitle.TLabel",
            wraplength=580,
        ).pack(anchor="w", pady=(6, 0))

    def _on_drop(self, event):
        try:
            paths = self.root.tk.splitlist(event.data)
            self._add_paths(paths)
        except Exception as e:
            messagebox.showerror("拖放失败", str(e))

    def _add_paths(self, paths):
        valid = [p for p in paths if Path(p).exists()]
        self.input_paths.extend(valid)
        images = collect_images(self.input_paths)
        self.listbox.delete(0, tk.END)
        for p in images[:1000]:
            self.listbox.insert(tk.END, str(p))
        self.status.set(f"已找到 {len(images)} 张图片")
        self._images = images

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
        self.color_swatch.configure(bg=value)
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

        self.start_btn.configure(state="disabled")
        self.progress.configure(maximum=len(images), value=0)
        threading.Thread(
            target=self._worker,
            args=(images, out, rgb, self.max_kb.get(), chosen_model_str),
            daemon=True,
        ).start()

    def _worker(self, images, out, rgb, max_kb, chosen_model_str):
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

        ok = 0
        errors = []
        for idx, src in enumerate(images, 1):
            try:
                with Image.open(src) as im:
                    im = ImageOps.exif_transpose(im).convert("RGBA")
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
                target = target_dir / (src.stem + ".jpg")
                if target.exists() and src.suffix.lower() not in {".jpg", ".jpeg"}:
                    target = target_dir / (src.stem + "_converted.jpg")
                data = encode_jpeg_under_limit(result, max_kb)
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
    global Image, ImageOps, remove, new_session, TkinterDnD, DND_FILES

    base_root = tk.Tk()
    base_root.withdraw()
    splash = LoadingWindow(base_root)
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

            load_q.put((85, "正在加载文件拖放组件..."))
            try:
                from tkinterdnd2 import TkinterDnD as _TkinterDnD, DND_FILES as _DND_FILES
                TkinterDnD, DND_FILES = _TkinterDnD, _DND_FILES
            except Exception:
                TkinterDnD, DND_FILES = None, None

            load_q.put((100, "加载完成，正在打开主界面..."))
            load_q.put(("DONE", None))
        except Exception as e:
            load_q.put(("ERROR", str(e)))

    threading.Thread(target=load_deps, daemon=True).start()

    state = {"err": None, "finished": False}

    def check_load():
        try:
            while True:
                step, payload = load_q.get_nowait()
                if step == "DONE":
                    state["finished"] = True
                    return
                elif step == "ERROR":
                    state["err"] = payload
                    state["finished"] = True
                    return
                else:
                    splash.update_progress(step, payload)
        except queue.Empty:
            pass
        base_root.after(80, check_load)

    check_load()
    while not state["finished"]:
        base_root.update()

    splash.close()

    if state["err"]:
        messagebox.showerror("启动失败", f"组件加载出错：\n{state['err']}")
        base_root.destroy()
        return

    if TkinterDnD is not None:
        base_root.destroy()
        main_root = TkinterDnD.Tk()
    else:
        base_root.destroy()
        main_root = tk.Tk()

    App(main_root)
    main_root.mainloop()


if __name__ == "__main__":
    main()
