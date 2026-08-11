"""Pydantic schemas cho request/response."""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class ScanIn(BaseModel):
    """Payload agent gửi lên khi quét được 1 mã."""
    code: str = Field(..., min_length=1, max_length=128)
    scanned_at: Optional[datetime] = None
    source_agent: Optional[str] = None


class ScanUpdate(BaseModel):
    """Sửa tay 1 bản ghi (supplier/note/carrier/pickup_status)."""
    supplier: Optional[str] = None
    note: Optional[str] = None
    carrier: Optional[str] = None
    pickup_status: Optional[str] = None  # "picked" | "pending"


class BulkDeleteIn(BaseModel):
    """Xoá hàng loạt: danh sách id cần xoá."""
    ids: list[int]


class CarrierRuleIn(BaseModel):
    """Luật nhận diện ĐVVC: tên hãng + prefix (chuỗi đầu mã) + ưu tiên +
    cấu hình auto import OPS. 1 hãng có thể có NHIỀU prefix (mỗi prefix 1 hàng),
    khi đó tất cả hàng cùng `name` nên có cùng giá trị auto import.
    """
    name: str = Field(..., min_length=1, max_length=64)
    prefix: str = Field(..., min_length=1, max_length=64)
    priority: int = Field(default=100)
    auto_import_enabled: bool = Field(default=False)
    auto_import_batch: int = Field(default=100)
    ops_template_id: int = Field(default=2)
    ops_partner: str = Field(default="")


class ScanOut(BaseModel):
    id: int
    code: str
    carrier: str
    supplier: Optional[str] = None
    note: Optional[str] = None
    scanned_at: Optional[datetime] = None
    source_agent: Optional[str] = None
    created_at: Optional[datetime] = None
    dup_count: int = 0
    last_dup_at: Optional[datetime] = None
    pickup_status: str = ""
    pickup_checked_at: Optional[datetime] = None

    class Config:
        from_attributes = True
