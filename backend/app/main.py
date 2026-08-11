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
from sqlalchemy import func, select, delete, update
from sqlalchemy.orm import Session

from . import config, events, export
from .carriers import carrier_names, detect_carrier, seed_default_rules
from .db import SessionLocal, get_db, init_db
from .mailer import send_duplicate_alert
from .models import Basket, CarrierRule, OpsLog, Scan
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
def update_scan(
    scan_id: int,
    payload: ScanUpdate,
    db: Session = Depends(get_db),
    x_delete_password: str = Header(default=""),
):
    scan = db.get(Scan, scan_id)
    if not scan:
        raise HTTPException(status_code=404, detail="Không tìm thấy")
    # Sửa mã vận đơn: cần mật khẩu + kiểm tra trùng.
    if payload.code is not None:
        new_code = payload.code.strip()
        if not new_code:
            raise HTTPException(status_code=400, detail="Mã vận đơn không được để trống")
        if x_delete_password != config.DELETE_PASSWORD:
            raise HTTPException(status_code=403, detail="Sai mật khẩu")
        if new_code != scan.code:
            existing = db.scalar(select(Scan).where(Scan.code == new_code))
            if existing:
                raise HTTPException(status_code=409, detail=f"Mã đã tồn tại: {new_code}")
            scan.code = new_code
            # Phân loại lại ĐVVC theo mã mới.
            scan.carrier = detect_carrier(new_code, db)
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
    rule = CarrierRule(name=name, prefix=prefix, priority=payload.priority,
                       auto_import_enabled=payload.auto_import_enabled,
                       auto_import_batch=payload.auto_import_batch,
                       ops_template_id=payload.ops_template_id,
                       ops_partner=payload.ops_partner.strip())
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
    rule.auto_import_enabled = payload.auto_import_enabled
    rule.auto_import_batch = payload.auto_import_batch
    rule.ops_template_id = payload.ops_template_id
    rule.ops_partner = payload.ops_partner.strip()
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
        # Lấy các mã hôm nay chưa lấy hàng (rỗng hoặc pending), nhóm theo hãng hỗ trợ.
        frm, to = period_range("day")
        stmt = select(Scan).where(Scan.pickup_status.in_(["", "pending"]))
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


def _get_ops_config_for_carrier(db, carrier: str) -> dict | None:
    """Đọc cấu hình OPS cho 1 hãng: ưu tiên DB (carrier_rules), fallback env.

    Trả {enabled, batch, template_id, partner} hoặc None nếu không cấu hình.
    """
    # Ưu tiên DB: lấy hàng CarrierRule có name = carrier, ưu tiên nhỏ nhất.
    rule = db.scalar(
        select(CarrierRule).where(CarrierRule.name == carrier)
        .order_by(CarrierRule.priority, CarrierRule.id).limit(1)
    )
    if rule and rule.ops_partner:  # chỉ dùng DB nếu đã điền partner
        return {
            "enabled": bool(rule.auto_import_enabled),
            "batch": int(rule.auto_import_batch or 100),
            "template_id": int(rule.ops_template_id or 2),
            "partner": rule.ops_partner,
        }
    # Fallback env
    env_map = (config.OPS_CARRIER_MAP or {}).get(carrier)
    if env_map:
        return {
            "enabled": bool(config.AUTO_IMPORT_ENABLED),
            "batch": int(config.AUTO_IMPORT_BATCH or 100),
            "template_id": int(env_map.get("template_id", 2)),
            "partner": env_map.get("partner", carrier),
        }
    return None


