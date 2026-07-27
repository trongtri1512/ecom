"""Gửi email báo mã trùng cho Admin qua SMTP."""
from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage

from . import config


def send_duplicate_alert(code: str, carrier: str, source_agent: str | None) -> None:
    """Gửi email cảnh báo 1 mã bị quét trùng.

    Chạy trong background task nên nuốt mọi lỗi (log ra stdout) để không làm
    hỏng response cho agent.
    """
    if not config.MAIL_ENABLED:
        print(f"[mailer] MAIL_ENABLED=false, bỏ qua email trùng mã {code}")
        return
    if not (config.SMTP_HOST and config.ADMIN_EMAIL and config.SMTP_FROM):
        print("[mailer] Thiếu cấu hình SMTP/ADMIN_EMAIL, bỏ qua gửi email.")
        return

    msg = EmailMessage()
    msg["Subject"] = f"[Scan Ecom] Mã vận đơn TRÙNG: {code}"
    msg["From"] = config.SMTP_FROM
    msg["To"] = config.ADMIN_EMAIL
    msg.set_content(
        "Phát hiện quét TRÙNG mã vận đơn.\n\n"
        f"- Mã vận đơn : {code}\n"
        f"- ĐVVC       : {carrier}\n"
        f"- Máy quét   : {source_agent or '(không rõ)'}\n\n"
        "Mã này đã có trong hệ thống nên KHÔNG được thêm lần nữa.\n"
        "-- Scan Ecom"
    )

    try:
        if config.SMTP_TLS:
            with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=15) as s:
                s.starttls(context=ssl.create_default_context())
                if config.SMTP_USER:
                    s.login(config.SMTP_USER, config.SMTP_PASS)
                s.send_message(msg)
        else:
            with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=15) as s:
                if config.SMTP_USER:
                    s.login(config.SMTP_USER, config.SMTP_PASS)
                s.send_message(msg)
        print(f"[mailer] Đã gửi email báo trùng mã {code} tới {config.ADMIN_EMAIL}")
    except Exception as exc:  # noqa: BLE001
        print(f"[mailer] Lỗi gửi email báo trùng mã {code}: {exc}")
