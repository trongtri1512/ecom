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
    """Sửa tay 1 bản ghi (supplier/note/carrier)."""
    supplier: Optional[str] = None
    note: Optional[str] = None
    carrier: Optional[str] = None


class CarrierRuleIn(BaseModel):
    """Luật nhận diện ĐVVC: tên hãng + prefix (chuỗi đầu mã) + ưu tiên."""
    name: str = Field(..., min_length=1, max_length=64)
    prefix: str = Field(..., min_length=1, max_length=64)
    priority: int = Field(default=100)


class ScanOut(BaseModel):
    id: int
    code: str
    carrier: str
    supplier: Optional[str] = None
    note: Optional[str] = None
    scanned_at: Optional[datetime] = None
    source_agent: Optional[str] = None
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True
