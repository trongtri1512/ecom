"""Cấu hình đọc từ biến môi trường (nạp .env khi chạy local)."""
import os

from dotenv import load_dotenv

load_dotenv()


def _get(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


# Kết nối DB. Mặc định trỏ tới service `db` trong docker-compose.
DATABASE_URL = _get(
    "DATABASE_URL",
    "postgresql+psycopg2://scan:scan@db:5432/scan",
)

# API key mà Scanner Agent phải gửi qua header X-API-Key.
API_KEY = _get("API_KEY", "change-me")

# CORS: danh sách origin của web app, cách nhau bằng dấu phẩy. "*" = cho tất cả.
CORS_ORIGINS = [o for o in _get("CORS_ORIGINS", "*").split(",") if o] or ["*"]

# SMTP để gửi email báo mã trùng.
SMTP_HOST = _get("SMTP_HOST")
SMTP_PORT = int(_get("SMTP_PORT", "587") or "587")
SMTP_USER = _get("SMTP_USER")
SMTP_PASS = _get("SMTP_PASS")
SMTP_FROM = _get("SMTP_FROM") or SMTP_USER
SMTP_TLS = _get("SMTP_TLS", "true").lower() in ("1", "true", "yes")
ADMIN_EMAIL = _get("ADMIN_EMAIL")

# Bật/tắt gửi email (tắt để test không cần SMTP).
MAIL_ENABLED = _get("MAIL_ENABLED", "true").lower() in ("1", "true", "yes")

# Cửa sổ "quét lại không tính trùng" (giây). Trong khoảng này kể từ lần quét
# ĐẦU TIÊN của một mã, quét lại sẽ bị bỏ qua ÊM (không lưu, không email, không
# cảnh báo). Sau khoảng này, quét lại mới tính là TRÙNG. Mặc định 60s.
DUP_GRACE_SECONDS = int(_get("DUP_GRACE_SECONDS", "60") or "60")

# Mật khẩu xác nhận khi XOÁ dòng (kiểm ở server). Đổi trong .env.
DELETE_PASSWORD = _get("DELETE_PASSWORD", "1512")
