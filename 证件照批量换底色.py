# -*- coding: utf-8 -*-
"""
证件照批量去底色工具_CZQ.py
融合特性：
  1. 保留全图双模型模式：头部模型与服装模型分别处理完整照片，再按颈肩区域融合
  2. 优化背景后处理：纯色背景 HSV 识别、逐行亮度估计、边界连通与发缝残余背景清除、
     基于衣物主体的白肩/浅色衣块连通域找回、发丝去红溢色
  3. 保留多模型/镜像下载/自定义 .onnx、右键菜单、名称清单实时映射、
     双进度条与圆角白蓝 UI
  4. 支持证件照常用尺寸（含自定义）、人脸自动裁剪、亮度/对比度调节、
     渐变色背景、6 寸相纸自动排版
"""
import gc
import io
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import traceback
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox, simpledialog, ttk

try:
    import numpy as np
    HAS_NUMPY = True
except Exception:
    np = None
    HAS_NUMPY = False

try:
    import cv2
    HAS_OPENCV = True
except Exception:
    cv2 = None
    HAS_OPENCV = False

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    HAS_DND = True
except Exception:
    DND_FILES = None
    TkinterDnD = None
    HAS_DND = False

APP_DISPLAY_NAME = "证件照批量换底色工具_Czq"
SPLASH_DISPLAY_NAME = ("证件照批量换底色工具_Czq")

if sys.stdout is None:
    sys.stdout = io.StringIO()
if sys.stderr is None:
    sys.stderr = io.StringIO()

MODEL_WEIGHT_EXTS = {".onnx", ".pth", ".pt", ".bin", ".safetensors"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}

PHOTO_SIZES = [
    ("保持原比例（不裁切）", 0, 0),
    ("小一寸  22×32mm", 259, 377),
    ("标准一寸  25×35mm", 295, 413),
    ("大一寸  33×48mm", 390, 567),
    ("小二寸  35×45mm", 413, 531),
    ("标准二寸  35×49mm", 413, 579),
    ("大二寸  40×60mm", 472, 709),
    ("自定义尺寸", -1, -1),
]

PHOTO_PAPER_W = 1800
PHOTO_PAPER_H = 1200
PHOTO_PAPER_MARGIN = 16

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


class HairRedSpillModule:
    """
    发丝边缘去红、发缝背景清理与白色肩袖完整找回模块：
    - 识别纯色原背景（含发丝包围的小块红底缝隙），避免右侧碎发残留红块；
    - 从黑色/深色衣物前景主体出发，通过连通域生长完整找回贴在肩膀两侧的白色/浅色衣片；
    - 在边缘做逐行背景反混合 + 发丝红溢色抑制，保持边缘自然柔和。
    """

    def __init__(
        self,
        red_excess_threshold: float = 11.0,
        hair_luma_max: float = 125.0,
        hair_blend_ratio: float = 0.82,
        band_expand_ratio: float = 0.012,
    ):
        self.red_excess_threshold = float(red_excess_threshold)
        self.hair_luma_max = float(hair_luma_max)
        self.hair_blend_ratio = float(hair_blend_ratio)
        self.band_expand_ratio = float(band_expand_ratio)

    def _build_bg_array(self, h: int, w: int, bg_rgb, gradient: bool = False):
        if not HAS_NUMPY:
            return None
        r, g, b = [float(x) for x in bg_rgb]
        if not gradient:
            return np.broadcast_to(np.asarray([r, g, b], dtype=np.float32).reshape(1, 1, 3), (h, w, 3)).copy()
        ratio = np.linspace(0.0, 1.0, h, dtype=np.float32).reshape(h, 1, 1)
        top = np.asarray([r, g, b], dtype=np.float32).reshape(1, 1, 3)
        bottom = np.asarray([255.0, 255.0, 255.0], dtype=np.float32).reshape(1, 1, 3)
        grad_col = top * (1.0 - ratio) + bottom * ratio
        return np.broadcast_to(grad_col, (h, w, 3)).copy()

    def _rescue_connected_white_shoulders(
        self,
        src_rgb: "np.ndarray",
        alpha_2d: "np.ndarray",
        bg_mask: "np.ndarray",
        bg_sat: float,
    ) -> "np.ndarray":
        """
        放宽衣物阈值并结合连通域生长，完整找回左侧与右侧的白色/浅色肩片与袖条，
        防止左肩衣服边缘被过度裁切。
        """
        if not HAS_OPENCV:
            return alpha_2d

        # 若原图本身就是白底/浅灰底（低饱和度背景），不启用白衣连通扩张，防止把白背景吸入前景
        if bg_sat < 55.0:
            return alpha_2d

        h, w = alpha_2d.shape[:2]
        hsv = cv2.cvtColor(src_rgb, cv2.COLOR_RGB2HSV)
        core = (alpha_2d > 80.0).astype(np.uint8)
        if not np.any(core):
            return alpha_2d

        ys, _xs = np.where(core > 0)
        top_y = int(np.min(ys))
        bot_y = int(np.max(ys))
        person_h = max(1, bot_y - top_y)
        shoulder_top = max(int(h * 0.45), top_y + int(person_h * 0.44))
        shoulder_top = min(shoulder_top, int(h * 0.72))

        cloth_zone = np.zeros((h, w), dtype=bool)
        cloth_zone[shoulder_top:h, :] = True

        # 放宽白色/浅灰衣物阈值：高亮度 + 较宽饱和度容差 + 非原背景
        bright_low_sat = (
            (hsv[:, :, 2] >= 120) &
            (hsv[:, :, 1] <= 145) &
            (~bg_mask) &
            cloth_zone
        )
        if not np.any(bright_low_sat):
            return alpha_2d

        bridge_k = max(11, int(min(w, h) * 0.022))
        if bridge_k % 2 == 0:
            bridge_k += 1
        bridged_white = cv2.morphologyEx(
            bright_low_sat.astype(np.uint8),
            cv2.MORPH_CLOSE,
            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (bridge_k, bridge_k)),
        ) > 0
        bridged_white = bridged_white & cloth_zone & (~bg_mask)

        seed_r = max(19, int(min(w, h) * 0.032))
        if seed_r % 2 == 0:
            seed_r += 1
        body_core = core & cloth_zone
        body_seed = cv2.dilate(
            body_core,
            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (seed_r, seed_r)),
            iterations=1,
        ) > 0

        num_labels, labels = cv2.connectedComponents(bridged_white.astype(np.uint8), connectivity=8)
        if num_labels <= 1:
            return alpha_2d

        touched_labels = np.unique(labels[bridged_white & body_seed])
        touched_labels = touched_labels[touched_labels != 0]
        if touched_labels.size == 0:
            return alpha_2d

        connected_white = np.isin(labels, touched_labels) & (bright_low_sat | bridged_white)
        if not np.any(connected_white):
            return alpha_2d

        v_conf = np.clip((hsv[:, :, 2].astype(np.float32) - 105.0) / 90.0, 0.0, 1.0)
        s_conf = np.clip((165.0 - hsv[:, :, 1].astype(np.float32)) / 85.0, 0.0, 1.0)
        rescue_alpha = 228.0 + 27.0 * np.minimum(v_conf, s_conf)
        rescue_alpha = cv2.GaussianBlur(rescue_alpha * connected_white.astype(np.float32), (3, 3), 0)
        return np.maximum(alpha_2d, rescue_alpha)

    def process(self, im_rgba, alpha_img, bg_rgb, clean_edge: bool = True, gradient: bool = False):
        src_rgb = np.asarray(im_rgba.convert("RGB"), dtype=np.uint8)
        h, w = src_rgb.shape[:2]
        alpha_2d = np.asarray(
            alpha_img.resize((w, h), Image.Resampling.BILINEAR), dtype=np.float32
        )
        alpha_2d = np.clip(alpha_2d, 0.0, 255.0)

        if not HAS_NUMPY:
            bg_img = Image.new("RGB", (w, h), tuple(int(x) for x in bg_rgb))
            return Image.composite(
                im_rgba.convert("RGB"), bg_img,
                alpha_img.resize((w, h), Image.Resampling.BILINEAR).convert("L")
            )

        patch = max(8, int(min(w, h) * 0.04))
        top_samples = np.concatenate([
            src_rgb[:patch, :patch].reshape(-1, 3),
            src_rgb[:patch, -patch:].reshape(-1, 3),
        ], axis=0)
        fallback_bg = np.median(top_samples, axis=0).astype(np.float32)

        bg_mask = np.zeros((h, w), dtype=bool)
        bg_row = np.broadcast_to(fallback_bg[None, None, :], (h, 1, 3)).copy()
        bg_sat = 0.0

        if HAS_OPENCV:
            hsv = cv2.cvtColor(src_rgb, cv2.COLOR_RGB2HSV)
            top_hsv = np.concatenate([
                hsv[:patch, :patch].reshape(-1, 3),
                hsv[:patch, -patch:].reshape(-1, 3),
            ], axis=0).astype(np.float32)

            angles = top_hsv[:, 0] * (2.0 * np.pi / 180.0)
            mean_sin = float(np.mean(np.sin(angles)))
            mean_cos = float(np.mean(np.cos(angles)))
            bg_hue = (
                (np.arctan2(mean_sin, mean_cos) % (2.0 * np.pi))
                * (180.0 / (2.0 * np.pi))
            )
            bg_sat = float(np.median(top_hsv[:, 1]))

            if bg_sat >= 85.0:
                hue = hsv[:, :, 0].astype(np.float32)
                hue_diff = np.abs(hue - bg_hue)
                hue_diff = np.minimum(hue_diff, 180.0 - hue_diff)
                sat_floor = max(165.0, bg_sat * 0.80)
                hue_sat_candidate = (
                    (hue_diff <= 9.0) &
                    (hsv[:, :, 1].astype(np.float32) >= sat_floor)
                )

                prelim_u8 = hue_sat_candidate.astype(np.uint8)
                n0, labels0, _, _ = cv2.connectedComponentsWithStats(prelim_u8, connectivity=8)
                preliminary_bg = np.zeros((h, w), dtype=bool)
                if n0 > 1:
                    edge_labels = np.unique(np.concatenate([
                        labels0[0, :], labels0[-1, :], labels0[:, 0], labels0[:, -1]
                    ]))
                    edge_labels = edge_labels[edge_labels != 0]
                    if edge_labels.size:
                        preliminary_bg = np.isin(labels0, edge_labels)

                row_rgb = np.full((h, 3), np.nan, dtype=np.float32)
                row_value = np.full(h, np.nan, dtype=np.float32)
                for yy in range(h):
                    row_sel = preliminary_bg[yy]
                    if int(np.count_nonzero(row_sel)) >= 4:
                        row_rgb[yy] = np.median(src_rgb[yy][row_sel], axis=0).astype(np.float32)
                        row_value[yy] = float(np.median(hsv[yy, :, 2][row_sel]))

                valid_rows = np.where(np.isfinite(row_rgb[:, 0]))[0]
                if valid_rows.size >= 2:
                    all_rows = np.arange(h)
                    for channel in range(3):
                        row_rgb[:, channel] = np.interp(
                            all_rows, valid_rows, row_rgb[valid_rows, channel]
                        )
                    row_value = np.interp(all_rows, valid_rows, row_value[valid_rows])
                    row_rgb = cv2.GaussianBlur(
                        row_rgb.reshape(h, 1, 3), (1, 31), 0
                    ).reshape(h, 3)
                    row_value = cv2.GaussianBlur(
                        row_value.reshape(h, 1), (1, 31), 0
                    ).reshape(h)
                    bg_row = row_rgb.reshape(h, 1, 3)

                    value_floor = np.maximum(45.0, row_value[:, None] * 0.70)
                    refined_candidate = (
                        hue_sat_candidate &
                        (hsv[:, :, 2].astype(np.float32) >= value_floor)
                    )
                else:
                    refined_candidate = hue_sat_candidate

                n1, labels1, stats1, centroids1 = cv2.connectedComponentsWithStats(
                    refined_candidate.astype(np.uint8), connectivity=8
                )
                keep_labels = np.zeros(n1, dtype=bool)
                if n1 > 1:
                    edge_labels = np.unique(np.concatenate([
                        labels1[0, :], labels1[-1, :], labels1[:, 0], labels1[:, -1]
                    ]))
                    edge_labels = edge_labels[edge_labels != 0]
                    keep_labels[edge_labels] = True

                    src_f = src_rgb.astype(np.float32)
                    row_bg_field = np.broadcast_to(bg_row, (h, w, 3))
                    row_color_dist = np.sqrt(
                        np.sum((src_f - row_bg_field) ** 2, axis=2)
                    )
                    min_component_area = max(90, int(w * h * 0.00006))
                    for label_id in range(1, n1):
                        if keep_labels[label_id]:
                            continue
                        area = int(stats1[label_id, cv2.CC_STAT_AREA])
                        if area < min_component_area:
                            continue
                        cx, cy = centroids1[label_id]
                        is_outer_or_hair = (cx < w * 0.34) or (cx > w * 0.66) or (cy < h * 0.25)
                        if not is_outer_or_hair:
                            continue
                        component = (labels1 == label_id)
                        if float(np.median(row_color_dist[component])) < 45.0:
                            keep_labels[label_id] = True

                    bg_mask = keep_labels[labels1]

        if np.any(bg_mask):
            alpha_2d[bg_mask] = 0.0

        alpha_2d = self._rescue_connected_white_shoulders(
            src_rgb=src_rgb,
            alpha_2d=alpha_2d,
            bg_mask=bg_mask,
            bg_sat=bg_sat,
        )

        if clean_edge and HAS_OPENCV and np.any(bg_mask):
            edge_radius = max(7, int(min(w, h) * 0.011))
            if edge_radius % 2 == 0:
                edge_radius += 1
            near_background = cv2.dilate(
                bg_mask.astype(np.uint8),
                cv2.getStructuringElement(
                    cv2.MORPH_ELLIPSE, (edge_radius, edge_radius)
                ),
                iterations=1,
            ) > 0

            a = np.clip(alpha_2d / 255.0, 0.0, 1.0)
            edge_pixels = near_background & (a >= 0.06) & (a <= 0.99)
            if np.any(edge_pixels):
                bg_field = np.broadcast_to(bg_row, (h, w, 3)).astype(np.float32)
                src_f = src_rgb.astype(np.float32)
                denom = np.maximum(a[:, :, None], 0.25)
                recovered_fg = (
                    src_f - (1.0 - a[:, :, None]) * bg_field
                ) / denom
                recovered_fg = np.clip(recovered_fg, 0.0, 255.0)

                bg_dist = np.sqrt(
                    np.sum((src_f - bg_field) ** 2, axis=2)
                )
                spill_conf = np.clip((175.0 - bg_dist) / 110.0, 0.0, 1.0)
                alpha_conf = np.clip((1.0 - a) / 0.72, 0.0, 1.0)
                strength = (spill_conf * alpha_conf * 0.85)[:, :, None]
                corrected = src_f * (1.0 - strength) + recovered_fg * strength

                # 精细压制发丝边缘的红通道溢色（仅作用于背景邻接边缘带，不影响脸部/眉毛/眼镜）
                r_ch = corrected[:, :, 0]
                g_ch = corrected[:, :, 1]
                b_ch = corrected[:, :, 2]
                gb_max = np.maximum(g_ch, b_ch)
                gb_mean = 0.5 * (g_ch + b_ch)
                excess_red = r_ch - gb_max
                red_eff_thresh = max(8.0, self.red_excess_threshold * 0.75)
                red_spill = edge_pixels & (a < 0.96) & (excess_red > red_eff_thresh)
                if np.any(red_spill):
                    target_r = gb_mean + red_eff_thresh + (excess_red - red_eff_thresh) * 0.12
                    corrected[:, :, 0] = np.where(red_spill, np.minimum(r_ch, target_r), r_ch)

                src_rgb = np.where(
                    edge_pixels[:, :, None],
                    np.clip(corrected, 0, 255),
                    src_f,
                ).astype(np.uint8)

        bg_arr = self._build_bg_array(h, w, bg_rgb, gradient=gradient)
        a_norm = np.clip(alpha_2d / 255.0, 0.0, 1.0)[:, :, None]
        out = src_rgb.astype(np.float32) * a_norm + bg_arr * (1.0 - a_norm)
        return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8), mode="RGB")