def _try_auto_import():
    """Duyệt tất cả ĐVVC có cấu hình auto import (DB hoặc env): nếu có >= batch
    đơn 'picked' hôm nay chưa import -> xuất Excel + upload + gán session_id.
    """
    from . import ops_uploader
    if not (config.OPS_USER and config.OPS_PASS):
        return
    frm, to = period_range("day")
    db = SessionLocal()
    try:
        # Lấy danh sách các name (ĐVVC) có config trong DB HOẶC env.
        names_db = [r[0] for r in db.execute(
            select(CarrierRule.name).where(CarrierRule.auto_import_enabled == True)
            .distinct()
        ).all()]
        names_env = list((config.OPS_CARRIER_MAP or {}).keys()) if config.AUTO_IMPORT_ENABLED else []
        names = list(dict.fromkeys(names_db + names_env))
        for carrier in names:
            cfg = _get_ops_config_for_carrier(db, carrier)
            if not cfg or not cfg["enabled"]:
                continue
            template_id = cfg["template_id"]
            partner = cfg["partner"]
            batch = cfg["batch"]
            stmt = (select(Scan).where(Scan.carrier == carrier,
                                       Scan.pickup_status == "picked",
                                       Scan.session_id == "")
                    .order_by(Scan.scanned_at.asc()))
            if frm is not None:
                stmt = stmt.where(Scan.scanned_at >= frm, Scan.scanned_at <= to)
            rows = db.scalars(stmt.limit(batch)).all()
            if len(rows) < batch:
                continue  # chưa đủ batch
            codes = [r.code for r in rows]
            stamp = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{carrier}"
            print(f"[auto-import] {carrier} bắt đầu nhập {len(codes)} đơn qua scan_import")
            # Dùng scan_import (gõ từng mã) thay vì upload file Excel.
            result = ops_uploader.scan_import(carrier, codes, template_id, partner, stamp)
            if result.get("ok"):
                session_id = result.get("ops_session_id") or stamp
                failed_codes = result.get("failed_codes", [])
                successful_codes = [c for c in codes if c not in failed_codes]
                entered = len(successful_codes)
                
                # Cập nhật session_id cho các mã thành công
                for r in rows:
                    if r.code in successful_codes:
                        r.session_id = session_id
                
                # Xóa hẳn các mã lỗi (trùng) khỏi database
                if failed_codes:
                    db.execute(delete(Scan).where(Scan.code.in_(failed_codes)))
                
                db.commit()
                print(f"[auto-import] {carrier} OK session={session_id} ({entered} mã)")
                _log_ops(db, "success", "auto_import", carrier, entered,
                         session_id, f"OK, mã phiên: {session_id}", "")
                events.publish("auto_import", {"carrier": carrier, "count": entered,
                                                "session_id": session_id})
            else:
                err = result.get("error", "")
                shot = result.get("screenshot_file", "")
                print(f"[auto-import] {carrier} LỖI: {err}")
                _log_ops(db, "error", "auto_import", carrier, len(codes), "", err, shot)
                events.publish("auto_import_error", {"carrier": carrier, "error": err})
    finally:
        db.close()


def _log_ops(db, level: str, action: str, carrier: str, count: int,
             session_id: str, message: str, screenshot_file: str):
    """Ghi 1 dòng OpsLog. Không raise nếu insert lỗi (không cản luồng chính)."""
    try:
        log = OpsLog(level=level, action=action, carrier=carrier, count=count,
                     session_id=session_id, message=message[:2000] if message else "",
                     screenshot_file=screenshot_file or "")
        db.add(log)
        db.commit()
        events.publish("ops_log", log.as_dict())
    except Exception as e:  # noqa: BLE001
        print(f"[log_ops] không lưu được log: {e}")
        try:
            db.rollback()
        except Exception:
            pass


