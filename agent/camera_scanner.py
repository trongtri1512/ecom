"""Camera barcode scanner cho Scan Ecom Agent.

Đọc video từ webcam/USB camera, detect + decode barcode trong 'reading zone',
gửi mã lên server qua sender.send() giống như máy quét bàn phím.

Ưu tiên:
1. OpenCV BarcodeDetector (nhanh, C++ backend). Cần opencv-python 4.7+.
2. Fallback pyzbar (chậm hơn nhưng detect ổn định với ảnh nhiễu).

Thread model:
- CaptureLoop: đọc frame mới nhất từ cv2.VideoCapture (thread riêng, drop frame
  cũ để không lag).
- DecodeLoop: lấy frame gần nhất, decode barcode, gọi callback.
- UI thread (tkinter): render frame + overlay (không block).
"""
from __future__ import annotations

import time
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

try:
    import cv2
    import numpy as np
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False

try:
    from pyzbar.pyzbar import decode as _pyzbar_decode, ZBarSymbol  # noqa
    HAS_PYZBAR = True
except ImportError:
    HAS_PYZBAR = False


@dataclass
class BarcodeResult:
    """1 barcode phát hiện được trong 1 frame."""
    data: str                         # nội dung barcode
    barcode_type: str = ""            # EAN13 / CODE128 / QRCODE / ...
    polygon: list = field(default_factory=list)  # [(x,y), (x,y), ...] 4 điểm
    rect: tuple = (0, 0, 0, 0)        # (x, y, w, h) bounding box
    in_zone: bool = True              # có nằm trong reading zone không

    @property
    def center(self) -> tuple:
        x, y, w, h = self.rect
        return (x + w // 2, y + h // 2)


class CameraScanner:
    """Quản lý webcam + decode barcode + dedup + trigger callback."""

    def __init__(
        self,
        source: str = "0",
        resolution: tuple = (640, 480),
        fps_target: int = 15,
        zone_ratio: float = 0.6,
        dedup_seconds: float = 3.0,
        on_scan: Optional[Callable[[str], None]] = None,
    ):
        """
        source:
          - "0" / "1" / ... (số) -> webcam local index đó
          - "rtsp://..."         -> IP camera RTSP
          - "http://..."         -> HTTP MJPEG stream
        on_scan(code: str): callback khi detect mã hợp lệ (đã dedup + in zone).
        """
        if not HAS_CV2:
            raise RuntimeError("Chưa cài opencv-python. pip install opencv-python")
        self.source = source
        self.resolution = resolution
        self.fps_target = fps_target
        self.zone_ratio = zone_ratio
        self.dedup_seconds = dedup_seconds
        self.on_scan = on_scan

        self._cap: Optional[cv2.VideoCapture] = None
        self._running = False
        self._latest_frame: Optional[np.ndarray] = None
        self._latest_results: list[BarcodeResult] = []
        self._lock = threading.Lock()
        self._recent = {}  # code -> last_sent_ts

        # OpenCV BarcodeDetector (nhanh)
        try:
            self._cv_detector = cv2.barcode.BarcodeDetector()
            self._has_cv_detector = True
        except Exception:
            self._has_cv_detector = False

    # -------------- Lifecycle --------------
    def start(self) -> bool:
        """Mở camera, khởi động 2 thread capture + decode. Trả False nếu fail."""
        # Nếu source là số -> local webcam (dùng CAP_DSHOW trên Windows cho stable).
        # Nếu URL rtsp/http -> để OpenCV auto pick backend (FFmpeg).
        src = self.source
        try:
            src_int = int(src)
            backend = cv2.CAP_DSHOW if hasattr(cv2, "CAP_DSHOW") else 0
            self._cap = cv2.VideoCapture(src_int, backend)
        except ValueError:
            # RTSP/HTTP URL
            self._cap = cv2.VideoCapture(src)
        if not self._cap.isOpened():
            return False
        self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.resolution[0])
        self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.resolution[1])
        self._cap.set(cv2.CAP_PROP_FPS, self.fps_target)
        self._cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # drop frame cũ
        self._running = True
        threading.Thread(target=self._capture_loop, daemon=True).start()
        threading.Thread(target=self._decode_loop, daemon=True).start()
        return True

    def stop(self):
        self._running = False
        time.sleep(0.15)
        if self._cap:
            self._cap.release()
            self._cap = None

    # -------------- Loops (threads) --------------
    def _capture_loop(self):
        """Đọc frame liên tục, giữ frame mới nhất trong self._latest_frame."""
        while self._running and self._cap:
            ok, frame = self._cap.read()
            if not ok:
                time.sleep(0.05)
                continue
            with self._lock:
                self._latest_frame = frame

    def _decode_loop(self):
        """Loop decode: lấy frame gần nhất → decode barcode → callback."""
        interval = 1.0 / max(1, self.fps_target)
        while self._running:
            t0 = time.time()
            with self._lock:
                frame = None if self._latest_frame is None else self._latest_frame.copy()
            if frame is not None:
                results = self._decode_frame(frame)
                # Mark in_zone
                zone = self._compute_zone(frame.shape[1], frame.shape[0])
                for r in results:
                    r.in_zone = self._rect_intersects_zone(r.rect, zone)
                with self._lock:
                    self._latest_results = results
                # Trigger callback cho mã in_zone + không dedup
                now = time.time()
                for r in results:
                    if not r.in_zone or not r.data:
                        continue
                    last = self._recent.get(r.data, 0)
                    if now - last < self.dedup_seconds:
                        continue
                    self._recent[r.data] = now
                    # Prune bảng dedup
                    if len(self._recent) > 500:
                        cutoff = now - self.dedup_seconds * 5
                        self._recent = {k: v for k, v in self._recent.items() if v > cutoff}
                    if self.on_scan:
                        try:
                            self.on_scan(r.data)
                        except Exception as e:
                            print(f"[camera] on_scan callback error: {e}")
            # Sleep để không burn CPU
            elapsed = time.time() - t0
            if elapsed < interval:
                time.sleep(interval - elapsed)

    # -------------- Decode logic --------------
    def _decode_frame(self, frame: "np.ndarray") -> list[BarcodeResult]:
        results: list[BarcodeResult] = []
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        # 1. Thử OpenCV BarcodeDetector (nhanh)
        if self._has_cv_detector:
            try:
                # detectAndDecode trả (data, decoded_type, points)
                ret, decoded, types, points = self._cv_detector.detectAndDecodeWithType(gray)
                if ret and decoded is not None:
                    for i, code in enumerate(decoded):
                        if not code:
                            continue
                        pts = points[i] if points is not None and i < len(points) else []
                        polygon = [(int(x), int(y)) for x, y in pts] if len(pts) > 0 else []
                        rect = self._polygon_to_rect(polygon)
                        btype = types[i] if types is not None and i < len(types) else ""
                        results.append(BarcodeResult(
                            data=code, barcode_type=btype,
                            polygon=polygon, rect=rect,
                        ))
            except Exception:
                pass

        # 2. Fallback pyzbar nếu OpenCV không thấy gì
        if not results and HAS_PYZBAR:
            try:
                decoded = _pyzbar_decode(gray)
                for obj in decoded:
                    code = obj.data.decode("utf-8", errors="ignore")
                    if not code:
                        continue
                    polygon = [(p.x, p.y) for p in obj.polygon]
                    rect = (obj.rect.left, obj.rect.top, obj.rect.width, obj.rect.height)
                    results.append(BarcodeResult(
                        data=code, barcode_type=obj.type,
                        polygon=polygon, rect=rect,
                    ))
            except Exception as e:
                print(f"[camera] pyzbar error: {e}")

        return results

    @staticmethod
    def _polygon_to_rect(polygon: list) -> tuple:
        if not polygon:
            return (0, 0, 0, 0)
        xs = [p[0] for p in polygon]
        ys = [p[1] for p in polygon]
        x, y = min(xs), min(ys)
        return (x, y, max(xs) - x, max(ys) - y)

    def _compute_zone(self, frame_w: int, frame_h: int) -> tuple:
        """Reading zone = hình chữ nhật giữa frame, chiếm zone_ratio."""
        r = self.zone_ratio
        w = int(frame_w * r)
        h = int(frame_h * r)
        x = (frame_w - w) // 2
        y = (frame_h - h) // 2
        return (x, y, w, h)

    @staticmethod
    def _rect_intersects_zone(rect: tuple, zone: tuple) -> bool:
        """True nếu bounding box của barcode GIAO với reading zone."""
        rx, ry, rw, rh = rect
        zx, zy, zw, zh = zone
        return not (rx + rw < zx or rx > zx + zw or ry + rh < zy or ry > zy + zh)

    # -------------- Public API cho UI --------------
    def get_annotated_frame(self):
        """Trả frame hiện tại + overlay (zone, polygon, text). None nếu chưa có."""
        with self._lock:
            frame = None if self._latest_frame is None else self._latest_frame.copy()
            results = list(self._latest_results)
        if frame is None:
            return None
        h, w = frame.shape[:2]
        zone = self._compute_zone(w, h)
        # Draw reading zone (dashed rectangle)
        self._draw_dashed_rect(frame, zone, color=(255, 200, 0), thickness=2)
        cv2.putText(frame, "READING ZONE", (zone[0], zone[1] - 8),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 200, 0), 1, cv2.LINE_AA)
        # Draw each barcode
        for r in results:
            color = (0, 220, 0) if r.in_zone else (120, 120, 120)
            if r.polygon and len(r.polygon) >= 3:
                pts = np.array(r.polygon, dtype=np.int32)
                cv2.polylines(frame, [pts], True, color, 2)
            else:
                x, y, rw, rh = r.rect
                cv2.rectangle(frame, (x, y), (x + rw, y + rh), color, 2)
            # Label
            x, y = r.rect[0], r.rect[1]
            label = f"{r.data} [{r.barcode_type}]" if r.barcode_type else r.data
            cv2.putText(frame, label, (x, max(15, y - 6)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)
        return frame

    @staticmethod
    def _draw_dashed_rect(img, rect, color, thickness=2, dash=10):
        x, y, w, h = rect
        # Top + bottom
        for i in range(x, x + w, dash * 2):
            cv2.line(img, (i, y), (min(i + dash, x + w), y), color, thickness)
            cv2.line(img, (i, y + h), (min(i + dash, x + w), y + h), color, thickness)
        # Left + right
        for i in range(y, y + h, dash * 2):
            cv2.line(img, (x, i), (x, min(i + dash, y + h)), color, thickness)
            cv2.line(img, (x + w, i), (x + w, min(i + dash, y + h)), color, thickness)