def apply_brightness_contrast_pil(img, brightness: int = 0, contrast: int = 0):
    if brightness == 0 and contrast == 0:
        return img
    if HAS_NUMPY and HAS_OPENCV:
        arr = np.asarray(img.convert("RGB"), dtype=np.uint8)
        bgr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
        beta = brightness * 2.55
        alpha = 1.0 + contrast / 100.0
        adjusted = cv2.convertScaleAbs(bgr, alpha=alpha, beta=beta)
        rgb = cv2.cvtColor(adjusted, cv2.COLOR_BGR2RGB)
        return Image.fromarray(rgb, mode="RGB")
    from PIL import ImageEnhance
    out = img.convert("RGB")
    if brightness != 0:
        out = ImageEnhance.Brightness(out).enhance(max(0.1, 1.0 + brightness / 100.0))
    if contrast != 0:
        out = ImageEnhance.Contrast(out).enhance(max(0.1, 1.0 + contrast / 100.0))
    return out


_FACE_CASCADE = None


def _get_face_cascade():
    global _FACE_CASCADE
    if _FACE_CASCADE is not None:
        return _FACE_CASCADE
    if not HAS_OPENCV:
        return None
    try:
        xml_path = os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml")
        if os.path.exists(xml_path):
            _FACE_CASCADE = cv2.CascadeClassifier(xml_path)
    except Exception:
        _FACE_CASCADE = None
    return _FACE_CASCADE


def estimate_neck_split_y(im_rgba, offset_ratio: float = 0.0) -> int:
    w, h = im_rgba.size
    split_y = int(h * 0.56)
    if HAS_NUMPY and HAS_OPENCV:
        try:
            cascade = _get_face_cascade()
            if cascade is not None:
                rgb = np.asarray(im_rgba.convert("RGB"), dtype=np.uint8)
                gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
                min_side = max(36, int(min(w, h) * 0.12))
                faces = cascade.detectMultiScale(
                    gray,
                    scaleFactor=1.1,
                    minNeighbors=5,
                    minSize=(min_side, min_side),
                )
                if len(faces) > 0:
                    _fx, fy, _fw, fh = max(faces, key=lambda b: b[2] * b[3])
                    split_y = int(fy + fh * 1.28)
        except Exception:
            pass

    split_y += int(h * float(offset_ratio))
    min_y = max(32, int(h * 0.30))
    max_y = max(min_y + 20, int(h * 0.85))
    return max(min_y, min(max_y, split_y))


def detect_ort_providers(prefer_gpu: bool = True) -> tuple[list[str], str]:
    avail = []
    try:
        import onnxruntime as ort
        avail = list(ort.get_available_providers() or [])
    except Exception:
        avail = []

    if not prefer_gpu:
        return (["CPUExecutionProvider"], "CPU 模式（已手动切换）")

    if "CUDAExecutionProvider" in avail:
        return (["CUDAExecutionProvider", "CPUExecutionProvider"], "NVIDIA GPU (CUDA)")
    if "DmlExecutionProvider" in avail:
        return (["DmlExecutionProvider", "CPUExecutionProvider"], "GPU 加速 (DirectML)")
    return (["CPUExecutionProvider"], "CPU 模式（未检测到 GPU 加速包）")