@app.post("/api/ops/import-now")
def ops_import_now(
    carrier: str = Query(..., description="Tên ĐVVC (khớp name trong bảng carrier_rules)"),
    limit: int = Query(default=0, ge=0, description="0 = dùng batch trong cấu hình"),
    require_picked: bool = Query(default=True, description="False = post CẢ đơn chưa tra pickup"),
):
    """Kích hoạt import ngay 1 hãng lên OPS (không đợi đủ batch, dùng để test).

    Đọc cấu hình template_id/partner từ DB (carrier_rules) trước, fallback env.
    """
    from . import ops_uploader
    if not (config.OPS_USER and config.OPS_PASS):
        raise HTTPException(status_code=400, detail="Thiếu OPS_USER/OPS_PASS trong env")
    db = SessionLocal()
    try:
        cfg = _get_ops_config_for_carrier(db, carrier)
        if not cfg:
            raise HTTPException(status_code=400,
                                detail=f"Chưa cấu hình OPS cho '{carrier}' (điền ops_partner trong Admin hoặc OPS_CARRIER_MAP)")
        batch = int(limit) if limit else cfg["batch"]
        frm, to = period_range("day")
        stmt = (select(Scan).where(Scan.carrier == carrier,
                                   Scan.session_id == "")
                .order_by(Scan.scanned_at.asc()))
        if require_picked:
            stmt = stmt.where(Scan.pickup_status == "picked")
        if frm is not None:
            stmt = stmt.where(Scan.scanned_at >= frm, Scan.scanned_at <= to)
        rows = db.scalars(stmt.limit(batch)).all()
        if not rows:
            hint = "picked & chưa import" if require_picked else "chưa import"
            return {"status": "empty", "message": f"Không có đơn {carrier} nào ({hint})"}
        codes = [r.code for r in rows]
        stamp = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{carrier}"
        # Dùng scan_import (gõ từng mã) thay vì upload file Excel.
        result = ops_uploader.scan_import(carrier, codes, cfg["template_id"], cfg["partner"], stamp)
        session_id = ""
        shot = result.get("screenshot_file", "")
        entered = result.get("codes_entered", 0)
        if result.get("ok"):
            session_id = result.get("ops_session_id") or stamp
            failed_codes = result.get("failed_codes", [])
            successful_codes = [c for c in codes if c not in failed_codes]
            # Sử dụng số lượng thực tế trả về từ uploader thay vì đếm số mã thành công giả định
            if result.get("codes_entered"):
                entered = result.get("codes_entered")
            else:
                entered = len(successful_codes)
            
            # Cập nhật session_id cho các mã thành công
            for r in rows:
                if r.code in successful_codes:
                    r.session_id = session_id
                    
            # Xóa hẳn các mã lỗi (trùng) khỏi database
            if failed_codes:
                db.execute(delete(Scan).where(Scan.code.in_(failed_codes)))
                
            db.commit()
            
            msg = f"OK, mã phiên: {session_id}"
            if failed_codes:
                # Giới hạn text hiển thị log tránh tràn bảng
                failed_str = ", ".join(failed_codes)
                if len(failed_str) > 200:
                    failed_str = failed_str[:197] + "..."
                msg += f" | OPS ĐÃ LOẠI {len(failed_codes)} MÃ TRÙNG/LỖI: {failed_str}"
                
            _log_ops(db, "success", "manual_import", carrier, entered,
                     session_id, msg, "")
            events.publish("auto_import", {"carrier": carrier, "count": entered,
                                            "session_id": session_id})
        else:
            _log_ops(db, "error", "manual_import", carrier, len(codes), "",
                     result.get("error", ""), shot)
            events.publish("auto_import_error", {"carrier": carrier,
                                                  "error": result.get("error", "")})
        return {"status": "ok" if result.get("ok") else "error",
                "count": entered, "session_id": session_id,
                "ops_session_id": result.get("ops_session_id", ""),
                "error": result.get("error", "")}
    finally:
        db.close()


# ----------------------------- OPS logs API -----------------------------
@app.get("/api/ops/logs")
def ops_logs_list(limit: int = Query(default=100, le=1000), db: Session = Depends(get_db)):
    rows = db.scalars(select(OpsLog).order_by(OpsLog.id.desc()).limit(limit)).all()
    return {"items": [r.as_dict() for r in rows]}


@app.get("/api/ops/logs/{log_id}/screenshot")
def ops_log_screenshot(log_id: int, db: Session = Depends(get_db)):
    log = db.get(OpsLog, log_id)
    if not log or not log.screenshot_file:
        raise HTTPException(status_code=404, detail="Không có screenshot")
    path = os.path.join(config.OPS_LOGS_DIR, log.screenshot_file)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="File screenshot đã bị xóa")
    with open(path, "rb") as f:
        content = f.read()
    return Response(content=content, media_type="image/png")


@app.delete("/api/ops/logs/{log_id}", status_code=204)
def ops_log_delete(log_id: int, db: Session = Depends(get_db)):
    log = db.get(OpsLog, log_id)
    if not log:
        raise HTTPException(status_code=404, detail="Không tìm thấy log")
    # Xoá file screenshot nếu có.
    if log.screenshot_file:
        try:
            os.remove(os.path.join(config.OPS_LOGS_DIR, log.screenshot_file))
        except Exception:
            pass
    db.delete(log)
    db.commit()
    events.publish("ops_log", {"deleted": log_id})
    return Response(status_code=204)


@app.delete("/api/ops/logs", status_code=204)
def ops_log_clear(db: Session = Depends(get_db)):
    """Xoá TẤT CẢ log OPS + file screenshot kèm theo."""
    logs = db.scalars(select(OpsLog)).all()
    for log in logs:
        if log.screenshot_file:
            try:
                os.remove(os.path.join(config.OPS_LOGS_DIR, log.screenshot_file))
            except Exception:
                pass
        db.delete(log)
    db.commit()
    events.publish("ops_log", {"cleared": True})
    return Response(status_code=204)

@app.delete("/api/sessions/{session_id}")
def delete_session(session_id: str, db: Session = Depends(get_db)):
    """Xóa một phiên OPS khỏi hệ thống (để có thể import lại)."""
    # Gỡ session_id khỏi các kiện hàng
    db.execute(
        update(Scan)
        .where(Scan.session_id == session_id)
        .values(session_id="")
    )
    # Xóa log
    db.execute(
        delete(OpsLog)
        .where(OpsLog.session_id == session_id)
    )
    db.commit()
    events.publish("ops_log", {"action": "delete_session", "session_id": session_id})
    return {"status": "ok", "message": f"Đã xóa phiên {session_id}"}


