"""Khởi tạo kết nối DB (SQLAlchemy)."""
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import DATABASE_URL

engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()


def get_db():
    """Dependency của FastAPI: mở session, đóng khi xong request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Tạo bảng nếu chưa có + thêm cột mới nếu thiếu. Gọi lúc app khởi động."""
    from . import models  # noqa: F401  (đăng ký model vào Base.metadata)

    Base.metadata.create_all(bind=engine)
    _run_light_migrations()


def _run_light_migrations():
    """Thêm cột mới vào bảng đã tồn tại (create_all không tự thêm cột).

    Chỉ ALTER khi cột còn thiếu -> an toàn chạy lặp lại. Hỗ trợ Postgres & SQLite.
    """
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    to_add: list[str] = []
    # --- scans ---
    if "scans" in tables:
        cols = {c["name"] for c in inspector.get_columns("scans")}
        if "dup_count" not in cols:
            to_add.append("ALTER TABLE scans ADD COLUMN dup_count INTEGER NOT NULL DEFAULT 0")
        if "last_dup_at" not in cols:
            to_add.append("ALTER TABLE scans ADD COLUMN last_dup_at TIMESTAMP NULL")
        if "pickup_status" not in cols:
            to_add.append("ALTER TABLE scans ADD COLUMN pickup_status VARCHAR(16) NOT NULL DEFAULT ''")
        if "pickup_checked_at" not in cols:
            to_add.append("ALTER TABLE scans ADD COLUMN pickup_checked_at TIMESTAMP NULL")
        if "session_id" not in cols:
            to_add.append("ALTER TABLE scans ADD COLUMN session_id VARCHAR(64) NOT NULL DEFAULT ''")
        if "basket_id" not in cols:
            to_add.append("ALTER TABLE scans ADD COLUMN basket_id INTEGER NULL")
    # --- baskets: trạng thái bàn giao OPS + nhật ký lỗi ---
    if "baskets" in tables:
        cols = {c["name"] for c in inspector.get_columns("baskets")}
        if "ops_status" not in cols:
            to_add.append("ALTER TABLE baskets ADD COLUMN ops_status VARCHAR(16) NOT NULL DEFAULT ''")
        if "ops_handed_at" not in cols:
            to_add.append("ALTER TABLE baskets ADD COLUMN ops_handed_at TIMESTAMP NULL")
        if "ops_sessions_json" not in cols:
            to_add.append("ALTER TABLE baskets ADD COLUMN ops_sessions_json VARCHAR(2000) NOT NULL DEFAULT '{}'")
        if "ops_errors_json" not in cols:
            to_add.append("ALTER TABLE baskets ADD COLUMN ops_errors_json VARCHAR(8000) NOT NULL DEFAULT '[]'")
        if "is_closed" not in cols:
            # BOOLEAN DEFAULT FALSE để tương thích cả SQLite lẫn Postgres.
            # Backfill: sọt cũ trong DB được coi là ĐÃ CHỐT (is_closed=True)
            # NGOẠI TRỪ sọt "đang mở" (record total=0 tạo bởi _upsert_basket
            # khi mã đầu tiên vào nhưng chưa qua close_basket). Heuristic:
            # sọt total=0 mà là sọt seq lớn nhất của agent trong hôm nay ->
            # đang mở. Giản lược: set is_closed=True cho sọt có total>0,
            # và cho sọt total=0 mà KHÔNG phải seq lớn nhất theo agent (sọt
            # rỗng đã chốt bằng cách bấm nút Hoàn thành sọt trước đây, bản cũ
            # không chặn <10 mã).
            to_add.append("ALTER TABLE baskets ADD COLUMN is_closed BOOLEAN NOT NULL DEFAULT FALSE")
            to_add.append("UPDATE baskets SET is_closed=TRUE WHERE total > 0")
            # Với sọt total=0: chỉ giữ sọt cuối cùng của agent là "đang mở",
            # còn lại đều là chốt-rỗng do bấm nút bản cũ.
            # Postgres cú pháp: dùng subquery + tuple compare.
            to_add.append(
                "UPDATE baskets SET is_closed=TRUE WHERE total = 0 "
                "AND (source_agent, seq) NOT IN ("
                "  SELECT source_agent, MAX(seq) FROM baskets "
                "  WHERE total = 0 GROUP BY source_agent"
                ")"
            )
    # --- carrier_rules (cấu hình auto import theo hãng) ---
    if "carrier_rules" in tables:
        cols = {c["name"] for c in inspector.get_columns("carrier_rules")}
        if "auto_import_enabled" not in cols:
            # Postgres nghiêm ngặt: BOOLEAN không nhận DEFAULT 0, phải FALSE.
            # SQLite nhận cả FALSE lẫn 0 -> dùng FALSE để tương thích cả 2.
            to_add.append("ALTER TABLE carrier_rules ADD COLUMN auto_import_enabled BOOLEAN NOT NULL DEFAULT FALSE")
        if "auto_import_batch" not in cols:
            to_add.append("ALTER TABLE carrier_rules ADD COLUMN auto_import_batch INTEGER NOT NULL DEFAULT 100")
        if "ops_template_id" not in cols:
            to_add.append("ALTER TABLE carrier_rules ADD COLUMN ops_template_id INTEGER NOT NULL DEFAULT 2")
        if "ops_partner" not in cols:
            to_add.append("ALTER TABLE carrier_rules ADD COLUMN ops_partner VARCHAR(128) NOT NULL DEFAULT ''")
    # --- Fix cột ops_errors_json: VARCHAR(8000) -> TEXT (chỉ Postgres) ---
    # Trước đây code cắt [:8000] giữa chuỗi JSON gây "Unterminated string"
    # khi loads lại -> _force_auto_import_all crash -> auto-import không chạy.
    # Đổi sang TEXT để không bao giờ bị giới hạn độ dài ở tầng DB.
    is_postgres = engine.dialect.name == "postgresql"
    if is_postgres and "baskets" in tables:
        try:
            col = next((c for c in inspector.get_columns("baskets")
                        if c["name"] == "ops_errors_json"), None)
            # SQLAlchemy trả type; chỉ ALTER nếu còn là VARCHAR (có length).
            if col is not None and getattr(col["type"], "length", None):
                to_add.append("ALTER TABLE baskets ALTER COLUMN ops_errors_json TYPE TEXT")
        except Exception as _e:
            print(f"[migrate] skip ops_errors_json TYPE check: {_e}")

    if not to_add:
        # Vẫn cần chạy bước sửa data hỏng (không phụ thuộc to_add).
        _repair_broken_ops_errors_json()
        return
    with engine.begin() as conn:
        for stmt in to_add:
            conn.execute(text(stmt))
            print(f"[migrate] {stmt}")
    # Sau khi ALTER xong, sửa các dòng JSON đã hỏng do bug cắt [:8000] cũ.
    _repair_broken_ops_errors_json()


