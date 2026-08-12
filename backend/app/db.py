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
    if not to_add:
        return
    with engine.begin() as conn:
        for stmt in to_add:
            conn.execute(text(stmt))
            print(f"[migrate] {stmt}")
