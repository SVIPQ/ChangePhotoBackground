# -*- coding: utf-8 -*-
"""
证件照批量换底色工具-Czq
功能：批量导入图片/文件夹（递归扫描子文件夹）、AI抠图、统一替换底色、
保留子文件夹结构和文件主名称、导出 JPG 并尽量压缩到指定 KB 以内。
"""
import os
import sys
import io
import threading
import queue
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, colorchooser, messagebox

# 修复 PyInstaller --windowed 模式下 sys.stdout / sys.stderr 为 None 导致进度条库报错
if sys.stdout is None:
    sys.stdout = io.StringIO()
if sys.stderr is None:
    sys.stderr = io.StringIO()

# 兼容 PyInstaller 6.x (_internal\.u2net) 与同级目录的离线模型路径
if getattr(sys, "frozen", False):
    exe_dir = Path(sys.executable).resolve().parent
    internal_u2net = exe_dir / "_internal" / ".u2net"
    local_u2net = exe_dir / ".u2net"
    if internal_u2net.exists():
        os.environ["U2NET_HOME"] = str(internal_u2net)
    elif local_u2net.exists():
        os.environ["U2NET_HOME"] = str(local_u2net)

from PIL import Image, ImageOps

try:
    from rembg import remove, new_session
except Exception:
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
    # 去重并保持顺序
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
    # 保留证件照常用清晰度；极端情况下逐步缩小
    for scale_round in range(12):
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

