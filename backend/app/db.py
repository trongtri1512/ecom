"""Khởi tạo kết nối DB (SQLAlchemy)."""
from sqlalchemy import create_engine
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
    """Tạo bảng nếu chưa có. Gọi lúc app khởi động."""
    from . import models  # noqa: F401  (đăng ký model vào Base.metadata)

    Base.metadata.create_all(bind=engine)