@app.delete("/api/sessions/carrier/{carrier}")
def reset_sessions_by_carrier(carrier: str, db: Session = Depends(get_db)):
    """Xóa TẤT CẢ phiên giao của 1 ĐVVC để làm lại từ đầu."""
    # Đưa toàn bộ kiện hàng của ĐVVC này về trạng thái chưa có phiên
    db.execute(
        update(Scan)
        .where(Scan.carrier == carrier)
        .values(session_id="")
    )
    # Xóa tất cả log import của ĐVVC này
    db.execute(
        delete(OpsLog)
        .where(OpsLog.carrier == carrier)
    )
    db.commit()
    events.publish("ops_log", {"action": "reset_carrier", "carrier": carrier})
    return {"status": "ok", "message": f"Đã reset toàn bộ phiên giao của {carrier}"}


@app.get("/api/sessions")
def list_sessions(
    db: Session = Depends(get_db),
    period: str | None = Query(default="day"),
):
    """Danh sách mã phiên (session_id) đã import lên imv.ops, gộp theo carrier.

    Trả:
      {
        "by_carrier": {
          "SPX": [{"session_id": "...", "count": 100, "first": iso, "last": iso}, ...],
          "J&T": [...],
        }
      }
    """
    stmt = (select(Scan.carrier, Scan.session_id,
                   func.count().label("cnt"),
                   func.min(Scan.scanned_at).label("first"),
                   func.max(Scan.scanned_at).label("last"))
            .where(Scan.session_id != "")
            .group_by(Scan.carrier, Scan.session_id)
            .order_by(Scan.session_id.desc()))
    frm, to = period_range(period)
    if frm is not None:
        # lọc theo scanned_at cũng đủ để bao "session hôm nay"
        stmt = stmt.where(Scan.scanned_at >= frm, Scan.scanned_at <= to)
    rows = db.execute(stmt).all()
    by_carrier: dict = {}
    for carrier, sid, cnt, first, last in rows:
        by_carrier.setdefault(carrier, []).append({
            "session_id": sid,
            "count": cnt,
            "first": first.isoformat() if first else None,
            "last": last.isoformat() if last else None,
        })
    return {"by_carrier": by_carrier}


# ----------------------------- Sọt (basket) -----------------------------
@app.post("/api/baskets/close")
def close_basket(
    agent_name: str = Query(..., description="tên máy quét (trùng source_agent)"),
    db: Session = Depends(get_db),
):
    """Chốt 1 sọt: gom các mã QUÉT BỞI máy này từ sau sọt trước tới now.

    Nếu chưa có sọt nào hôm nay -> lấy từ 00:00 hôm nay. Trả record sọt mới.
    """
    import json
    now = datetime.now(timezone.utc)
    frm, _ = period_range("day")
    # Tìm sọt CUỐI CÙNG hôm nay của agent này để lấy mốc bắt đầu.
    last_basket = db.scalar(
        select(Basket).where(Basket.source_agent == agent_name)
        .where(Basket.closed_at >= frm)
        .order_by(Basket.seq.desc()).limit(1)
    )
    started_at = last_basket.closed_at if last_basket else frm
    next_seq = (last_basket.seq + 1) if last_basket else 1

    # Đếm các mã quét bởi agent này trong khoảng (started_at, now].
    rows = db.execute(
        select(Scan.carrier, func.count())
        .where(Scan.source_agent == agent_name,
               Scan.scanned_at > started_at, Scan.scanned_at <= now)
        .group_by(Scan.carrier)
    ).all()
    by_carrier = {c: int(n) for c, n in rows}
    total = sum(by_carrier.values())

    basket = Basket(
        seq=next_seq, source_agent=agent_name,
        started_at=started_at, closed_at=now,
        total=total, by_carrier_json=json.dumps(by_carrier, ensure_ascii=False),
    )
    db.add(basket)
    db.commit()
    db.refresh(basket)
    events.publish("basket_close", basket.as_dict())
    return basket.as_dict()