def _repair_broken_ops_errors_json():
    """Reset ops_errors_json về '[]' cho các basket có JSON hỏng.

    Bug cũ cắt [:8000] giữa chuỗi JSON -> json.loads raise. Đọc từng dòng,
    thử parse, nếu fail thì set lại '[]' (chỉ mất log lỗi chi tiết, không
    ảnh hưởng mã đã import — session_id nằm ở ops_sessions_json riêng)."""
    import json as _json
    fixed = 0
    try:
        with engine.begin() as conn:
            # Dùng raw để không phụ thuộc session; đọc id + json.
            rows = conn.execute(text(
                "SELECT id, ops_errors_json FROM baskets "
                "WHERE ops_errors_json IS NOT NULL AND ops_errors_json <> '[]'"
            )).fetchall()
            for row in rows:
                bid, raw = row[0], row[1]
                if not raw:
                    continue
                try:
                    _json.loads(raw)
                except Exception:
                    conn.execute(
                        text("UPDATE baskets SET ops_errors_json = '[]' WHERE id = :id"),
                        {"id": bid},
                    )
                    fixed += 1
        if fixed:
            print(f"[migrate] repaired {fixed} basket(s) with broken ops_errors_json")
    except Exception as e:
        print(f"[migrate] _repair_broken_ops_errors_json error: {e}")
