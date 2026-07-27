"""Xuất danh sách quét ra Excel (xlsx) hoặc CSV."""
import csv
import io
from typing import List

from openpyxl import Workbook

from .models import Scan

_HEADERS = ["Mã vận đơn", "ĐVVC", "NCC", "Ghi chú", "Thời gian quét", "Nguồn"]


def _row(s: Scan) -> list:
    return [
        s.code,
        s.carrier,
        s.supplier or "",
        s.note or "",
        s.scanned_at.strftime("%Y-%m-%d %H:%M:%S") if s.scanned_at else "",
        s.source_agent or "",
    ]


def to_xlsx(scans: List[Scan]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Scans"
    ws.append(_HEADERS)
    for s in scans:
        ws.append(_row(s))
    # Cột mã rộng hơn cho dễ đọc.
    ws.column_dimensions["A"].width = 26
    ws.column_dimensions["E"].width = 20
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def to_csv(scans: List[Scan]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(_HEADERS)
    for s in scans:
        writer.writerow(_row(s))
    # BOM để Excel mở tiếng Việt không lỗi font.
    return ("﻿" + buf.getvalue()).encode("utf-8")