@app.get("/api/baskets")
def list_baskets(
    agent_name: str | None = Query(default=None),
    period: str | None = Query(default="day"),
    db: Session = Depends(get_db),
):
    """Danh sách các sọt (lọc theo agent + kỳ)."""
    stmt = select(Basket).order_by(Basket.closed_at.desc())
    if agent_name:
        stmt = stmt.where(Basket.source_agent == agent_name)
    frm, to = period_range(period)
    if frm is not None:
        stmt = stmt.where(Basket.closed_at >= frm, Basket.closed_at <= to)
    rows = db.scalars(stmt).all()
    return {"items": [r.as_dict() for r in rows]}


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
def _import_query(carrier: str, period: str | None, only_picked: bool, db: Session):
    """Tạo query chung cho preview + export import-file."""
    frm, to = period_range(period or "day")
    stmt = select(Scan).where(Scan.carrier == carrier).order_by(Scan.scanned_at.asc())
    if frm is not None:
        stmt = stmt.where(Scan.scanned_at >= frm, Scan.scanned_at <= to)
    if only_picked:
        stmt = stmt.where(Scan.pickup_status == "picked")
    return stmt


@app.get("/api/export/import-file/preview")
def preview_import_file(
    carrier: str = Query(..., description="ĐVVC cần xuất (vd SPX)"),
    period: str | None = Query(default="day", description="day|week|month|quarter|year|all"),
    limit: int = Query(default=500, ge=1, le=5000),
    only_picked: bool = Query(default=True, description="chỉ xuất đơn đã lấy hàng"),
    db: Session = Depends(get_db),
):
    """Preview danh sách mã sẽ được xuất — dùng cho modal trên web.

    Trả danh sách mã + tổng số để user kiểm tra trước khi bấm Xuất.
    """
    stmt = _import_query(carrier, period, only_picked, db)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.limit(limit)).all()
    return {
        "total": total,
        "items": [{"id": r.id, "code": r.code, "carrier": r.carrier,
                   "pickup_status": r.pickup_status or "",
                   "scanned_at": r.scanned_at.isoformat() if r.scanned_at else None}
                  for r in rows],
    }


@app.get("/api/export/import-file")
def export_import_file(
    carrier: str = Query(..., description="ĐVVC cần xuất (vd SPX)"),
    period: str | None = Query(default="day", description="day|week|month|quarter|year|all"),
    limit: int = Query(default=100, ge=1, le=5000),
    only_picked: bool = Query(default=True, description="chỉ xuất đơn đã lấy hàng"),
    db: Session = Depends(get_db),
):
    """Xuất Excel ĐÚNG format import lên imv.ops: 1 cột 'Mã'.

    Lấy tối đa `limit` đơn của `carrier` theo kỳ (mặc định hôm nay), theo thứ tự
    quét (từ đơn đầu tiên). Mặc định chỉ lấy đơn ĐÃ lấy hàng (only_picked=True).
    """
    stmt = _import_query(carrier, period, only_picked, db)
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


from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import secrets

security = HTTPBasic()

def check_admin(credentials: HTTPBasicCredentials = Depends(security)):
    correct_username = secrets.compare_digest(credentials.username, "admin")
    correct_password = secrets.compare_digest(credentials.password, "123456")
    if not (correct_username and correct_password):
        raise HTTPException(
            status_code=401,
            detail="Sai thông tin đăng nhập",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

@app.get("/admin", response_class=HTMLResponse)
def serve_admin(username: str = Depends(check_admin)):
    """Phục vụ file admin có bảo vệ mật khẩu."""
    # Read the admin.html file from backend/app/templates
    admin_file = os.path.join(os.path.dirname(__file__), "templates", "admin.html")
    if os.path.exists(admin_file):
        with open(admin_file, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse(content="Admin template not found", status_code=404)

# Nếu thư mục web được mount vào (Docker), backend phục vụ luôn UI ở "/".
_WEB_DIR = os.getenv("WEB_DIR", "/app/web")
if os.path.isdir(_WEB_DIR):
    app.mount("/", StaticFiles(directory=_WEB_DIR, html=True), name="web")

# ----------------------------- Auto Tracking -----------------------------
import threading
import time

def _auto_track_loop():
    """Vòng lặp chạy ngầm tự động kiểm tra trạng thái lấy hàng mỗi 30 phút."""
    from .tracker import CHECKERS
    while True:
        time.sleep(30 * 60) # 30 minutes
        if not _track_running["on"]:
            _track_running["on"] = True
            try:
                print("[auto-track] Đang tự động kiểm tra trạng thái lấy hàng...")
                _do_tracking(list(CHECKERS.keys()))
            except Exception as e:
                print(f"[auto-track] Lỗi: {e}")
            finally:
                _track_running["on"] = False
                
@app.on_event("startup")
def startup_event():
    """Khởi chạy các luồng ngầm khi server start."""
    threading.Thread(target=_auto_track_loop, daemon=True).start()
