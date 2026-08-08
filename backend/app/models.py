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

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "prefix": self.prefix,
            "priority": self.priority,
        }
