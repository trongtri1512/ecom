"""Tự động import file Excel lên imv.ops.vnfai.com bằng Playwright.

Luồng: mở trang, login (nếu form login hiện), vào /tpl-sessions/new/{template_id},
bấm nút IMPORT, chọn file Excel, xác nhận, chọn Đối tác vận chuyển, bấm TẠO.

Ghi chú:
- Selector login viết theo pattern linh hoạt (thử nhiều cách) vì tôi không có
  quyền xem form login của bạn khi đã đăng nhập sẵn. Nếu login sai, chỉnh trong
  hàm _login() theo id/name thật của form.
- Cần env: OPS_URL, OPS_USER, OPS_PASS, OPS_CARRIER_MAP (JSON).
"""
from __future__ import annotations

import os
import time
from typing import Optional

from . import config


def upload_import(carrier: str, excel_path: str, template_id: int, partner_name: str,
                  headless: bool = True, timeout_ms: int = 45000) -> dict:
    """Upload 1 file Excel lên imv.ops. Trả {ok: bool, error: str}."""
    from playwright.sync_api import sync_playwright

    if not (config.OPS_USER and config.OPS_PASS):
        return {"ok": False, "error": "Thiếu OPS_USER/OPS_PASS trong env"}
    if not os.path.exists(excel_path):
        return {"ok": False, "error": f"Không thấy file: {excel_path}"}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless,
                                    args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
            locale="vi-VN",
            accept_downloads=True,
        )
        page = context.new_page()
        page.set_default_timeout(timeout_ms)
        try:
            # 1) Login (nếu cần)
            _login(page)

            # 2) Vào trang tạo phiên với đúng template
            page.goto(f"{config.OPS_URL}/#/tpl-sessions/new/{template_id}",
                      wait_until="networkidle")
            page.wait_for_timeout(1500)

            # 3) Bấm nút IMPORT (nút xanh lá góc phải)
            _click_first(page, [
                "button:has-text('IMPORT')",
                "button:has-text('Import')",
                "text=/^\\s*IMPORT\\s*$/i",
            ])
            page.wait_for_timeout(800)

            # 4) Chọn file — dialog có nút "Chọn file" mở file picker.
            #    Playwright dùng expect_file_chooser để chặn dialog hệ điều hành.
            with page.expect_file_chooser() as fc_info:
                _click_first(page, [
                    "button:has-text('Chọn file')",
                    "text=/Chọn file/i",
                ])
            fc = fc_info.value
            fc.set_files(excel_path)
            page.wait_for_timeout(1500)

            # 5) Bấm ĐỒNG Ý
            _click_first(page, [
                "button:has-text('ĐỒNG Ý')",
                "button:has-text('Đồng ý')",
            ])
            page.wait_for_timeout(2000)

            # 6) Chọn Đối tác vận chuyển (dropdown ở panel bên phải)
            _select_partner(page, partner_name)
            page.wait_for_timeout(500)

            # 7) Bấm TẠO (URL trước khi bấm, để so sánh sau)
            url_before = page.url
            _click_first(page, [
                "button:has-text('TẠO')",
                "button:has-text('Tạo')",
            ])
            # Chờ URL đổi (OPS thường redirect sang trang chi tiết phiên vừa tạo)
            ops_session_id = ""
            try:
                page.wait_for_function(f"() => location.href !== {url_before!r}", timeout=8000)
            except Exception:
                pass
            page.wait_for_timeout(1500)

            # 8) Đọc mã phiên do OPS cấp từ URL sau khi TẠO
            #    Ví dụ URL: /#/tpl-sessions/detail/SPXCCEJDN26U hoặc ?id=SPXCCEJDN26U
            import re
            new_url = page.url
            # Mã phiên OPS: các chuỗi dạng SPXCCE.../JTE.../BEXCCE... (chữ HOA + số, 8-32 ký tự)
            m = re.search(r"([A-Z]{3,6}CCE[A-Z0-9]{4,}|JTE[A-Z0-9]{6,}|BEX[A-Z0-9]{6,}|[A-Z]{2,}[A-Z0-9]{8,})", new_url)
            if m:
                ops_session_id = m.group(1)
            # Fallback: tìm trong text trang (VD tiêu đề "Phiên bàn giao SPXCCE...")
            if not ops_session_id:
                body = page.inner_text("body")
                m2 = re.search(r"\b((?:SPXCCE|JTE|BEXCCE)[A-Z0-9]{4,20})\b", body)
                if m2:
                    ops_session_id = m2.group(1)

            body = page.inner_text("body")
            if "không có quyền" in body.lower() or "access denied" in body.lower():
                return {"ok": False, "error": "Không có quyền truy cập (kiểm tra tài khoản)"}
            return {"ok": True, "error": "", "ops_session_id": ops_session_id}
        except Exception as e:  # noqa: BLE001
            # Chụp màn hình để debug
            try:
                shot = os.path.join("/tmp", f"ops_error_{int(time.time())}.png")
                page.screenshot(path=shot, full_page=True)
            except Exception:
                shot = ""
            return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]} (screenshot: {shot})"}
        finally:
            browser.close()


def _login(page):
    """Nếu trang hiện form login, tự nhập user/pass. Chờ redirect vào dashboard."""
    page.goto(f"{config.OPS_URL}/", wait_until="networkidle")
    page.wait_for_timeout(1000)
    # Nếu URL redirect về landing/dashboard, tức là đã login (session cookie).
    if "landing" in page.url or "dashboard" in page.url or "tpl-sessions" in page.url:
        return
    # Thử tìm ô nhập username (nhiều pattern).
    user_input = _find_first(page, [
        "input[name='username']", "input[name='user']", "input[name='email']",
        "input[id='username']", "input[id='user']", "input[placeholder*='tên']",
        "input[placeholder*='ăng']",  # đăng nhập
        "input[type='text']",
    ])
    pass_input = _find_first(page, [
        "input[name='password']", "input[id='password']",
        "input[type='password']",
    ])
    if user_input and pass_input:
        user_input.fill(config.OPS_USER)
        pass_input.fill(config.OPS_PASS)
        # Nút login
        _click_first(page, [
            "button:has-text('Đăng nhập')", "button:has-text('Login')",
            "button[type='submit']",
        ])
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1500)


def _find_first(page, selectors):
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible():
                return el
        except Exception:
            continue
    return None


def _click_first(page, selectors):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                loc.click(timeout=5000)
                return True
        except Exception:
            continue
    raise RuntimeError(f"Không tìm thấy nút nào trong: {selectors}")


def _select_partner(page, partner_name: str):
    """Mở dropdown 'Đối tác vận chuyển' và chọn option chứa partner_name."""
    # Dropdown thường là 1 element .ant-select hoặc select. Thử cả 2.
    try:
        # Ant Design pattern
        trigger = page.locator("text=Đối tác vận chuyển").locator(
            "xpath=ancestor::*[contains(@class,'ant-form-item') or self::div][1]"
        ).locator(".ant-select-selector").first
        if trigger.count() > 0:
            trigger.click()
            page.wait_for_timeout(400)
            page.locator(f".ant-select-item:has-text('{partner_name}')").first.click()
            return
    except Exception:
        pass
    # Fallback: native select
    try:
        sel = page.locator("select").first
        sel.select_option(label=partner_name)
    except Exception as e:
        raise RuntimeError(f"Không chọn được đối tác '{partner_name}': {e}")
