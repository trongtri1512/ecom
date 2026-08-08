"""FastAPI app: nhận mã quét, tổng hợp, xuất Excel, realtime SSE."""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import config, events, export
from .carriers import carrier_names, detect_carrier, seed_default_rules
from .db import SessionLocal, get_db, init_db
from .mailer import send_duplicate_alert
from .models import CarrierRule, Scan
from .schemas import BulkDeleteIn, CarrierRuleIn, ScanIn, ScanOut, ScanUpdate

app = FastAPI(title="Scan Ecom API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup():
    init_db()
    # Seed luật ĐVVC mặc định nếu bảng còn rỗng.
    db = SessionLocal()
    try:
        seed_default_rules(db)
    finally:
        db.close()
    events.set_loop(asyncio.get_event_loop())


def require_api_key(x_api_key: str = Header(default="")):
    """Bảo vệ endpoint ghi (agent gửi mã)."""
    if x_api_key != config.API_KEY:
        raise HTTPException(status_code=401, detail="Sai hoặc thiếu API key")


# Giờ Việt Nam (UTC+7) — để "hôm nay", "tuần này"… tính theo lịch VN.
_VN_TZ = timezone(timedelta(hours=7))


def _add_months(d: datetime, months: int) -> datetime:
    """Cộng/trừ tháng an toàn (giữ ngày 1)."""
    m = d.month - 1 + months
    year = d.year + m // 12
    month = m % 12 + 1
    return d.replace(year=year, month=month, day=1)


def period_range(period: str | None):
    """Trả (from_utc, to_utc) cho một kỳ, hoặc (None, None) nếu 'all'/không hợp lệ.

    Kỳ hỗ trợ (mốc tính theo giờ VN, trả về UTC để so với scanned_at lưu UTC):
      day, yesterday, week, last_week, month, last_month, quarter, year, all.
    """
    if not period or period == "all":
        return None, None
    now = datetime.now(_VN_TZ)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = today + timedelta(days=1)
    week_start = today - timedelta(days=today.weekday())  # thứ 2 đầu tuần này
    month_start = today.replace(day=1)
    q_first_month = 3 * ((today.month - 1) // 3) + 1
    quarter_start = today.replace(month=q_first_month, day=1)
    year_start = today.replace(month=1, day=1)

    ranges = {
        "day":        (today, now),
        "yesterday":  (today - timedelta(days=1), today),
        "week":       (week_start, now),
        "last_week":  (week_start - timedelta(days=7), week_start),
        "month":      (month_start, now),
        "last_month": (_add_months(month_start, -1), month_start),
        "quarter":    (quarter_start, now),
        "year":       (year_start, now),
    }
    if period not in ranges:
        return None, None
    start, end = ranges[period]
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def apply_period(stmt, period: str | None):
    frm, to = period_range(period)
    if frm is not None:
        stmt = stmt.where(Scan.scanned_at >= frm, Scan.scanned_at <= to)
    return stmt


# ----------------------------- Ghi mã (agent) -----------------------------
@app.post("/api/scans", status_code=201)
def create_scan(
    payload: ScanIn,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    _=Depends(require_api_key),
):
    code = payload.code.strip()
    if not code:
        raise HTTPException(status_code=400, detail="Mã rỗng")

    carrier = detect_carrier(code, db)

    # Chống trùng: kiểm tra trước cho phản hồi thân thiện.
    existing = db.scalar(select(Scan).where(Scan.code == code))
    if existing:
        return _handle_existing(existing, code, payload, background, db)

    scan = Scan(
        code=code,
        carrier=carrier,
        scanned_at=payload.scanned_at or datetime.now(timezone.utc),
        source_agent=payload.source_agent,
    )
    db.add(scan)
    try:
        db.commit()
    except Exception:
        # Race condition: 2 agent quét cùng mã gần như đồng thời.
        db.rollback()
        dup = db.scalar(select(Scan).where(Scan.code == code))
        if dup:
            return _handle_existing(dup, code, payload, background, db)
        raise HTTPException(status_code=409, detail=f"Mã trùng: {code}")
    db.refresh(scan)

    events.publish("scan", scan.as_dict())
    return ScanOut.model_validate(scan)


def _seconds_since(scanned_at: datetime) -> float:
    """Số giây từ scanned_at đến giờ (an toàn với datetime naive từ SQLite)."""
    now = datetime.now(timezone.utc)
    if scanned_at.tzinfo is None:
        scanned_at = scanned_at.replace(tzinfo=timezone.utc)
    return (now - scanned_at).total_seconds()


def _handle_existing(existing: Scan, code: str, payload: ScanIn, background: BackgroundTasks, db: Session):
    """Mã đã tồn tại: trong cửa sổ grace -> bỏ qua ÊM; ngoài -> tính TRÙNG.

    - <= DUP_GRACE_SECONDS kể từ lần quét đầu: trả 200 status "ignored",
      KHÔNG email, KHÔNG cảnh báo (nhân viên lỡ quét lại ngay).
    - > DUP_GRACE_SECONDS: KHÔNG thêm dòng mới, mà ĐÁNH DẤU dòng gốc bị trùng
      (tăng dup_count + last_dup_at), trả 409, gửi email Admin + phát sự kiện
      duplicate (agent kêu cảnh báo tại máy local).
    """
    if _seconds_since(existing.scanned_at) <= config.DUP_GRACE_SECONDS:
        # Trong 1 phút: im lặng, coi như quét lại vô hại.
        return {"status": "ignored", "code": code, "carrier": existing.carrier}
    # Quá 1 phút: đánh dấu trùng trên dòng gốc.
    existing.dup_count = (existing.dup_count or 0) + 1
    existing.last_dup_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(existing)
    background.add_task(send_duplicate_alert, code, existing.carrier, payload.source_agent)
    events.publish("duplicate", {"code": code, "carrier": existing.carrier, "dup_count": existing.dup_count})
    # Cập nhật dòng gốc trên web (để badge số lần trùng nhảy realtime).
    events.publish("update", existing.as_dict())
    raise HTTPException(status_code=409, detail=f"Mã trùng: {code} (lần {existing.dup_count})")


# ----------------------------- Đọc / quản lý -----------------------------
@app.get("/api/scans")
def list_scans(
    db: Session = Depends(get_db),
    carrier: str | None = Query(default=None),
    q: str | None = Query(default=None, description="tìm theo mã"),
    period: str | None = Query(default=None, description="day|week|month|quarter|year|all"),
    limit: int = Query(default=100, le=1000),
    offset: int = Query(default=0, ge=0),
):
    stmt = select(Scan).order_by(Scan.scanned_at.desc())
    if carrier:
        stmt = stmt.where(Scan.carrier == carrier)
    if q:
        stmt = stmt.where(Scan.code.ilike(f"%{q.strip()}%"))
    stmt = apply_period(stmt, period)
    total = db.scalar(
        select(func.count()).select_from(stmt.subquery())
    )
    rows = db.scalars(stmt.limit(limit).offset(offset)).all()
    return {
        "total": total,
        "items": [r.as_dict() for r in rows],
    }


@app.get("/api/summary")
def summary(
    db: Session = Depends(get_db),
    period: str | None = Query(default=None, description="day|week|month|quarter|year|all"),
):
    base = apply_period(select(Scan), period)
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = db.execute(
        apply_period(select(Scan.carrier, func.count()).group_by(Scan.carrier), period)
    ).all()
    names = carrier_names(db)
    counts = {name: 0 for name in names}
    for name, cnt in rows:
        counts[name] = counts.get(name, 0) + cnt  # gồm cả tên lạ nếu có (do sửa tay)
    return {"total": total, "by_carrier": counts, "carrier_order": names}


@app.patch("/api/scans/{scan_id}")
def update_scan(scan_id: int, payload: ScanUpdate, db: Session = Depends(get_db)):
    scan = db.get(Scan, scan_id)
    if not scan:
        raise HTTPException(status_code=404, detail="Không tìm thấy")
    if payload.supplier is not None:
        scan.supplier = payload.supplier
    if payload.note is not None:
        scan.note = payload.note
    if payload.carrier is not None:
        scan.carrier = payload.carrier
    if payload.pickup_status is not None:
        scan.pickup_status = payload.pickup_status
        scan.pickup_checked_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(scan)
    events.publish("update", scan.as_dict())
    return ScanOut.model_validate(scan)


@app.delete("/api/scans/{scan_id}", status_code=204)
def delete_scan(
    scan_id: int,
    db: Session = Depends(get_db),
    x_delete_password: str = Header(default=""),
):
    # Kiểm mật khẩu xoá Ở SERVER (an toàn thật, không thể bỏ qua bằng Dev Tools).
    if x_delete_password != config.DELETE_PASSWORD:
        raise HTTPException(status_code=403, detail="Sai mật khẩu xoá")
    scan = db.get(Scan, scan_id)
    if not scan:
        raise HTTPException(status_code=404, detail="Không tìm thấy")
    db.delete(scan)
    db.commit()
    events.publish("delete", {"id": scan_id})
    return Response(status_code=204)


@app.post("/api/scans/bulk-delete")
def bulk_delete_scans(
    payload: BulkDeleteIn,
    db: Session = Depends(get_db),
    x_delete_password: str = Header(default=""),
):
    """Xoá hàng loạt theo danh sách id (Admin tự chọn). Cần mật khẩu xoá."""
    if x_delete_password != config.DELETE_PASSWORD:
        raise HTTPException(status_code=403, detail="Sai mật khẩu xoá")
    ids = list({int(i) for i in payload.ids})
    if not ids:
        return {"deleted": 0}
    from sqlalchemy import delete as sa_delete
    result = db.execute(sa_delete(Scan).where(Scan.id.in_(ids)))
    db.commit()
    deleted = result.rowcount or 0
    events.publish("delete", {"ids": ids})
    return {"deleted": deleted}


@app.post("/api/reclassify")
def reclassify(db: Session = Depends(get_db)):
    """Phân loại lại ĐVVC cho toàn bộ mã (sau khi sửa luật ĐVVC)."""
    changed = 0
    for scan in db.scalars(select(Scan)).all():
        new_carrier = detect_carrier(scan.code, db)
        if new_carrier != scan.carrier:
            scan.carrier = new_carrier
            changed += 1
    db.commit()
    events.publish("reclassify", {"changed": changed})
    return {"changed": changed}


# ----------------------------- Quản lý luật ĐVVC -----------------------------
@app.get("/api/carriers")
def list_carrier_rules(db: Session = Depends(get_db)):
    """Danh sách luật nhận diện ĐVVC (theo thứ tự ưu tiên)."""
    rules = db.scalars(
        select(CarrierRule).order_by(CarrierRule.priority, CarrierRule.id)
    ).all()
    return {"items": [r.as_dict() for r in rules]}


@app.post("/api/carriers", status_code=201)
def create_carrier_rule(payload: CarrierRuleIn, db: Session = Depends(get_db)):
    name = payload.name.strip()
    prefix = payload.prefix.strip()
    if not name or not prefix:
        raise HTTPException(status_code=400, detail="Tên ĐVVC và prefix không được để trống")
    rule = CarrierRule(name=name, prefix=prefix, priority=payload.priority)
    db.add(rule)
    db.commit()
    db.refresh(rule)
    events.publish("carriers", {"action": "create"})
    return rule.as_dict()


@app.patch("/api/carriers/{rule_id}")
def update_carrier_rule(rule_id: int, payload: CarrierRuleIn, db: Session = Depends(get_db)):
    rule = db.get(CarrierRule, rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail="Không tìm thấy luật")
    name = payload.name.strip()
    prefix = payload.prefix.strip()
    if not name or not prefix:
        raise HTTPException(status_code=400, detail="Tên ĐVVC và prefix không được để trống")
    rule.name = name
    rule.prefix = prefix
    rule.priority = payload.priority
    db.commit()
    db.refresh(rule)
    events.publish("carriers", {"action": "update"})
    return rule.as_dict()


@app.delete("/api/carriers/{rule_id}", status_code=204)
def delete_carrier_rule(rule_id: int, db: Session = Depends(get_db)):
    rule = db.get(CarrierRule, rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail="Không tìm thấy luật")
    db.delete(rule)
    db.commit()
    events.publish("carriers", {"action": "delete"})
    return Response(status_code=204)


# ----------------------------- Tracking (Playwright) -----------------------------
_track_running = {"on": False}


def _do_tracking(carriers_supported: list[str]):
    """Chạy trong background: tra các mã chưa xác định trạng thái, cập nhật DB."""
    from . import tracker
    db = SessionLocal()
    try:
        # Lấy các mã hôm nay chưa tra (pickup_status rỗng), nhóm theo hãng hỗ trợ.
        frm, to = period_range("day")
        stmt = select(Scan).where(Scan.pickup_status == "")
        if frm is not None:
            stmt = stmt.where(Scan.scanned_at >= frm, Scan.scanned_at <= to)
        rows = db.scalars(stmt).all()
        by_carrier: dict = {}
        for r in rows:
            if r.carrier in carriers_supported:
                by_carrier.setdefault(r.carrier, []).append(r.code)
        if not by_carrier:
            return

        code_to_id = {r.code: r.id for r in rows}

        def on_result(carrier, code, picked):
            sid = code_to_id.get(code)
            if sid is None:
                return
            scan = db.get(Scan, sid)
            if scan:
                scan.pickup_status = "picked" if picked else "pending"
                scan.pickup_checked_at = datetime.now(timezone.utc)
                db.commit()
                events.publish("update", scan.as_dict())

        tracker.track_codes(by_carrier, headless=True, on_result=on_result)
    finally:
        db.close()
        _track_running["on"] = False
    # Sau khi tra xong, thử tự động import các lô đủ 100.
    if config.AUTO_IMPORT_ENABLED:
        _try_auto_import()


def _try_auto_import():
    """Với mỗi hãng có trong OPS_CARRIER_MAP: nếu có >= AUTO_IMPORT_BATCH đơn
    'picked' hôm nay chưa được import (session_id=""), xuất Excel + upload lên ops
    + gán session_id cho các mã trong batch để không import lại.
    """
    from . import ops_uploader
    if not (config.OPS_USER and config.OPS_PASS and config.OPS_CARRIER_MAP):
        return
    frm, to = period_range("day")
    db = SessionLocal()
    try:
        for carrier, cfg in config.OPS_CARRIER_MAP.items():
            template_id = int(cfg.get("template_id", 2))
            partner = cfg.get("partner", carrier)
            stmt = (select(Scan).where(Scan.carrier == carrier,
                                       Scan.pickup_status == "picked",
                                       Scan.session_id == "")
                    .order_by(Scan.scanned_at.asc()))
            if frm is not None:
                stmt = stmt.where(Scan.scanned_at >= frm, Scan.scanned_at <= to)
            rows = db.scalars(stmt.limit(config.AUTO_IMPORT_BATCH)).all()
            if len(rows) < config.AUTO_IMPORT_BATCH:
                continue  # chưa đủ batch
            codes = [r.code for r in rows]
            session_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{carrier}"
            # Xuất file tạm
            content = export.to_import_xlsx(codes)
            tmp_path = f"/tmp/{session_id}.xlsx"
            with open(tmp_path, "wb") as f:
                f.write(content)
            print(f"[auto-import] {carrier} bắt đầu upload {len(codes)} đơn -> {tmp_path}")
            result = ops_uploader.upload_import(carrier, tmp_path, template_id, partner)
            if result.get("ok"):
                for r in rows:
                    r.session_id = session_id
                db.commit()
                print(f"[auto-import] {carrier} OK session={session_id}")
                events.publish("auto_import", {"carrier": carrier, "count": len(codes),
                                                "session_id": session_id})
            else:
                print(f"[auto-import] {carrier} LỖI: {result.get('error')}")
                events.publish("auto_import_error", {"carrier": carrier,
                                                      "error": result.get("error")})
    finally:
        db.close()


@app.post("/api/ops/import-now")
def ops_import_now(carrier: str = Query(...)):
    """Kích hoạt import ngay 1 hãng (không đợi đủ batch). Dùng để test cấu hình."""
    from . import ops_uploader
    cfg = (config.OPS_CARRIER_MAP or {}).get(carrier)
    if not cfg:
        raise HTTPException(status_code=400, detail=f"OPS_CARRIER_MAP chưa có '{carrier}'")
    if not (config.OPS_USER and config.OPS_PASS):
        raise HTTPException(status_code=400, detail="Thiếu OPS_USER/OPS_PASS trong env")
    frm, to = period_range("day")
    db = SessionLocal()
    try:
        stmt = (select(Scan).where(Scan.carrier == carrier,
                                   Scan.pickup_status == "picked",
                                   Scan.session_id == "")
                .order_by(Scan.scanned_at.asc()))
        if frm is not None:
            stmt = stmt.where(Scan.scanned_at >= frm, Scan.scanned_at <= to)
        rows = db.scalars(stmt.limit(config.AUTO_IMPORT_BATCH)).all()
        if not rows:
            return {"status": "empty", "message": "Không có đơn 'picked' chưa import"}
        codes = [r.code for r in rows]
        session_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{carrier}"
        tmp_path = f"/tmp/{session_id}.xlsx"
        with open(tmp_path, "wb") as f:
            f.write(export.to_import_xlsx(codes))
        result = ops_uploader.upload_import(carrier, tmp_path, int(cfg["template_id"]),
                                            cfg.get("partner", carrier))
        if result.get("ok"):
            for r in rows:
                r.session_id = session_id
            db.commit()
        return {"status": "ok" if result.get("ok") else "error",
                "count": len(codes), "session_id": session_id,
                "error": result.get("error", "")}
    finally:
        db.close()


@app.post("/api/track/run")
def track_run(background: BackgroundTasks):
    """Tra trạng thái lấy hàng cho các mã hôm nay chưa xác định (chạy nền)."""
    from .tracker import CHECKERS
    if _track_running["on"]:
        return {"status": "already_running"}
    _track_running["on"] = True
    background.add_task(_do_tracking, list(CHECKERS.keys()))
    return {"status": "started", "carriers": list(CHECKERS.keys())}


# ----------------------------- Xuất file -----------------------------
@app.get("/api/export/import-file")
def export_import_file(
    carrier: str = Query(..., description="ĐVVC cần xuất (vd SPX)"),
    limit: int = Query(default=100, ge=1, le=5000),
    only_picked: bool = Query(default=True, description="chỉ xuất đơn đã lấy hàng"),
    db: Session = Depends(get_db),
):
    """Xuất Excel ĐÚNG format import lên imv.ops: 1 cột 'Mã'.

    Lấy tối đa `limit` đơn của `carrier` trong NGÀY, theo thứ tự quét (từ đơn đầu
    tiên trong ngày). Mặc định chỉ lấy đơn ĐÃ lấy hàng (only_picked=True).
    """
    frm, to = period_range("day")
    stmt = select(Scan).where(Scan.carrier == carrier).order_by(Scan.scanned_at.asc())
    if frm is not None:
        stmt = stmt.where(Scan.scanned_at >= frm, Scan.scanned_at <= to)
    if only_picked:
        stmt = stmt.where(Scan.pickup_status == "picked")
    rows = db.scalars(stmt.limit(limit)).all()
    codes = [r.code for r in rows]
    content = export.to_import_xlsx(codes)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    fname = f"{carrier}_{stamp}.xlsx"
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )
@app.get("/api/export")
def export_scans(
    format: str = Query(default="xlsx", pattern="^(xlsx|csv)$"),
    carrier: str | None = Query(default=None),
    period: str | None = Query(default=None, description="day|week|month|quarter|year|all"),
    db: Session = Depends(get_db),
):
    stmt = select(Scan).order_by(Scan.scanned_at.desc())
    if carrier:
        stmt = stmt.where(Scan.carrier == carrier)
    stmt = apply_period(stmt, period)
    scans = db.scalars(stmt).all()
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    if format == "csv":
        content = export.to_csv(scans)
        return Response(
            content=content,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="scans_{stamp}.csv"'},
        )
    content = export.to_xlsx(scans)
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="scans_{stamp}.xlsx"'},
    )


# ----------------------------- Realtime SSE -----------------------------
@app.get("/api/stream")
async def stream(request: Request):
    queue = events.subscribe()

    async def gen():
        try:
            # ping mở màn để client biết đã kết nối
            yield "event: ping\ndata: {}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=20)
                    yield events.format_sse(payload)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"  # comment giữ kết nối
        finally:
            events.unsubscribe(queue)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/health")
def health():
    return {"status": "ok"}


# ----------------------------- Phục vụ web app tĩnh -----------------------------
# Nếu thư mục web được mount vào (Docker), backend phục vụ luôn UI ở "/".
_WEB_DIR = os.getenv("WEB_DIR", "/app/web")
if os.path.isdir(_WEB_DIR):
    app.mount("/", StaticFiles(directory=_WEB_DIR, html=True), name="web")
