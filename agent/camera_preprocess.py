"""Tiền xử lý ảnh trước khi decode barcode.

Tăng detect rate với ảnh camera thực tế (mờ, nghiêng, thiếu sáng, phản
sáng). Full pipeline: grayscale → CLAHE (contrast) → blur → sharpen →
adaptive threshold → morphology close.

Trả về LIST các variant (frame gốc + preprocessed) để CameraScanner thử
decode trên từng variant, merge kết quả -> maximize recall.
"""
from __future__ import annotations

try:
    import cv2
    import numpy as np
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False


def preprocess_pipeline(bgr_frame) -> list:
    """Trả list các frame variants đã preprocess để decode barcode.

    Order từ nhanh nhất → chậm nhất. CameraScanner sẽ thử theo thứ tự và
    dừng ngay khi decode ra mã (early exit).

    Variants:
      1. gray_raw            — grayscale thuần (fast, dùng cho ảnh đẹp).
      2. gray_clahe          — CLAHE contrast (ảnh thiếu sáng / phản sáng).
      3. gray_sharpen        — unsharp mask (ảnh mờ nhẹ).
      4. gray_adaptive_thresh— adaptive threshold (barcode in mờ, giấy nhăn).
      5. gray_morph_close    — morph close (barcode có noise / vạch bị đứt).

    Với ảnh LỚN (Hikvision main stream 2560×1440), tự resize xuống 1280
    trước khi preprocess để giảm 4x CPU cost.
    """
    if not HAS_CV2 or bgr_frame is None:
        return [bgr_frame] if bgr_frame is not None else []
    # Auto resize nếu ảnh quá to (Hikvision 4MP → resize xuống 1280).
    bgr_frame = resize_if_large(bgr_frame, max_width=1280)
    gray = cv2.cvtColor(bgr_frame, cv2.COLOR_BGR2GRAY)
    variants = [("gray_raw", gray)]

    # 2. CLAHE - Contrast Limited Adaptive Histogram Equalization
    try:
        clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
        gray_clahe = clahe.apply(gray)
        variants.append(("gray_clahe", gray_clahe))
    except Exception:
        gray_clahe = gray

    # 3. Unsharp mask - làm nét vạch barcode mờ
    try:
        blurred = cv2.GaussianBlur(gray_clahe, (0, 0), sigmaX=3)
        sharpen = cv2.addWeighted(gray_clahe, 1.5, blurred, -0.5, 0)
        variants.append(("gray_sharpen", sharpen))
    except Exception:
        pass

    # 4. Adaptive threshold - binarize để pyzbar/OpenCV dễ đọc
    try:
        thresh = cv2.adaptiveThreshold(
            gray_clahe, 255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY, 21, 5
        )
        variants.append(("gray_adaptive", thresh))
    except Exception:
        pass

    # 5. Morphology close - nối vạch barcode bị đứt
    try:
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        morph = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel, iterations=1)
        variants.append(("gray_morph", morph))
    except Exception:
        pass

    return variants


def resize_if_large(frame, max_width: int = 1280):
    """Resize xuống max_width nếu ảnh quá to (tăng tốc decode)."""
    if frame is None or not HAS_CV2:
        return frame
    h, w = frame.shape[:2]
    if w <= max_width:
        return frame
    scale = max_width / w
    return cv2.resize(frame, (max_width, int(h * scale)), interpolation=cv2.INTER_AREA)