def fit_photo_size_pil(img, target_w: int, target_h: int, auto_crop: bool = True):
    if target_w <= 0 or target_h <= 0:
        return img
    w, h = img.size
    if w <= 1 or h <= 1:
        return img

    aspect = target_w / target_h
    cropped = img

    if auto_crop and HAS_NUMPY and HAS_OPENCV:
        cascade = _get_face_cascade()
        gray = cv2.cvtColor(np.asarray(img.convert("RGB"), dtype=np.uint8), cv2.COLOR_RGB2GRAY)
        faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(40, 40)) if cascade is not None else ()
        if len(faces) > 0:
            fx, fy, fw, fh = max(faces, key=lambda b: b[2] * b[3])
            top = max(0, int(fy - fh * 0.35))
            bottom = min(h, int(fy + fh * 1.60))
            crop_h = max(1, bottom - top)
            crop_w = int(crop_h * aspect)
            if crop_w > w:
                crop_w = w
                crop_h = max(1, int(crop_w / aspect))
                bottom = min(h, top + crop_h)
                top = max(0, bottom - crop_h)
                crop_h = bottom - top
            cx = int(fx + fw / 2)
            left = max(0, min(w - crop_w, cx - crop_w // 2))
            cropped = img.crop((left, top, left + crop_w, top + crop_h))
        else:
            cur_ratio = w / h
            if cur_ratio > aspect:
                new_w = int(h * aspect)
                left = (w - new_w) // 2
                cropped = img.crop((left, 0, left + new_w, h))
            else:
                new_h = int(w / aspect)
                top = (h - new_h) // 2
                cropped = img.crop((0, top, w, top + new_h))
    else:
        cur_ratio = w / h
        if cur_ratio > aspect:
            new_w = int(h * aspect)
            left = (w - new_w) // 2
            cropped = img.crop((left, 0, left + new_w, h))
        else:
            new_h = int(w / aspect)
            top = (h - new_h) // 2
            cropped = img.crop((0, top, w, top + new_h))

    return cropped.resize((int(target_w), int(target_h)), Image.Resampling.LANCZOS)


def calc_6in_layout(photo_w: int, photo_h: int):
    pw = max(1, int(photo_w))
    ph = max(1, int(photo_h))
    avail_w = max(pw, PHOTO_PAPER_W - PHOTO_PAPER_MARGIN * 2)
    avail_h = max(ph, PHOTO_PAPER_H - PHOTO_PAPER_MARGIN * 2)
    cols = max(1, avail_w // pw)
    rows = max(1, avail_h // ph)
    total = cols * rows
    names = {
        1: "一", 2: "二", 3: "三", 4: "四", 6: "六",
        8: "八", 9: "九", 10: "十", 12: "十二", 16: "十六", 18: "十八",
    }
    label = f"{names.get(total, total)}分身（{cols}×{rows}）"
    return cols, rows, label


def create_6in_layout_pil(single_img):
    single_rgb = single_img.convert("RGB")
    w, h = single_rgb.size
    cols, rows, _ = calc_6in_layout(w, h)
    margin_x = max(4, (PHOTO_PAPER_W - cols * w) // (cols + 1))
    margin_y = max(4, (PHOTO_PAPER_H - rows * h) // (rows + 1))
    canvas = Image.new("RGB", (PHOTO_PAPER_W, PHOTO_PAPER_H), (255, 255, 255))
    for r_idx in range(rows):
        for c_idx in range(cols):
            x1 = margin_x + c_idx * (w + margin_x)
            y1 = margin_y + r_idx * (h + margin_y)
            if x1 + w <= PHOTO_PAPER_W and y1 + h <= PHOTO_PAPER_H:
                canvas.paste(single_rgb, (x1, y1))
    return canvas


def format_model_label(model_name: str) -> str:
    if model_name == "birefnet-portrait":
        return "BiRefNet"
    if model_name == "isnet-general-use":
        return "ISNet"
    if model_name == "u2net":
        return "U2Net"
    return model_name


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


def sanitize_stem_name(raw_name: str) -> str:
    s = (raw_name or "").strip()
    if not s:
        return ""
    s_no_ext = re.sub(r"\.(jpg|jpeg|png|bmp|webp|tif|tiff)$", "", s, flags=re.IGNORECASE).strip()
    if not s_no_ext:
        s_no_ext = s
    return re.sub(r'[\/:*?"<>|]+', "_", s_no_ext).strip(" .")


def parse_name_lines(raw_text: str) -> list[str]:
    if not raw_text:
        return []
    lines = []
    for raw_line in raw_text.splitlines():
        safe = sanitize_stem_name(raw_line)
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
    override_dir: Path | None = None,
    layout_6in: bool = False,
) -> tuple[Path, Path]:
    base_stem = custom_stem if custom_stem else src.stem
    stem_to_use = f"{base_stem}_6寸排版" if layout_6in else base_stem
    if override_dir is not None:
        out_folder = override_dir
        target = out_folder / (stem_to_use + ext)
    else:
        out_folder, relative = resolve_output_root_for_image(src, input_paths, base_out)
        target_dir = out_folder / relative.parent
        target = target_dir / (stem_to_use + ext)
    if target.resolve() == src.resolve():
        target = target.parent / (stem_to_use + f"_converted{ext}")
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


def copy_image_to_clipboard(img_path: Path):
    resolved = str(img_path.resolve()).replace("'", "''")
    ps_cmd = (
        f"$p = '{resolved}'; "
        f"Add-Type -AssemblyName System.Windows.Forms; "
        f"Add-Type -AssemblyName System.Drawing; "
        f"$img = [System.Drawing.Image]::FromFile($p); "
        f"$bmp = New-Object System.Drawing.Bitmap($img); "
        f"$img.Dispose(); "
        f"[System.Windows.Forms.Clipboard]::SetImage($bmp); "
        f"$files = New-Object System.Collections.Specialized.StringCollection; "
        f"[void]$files.Add($p); "
        f"[System.Windows.Forms.Clipboard]::SetFileDropList($files); "
        f"$bmp.Dispose()"
    )
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    subprocess.run(
        ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_cmd],
        check=True,
        startupinfo=startupinfo,
    )


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
        radius=18,
        bg_color="#FFFFFF",
        border_color="#D8E4F3",
        parent_bg="#F1F6FD",
        padx=14,
        pady=4,
        outer_pady=(0, 3),
    ):
        self.pill_r = float(radius)
        self.bg_color = bg_color
        self.border_color = border_color
        self.card_w = 580.0
        self.card_h = 70.0

        self.canvas = tk.Canvas(
            parent,
            width=580,
            height=70,
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
            width=584,
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
        if event.height > 8:
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
        height=32,
        radius=16,
        bg_color="#F8FAFC",
        border_color="#CBD5E1",
        parent_bg="#FFFFFF",
        padx=10,
        pady=2,
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
        height=10,
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
        height=32,
        radius=16,
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
        height=36,
        radius=18,
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


def _get_workarea_bounds(win):
    sw = win.winfo_screenwidth()
    sh = win.winfo_screenheight()
    left, top, right, bottom = 0, 0, sw, sh
    if sys.platform.startswith("win"):
        try:
            import ctypes
            from ctypes import wintypes

            class RECT(ctypes.Structure):
                _fields_ = [
                    ("left", wintypes.LONG),
                    ("top", wintypes.LONG),
                    ("right", wintypes.LONG),
                    ("bottom", wintypes.LONG),
                ]

            rect = RECT()
            SPI_GETWORKAREA = 0x0030
            if ctypes.windll.user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
                if rect.right > rect.left and rect.bottom > rect.top:
                    left, top, right, bottom = int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
        except Exception:
            pass
    return left, top, right, bottom


def center_window_on_screen(win, width: int, height: int):
    win.update_idletasks()
    sw = win.winfo_screenwidth()
    left, top, right, bottom = _get_workarea_bounds(win)
    work_w = max(320, right - left)
    work_h = max(320, bottom - top)
    fit_w = min(int(width), work_w)
    fit_h = min(int(height), work_h - 6)
    fit_h = max(320, fit_h)
    x = left + max(0, (work_w - fit_w) // 2)
    # Place above taskbar/status area, slightly above vertical center
    y = top + max(0, min(work_h - fit_h - 4, int((work_h - fit_h) * 0.25)))
    win.geometry(f"{fit_w}x{fit_h}+{x}+{y}")


class App:
    RIGHT_BTN_W = 112

    def __init__(self, root):
        self.root = root
        self.root.title(APP_DISPLAY_NAME)
        win_w, win_h = 740, 910
        self.root.minsize(710, 780)
        self.root.configure(bg=THEME["bg"])
        center_window_on_screen(self.root, win_w, win_h)

        default_output_path = str(Path.home() / "Desktop")
        default_threads = 1

        self.hair_spill_module = HairRedSpillModule(
            red_excess_threshold=11.0,
            hair_luma_max=125.0,
            hair_blend_ratio=0.82,
            band_expand_ratio=0.012,
        )

        self.msgq = queue.Queue()
        self.input_paths = []
        self._images = []
        self._img_index_map = {}
        self._img_states = {}
        self._custom_stems = {}
        self._custom_out_dirs = {}
        self._last_output_files = {}
        self._name_lines = []
        self._is_downloading_model = False
        self._is_running = False
        self._closing = False
        self._poll_job = None
        self._name_sync_job = None

        self.output_dir = tk.StringVar(value=default_output_path)
        self.mode_choice = tk.StringVar(value="split")
        self.single_model = tk.StringVar(value=DEFAULT_MODEL)
        self.head_model = tk.StringVar(value="birefnet-portrait")
        self.body_model = tk.StringVar(value="isnet-general-use")
        self.neck_offset_pct = tk.IntVar(value=0)
        self.use_custom_model_path = tk.BooleanVar(value=False)
        self.custom_model_file = tk.StringVar(value="")
        self.ensemble_choice = tk.StringVar(value="双模型叠加 (birefnet + isnet)")

        self.size_choice = tk.StringVar(value=PHOTO_SIZES[0][0])
        self.custom_w = tk.IntVar(value=295)
        self.custom_h = tk.IntVar(value=413)
        self.auto_crop = tk.BooleanVar(value=False)
        self.brightness_val = tk.IntVar(value=0)
        self.contrast_val = tk.IntVar(value=0)
        self.gradient_bg = tk.BooleanVar(value=False)
        self.layout_6in = tk.BooleanVar(value=False)

        self.fine_edge = tk.BooleanVar(value=True)
        self.color = tk.StringVar(value=PRESETS["蓝色（常用）"])
        self.output_format = tk.StringVar(value="JPG")
        self.limit_size_enabled = tk.BooleanVar(value=True)
        self.max_kb = tk.IntVar(value=50)
        self.use_gpu = tk.BooleanVar(value=True)
        self.thread_count = tk.IntVar(value=default_threads)
        self.status = tk.StringVar(value="拖入文件夹确认后，再点击下方“开始批量处理”")
        self.overall_status = tk.StringVar(value="总体进度：0%（已完成 0/0 张） | 当前照片：无")
        self.pct_text = tk.StringVar(value="0%")
        self.overall_pct_text = tk.StringVar(value="0%")

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self._setup_styles()
        try:
            self._build()
        except Exception:
            messagebox.showerror("界面构建错误", traceback.format_exc())
        self._sync_mode_panels()
        self._sync_size_limit_state()
        self._sync_size_custom_state()
        self.root.after(40, self._fit_window_to_content_and_screen)
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

    def _make_pill_card(self, parent, radius=20, outer_pady=(0, 6)):
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

    def _make_sec_btn(self, parent, text, command, width=112, height=24):
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
            font=("Microsoft YaHei UI", 8, "bold"),
            parent_bg=THEME["card"],
        )

    def _current_ext(self) -> str:
        return ".png" if (self.output_format.get() or "JPG").upper() == "PNG" else ".jpg"

    def _effective_stem(self, idx: int, src: Path) -> str:
        key = str(src.resolve())
        if key in self._custom_stems and self._custom_stems[key]:
            return self._custom_stems[key]
        if idx < len(self._name_lines):
            return self._name_lines[idx]
        return src.stem

    def _preview_target_name(self, idx: int, src: Path) -> str:
        ext = self._current_ext()
        key = str(src.resolve())
        stem = self._effective_stem(idx, src)
        if bool(self.layout_6in.get()):
            stem = f"{stem}_6寸排版"
        tag_parts = []
        if key in self._custom_stems and self._custom_stems[key]:
            tag_parts.append("单独改名")
        elif self._name_lines and idx >= len(self._name_lines):
            tag_parts.append("超行沿用原名")
        if key in self._custom_out_dirs and self._custom_out_dirs[key]:
            tag_parts.append(f"另存:{self._custom_out_dirs[key].name or self._custom_out_dirs[key]}")
        suffix_tag = f" [{' | '.join(tag_parts)}]" if tag_parts else ""
        return f"{stem}{ext}{suffix_tag}"

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

    def _calc_overall_pct(self, finished_count: int, total_count: int, active_pct: float = 0.0) -> float:
        if total_count <= 0:
            return 0.0
        if finished_count >= total_count:
            return 100.0
        val = ((finished_count + max(0.0, min(100.0, active_pct)) / 100.0) / total_count) * 100.0
        return max(0.0, min(99.0, val))

    def _update_progress_display(
        self,
        pct: float,
        step_text: str,
        overall_text: str | None = None,
        overall_pct: float | None = None,
    ):
        clamped = max(0.0, min(100.0, float(pct)))
        self.progress.configure(maximum=100.0, value=clamped)
        self.pct_text.set(f"{int(round(clamped))}%")
        self.status.set(step_text)
        if overall_text is not None:
            self.overall_status.set(overall_text)
        if overall_pct is not None:
            o_clamped = max(0.0, min(100.0, float(overall_pct)))
            self.overall_progress.configure(maximum=100.0, value=o_clamped)
            self.overall_pct_text.set(f"{int(round(o_clamped))}%")

    def _format_overall_progress(self, finished_count: int, total_count: int, current_photo: str, active_pct: float = 0.0) -> str:
        overall_pct = int(round(self._calc_overall_pct(finished_count, total_count, active_pct)))
        return f"总体进度：{overall_pct}%（已完成 {finished_count}/{total_count} 张） | 当前照片：{current_photo}"

    def _get_target_dimensions(self) -> tuple[int, int]:
        label = self.size_choice.get()
        for name, w, h in PHOTO_SIZES:
            if name == label:
                if w == -1 and h == -1:
                    return max(10, int(self.custom_w.get())), max(10, int(self.custom_h.get()))
                return w, h
        return 0, 0

    def _refresh_gpu_hint(self):
        if not hasattr(self, "gpu_status_lbl"):
            return
        _provs, desc = detect_ort_providers(bool(self.use_gpu.get()))
        is_gpu = ("GPU" in desc) or ("CUDA" in desc)
        self.gpu_status_lbl.configure(
            text=f"[{desc}]",
            fg=THEME["ok_green"] if is_gpu else THEME["muted"],
        )

    def _snapshot_process_params(self):
        tw, th = self._get_target_dimensions()
        provs, gpu_desc = detect_ort_providers(bool(self.use_gpu.get()))
        return {
            "mode_choice": self.mode_choice.get(),
            "ort_providers": provs,
            "gpu_desc": gpu_desc,
            "neck_offset_ratio": float(self.neck_offset_pct.get()) / 100.0,
            "target_w": tw,
            "target_h": th,
            "auto_crop": bool(self.auto_crop.get()),
            "brightness": int(self.brightness_val.get()),
            "contrast": int(self.contrast_val.get()),
            "gradient_bg": bool(self.gradient_bg.get()),
            "layout_6in": bool(self.layout_6in.get()),
            "fine_edge": bool(self.fine_edge.get()),
            "fmt": (self.output_format.get() or "JPG").upper(),
            "ext": self._current_ext(),
            "limit_enabled": bool(self.limit_size_enabled.get()),
            "max_kb": int(self.max_kb.get()),
        }

    def _on_listbox_right_click(self, event):
        if self._is_running or self._is_downloading_model:
            return
        idx = self.listbox.nearest(event.y)
        if idx < 0 or idx >= len(self._images):
            return
        self.listbox.selection_clear(0, tk.END)
        self.listbox.selection_set(idx)
        self.listbox.activate(idx)

        src = self._images[idx]
        key = str(src.resolve())

        menu = tk.Menu(self.root, tearoff=0)
        regen_submenu = tk.Menu(menu, tearoff=0)
        for m_name in ALL_MODEL_NAMES:
            ready = is_model_ready(m_name)
            label = f"{m_name}（已就绪）" if ready else f"{m_name}（需先下载）"
            regen_submenu.add_command(
                label=label,
                command=lambda target_m=m_name, item_idx=idx: self._regen_single_with_model(item_idx, target_m),
            )
        menu.add_cascade(label="重新生成", menu=regen_submenu)

        menu.add_command(label="更改生成名称", command=lambda item_idx=idx: self._rename_single_output(item_idx))
        if key in self._custom_stems:
            menu.add_command(label="恢复清单/原名", command=lambda item_idx=idx: self._clear_single_custom_name(item_idx))

        menu.add_command(label="另存为...", command=lambda item_idx=idx: self._save_as_single_dir(item_idx))
        if key in self._custom_out_dirs:
            menu.add_command(label="取消单独另存位置", command=lambda item_idx=idx: self._clear_single_out_dir(item_idx))

        menu.add_command(label="复制图片", command=lambda item_idx=idx: self._copy_single_image(item_idx))
        menu.add_separator()
        menu.add_command(label="从列表移除", command=lambda item_idx=idx: self._remove_from_list(item_idx))
        menu.add_command(label="删除本地文件", command=lambda item_idx=idx: self._delete_local_file(item_idx))

        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _rename_single_output(self, idx: int):
        if idx < 0 or idx >= len(self._images):
            return
        src = self._images[idx]
        key = str(src.resolve())
        current_stem = self._effective_stem(idx, src)
        new_raw = simpledialog.askstring(
            "更改生成名称",
            f"原图：{src.name}请输入单独生成的名称（底部名称清单不会改动）：",
            initialvalue=current_stem,
            parent=self.root,
        )
        if new_raw is None:
            return
        cleaned = sanitize_stem_name(new_raw)
        if not cleaned:
            messagebox.showwarning("提示", "名称不能为空或仅含非法字符。")
            return
        self._custom_stems[key] = cleaned
        self._refresh_list_view(self._images)
        self.status.set(f"已为 #{idx + 1} {src.name} 单独设置输出名称：{cleaned}{self._current_ext()}")

    def _clear_single_custom_name(self, idx: int):
        if idx < 0 or idx >= len(self._images):
            return
        src = self._images[idx]
        key = str(src.resolve())
        self._custom_stems.pop(key, None)
        self._refresh_list_view(self._images)
        self.status.set(f"已恢复 #{idx + 1} {src.name} 的默认/清单命名规则")

    def _save_as_single_dir(self, idx: int):
        if idx < 0 or idx >= len(self._images):
            return
        src = self._images[idx]
        key = str(src.resolve())
        chosen = filedialog.askdirectory(
            title=f"选择“{src.name}”的另存为文件夹",
            parent=self.root,
        )
        if chosen:
            self._custom_out_dirs[key] = Path(chosen)
            self._refresh_list_view(self._images)
            self.status.set(f"已设置 #{idx + 1} {src.name} 另存为：{chosen}")

    def _clear_single_out_dir(self, idx: int):
        if idx < 0 or idx >= len(self._images):
            return
        src = self._images[idx]
        key = str(src.resolve())
        self._custom_out_dirs.pop(key, None)
        self._refresh_list_view(self._images)
        self.status.set(f"已取消 #{idx + 1} {src.name} 的单独另存位置")

    def _copy_single_image(self, idx: int):
        if idx < 0 or idx >= len(self._images):
            return
        src = self._images[idx]
        key = str(src.resolve())
        out_p = self._last_output_files.get(key)
        target_to_copy = out_p if (out_p is not None and out_p.exists()) else src
        try:
            copy_image_to_clipboard(target_to_copy)
            kind_label = "已生成图片" if target_to_copy == out_p else "原图"
            self.status.set(f"已复制{kind_label}到剪贴板：{target_to_copy.name}")
        except Exception as e:
            messagebox.showerror("复制失败", f"无法复制到剪贴板：{e}")

    def _remove_from_list(self, idx: int):
        if idx < 0 or idx >= len(self._images):
            return
        removed = self._images.pop(idx)
        key = str(removed.resolve())
        self._img_states.pop(key, None)
        self._custom_stems.pop(key, None)
        self._custom_out_dirs.pop(key, None)
        self._last_output_files.pop(key, None)
        self._apply_name_lines_preview()
        self.status.set(f"已从列表移除：{removed.name}")

    def _delete_local_file(self, idx: int):
        if idx < 0 or idx >= len(self._images):
            return
        src = self._images[idx]
        key = str(src.resolve())
        out_p = self._last_output_files.get(key)
        lines = [f"原文件：{src}"]
        if out_p is not None and out_p.exists():
            lines.append(f"已生成文件：{out_p}")
        confirm = messagebox.askyesno(
            "删除本地文件",
            "确定要从硬盘删除以下本地文件吗？删除后无法恢复！" + "".join(lines),
            parent=self.root,
        )
        if not confirm:
            return
        try:
            if out_p is not None and out_p.exists():
                out_p.unlink()
            if src.exists():
                src.unlink()
            self._remove_from_list(idx)
            self.status.set(f"已删除本地文件：{src.name}")
        except Exception as e:
            messagebox.showerror("删除失败", f"删除文件时出错：{e}")

    def _regen_single_with_model(self, idx: int, model_name: str):
        if self._is_running or self._is_downloading_model:
            return
        if idx < 0 or idx >= len(self._images):
            return
        if not is_model_ready(model_name):
            messagebox.showwarning(
                "模型未就绪",
                f"模型 [{model_name}] 尚未下载。请先在上方“抠图模型”中选择该模型并点击“下载模型”。",
            )
            return

        src = self._images[idx]
        key = str(src.resolve())
        c_stem = self._custom_stems.get(key) or (self._name_lines[idx] if idx < len(self._name_lines) else None)
        out_dir_override = self._custom_out_dirs.get(key)

        out_str = self.output_dir.get().strip()
        if not out_str and out_dir_override is None:
            messagebox.showwarning("提示", "请先设置导出位置。")
            return
        base_out = Path(out_str or str(Path.home() / "Desktop")).expanduser()
        if out_dir_override is None:
            base_out.mkdir(parents=True, exist_ok=True)
        else:
            out_dir_override.mkdir(parents=True, exist_ok=True)

        try:
            rgb = hex_to_rgb(self.color.get())
        except Exception as e:
            messagebox.showerror("颜色设置错误", str(e))
            return

        params = self._snapshot_process_params()
        params["mode_choice"] = "single"
        input_paths_snapshot = list(self.input_paths)
        model_label = format_model_label(model_name)

        self._is_running = True
        self.start_btn.set_enabled(False)
        self._update_progress_display(
            10.0,
            f"正在加载 {model_label} 模型（10%）",
            f"总体进度：0%（已完成 0/1 张） | 当前照片：{src.name}",
            overall_pct=0.0,
        )

        def _single_worker():
            sess = None
            try:
                prepare_models_env()
                self.msgq.put((
                    "step_progress",
                    10.0,
                    f"正在加载 {model_label} 模型（10%）",
                    f"总体进度：10%（已完成 0/1 张） | 当前照片：{src.name}",
                    10.0,
                ))
                sess = self._create_session_with_providers(model_name, providers=params.get("ort_providers"))
                out_folder, out_fname = self._process_single_image(
                    src=src,
                    c_stem=c_stem,
                    input_paths=input_paths_snapshot,
                    weighted_sessions=[(model_name, 1.0, sess)],
                    base_out=base_out,
                    rgb=rgb,
                    params=params,
                    override_dir=out_dir_override,
                    step_cb=lambda pct, desc: self.msgq.put((
                        "step_progress",
                        pct,
                        desc,
                        f"总体进度：{int(round(pct))}%（已完成 0/1 张） | 当前照片：{src.name}",
                        pct,
                    )),
                )
                self._last_output_files[key] = out_folder / out_fname
                if not self._closing:
                    self.msgq.put(("single_regen_done", True, src, f"当前照片处理完成（100%） - {out_fname} ✔"))
            except Exception as e:
                if not self._closing:
                    self.msgq.put(("single_regen_done", False, src, f"重新生成失败：{src.name} -> {e}"))
            finally:
                if sess is not None:
                    try:
                        del sess
                    except Exception:
                        pass
                gc.collect()

        threading.Thread(target=_single_worker, daemon=True).start()

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
                f"检测到拖入：{preview_names}共扫描到 {len(candidate_images)} 张待处理图片。是否确认加入列表？（加入后需手动点击“开始批量处理”）",
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
        init_overall_pct = (done_cnt / len(images) * 100.0) if images else 0.0
        self._update_progress_display(
            0.0,
            self.status.get(),
            f"总体进度：{int(round(init_overall_pct))}%（已完成 {done_cnt}/{len(images)} 张） | 当前照片：无",
            overall_pct=init_overall_pct,
        )

    def _build(self):
        header = tk.Frame(self.root, bg=THEME["bg"])
        header.pack(fill="x", padx=14, pady=(4, 1))
        tk.Label(
            header,
            text=APP_DISPLAY_NAME,
            bg=THEME["bg"],
            fg=THEME["text"],
            font=("Microsoft YaHei UI", 12, "bold"),
        ).pack(anchor="w")

        main_stack = tk.Frame(self.root, bg=THEME["bg"])
        main_stack.pack(side="top", fill="both", expand=False)

        # 1. 导入区域
        import_outer, import_card = self._make_pill_card(main_stack, radius=20, outer_pady=(0, 3))

        main_hint = (
            "将文件夹或图片拖入此处；列表项支持右键重新生成/改名/另存/复制"
            if HAS_DND
            else "点击下方添加图片；列表项支持右键重新生成/改名/另存/复制"
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
        drop_banner.pack(fill="x", padx=0, pady=(2, 7))

        btn_row = tk.Frame(import_card, bg=THEME["card"])
        btn_row.pack(fill="x", pady=(0, 6))
        self._make_sec_btn(btn_row, "添加照片", self.add_files, width=102, height=23).pack(side="left", padx=(0, 8))
        self._make_sec_btn(btn_row, "添加文件夹", self.add_folder, width=110, height=23).pack(side="left")
        self._make_sec_btn(btn_row, "清空", self.clear, width=88, height=23).pack(side="right")

        list_pill = SoftPillField(
            import_card,
            height=112,
            radius=16,
            bg_color=THEME["list_bg"],
            border_color=THEME["border"],
            parent_bg=THEME["card"],
            padx=10,
            pady=2,
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
            font=("Microsoft YaHei UI", 8),
        )
        self.listbox.pack(fill="both", expand=True)
        self.listbox.bind("<Button-3>", self._on_listbox_right_click)

        self._enable_full_drop_zone(import_outer, drop_banner)

        # 2. 处理参数与模型设置
        _, set_card = self._make_pill_card(main_stack, radius=20, outer_pady=(0, 3))
        tk.Label(
            set_card,
            text="处理参数与模型设置",
            bg=THEME["card"],
            fg=THEME["text"],
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(anchor="w", pady=(0, 2))

        r_mode = tk.Frame(set_card, bg=THEME["card"])
        r_mode.pack(fill="x", pady=(2, 5))
        tk.Radiobutton(
            r_mode,
            text="上下分区双模型（发丝+衣边精修）",
            variable=self.mode_choice,
            value="split",
            command=self._sync_mode_panels,
            bg=THEME["card"],
            fg=THEME["text"],
            selectcolor="#EFF6FF",
            activebackground=THEME["card"],
            font=("Microsoft YaHei UI", 8, "bold"),
        ).pack(side="left", padx=(0, 12))
        tk.Radiobutton(
            r_mode,
            text="单模型全图抠图",
            variable=self.mode_choice,
            value="single",
            command=self._sync_mode_panels,
            bg=THEME["card"],
            fg=THEME["text"],
            selectcolor="#EFF6FF",
            activebackground=THEME["card"],
            font=("Microsoft YaHei UI", 8, "bold"),
        ).pack(side="left")

        self.single_container = tk.Frame(set_card, bg=THEME["card"])

        r_models = tk.Frame(self.single_container, bg=THEME["card"])
        r_models.pack(fill="x", pady=(0, 2))

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
        self.model_state_lbl.pack(anchor="w", padx=(10, 0))

        self.download_model_btn = self._make_sec_btn(
            r_models,
            "下载模型",
            self.download_selected_model_ui,
            width=self.RIGHT_BTN_W,
            height=23,
        )
        self.download_model_btn.pack(side="right", anchor="n")

        r_custom_chk = tk.Frame(self.single_container, bg=THEME["card"])
        r_custom_chk.pack(fill="x", pady=(0, 1))
        tk.Checkbutton(
            r_custom_chk,
            text="自定义路径（指定电脑中其他位置的 .onnx 模型文件）",
            variable=self.use_custom_model_path,
            command=self._sync_mode_panels,
            bg=THEME["card"],
            fg=THEME["text"],
            selectcolor="#EFF6FF",
            activebackground=THEME["card"],
            font=("Microsoft YaHei UI", 8),
        ).pack(anchor="w", padx=(6, 0))

        r_custom_path = tk.Frame(self.single_container, bg=THEME["card"])
        r_custom_path.pack(fill="x", pady=(0, 2))
        custom_pill = SoftPillField(
            r_custom_path,
            height=24,
            radius=12,
            bg_color="#F8FAFC",
            border_color="#CBD5E1",
            parent_bg=THEME["card"],
            padx=10,
            pady=2,
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
            height=23,
        )
        self.pick_onnx_btn.pack(side="right")

        self.ensemble_container = tk.Frame(set_card, bg=THEME["card"])
        tk.Label(
            self.ensemble_container,
            text="叠加方案：",
            bg=THEME["card"],
            fg=THEME["text"],
            font=("Microsoft YaHei UI", 8),
        ).pack(side="left", padx=(8, 6))
        self.ensemble_combo = ttk.Combobox(
            self.ensemble_container,
            textvariable=self.ensemble_choice,
            values=list(ENSEMBLE_PRESETS.keys()),
            state="readonly",
            width=36,
            font=("Microsoft YaHei UI", 8),
        )
        self.ensemble_combo.pack(side="left")

        self.split_container = tk.Frame(set_card, bg=THEME["card"])

        r_split_top = tk.Frame(self.split_container, bg=THEME["card"])
        r_split_top.pack(fill="x", pady=(0, 2))
        tk.Label(
            r_split_top,
            text="脖子以上(发丝/头部)：",
            bg=THEME["card"],
            fg=THEME["text"],
            font=("Microsoft YaHei UI", 8, "bold"),
        ).pack(side="left", padx=(6, 4))
        self.head_model_combo = ttk.Combobox(
            r_split_top,
            textvariable=self.head_model,
            values=ALL_MODEL_NAMES,
            state="readonly",
            width=18,
            font=("Microsoft YaHei UI", 8),
        )
        self.head_model_combo.pack(side="left", padx=(0, 8))
        self.head_model_combo.bind("<<ComboboxSelected>>", lambda _e: self._sync_mode_panels())

        self.dl_head_btn = self._make_sec_btn(
            r_split_top,
            "下载上模型",
            lambda: self.download_specific_model_ui(self.head_model.get()),
            width=self.RIGHT_BTN_W,
            height=22,
        )
        self.dl_head_btn.pack(side="right")

        r_split_bot = tk.Frame(self.split_container, bg=THEME["card"])
        r_split_bot.pack(fill="x", pady=(0, 2))
        tk.Label(
            r_split_bot,
            text="脖子以下(衣领/衣边)：",
            bg=THEME["card"],
            fg=THEME["text"],
            font=("Microsoft YaHei UI", 8, "bold"),
        ).pack(side="left", padx=(6, 4))
        self.body_model_combo = ttk.Combobox(
            r_split_bot,
            textvariable=self.body_model,
            values=ALL_MODEL_NAMES,
            state="readonly",
            width=18,
            font=("Microsoft YaHei UI", 8),
        )
        self.body_model_combo.pack(side="left", padx=(0, 8))
        self.body_model_combo.bind("<<ComboboxSelected>>", lambda _e: self._sync_mode_panels())

        self.dl_body_btn = self._make_sec_btn(
            r_split_bot,
            "下载下模型",
            lambda: self.download_specific_model_ui(self.body_model.get()),
            width=self.RIGHT_BTN_W,
            height=22,
        )
        self.dl_body_btn.pack(side="right")

        r_split_info = tk.Frame(self.split_container, bg=THEME["card"])
        r_split_info.pack(fill="x", pady=(0, 2))
        self.split_state_lbl = tk.Label(
            r_split_info,
            text="",
            bg=THEME["card"],
            fg=THEME["muted"],
            font=("Microsoft YaHei UI", 8, "bold"),
        )
        self.split_state_lbl.pack(side="left", padx=(6, 8))

        tk.Label(
            r_split_info,
            text="分界微调(%):",
            bg=THEME["card"],
            fg=THEME["muted"],
            font=("Microsoft YaHei UI", 8),
        ).pack(side="left")
        ttk.Spinbox(
            r_split_info,
            from_=-20,
            to=20,
            textvariable=self.neck_offset_pct,
            width=4,
            font=("Microsoft YaHei UI", 8),
        ).pack(side="left", padx=(2, 0))

        self.mode_anchor = tk.Frame(set_card, bg=THEME["card"])
        self.mode_anchor.pack(fill="x")

        # 尺寸与人脸裁剪行
        r_size = tk.Frame(set_card, bg=THEME["card"])
        r_size.pack(fill="x", pady=(4, 5))
        tk.Label(r_size, text="照片尺寸：", bg=THEME["card"], fg=THEME["text"], font=("Microsoft YaHei UI", 8)).pack(side="left")
        self.size_combo = ttk.Combobox(
            r_size,
            textvariable=self.size_choice,
            values=[s[0] for s in PHOTO_SIZES],
            state="readonly",
            width=18,
            font=("Microsoft YaHei UI", 8),
        )
        self.size_combo.pack(side="left", padx=(4, 6))
        self.size_combo.bind("<<ComboboxSelected>>", lambda _e: self._sync_size_custom_state())

        self.custom_w_spin = ttk.Spinbox(r_size, from_=50, to=3000, textvariable=self.custom_w, width=5, font=("Microsoft YaHei UI", 8))
        self.custom_w_spin.pack(side="left")
        tk.Label(r_size, text="×", bg=THEME["card"], fg=THEME["muted"], font=("Microsoft YaHei UI", 8)).pack(side="left", padx=2)
        self.custom_h_spin = ttk.Spinbox(r_size, from_=50, to=3000, textvariable=self.custom_h, width=5, font=("Microsoft YaHei UI", 8))
        self.custom_h_spin.pack(side="left", padx=(0, 6))

        tk.Checkbutton(
            r_size,
            text="自动人脸裁切",
            variable=self.auto_crop,
            bg=THEME["card"],
            fg=THEME["text"],
            selectcolor="#EFF6FF",
            activebackground=THEME["card"],
            font=("Microsoft YaHei UI", 8),
        ).pack(side="right")

        # 亮度 / 对比度 / 6寸排版行
        r_adj = tk.Frame(set_card, bg=THEME["card"])
        r_adj.pack(fill="x", pady=(0, 5))
        tk.Label(r_adj, text="亮度:", bg=THEME["card"], fg=THEME["text"], font=("Microsoft YaHei UI", 8)).pack(side="left")
        ttk.Spinbox(r_adj, from_=-100, to=100, textvariable=self.brightness_val, width=5, font=("Microsoft YaHei UI", 8)).pack(side="left", padx=(2, 8))
        tk.Label(r_adj, text="对比度:", bg=THEME["card"], fg=THEME["text"], font=("Microsoft YaHei UI", 8)).pack(side="left")
        ttk.Spinbox(r_adj, from_=-100, to=100, textvariable=self.contrast_val, width=5, font=("Microsoft YaHei UI", 8)).pack(side="left", padx=(2, 10))
        tk.Checkbutton(
            r_adj,
            text="6寸相纸排版（1800×1200）",
            variable=self.layout_6in,
            command=self._apply_name_lines_preview,
            bg=THEME["card"],
            fg=THEME["text"],
            selectcolor="#EFF6FF",
            activebackground=THEME["card"],
            font=("Microsoft YaHei UI", 8, "bold"),
        ).pack(side="right")

        # 去红发丝与渐变背景行
        r_fine = tk.Frame(set_card, bg=THEME["card"])
        r_fine.pack(fill="x", pady=(0, 5))
        tk.Checkbutton(
            r_fine,
            text="去红发丝/护衣边",
            variable=self.fine_edge,
            bg=THEME["card"],
            fg=THEME["text"],
            selectcolor="#EFF6FF",
            activebackground=THEME["card"],
            font=("Microsoft YaHei UI", 8, "bold"),
        ).pack(side="left", padx=(0, 12))
        tk.Checkbutton(
            r_fine,
            text="渐变背景（顶部→底部白色）",
            variable=self.gradient_bg,
            bg=THEME["card"],
            fg=THEME["text"],
            selectcolor="#EFF6FF",
            activebackground=THEME["card"],
            font=("Microsoft YaHei UI", 8),
        ).pack(side="left")

        # 背景颜色行
        r1 = tk.Frame(set_card, bg=THEME["card"])
        r1.pack(fill="x", pady=(0, 5))
        tk.Label(r1, text="背景颜色：", bg=THEME["card"], fg=THEME["text"], font=("Microsoft YaHei UI", 8)).pack(side="left")
        self.preset = ttk.Combobox(
            r1,
            values=list(PRESETS.keys()) + ["自定义颜色"],
            state="readonly",
            width=15,
            font=("Microsoft YaHei UI", 8),
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
            font=("Microsoft YaHei UI", 8),
        )
        self.hex_label.pack(side="left")

        self._make_sec_btn(
            r1,
            "自定义颜色",
            self.choose_color,
            width=self.RIGHT_BTN_W,
            height=23,
        ).pack(side="right")

        # 输出格式与大小限制行
        r2 = tk.Frame(set_card, bg=THEME["card"])
        r2.pack(fill="x", pady=(0, 5))
        tk.Label(r2, text="输出格式：", bg=THEME["card"], fg=THEME["text"], font=("Microsoft YaHei UI", 8)).pack(side="left")
        self.format_combo = ttk.Combobox(
            r2,
            textvariable=self.output_format,
            values=["JPG", "PNG"],
            state="readonly",
            width=7,
            font=("Microsoft YaHei UI", 8),
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
            font=("Microsoft YaHei UI", 8, "bold"),
        ).pack(side="left", padx=(0, 4))

        self.max_kb_spin = ttk.Spinbox(
            r2,
            from_=10,
            to=5000,
            textvariable=self.max_kb,
            width=7,
            font=("Microsoft YaHei UI", 8),
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

        # 并发线程与 GPU 加速行
        r_th = tk.Frame(set_card, bg=THEME["card"])
        r_th.pack(fill="x")
        tk.Label(r_th, text="并发线程：", bg=THEME["card"], fg=THEME["text"], font=("Microsoft YaHei UI", 8)).pack(side="left")
        ttk.Spinbox(
            r_th,
            from_=1,
            to=4,
            textvariable=self.thread_count,
            width=5,
            font=("Microsoft YaHei UI", 8),
        ).pack(side="left", padx=(4, 4))
        tk.Label(r_th, text="线程（建议 1）", bg=THEME["card"], fg=THEME["muted"], font=("Microsoft YaHei UI", 8)).pack(side="left", padx=(0, 8))

        tk.Checkbutton(
            r_th,
            text="GPU 加速",
            variable=self.use_gpu,
            command=self._refresh_gpu_hint,
            bg=THEME["card"],
            fg=THEME["text"],
            selectcolor="#EFF6FF",
            activebackground=THEME["card"],
            font=("Microsoft YaHei UI", 8, "bold"),
        ).pack(side="left")

        _, _gpu_desc = detect_ort_providers(True)
        self.gpu_status_lbl = tk.Label(
            r_th,
            text=f"[{_gpu_desc}]",
            bg=THEME["card"],
            fg=THEME["ok_green"] if "GPU" in _gpu_desc or "CUDA" in _gpu_desc else THEME["muted"],
            font=("Microsoft YaHei UI", 8),
        )
        self.gpu_status_lbl.pack(side="left", padx=(4, 0))

        # 3. 导出位置
        _, dir_card = self._make_pill_card(main_stack, radius=20, outer_pady=(0, 3))
        tk.Label(
            dir_card,
            text="导出位置（自动生成同名文件夹，如导入 16 则导出为 <所选位置>\16）",
            bg=THEME["card"],
            fg=THEME["text"],
            font=("Microsoft YaHei UI", 8, "bold"),
        ).pack(anchor="w", pady=(0, 2))

        r3 = tk.Frame(dir_card, bg=THEME["card"])
        r3.pack(fill="x")
        out_pill = SoftPillField(
            r3,
            height=24,
            radius=12,
            bg_color="#F8FAFC",
            border_color="#CBD5E1",
            parent_bg=THEME["card"],
            padx=10,
            pady=2,
        )
        out_pill.canvas.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.out_entry = tk.Entry(
            out_pill.inner,
            textvariable=self.output_dir,
            font=("Microsoft YaHei UI", 8),
            bg="#F8FAFC",
            fg=THEME["text"],
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
        )
        self.out_entry.pack(fill="both", expand=True)
        self._make_sec_btn(r3, "原目录", self.use_source_dir_as_output, width=74, height=23).pack(side="left", padx=(0, 6))
        self._make_sec_btn(r3, "浏览", self.choose_output, width=74, height=23).pack(side="right")

        # 4. 名称清单
        _, name_card = self._make_pill_card(main_stack, radius=20, outer_pady=(0, 3))
        name_top = tk.Frame(name_card, bg=THEME["card"])
        name_top.pack(fill="x", pady=(0, 2))
        tk.Label(
            name_top,
            text="名称清单（每行一个新名称，支持直接粘贴表格一列或导入 TXT）",
            bg=THEME["card"],
            fg=THEME["text"],
            font=("Microsoft YaHei UI", 8, "bold"),
        ).pack(side="left")

        self._make_sec_btn(name_top, "清空", self.clear_names_text, width=64, height=21).pack(side="right")
        self._make_sec_btn(name_top, "导入 TXT", self.import_names_txt, width=80, height=21).pack(side="right", padx=(0, 6))

        name_pill = SoftPillField(
            name_card,
            height=64,
            radius=14,
            bg_color="#F8FAFC",
            border_color="#CBD5E1",
            parent_bg=THEME["card"],
            padx=10,
            pady=4,
        )
        name_pill.canvas.pack(fill="x", pady=(2, 4))
        self.name_text = tk.Text(
            name_pill.inner,
            height=2,
            bg="#F8FAFC",
            fg=THEME["text"],
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            font=("Microsoft YaHei UI", 8),
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
        self.name_count_lbl.pack(anchor="w", pady=(0, 0))

        # 5. 底部双进度条与主按钮
        bottom = tk.Frame(self.root, bg=THEME["bg"])
        bottom.pack(side="bottom", fill="x", padx=14, pady=(2, 8))

        tk.Frame(bottom, height=2, bg=THEME["bg"], bd=0, highlightthickness=0).pack(fill="x")

        prog_row = tk.Frame(bottom, bg=THEME["bg"])
        prog_row.pack(fill="x", pady=(0, 2))
        tk.Label(
            prog_row,
            text="当前：",
            bg=THEME["bg"],
            fg=THEME["muted"],
            font=("Microsoft YaHei UI", 8, "bold"),
        ).pack(side="left", padx=(0, 4))
        self.progress = SoftPillProgressBar(
            prog_row,
            height=9,
            track_color="#DCEAFF",
            fill_color=THEME["accent"],
            parent_bg=THEME["bg"],
        )
        self.progress.pack(side="left", fill="x", expand=True, padx=(0, 6))
        tk.Label(
            prog_row,
            textvariable=self.pct_text,
            bg=THEME["bg"],
            fg=THEME["accent"],
            font=("Microsoft YaHei UI", 8, "bold"),
            width=6,
            anchor="e",
        ).pack(side="right", padx=(0, 2))

        overall_prog_row = tk.Frame(bottom, bg=THEME["bg"])
        overall_prog_row.pack(fill="x", pady=(0, 2))
        tk.Label(
            overall_prog_row,
            text="总计：",
            bg=THEME["bg"],
            fg=THEME["muted"],
            font=("Microsoft YaHei UI", 8, "bold"),
        ).pack(side="left", padx=(0, 4))
        self.overall_progress = SoftPillProgressBar(
            overall_prog_row,
            height=11,
            track_color="#DBEAFE",
            fill_color="#1D4ED8",
            parent_bg=THEME["bg"],
        )
        self.overall_progress.pack(side="left", fill="x", expand=True, padx=(0, 6))
        tk.Label(
            overall_prog_row,
            textvariable=self.overall_pct_text,
            bg=THEME["bg"],
            fg="#1D4ED8",
            font=("Microsoft YaHei UI", 8, "bold"),
            width=6,
            anchor="e",
        ).pack(side="right", padx=(0, 2))

        tk.Label(
            bottom,
            textvariable=self.status,
            bg=THEME["bg"],
            fg=THEME["text"],
            font=("Microsoft YaHei UI", 8, "bold"),
            anchor="w",
        ).pack(fill="x", pady=(0, 1))

        tk.Label(
            bottom,
            textvariable=self.overall_status,
            bg=THEME["bg"],
            fg=THEME["muted"],
            font=("Microsoft YaHei UI", 8),
            anchor="w",
        ).pack(fill="x", pady=(0, 3))

        self.start_btn = SoftPillButton(
            bottom,
            text="开始批量处理",
            command=self.start,
            width=600,
            height=36,
            radius=18,
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

    def _sync_size_custom_state(self):
        is_custom = (self.size_choice.get() == "自定义尺寸")
        st = "normal" if is_custom else "disabled"
        self.custom_w_spin.configure(state=st)
        self.custom_h_spin.configure(state=st)

    def _sync_size_limit_state(self):
        enabled = bool(self.limit_size_enabled.get())
        self.max_kb_spin.configure(state="normal" if enabled else "disabled")
        if enabled:
            self.max_kb_unit_lbl.configure(text="KB（超出自动压缩）", fg=THEME["muted"])
        else:
            self.max_kb_unit_lbl.configure(text="KB（已关闭：原像素尺寸无限制输出）", fg=THEME["accent"])

    def _sync_mode_panels(self):
        mode = self.mode_choice.get()
        is_single = (mode == "single")
        is_split = (mode == "split")

        self.single_container.pack_forget()
        self.split_container.pack_forget()
        self.ensemble_container.pack_forget()

        if is_split:
            self.split_container.pack(fill="x", before=self.mode_anchor)
        elif is_single:
            self.single_container.pack(fill="x", before=self.mode_anchor)
        else:
            self.ensemble_container.pack(fill="x", before=self.mode_anchor)

        hm = self.head_model.get()
        bm = self.body_model.get()
        h_ready = is_model_ready(hm)
        b_ready = is_model_ready(bm)

        self.dl_head_btn.set_text("上模型就绪" if h_ready else "下载上模型")
        self.dl_head_btn.set_enabled((not h_ready) and (not self._is_downloading_model) and (not self._is_running))
        self.dl_body_btn.set_text("下模型就绪" if b_ready else "下载下模型")
        self.dl_body_btn.set_enabled((not b_ready) and (not self._is_downloading_model) and (not self._is_running))

        if h_ready and b_ready:
            self.split_state_lbl.configure(
                text=f"分区就绪：上[{format_model_label(hm)}] + 下[{format_model_label(bm)}]",
                fg=THEME["ok_green"],
            )
        else:
            missing_parts = []
            if not h_ready:
                missing_parts.append(f"上:{hm}")
            if not b_ready:
                missing_parts.append(f"下:{bm}")
            self.split_state_lbl.configure(
                text=f"待下载：{' / '.join(missing_parts)}",
                fg=THEME["err_red"],
            )

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
        self._custom_stems = {}
        self._custom_out_dirs = {}
        self._last_output_files = {}
        self.listbox.delete(0, tk.END)
        self._apply_name_lines_preview()
        self._update_progress_display(
            0.0,
            "列表已清空",
            "总体进度：0%（已完成 0/0 张） | 当前照片：无",
            overall_pct=0.0,
        )

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

    def download_specific_model_ui(self, target_model: str):
        if self._is_downloading_model or self._is_running:
            return
        if is_model_ready(target_model):
            self._sync_mode_panels()
            return

        self._is_downloading_model = True
        self.download_model_btn.set_enabled(False)
        self.dl_head_btn.set_enabled(False)
        self.dl_body_btn.set_enabled(False)
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

    def download_selected_model_ui(self):
        self.download_specific_model_ui(self.single_model.get())

    def _ask_conflict_or_done_policy(self, done_count: int, disk_exist_count: int, overlap_total: int, total_count: int) -> str | None:
        dlg = tk.Toplevel(self.root)
        dlg.title("已存在 / 已处理文件确认")
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.resizable(False, False)
        dlg.configure(bg=THEME["bg"])

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
        card.win_id and card.canvas.itemconfigure(card.win_id, width=380)

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
            text=f"{detail_str}请选择统一处理方式：",
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

        dlg.update_idletasks()
        card.canvas.configure(width=416)
        card._redraw()
        dlg.update_idletasks()
        req_w = 436
        req_h = max(168, dlg.winfo_reqheight() + 8)
        left, top, right, bottom = _get_workarea_bounds(dlg)
        work_w = max(320, right - left)
        work_h = max(320, bottom - top)
        dx = left + max(0, (work_w - req_w) // 2)
        dy = top + max(0, (work_h - req_h) // 2)
        dlg.geometry(f"{req_w}x{req_h}+{dx}+{dy}")

        dlg.protocol("WM_DELETE_WINDOW", lambda: _pick(None))
        self.root.wait_window(dlg)
        return choice["val"]

    def _get_target_model_specs(self):
        mode = self.mode_choice.get()
        if mode == "split":
            hm = self.head_model.get()
            bm = self.body_model.get()
            return [(hm, 1.0, ""), (bm, 1.0, "")]
        if mode == "single":
            m_name = self.single_model.get()
            custom_p = self.custom_model_file.get().strip() if self.use_custom_model_path.get() else ""
            return [(m_name, 1.0, custom_p)]
        specs = ENSEMBLE_PRESETS.get(self.ensemble_choice.get(), ENSEMBLE_PRESETS["双模型叠加 (birefnet + isnet)"])
        return [(m_name, w, "") for m_name, w in specs]

    def _create_session_with_providers(self, m_name: str, providers: list[str] | None = None):
        if providers:
            try:
                return new_session(m_name, providers=providers)
            except TypeError:
                return new_session(m_name)
        return new_session(m_name)

    def _build_session_for_spec(self, m_name: str, custom_p: str, providers: list[str] | None = None):
        if custom_p:
            src_onnx = Path(custom_p)
            if not src_onnx.is_file():
                raise FileNotFoundError(f"自定义模型文件不存在：{src_onnx}")
            target_dir = get_model_subdir(m_name)
            target_dir.mkdir(parents=True, exist_ok=True)
            linked_onnx = target_dir / f"{m_name}.onnx"
            if src_onnx.resolve() != linked_onnx.resolve():
                shutil.copy2(src_onnx, linked_onnx)
        return self._create_session_with_providers(m_name, providers=providers)

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
                f"当前任务需要模型：{', '.join(missing_models)}"
                f"请先在“抠图模型”中下载该模型，或勾选“自定义路径”选择对应的 .onnx 文件。",
            )
            return

        self._apply_name_lines_preview()
        name_snapshot = list(self._name_lines)
        params = self._snapshot_process_params()
        ext = params["ext"]
        layout_6in = params["layout_6in"]
        input_paths_snapshot = list(self.input_paths)

        task_entries = []
        for idx, p in enumerate(all_images):
            key = str(p.resolve())
            if key in self._custom_stems and self._custom_stems[key]:
                c_stem = self._custom_stems[key]
            elif idx < len(name_snapshot):
                c_stem = name_snapshot[idx]
            else:
                c_stem = None
            out_override = self._custom_out_dirs.get(key)
            if out_override is not None:
                out_override.mkdir(parents=True, exist_ok=True)
            task_entries.append((idx, p, c_stem, out_override))

        done_set = set()
        disk_exist_set = set()
        for _idx, p, c_stem, out_override in task_entries:
            key = str(p.resolve())
            if self._img_states.get(key) == "ok":
                done_set.add(key)
            _, target_file = compute_target_output_path(
                p, input_paths_snapshot, base_out, ext, c_stem, out_override, layout_6in=layout_6in
            )
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
                for _idx, p, _c, _o in task_entries:
                    key = str(p.resolve())
                    self._img_states[key] = "ok" if key in overlap_keys else "pending"
                self._refresh_list_view(all_images)
                if not tasks_to_run:
                    self._update_progress_display(
                        100.0,
                        "所有重复/已完成文件均已跳过",
                        f"总体进度：100%（已完成 {len(all_images)}/{len(all_images)} 张） | 当前照片：无",
                        overall_pct=100.0,
                    )
                    messagebox.showinfo("提示", "所选文件均已存在或已完成，已全部跳过。")
                    return
            else:
                tasks_to_run = list(task_entries)
                for _idx, p, _c, _o in tasks_to_run:
                    self._img_states[str(p.resolve())] = "pending"
                self._refresh_list_view(all_images)
        else:
            tasks_to_run = list(task_entries)
            for _idx, p, _c, _o in tasks_to_run:
                self._img_states[str(p.resolve())] = "pending"
            self._refresh_list_view(all_images)

        prepare_models_env()

        self._is_running = True
        first_model_label = format_model_label(target_specs[0][0])
        first_photo_name = tasks_to_run[0][1].name if tasks_to_run else "无"
        self.start_btn.set_enabled(False)
        self.download_model_btn.set_enabled(False)
        self.dl_head_btn.set_enabled(False)
        self.dl_body_btn.set_enabled(False)
        self.pick_onnx_btn.set_enabled(False)
        init_o_pct = self._calc_overall_pct(0, len(tasks_to_run), 10.0)
        self._update_progress_display(
            10.0,
            f"正在加载 {first_model_label} 模型（10%）",
            self._format_overall_progress(0, len(tasks_to_run), first_photo_name, 10.0),
            overall_pct=init_o_pct,
        )

        threading.Thread(
            target=self._worker,
            args=(tasks_to_run, input_paths_snapshot, base_out, rgb, target_specs, params, workers),
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

    def _split_head_body_alpha(self, im_rgba, head_sess, body_sess, offset_ratio: float = 0.0, step_cb=None):
        w, h = im_rgba.size
        if callable(step_cb):
            step_cb(28.0, "头部模型：分析完整照片中的头发与脸部边缘（28%）")
        head_alpha = self._run_model_alpha(im_rgba, head_sess).resize(
            (w, h), Image.Resampling.BILINEAR
        )
        if callable(step_cb):
            step_cb(52.0, "服装模型：分析完整照片中的衣领与肩部轮廓（52%）")
        body_alpha = self._run_model_alpha(im_rgba, body_sess).resize(
            (w, h), Image.Resampling.BILINEAR
        )

        if not HAS_NUMPY:
            return head_alpha

        head_arr = np.asarray(head_alpha, dtype=np.float32)
        body_arr = np.asarray(body_alpha, dtype=np.float32)
        head_alpha.close()
        body_alpha.close()

        split_y = estimate_neck_split_y(im_rgba, offset_ratio=offset_ratio)
        band = max(24, int(h * 0.18))
        y = np.arange(h, dtype=np.float32)
        t = np.clip((y - (split_y - band * 0.5)) / max(1.0, float(band)), 0.0, 1.0)
        body_weight = (t * t * (3.0 - 2.0 * t)).reshape(h, 1)
        head_weight = 1.0 - body_weight

        fused = head_arr * head_weight + body_arr * body_weight
        lower_zone = (y >= split_y).reshape(h, 1)
        fused = np.where(lower_zone, np.maximum(head_arr, body_arr), fused)

        if callable(step_cb):
            step_cb(70.0, "正在融合头发、颈部与服装蒙版（70%）")
        return Image.fromarray(np.clip(fused, 0, 255).astype(np.uint8), mode="L")

    def _fuse_alphas(self, im_rgba, weighted_sessions, fine_edge=True, step_cb=None, params=None):
        mode = (params or {}).get("mode_choice", "single")
        if mode == "split" and len(weighted_sessions) >= 2:
            raw_a = self._split_head_body_alpha(
                im_rgba=im_rgba,
                head_sess=weighted_sessions[0][2],
                body_sess=weighted_sessions[1][2],
                offset_ratio=float((params or {}).get("neck_offset_ratio", 0.0)),
                step_cb=step_cb,
            )
            if callable(step_cb):
                step_cb(70.0, "正在平滑融合颈部接缝与边缘蒙版（70%）")
            return raw_a

        if callable(step_cb):
            step_cb(30.0, "正在进行 AI 人像抠图（30%）")
        if len(weighted_sessions) == 1 or not HAS_NUMPY:
            raw_a = self._run_model_alpha(im_rgba, weighted_sessions[0][2])
        else:
            w, h = im_rgba.size
            acc = np.zeros((h, w), dtype=np.float32)
            w_sum = 0.0
            for _, weight, sess in weighted_sessions:
                a_i = self._run_model_alpha(im_rgba, sess).resize((w, h), Image.Resampling.BILINEAR)
                acc += np.asarray(a_i, dtype=np.float32) * float(weight)
                w_sum += float(weight)
                a_i.close()
            if w_sum > 0:
                acc /= w_sum
            raw_a = Image.fromarray(np.clip(acc, 0, 255).astype(np.uint8), mode="L")
        if callable(step_cb):
            step_cb(70.0, "正在整理边缘蒙版（70%）")
        return raw_a

    def _clean_fringe_and_composite(self, im_rgba, alpha_img, rgb, fine_edge, gradient_bg=False):
        return self.hair_spill_module.process(
            im_rgba=im_rgba,
            alpha_img=alpha_img,
            bg_rgb=rgb,
            clean_edge=fine_edge,
            gradient=gradient_bg,
        )

    def _process_single_image(
        self,
        src: Path,
        c_stem: str | None,
        input_paths: list,
        weighted_sessions,
        base_out: Path,
        rgb,
        params: dict,
        override_dir: Path | None = None,
        step_cb=None,
    ):
        im = None
        a_ch = None
        composed = None
        result = None
        layout_img = None
        try:
            if callable(step_cb):
                step_cb(15.0, "正在读取照片（15%）")
            im = ImageOps.exif_transpose(Image.open(src)).convert("RGBA")

            a_ch = self._fuse_alphas(im, weighted_sessions, params["fine_edge"], step_cb=step_cb, params=params)

            if callable(step_cb):
                step_cb(82.0, "正在恢复浅色衣物并清理背景色边缘（82%）")
            composed = self._clean_fringe_and_composite(
                im, a_ch, rgb, params["fine_edge"], gradient_bg=params["gradient_bg"]
            )

            if params["brightness"] != 0 or params["contrast"] != 0:
                composed = apply_brightness_contrast_pil(composed, params["brightness"], params["contrast"])

            if params["target_w"] > 0 and params["target_h"] > 0:
                composed = fit_photo_size_pil(composed, params["target_w"], params["target_h"], auto_crop=params["auto_crop"])

            if params["layout_6in"]:
                layout_img = create_6in_layout_pil(composed)
                final_img = layout_img
            else:
                final_img = composed

            if callable(step_cb):
                step_cb(95.0, "正在保存图片（95%）")
            out_folder, target = compute_target_output_path(
                src,
                input_paths,
                base_out,
                params["ext"],
                c_stem,
                override_dir=override_dir,
                layout_6in=params["layout_6in"],
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            data = encode_image_output(final_img, params["fmt"], params["limit_enabled"], params["max_kb"])
            target.write_bytes(data)
            self._last_output_files[str(src.resolve())] = target

            if callable(step_cb):
                step_cb(100.0, "当前照片处理完成（100%）")
            return out_folder, target.name
        finally:
            for obj in (layout_img, result, composed, a_ch, im):
                if obj is not None:
                    try:
                        obj.close()
                    except Exception:
                        pass

    def _worker(self, tasks_to_run, input_paths, base_out, rgb, target_specs, params, workers):
        weighted_sessions = []
        executor = None
        total_tasks = len(tasks_to_run)
        first_photo_name = tasks_to_run[0][1].name if tasks_to_run else "无"
        try:
            if remove is None or new_session is None:
                self.msgq.put(("fatal", "缺少 rembg 组件，请检查打包是否完整。"))
                return

            prepare_models_env()

            for idx, (m_name, weight, custom_p) in enumerate(target_specs, 1):
                if self._closing:
                    return
                m_label = format_model_label(m_name)
                o_pct = self._calc_overall_pct(0, total_tasks, 10.0)
                self.msgq.put((
                    "step_progress",
                    10.0,
                    f"正在加载 {m_label} 模型（10%）",
                    self._format_overall_progress(0, total_tasks, first_photo_name, 10.0),
                    o_pct,
                ))
                try:
                    sess = self._build_session_for_spec(m_name, custom_p, providers=params.get("ort_providers"))
                    weighted_sessions.append((m_name, weight, sess))
                except Exception as e:
                    self.msgq.put((
                        "fatal",
                        f"AI 模型 [{m_name}] 加载失败：{e}模型目录：{get_model_subdir(m_name)}",
                    ))
                    return

            if self._closing:
                return

            o_pct_read = self._calc_overall_pct(0, total_tasks, 15.0)
            self.msgq.put((
                "step_progress",
                15.0,
                "正在读取照片（15%）",
                self._format_overall_progress(0, total_tasks, first_photo_name, 15.0),
                o_pct_read,
            ))

            effective_workers = max(1, min(workers, total_tasks))
            ok = 0
            done_count = 0
            errors = []
            exported_folders = set()
            lock = threading.Lock()

            executor = ThreadPoolExecutor(max_workers=effective_workers)

            def _make_step_cb(photo_name: str):
                def _on_step(pct: float, desc: str):
                    with lock:
                        cur_done = done_count
                    cur_o_pct = self._calc_overall_pct(cur_done, total_tasks, pct)
                    self.msgq.put((
                        "step_progress",
                        pct,
                        desc,
                        self._format_overall_progress(cur_done, total_tasks, photo_name, pct),
                        cur_o_pct,
                    ))
                return _on_step

            future_map = {
                executor.submit(
                    self._process_single_image,
                    src,
                    c_stem,
                    input_paths,
                    weighted_sessions,
                    base_out,
                    rgb,
                    params,
                    out_override,
                    _make_step_cb(src.name),
                ): (idx, src, c_stem)
                for idx, src, c_stem, out_override in tasks_to_run
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
                        cur_done = done_count
                        exported_folders.add(str(out_folder))
                    cur_o_pct = self._calc_overall_pct(cur_done, total_tasks, 0.0)
                    self.msgq.put((
                        "item_done",
                        src,
                        "ok",
                        cur_done,
                        total_tasks,
                        "当前照片处理完成（100%）",
                        self._format_overall_progress(cur_done, total_tasks, src.name, 0.0),
                        cur_o_pct,
                    ))
                except Exception as e:
                    with lock:
                        errors.append(f"{src}: {e}")
                        done_count += 1
                        cur_done = done_count
                    cur_o_pct = self._calc_overall_pct(cur_done, total_tasks, 0.0)
                    self.msgq.put((
                        "item_done",
                        src,
                        "err",
                        cur_done,
                        total_tasks,
                        f"处理失败：{src.name}",
                        self._format_overall_progress(cur_done, total_tasks, src.name, 0.0),
                        cur_o_pct,
                    ))
                if done_count % 3 == 0:
                    gc.collect()

            if not self._closing:
                summary_out = "；".join(sorted(exported_folders)) if exported_folders else str(base_out)
                self.msgq.put(("done", ok, total_tasks, errors, summary_out))
        except Exception:
            if not self._closing:
                self.msgq.put(("fatal", f"后台处理线程发生未捕获异常：{traceback.format_exc()}"))
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

    def _fit_window_to_content_and_screen(self):
        if self._closing or not self.root.winfo_exists():
            return
        try:
            self.root.update_idletasks()
            req_h = self.root.winfo_reqheight()
            cur_w = max(600, self.root.winfo_width())
            left, top, right, bottom = _get_workarea_bounds(self.root)
            work_w = max(320, right - left)
            work_h = max(320, bottom - top)
            # Appropriately lengthen within available workarea above taskbar/status bar
            max_allowed_h = max(320, work_h - 12)
            desired_h = max(req_h + 6, 900)
            target_h = min(desired_h, max_allowed_h)
            target_w = min(max(cur_w, 740), work_w)
            x = left + max(0, (work_w - target_w) // 2)
            y = top + max(0, min(work_h - target_h - 6, int((work_h - target_h) * 0.20)))
            self.root.geometry(f"{target_w}x{target_h}+{x}+{y}")
        except Exception:
            pass

    def _poll(self):
        if self._closing:
            return
        try:
            while True:
                item = self.msgq.get_nowait()
                kind = item[0]
                if kind == "status":
                    self.status.set(item[1])
                elif kind == "step_progress":
                    _, pct, desc, overall_str, o_pct = item
                    self._update_progress_display(pct, desc, overall_str, overall_pct=o_pct)
                elif kind == "model_dl_done":
                    _, ok_flag, m_name, info_str = item
                    self._is_downloading_model = False
                    self.start_btn.set_enabled(True)
                    self._sync_mode_panels()
                    if ok_flag:
                        self.status.set(f"模型 [{m_name}] 已下载就绪")
                        messagebox.showinfo("下载完成", f"模型 [{m_name}] 已下载并准备就绪：{info_str}")
                    else:
                        self.status.set(f"模型 [{m_name}] 下载失败")
                        messagebox.showerror("下载失败", f"模型 [{m_name}] 下载出错：{info_str}")
                elif kind == "item_done":
                    _, src, state, v, total, msg, overall_str, o_pct = item
                    self._mark_image_state(src, state)
                    self._update_progress_display(100.0 if state == "ok" else 0.0, msg, overall_str, overall_pct=o_pct)
                elif kind == "single_regen_done":
                    _, ok_flag, src, msg = item
                    self._is_running = False
                    self.start_btn.set_enabled(True)
                    self._sync_mode_panels()
                    self._mark_image_state(src, "ok" if ok_flag else "err")
                    done_v = 100.0 if ok_flag else 0.0
                    self._update_progress_display(
                        done_v,
                        msg,
                        self._format_overall_progress(1 if ok_flag else 0, 1, src.name, done_v),
                        overall_pct=done_v,
                    )
                    if not ok_flag:
                        messagebox.showerror("重新生成失败", msg)
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
                    self._update_progress_display(
                        100.0,
                        "当前照片处理完成（100%）",
                        f"总体进度：100%（已完成 {ok}/{total} 张） | 当前照片：全部完成",
                        overall_pct=100.0,
                    )
                    msg = f"处理完成！成功：{ok}/{total} 张导出文件夹：{out_summary}"
                    if errors:
                        msg += "部分失败原因（最多显示 5 条）：" + "".join(errors[:5])
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
                messagebox.showerror("启动失败", f"组件加载出错：{err_msg}")
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
            messagebox.showerror("启动异常", f"程序启动出错：{e}")
        except Exception:
            pass
        os._exit(1)


if __name__ == "__main__":
    main()
