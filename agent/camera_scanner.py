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

# ZXing-cpp (decoder backup, mạnh hơn pyzbar trên ảnh mờ/nghiêng).
try:
    import zxingcpp as _zxingcpp  # type: ignore
    HAS_ZXING = True
except ImportError:
    HAS_ZXING = False

# Preprocess module (optional — nếu có sẽ dùng để tăng detect rate).
try:
    from camera_preprocess import preprocess_pipeline, resize_if_large
    HAS_PREPROCESS = True
except ImportError:
    try:
        from .camera_preprocess import preprocess_pipeline, resize_if_large  # type: ignore
        HAS_PREPROCESS = True
    except ImportError:
        HAS_PREPROCESS = False
        preprocess_pipeline = lambda f: [f] if f is not None else []
        resize_if_large = lambda f, **kw: f


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
        zone_rect: Optional[tuple] = None,     # (x%, y%, w%, h%) — legacy
        zone_polygon: Optional[list] = None,   # [(x%, y%), ...] polygon tự do (>= 3 điểm)
        dedup_seconds: float = 3.0,
        on_scan: Optional[Callable[[str], None]] = None,
    ):
        """
        Reading zone (thứ tự ưu tiên):
          1. zone_polygon: list điểm percent [(x1,y1), (x2,y2), ...] — polygon
             TỰ DO, hình thang / tam giác / bất kỳ hình gì user vẽ. >= 3 điểm.
          2. zone_rect: (x%, y%, w%, h%) — hình chữ nhật.
          3. zone_ratio: hình chữ nhật căn giữa, chiếm ratio.
        """
        if not HAS_CV2:
            raise RuntimeError("Chưa cài opencv-python. pip install opencv-python")
        self.source = source
        self.resolution = resolution
        self.fps_target = fps_target
        self.zone_ratio = zone_ratio
        self.zone_rect = zone_rect
        self.zone_polygon = zone_polygon if (zone_polygon and len(zone_polygon) >= 3) else None
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
                zone_poly = self._compute_zone_polygon_px(frame.shape[1], frame.shape[0])
                for r in results:
                    r.in_zone = self._barcode_in_zone(r.rect, zone_poly)
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
    def _decode_variant(self, gray_frame) -> list[BarcodeResult]:
        """Decode 1 variant (đã preprocess sang grayscale)."""
        results: list[BarcodeResult] = []
        # 1. OpenCV BarcodeDetector (nhanh, C++ backend)
        if self._has_cv_detector:
            try:
                ret, decoded, types, points = self._cv_detector.detectAndDecodeWithType(gray_frame)
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
        # 2. pyzbar
        if not results and HAS_PYZBAR:
            try:
                decoded = _pyzbar_decode(gray_frame)
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
        # 3. ZXing-cpp fallback (mạnh trên ảnh mờ/nghiêng — dùng khi pyzbar
        #    và CV BarcodeDetector đều fail)
        if not results and HAS_ZXING:
            try:
                zresults = _zxingcpp.read_barcodes(gray_frame)
                for zr in zresults:
                    code = getattr(zr, "text", "") or ""
                    if not code:
                        continue
                    btype = str(getattr(zr, "format", "")) or ""
                    polygon = []
                    rect = (0, 0, 0, 0)
                    try:
                        pos = zr.position
                        polygon = [
                            (int(pos.top_left.x), int(pos.top_left.y)),
                            (int(pos.top_right.x), int(pos.top_right.y)),
                            (int(pos.bottom_right.x), int(pos.bottom_right.y)),
                            (int(pos.bottom_left.x), int(pos.bottom_left.y)),
                        ]
                        rect = self._polygon_to_rect(polygon)
                    except Exception:
                        pass
                    results.append(BarcodeResult(
                        data=code, barcode_type=btype,
                        polygon=polygon, rect=rect,
                    ))
            except Exception as e:
                print(f"[camera] zxing error: {e}")
        return results

    def _decode_frame(self, frame: "np.ndarray") -> list[BarcodeResult]:
        """Decode frame — CROP theo bounding box của zone TRƯỚC preprocess
        + decode, sau đó offset toạ độ về hệ frame gốc.

        Vì sao crop: mã trong zone ~30% frame → crop tăng 3x kích thước
        tương đối, giúp decoder đọc dễ hơn + giảm 3-9x CPU cost.

        Thử tuần tự các variant preprocess, EARLY EXIT khi ra kết quả.
        """
        H, W = frame.shape[:2]
        # Tính bounding rect của zone polygon (pixel trong hệ frame gốc).
        zone_poly = self._compute_zone_polygon_px(W, H)
        if zone_poly:
            xs = [p[0] for p in zone_poly]
            ys = [p[1] for p in zone_poly]
            zx1 = max(0, min(xs) - 20)  # padding 20px cho barcode ở rìa
            zy1 = max(0, min(ys) - 20)
            zx2 = min(W, max(xs) + 20)
            zy2 = min(H, max(ys) + 20)
            if zx2 - zx1 < 50 or zy2 - zy1 < 50:  # zone quá nhỏ -> fallback full
                zx1, zy1, zx2, zy2 = 0, 0, W, H
        else:
            zx1, zy1, zx2, zy2 = 0, 0, W, H
        crop = frame[zy1:zy2, zx1:zx2]
        # Resize crop nếu vẫn còn lớn.
        crop = resize_if_large(crop, max_width=1280)
        scale = crop.shape[1] / max(1, zx2 - zx1)  # tỉ lệ resize crop
        variants = preprocess_pipeline(crop)
        merged: dict = {}
        for _name, gray in variants:
            found = self._decode_variant(gray)
            for r in found:
                if r.data and r.data not in merged:
                    # Offset toạ độ crop -> frame gốc.
                    rx, ry, rw, rh = r.rect
                    r.rect = (
                        int(rx / scale) + zx1,
                        int(ry / scale) + zy1,
                        int(rw / scale),
                        int(rh / scale),
                    )
                    if r.polygon:
                        r.polygon = [
                            (int(px / scale) + zx1, int(py / scale) + zy1)
                            for (px, py) in r.polygon
                        ]
                    merged[r.data] = r
            if merged:
                break
        return list(merged.values())

    @staticmethod
    def _polygon_to_rect(polygon: list) -> tuple:
        if not polygon:
            return (0, 0, 0, 0)
        xs = [p[0] for p in polygon]
        ys = [p[1] for p in polygon]
        x, y = min(xs), min(ys)
        return (x, y, max(xs) - x, max(ys) - y)

    def _compute_zone_rect(self, frame_w: int, frame_h: int) -> tuple:
        """Zone RECT dạng (x, y, w, h) pixel. Dùng cho vẽ overlay khi
        không có polygon."""
        if self.zone_rect:
            xp, yp, wp, hp = self.zone_rect
            return (int(frame_w * xp / 100), int(frame_h * yp / 100),
                    int(frame_w * wp / 100), int(frame_h * hp / 100))
        r = self.zone_ratio
        w = int(frame_w * r)
        h = int(frame_h * r)
        return ((frame_w - w) // 2, (frame_h - h) // 2, w, h)

    def _compute_zone_polygon_px(self, frame_w: int, frame_h: int) -> list:
        """Zone POLYGON dạng list điểm pixel. Dùng cho vẽ + hit-test.

        Nếu zone_polygon set (>= 3 điểm) -> convert percent → pixel.
        Nếu không -> convert rect thành 4 điểm.
        """
        if self.zone_polygon:
            return [(int(x * frame_w / 100), int(y * frame_h / 100))
                    for x, y in self.zone_polygon]
        x, y, w, h = self._compute_zone_rect(frame_w, frame_h)
        return [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]

    @staticmethod
    def _barcode_in_zone(rect: tuple, zone_polygon_px: list) -> bool:
        """True nếu CENTER của bounding box barcode nằm trong zone polygon.

        Dùng center check thay vì intersect vì polygon phức tạp — center
        stable hơn khi barcode ở rìa.
        """
        rx, ry, rw, rh = rect
        cx = rx + rw // 2
        cy = ry + rh // 2
        try:
            pts = np.array(zone_polygon_px, dtype=np.int32).reshape((-1, 1, 2))
            dist = cv2.pointPolygonTest(pts, (float(cx), float(cy)), False)
            return dist >= 0  # >= 0 = inside hoặc trên biên
        except Exception:
            return True  # Nếu lỗi thì chấp nhận (an toàn)

    # -------------- Public API cho UI --------------
    def get_annotated_frame(self):
        """Trả frame hiện tại + overlay (zone, polygon, text). None nếu chưa có."""
        with self._lock:
            frame = None if self._latest_frame is None else self._latest_frame.copy()
            results = list(self._latest_results)
        if frame is None:
            return None
        h, w = frame.shape[:2]
        zone_poly = self._compute_zone_polygon_px(w, h)
        # Draw reading zone (polygon, có thể là hình bất kỳ tự do).
        self._draw_dashed_polygon(frame, zone_poly, color=(255, 200, 0), thickness=2)
        if zone_poly:
            zx, zy = zone_poly[0]
            cv2.putText(frame, "READING ZONE", (zx, max(zy - 8, 12)),
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
    def _draw_dashed_polygon(img, polygon: list, color, thickness=2, dash=10):
        """Vẽ polygon closed với cạnh dashed. polygon = [(x,y), ...]."""
        if not polygon or len(polygon) < 2:
            return
        n = len(polygon)
        for i in range(n):
            x1, y1 = polygon[i]
            x2, y2 = polygon[(i + 1) % n]
            # Chia đoạn thành các dash nhỏ.
            import math
            dx, dy = x2 - x1, y2 - y1
            dist = math.sqrt(dx * dx + dy * dy)
            if dist < 1:
                continue
            steps = max(1, int(dist / dash))
            for s in range(0, steps, 2):  # vẽ mỗi 2 step 1 lần (dashed)
                t1 = s / steps
                t2 = min(1.0, (s + 1) / steps)
                px1 = int(x1 + dx * t1)
                py1 = int(y1 + dy * t1)
                px2 = int(x1 + dx * t2)
                py2 = int(y1 + dy * t2)
                cv2.line(img, (px1, py1), (px2, py2), color, thickness)

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
