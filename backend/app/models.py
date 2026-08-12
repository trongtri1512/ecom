"""Model DB cho bản ghi quét mã vận đơn."""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Scan(Base):
    __tablename__ = "scans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Mã vận đơn — unique để chống trùng ngay ở tầng DB.
    code: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    # Đơn vị vận chuyển tự nhận diện; "Other" nếu không khớp luật nào.
    carrier: Mapped[str] = mapped_column(String(64), index=True, nullable=False, default="Other")
    # NCC để trống, điền sau (chừa sẵn cột).
    supplier: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    note: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    # Thời điểm quét (do agent gửi lên) và thời điểm lưu vào server.
    scanned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    source_agent: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    # Đánh dấu bị quét trùng: số lần quét lại (sau cửa sổ ân hạn) + thời điểm gần nhất.
    dup_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_dup_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Trạng thái lấy hàng của ĐVVC: "picked" (đã lấy) | "pending" (chưa lấy) | "" (chưa tra).
    pickup_status: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    pickup_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Batch đã import lên imv.ops (rỗng = chưa import). Format: "YYYYMMDD-HHMMSS-CARRIER".
    session_id: Mapped[str] = mapped_column(String(64), nullable=False, default="", index=True)
    # ID sọt (Basket) chứa mã này. NULL = chưa vào sọt. Gán khi user bấm "Hoàn thành sọt".
    basket_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "code": self.code,
            "carrier": self.carrier,
            "supplier": self.supplier,
            "note": self.note,
            "scanned_at": self.scanned_at.isoformat() if self.scanned_at else None,
            "source_agent": self.source_agent,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "dup_count": self.dup_count or 0,
            "last_dup_at": self.last_dup_at.isoformat() if self.last_dup_at else None,
            "pickup_status": self.pickup_status or "",
            "pickup_checked_at": self.pickup_checked_at.isoformat() if self.pickup_checked_at else None,
            "session_id": self.session_id or "",
        }


class Basket(Base):
    """Sọt/lô hàng — chốt bằng nút 'Hoàn thành sọt' trên agent Windows.

    Mỗi sọt gom các mã quét giữa 2 lần bấm nút (theo TỪNG máy). Lưu Total + phân
    bố theo ĐVVC (JSON) để hiển thị nhanh, không phải query lại.
    """
    __tablename__ = "baskets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Số thứ tự sọt trong ngày, theo TỪNG máy: 1, 2, 3...
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    source_agent: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    # Khoảng thời gian sọt: từ sau lần chốt trước tới now.
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # JSON string: {"SPX": 20, "J&T": 15, ...}. Đơn giản là text để hỗ trợ cả SQLite/Postgres.
    by_carrier_json: Mapped[str] = mapped_column(String(2000), nullable=False, default="{}")
    # Trạng thái bàn giao lên OPS: "" = chưa bàn giao | "done" = đã bàn giao thành
    # công tất cả | "partial" = có mã lỗi bị OPS loại | "failed" = fail hoàn toàn.
    ops_status: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    # Thời điểm bàn giao gần nhất (UTC).
    ops_handed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Mã phiên OPS đã cấp (nhiều carrier có thể tạo nhiều phiên -> lưu JSON
    # {"SPX": "MVECCE...", "J&T": "JTECCE..."}).
    ops_sessions_json: Mapped[str] = mapped_column(String(2000), nullable=False, default="{}")
    # Nhật ký chi tiết mã lỗi: [{"code": "...", "reason": "đã tồn tại"}, ...].
    ops_errors_json: Mapped[str] = mapped_column(String(8000), nullable=False, default="[]")

    def as_dict(self) -> dict:
        import json
        try:
            by_carrier = json.loads(self.by_carrier_json or "{}")
        except Exception:
            by_carrier = {}
        try:
            ops_sessions = json.loads(self.ops_sessions_json or "{}")
        except Exception:
            ops_sessions = {}
        try:
            ops_errors = json.loads(self.ops_errors_json or "[]")
        except Exception:
            ops_errors = []
        return {
            "id": self.id,
            "seq": self.seq,
            "name": f"Sọt {self.seq}",
            "source_agent": self.source_agent,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
            "total": self.total,
            "by_carrier": by_carrier,
            "ops_status": self.ops_status or "",
            "ops_handed_at": self.ops_handed_at.isoformat() if self.ops_handed_at else None,
            "ops_sessions": ops_sessions,
            "ops_errors": ops_errors,
        }


class OpsLog(Base):
    """Log các lần import lên imv.ops (thành công/lỗi), có thể kèm screenshot.

    Xem/xoá qua trang Admin. `screenshot_file` là tên file trong OPS_LOGS_DIR
    (không lưu đường dẫn tuyệt đối để dễ chuyển máy).
    """
    __tablename__ = "ops_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    # "info" | "success" | "error"
    level: Mapped[str] = mapped_column(String(16), nullable=False, default="info")
    # "auto_import" | "manual_import" | "login" | ...
    action: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    carrier: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    message: Mapped[str] = mapped_column(String(2000), nullable=False, default="")
    screenshot_file: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "level": self.level,
            "action": self.action,
            "carrier": self.carrier,
            "count": self.count,
            "session_id": self.session_id,
            "message": self.message,
            "has_screenshot": bool(self.screenshot_file),
        }


class CarrierRule(Base):
    """Luật nhận diện ĐVVC theo prefix — sửa được ngay trên web, lưu trong DB.

    Mã bắt đầu bằng `prefix` (không phân biệt hoa/thường) -> thuộc ĐVVC `name`.
    Duyệt theo `priority` tăng dần; prefix nào dài hơn nên để priority nhỏ hơn
    (ưu tiên khớp trước) để tránh prefix ngắn "ăn" mất prefix dài.
    """
    __tablename__ = "carrier_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    # Prefix người dùng gõ (vd "SPXVN", "8621", "GYAB"). Lưu nguyên, so khớp không phân biệt hoa/thường.
    prefix: Mapped[str] = mapped_column(String(64), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    # ---- Cấu hình auto import lên imv.ops (áp cho ĐVVC name của luật này) ----
    # Nhiều luật cùng name -> lấy giá trị của luật có priority NHỎ NHẤT.
    auto_import_enabled: Mapped[bool] = mapped_column(default=False, nullable=False)
    auto_import_batch: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    ops_template_id: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    ops_partner: Mapped[str] = mapped_column(String(128), nullable=False, default="")

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "prefix": self.prefix,
            "priority": self.priority,
            "auto_import_enabled": bool(self.auto_import_enabled),
            "auto_import_batch": self.auto_import_batch,
            "ops_template_id": self.ops_template_id,
            "ops_partner": self.ops_partner,
        }
