"""Nhận diện Đơn vị vận chuyển (ĐVVC) từ prefix mã vận đơn.

Luật được LƯU TRONG DB (bảng carrier_rules) và sửa được ngay trên web — không
cần đụng code hay deploy lại. Mỗi luật gồm: tên ĐVVC + prefix (chuỗi đầu mã).
Mã bắt đầu bằng prefix nào (không phân biệt hoa/thường) thì thuộc ĐVVC đó;
không khớp prefix nào -> "Other".

Duyệt theo priority tăng dần. Prefix DÀI hơn nên có priority NHỎ hơn để được
so khớp trước (tránh prefix ngắn "ăn" mất prefix dài).
"""
from typing import List

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import CarrierRule

OTHER = "Other"

# Luật mặc định để seed lần đầu (khi bảng carrier_rules còn rỗng).
# Đây là 4 hãng đã xác nhận từ mã thật + 3 hãng khung. Người dùng sửa sau trên web.
DEFAULT_RULES = [
    ("SPX", "SPXVN", 10),
    ("J&T", "8621", 20),
    ("Best Express", "TTVN", 30),
    ("Best Express", "BE", 31),
    ("GHN", "GYAB", 40),
    ("GHTK", "S", 50),
    ("Viettel Post", "VTP", 60),
    ("Ninja Van", "NL", 70),
]


def seed_default_rules(db: Session) -> None:
    """Nạp luật mặc định nếu bảng đang rỗng (chạy lúc app khởi động)."""
    existing = db.scalar(select(CarrierRule).limit(1))
    if existing:
        return
    for name, prefix, priority in DEFAULT_RULES:
        db.add(CarrierRule(name=name, prefix=prefix, priority=priority))
    db.commit()


def detect_carrier(code: str, db: Session) -> str:
    """Trả về tên ĐVVC cho một mã dựa trên luật prefix trong DB, hoặc "Other"."""
    if not code:
        return OTHER
    code_up = code.strip().upper()
    rules = db.scalars(
        select(CarrierRule).order_by(CarrierRule.priority, CarrierRule.id)
    ).all()
    for rule in rules:
        prefix = (rule.prefix or "").strip().upper()
        # prefix rỗng thì bỏ qua; mã phải dài hơn prefix (có ký tự thật theo sau).
        if prefix and code_up.startswith(prefix) and len(code_up) > len(prefix):
            return rule.name
    return OTHER


def carrier_names(db: Session) -> List[str]:
    """Danh sách tên ĐVVC (theo priority, không trùng) + Other cuối cùng.

    Dùng cho KPI/summary và dropdown filter ở web app.
    """
    rules = db.scalars(
        select(CarrierRule).order_by(CarrierRule.priority, CarrierRule.id)
    ).all()
    names: List[str] = []
    for r in rules:
        if r.name not in names:
            names.append(r.name)
    names.append(OTHER)
    return names