class App:
    def __init__(self, root):
        self.root = root
        self.root.title("证件照批量换底色工具")
        self.root.geometry("760x650")
        self.root.minsize(680, 590)
        self.msgq = queue.Queue()
        self.input_paths = []
        self.output_dir = tk.StringVar(value=str(Path.home() / "Pictures" / "证件照换底色结果"))
        self.color = tk.StringVar(value=PRESETS["蓝色（常用）"])
        self.max_kb = tk.IntVar(value=50)
        self.status = tk.StringVar(value="等待导入照片或文件夹")
        self._build()
        self._poll()

    def _build(self):
        pad = {"padx": 14, "pady": 7}
        ttk.Label(self.root, text="证件照批量换底色", font=("Microsoft YaHei UI", 18, "bold")).pack(pady=(16, 4))
        ttk.Label(self.root, text="支持整批照片、总文件夹及多层子文件夹；自动保留目录结构和文件主名称",
                  foreground="#555").pack(pady=(0, 12))

        self.drop = tk.Label(self.root, text="将照片或文件夹拖到这里\n也可以点击下方按钮选择",
                             relief="groove", bd=2, bg="#F3F7FC", fg="#31577C",
                             font=("Microsoft YaHei UI", 13), height=5)
        self.drop.pack(fill="x", padx=18, pady=6)
        try:
            from tkinterdnd2 import DND_FILES
            self.drop.drop_target_register(DND_FILES)
            self.drop.dnd_bind("<<Drop>>", self._on_drop)
        except Exception:
            self.drop.configure(text="拖放功能需要安装 tkinterdnd2\n也可以点击下方按钮选择文件或文件夹")

        row = ttk.Frame(self.root); row.pack(fill="x", **pad)
        ttk.Button(row, text="添加照片", command=self.add_files).pack(side="left", padx=(0, 8))
        ttk.Button(row, text="添加文件夹（自动扫描所有子文件夹）", command=self.add_folder).pack(side="left")
        ttk.Button(row, text="清空列表", command=self.clear).pack(side="right")

        self.listbox = tk.Listbox(self.root, height=7, font=("Microsoft YaHei UI", 9))
        self.listbox.pack(fill="both", expand=True, padx=18, pady=4)

        settings = ttk.LabelFrame(self.root, text="处理设置")
        settings.pack(fill="x", padx=18, pady=10)
        r1 = ttk.Frame(settings); r1.pack(fill="x", padx=10, pady=8)
        ttk.Label(r1, text="背景颜色：").pack(side="left")
        self.preset = ttk.Combobox(r1, values=list(PRESETS.keys()) + ["自定义颜色"], state="readonly", width=16)
        self.preset.set("蓝色（常用）"); self.preset.pack(side="left", padx=5)
        self.preset.bind("<<ComboboxSelected>>", self._preset_changed)
        self.color_swatch = tk.Label(r1, text="      ", bg=self.color.get(), relief="solid")
        self.color_swatch.pack(side="left", padx=7)
        ttk.Button(r1, text="选择自定义颜色", command=self.choose_color).pack(side="left", padx=5)
        self.hex_label = ttk.Label(r1, text=self.color.get()); self.hex_label.pack(side="left", padx=5)

        r2 = ttk.Frame(settings); r2.pack(fill="x", padx=10, pady=8)
        ttk.Label(r2, text="单张图片大小上限：").pack(side="left")
        ttk.Spinbox(r2, from_=10, to=500, textvariable=self.max_kb, width=7).pack(side="left")
        ttk.Label(r2, text="KB（默认 50KB，超限时自动降低画质/尺寸）").pack(side="left", padx=5)

        r3 = ttk.Frame(self.root); r3.pack(fill="x", **pad)
        ttk.Label(r3, text="输出文件夹：").pack(side="left")
        ttk.Entry(r3, textvariable=self.output_dir).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(r3, text="浏览", command=self.choose_output).pack(side="right")

        self.progress = ttk.Progressbar(self.root, mode="determinate")
        self.progress.pack(fill="x", padx=18, pady=(10, 3))
        ttk.Label(self.root, textvariable=self.status, anchor="w").pack(fill="x", padx=20, pady=3)
        self.start_btn = ttk.Button(self.root, text="开始批量处理", command=self.start)
        self.start_btn.pack(pady=12, ipadx=28, ipady=7)

        note = "说明：输出统一为 JPG（便于控制在 50KB 内）；PNG 等格式会转换为 JPG，文件主名称不变。"
        ttk.Label(self.root, text=note, foreground="#666", wraplength=700).pack(padx=18, pady=(0, 12))

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
        paths = filedialog.askopenfilenames(title="选择照片", filetypes=[("图片", "*.jpg *.jpeg *.png *.bmp *.webp *.tif *.tiff"), ("所有文件", "*.*")])
        if paths: self._add_paths(paths)

    def add_folder(self):
        p = filedialog.askdirectory(title="选择包含照片的总文件夹")
        if p: self._add_paths([p])

    def clear(self):
        self.input_paths = []; self._images = []
        self.listbox.delete(0, tk.END)
        self.status.set("列表已清空")

    def choose_output(self):
        p = filedialog.askdirectory(title="选择输出文件夹")
        if p: self.output_dir.set(p)

    def _preset_changed(self, _=None):
        name = self.preset.get()
        if name in PRESETS:
            self._set_color(PRESETS[name])
        elif name == "自定义颜色":
            self.choose_color()

    def choose_color(self):
        result = colorchooser.askcolor(color=self.color.get(), title="选择证件照背景颜色")
        if result and result[1]: self._set_color(result[1])

    def _set_color(self, value):
        self.color.set(value.upper())
        self.color_swatch.configure(bg=value)
        self.hex_label.configure(text=value)
        if value not in PRESETS.values(): self.preset.set("自定义颜色")

    def start(self):
        images = getattr(self, "_images", [])
        if not images:
            messagebox.showwarning("提示", "请先添加照片或文件夹。"); return
        out = Path(self.output_dir.get())
        try: out.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            messagebox.showerror("输出目录错误", str(e)); return
        try: rgb = hex_to_rgb(self.color.get())
        except Exception as e:
            messagebox.showerror("颜色设置错误", str(e)); return
        self.start_btn.configure(state="disabled")
        self.progress.configure(maximum=len(images), value=0)
        threading.Thread(target=self._worker, args=(images, out, rgb, self.max_kb.get()), daemon=True).start()

    def _worker(self, images, out, rgb, max_kb):
        if remove is None:
            self.msgq.put(("fatal", "缺少 rembg。请先按使用说明安装依赖。")); return
        try:
            self.msgq.put(("status", "正在加载 AI 抠图模型（首次运行可能需要下载模型）……"))
            session = new_session("u2net")
        except Exception as e:
            self.msgq.put(("fatal", f"AI 模型加载失败：{e}")); return

        # 若只选了一个总文件夹，输出时保留其内部子文件夹结构
        roots = [Path(p) for p in self.input_paths if Path(p).is_dir()]
        root_for = {}
        for f in images:
            candidates = [r for r in roots if r == f or r in f.parents]
            root_for[f] = max(candidates, key=lambda x: len(x.parts)) if candidates else f.parent

        ok = 0; errors = []
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
                try: relative = src.relative_to(root)
                except ValueError: relative = Path(src.name)
                target_dir = out / (relative.parent if root.is_dir() else Path())
                target_dir.mkdir(parents=True, exist_ok=True)
                target = target_dir / (src.stem + ".jpg")
                # 同一目录存在同名不同扩展名时避免覆盖
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
                if kind == "status": self.status.set(item[1])
                elif kind == "progress":
                    _, v, total, msg = item
                    self.progress.configure(value=v); self.status.set(msg)
                elif kind == "fatal":
                    self.start_btn.configure(state="normal")
                    messagebox.showerror("无法处理", item[1])
                elif kind == "done":
                    _, ok, total, errors, out = item
                    self.start_btn.configure(state="normal")
                    self.status.set(f"完成：成功 {ok} 张，失败 {len(errors)} 张")
                    msg = f"处理完成！\n成功：{ok}/{total} 张\n输出位置：{out}"
                    if errors: msg += "\n\n部分失败原因（最多显示 5 条）：\n" + "\n".join(errors[:5])
                    messagebox.showinfo("处理完成", msg)
        except queue.Empty:
            pass
        self.root.after(150, self._poll)

if __name__ == "__main__":
    root = tk.Tk()
    try:
        from tkinterdnd2 import TkinterDnD
        root.destroy()
        root = TkinterDnD.Tk()
    except Exception:
        pass
    App(root)
    root.mainloop()
