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

# ---- Auto import file Excel lên imv.ops.vnfai.com ----
OPS_URL = _get("OPS_URL", "https://imv.ops.vnfai.com").rstrip("/")
OPS_USER = _get("OPS_USER")
OPS_PASS = _get("OPS_PASS")
# Số đơn 'picked' chưa import cần đạt để tự động upload cho 1 hãng.
AUTO_IMPORT_BATCH = int(_get("AUTO_IMPORT_BATCH", "100") or "100")
# Bật/tắt tính năng auto import.
AUTO_IMPORT_ENABLED = _get("AUTO_IMPORT_ENABLED", "false").lower() in ("1", "true", "yes")
# Map hãng -> {template_id, partner_name} (JSON string trong env). Ví dụ:
#   OPS_CARRIER_MAP='{"SPX":{"template_id":2,"partner":"SPX Express"},"J&T":{"template_id":2,"partner":"J&T Express"},"GHN":{"template_id":2,"partner":"Giao Hàng Nhanh"},"Best Express":{"template_id":2,"partner":"BEST Express"},"Viettel Post":{"template_id":2,"partner":"Viettel Post"}}'
import json as _json
try:
    OPS_CARRIER_MAP = _json.loads(_get("OPS_CARRIER_MAP", "{}") or "{}")
except Exception:  # noqa: BLE001
    OPS_CARRIER_MAP = {}

# Thư mục lưu log OPS (screenshot lỗi). Trong docker mount ra ngoài để không mất.
OPS_LOGS_DIR = _get("OPS_LOGS_DIR", "/app/ops_logs")
