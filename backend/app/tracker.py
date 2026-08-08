"""Tracking trạng thái 'đã lấy hàng' bằng Playwright (đọc DOM trang hãng).

Dựa trên cách của người dùng: mở trang tracking công khai của hãng, đọc text,
tìm dòng xác nhận đã lấy hàng. Không dùng API ẩn (bị ký/chặn bot).

Hiện hỗ trợ SPX (đã xác thực bằng mã thật). J&T/Best/GHN để khung, bổ sung sau
khi có mã mẫu + selector.

Chạy được trong Docker (image cài sẵn Chromium của Playwright).
"""
from __future__ import annotations

import random
import time
from datetime import datetime, timezone
from typing import Callable, Dict, List

# Chuỗi xác nhận "đã lấy hàng" trên trang SPX (xác thực từ mã thật).
SPX_PICKED_TEXT = "Đơn vị vận chuyển lấy hàng thành công"

DELAY_MIN = 1.2
DELAY_MAX = 2.5


def _check_spx(page, code: str) -> bool:
    """True nếu trang SPX có dòng 'Đơn vị vận chuyển lấy hàng thành công'."""
    page.goto(f"https://spx.vn/track?{code}", wait_until="networkidle", timeout=25000)
    page.wait_for_timeout(2500)
    body = page.inner_text("body")
    return SPX_PICKED_TEXT in body


# Map ĐVVC -> hàm tra. Thêm hãng mới ở đây.
CHECKERS: Dict[str, Callable] = {
    "SPX": _check_spx,
}


def track_codes(codes_by_carrier: Dict[str, List[str]], headless: bool = True,
                on_result: Callable[[str, str, bool], None] | None = None) -> Dict[str, dict]:
    """Tra trạng thái lấy hàng cho nhiều mã, nhóm theo ĐVVC.

    codes_by_carrier: {"SPX": ["SPXVN...", ...], ...}
    on_result(carrier, code, picked): callback sau mỗi mã (để cập nhật DB dần).
    Trả {code: {"carrier", "picked", "checked_at", "error"}}.
    """
    from playwright.sync_api import sync_playwright  # import trễ để backend chạy khi chưa cài

    results: Dict[str, dict] = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless,
                                    args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
            locale="vi-VN",
        )
        page = context.new_page()
        # Bỏ tải ảnh/font/css cho nhanh.
        page.route("**/*", lambda route: route.abort()
                   if route.request.resource_type in ("image", "font", "media", "stylesheet")
                   else route.continue_())

        for carrier, codes in codes_by_carrier.items():
            checker = CHECKERS.get(carrier)
            for i, code in enumerate(codes):
                rec = {"carrier": carrier, "picked": False, "error": "",
                       "checked_at": datetime.now(timezone.utc).isoformat()}
                if checker is None:
                    rec["error"] = f"Chưa hỗ trợ tra {carrier}"
                else:
                    try:
                        rec["picked"] = bool(checker(page, code))
                    except Exception as e:  # noqa: BLE001
                        rec["error"] = str(e)[:200]
                results[code] = rec
                if on_result:
                    on_result(carrier, code, rec["picked"])
                if i < len(codes) - 1:
                    time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
        browser.close()
    return results
