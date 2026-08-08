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
    if "scans" not in inspector.get_table_names():
        return
    existing_cols = {c["name"] for c in inspector.get_columns("scans")}
    to_add = []
    if "dup_count" not in existing_cols:
        to_add.append("ALTER TABLE scans ADD COLUMN dup_count INTEGER NOT NULL DEFAULT 0")
    if "last_dup_at" not in existing_cols:
        to_add.append("ALTER TABLE scans ADD COLUMN last_dup_at TIMESTAMP NULL")
    if "pickup_status" not in existing_cols:
        to_add.append("ALTER TABLE scans ADD COLUMN pickup_status VARCHAR(16) NOT NULL DEFAULT ''")
    if "pickup_checked_at" not in existing_cols:
        to_add.append("ALTER TABLE scans ADD COLUMN pickup_checked_at TIMESTAMP NULL")
    if not to_add:
        return
    with engine.begin() as conn:
        for stmt in to_add:
            conn.execute(text(stmt))
            print(f"[migrate] {stmt}")
