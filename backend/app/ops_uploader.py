"""Tự động tạo phiên bàn giao trên imv.ops.vnfai.com bằng Playwright.

Hai phương thức:
1. scan_import()  — nhập từng mã vào ô quét (ĐỀ XUẤT, đáng tin cậy hơn).
2. upload_import() — upload file Excel qua dialog IMPORT (fallback).

Ghi chú:
- OPS dùng Keycloak SSO (auth.vnfai.com) để login.
- Cần env: OPS_URL, OPS_USER, OPS_PASS, OPS_CARRIER_MAP (JSON).
"""
from __future__ import annotations

import os
import time
import random
from typing import Optional

from . import config


def scan_import(carrier: str, codes: list[str], template_id: int, partner_name: str,
                stamp: str = "", headless: bool = True, timeout_ms: int = 600000) -> dict:
    """Tạo phiên bàn giao bằng cách GÕ TỪNG MÃ vào ô quét trên OPS.

    Luồng:
    1. Login Keycloak
    2. Vào /tpl-sessions/new/{template_id}
    3. Chọn Đối tác vận chuyển
    4. Gõ từng mã vào ô "Quét mã kiện hàng..." → Enter
    5. Bấm TẠO

    Trả {ok: bool, error: str, ops_session_id: str, codes_entered: int}.
    """
    from playwright.sync_api import sync_playwright

    if not (config.OPS_USER and config.OPS_PASS):
        return {"ok": False, "error": "Thiếu OPS_USER/OPS_PASS trong env"}
    if not codes:
        return {"ok": False, "error": "Danh sách mã rỗng"}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless,
                                    args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
            locale="vi-VN",
        )
        page = context.new_page()
        page.set_default_timeout(timeout_ms)
        try:
            # 1) Login
            _login(page)

            # 2) Vào trang tạo phiên
            page.goto(f"{config.OPS_URL}/#/tpl-sessions/new/{template_id}",
                      wait_until="networkidle")
            page.wait_for_timeout(2000)

            # 3) Tìm ô nhập mã (ô quét mã kiện hàng)
            #    OPS tự nhận dạng đối tác vận chuyển khi nhập mã đầu tiên,
            #    KHÔNG cần chọn đối tác thủ công.
            scan_input = _find_first(page, [
                "input[placeholder*='kiện hàng']",
                "input[placeholder*='vận đơn']",
                "input[placeholder*='đơn hàng']",
                "input[placeholder*='Quét mã']",
                "input[placeholder*='quét mã']",
            ])
            if not scan_input:
                _save_screenshot(page, "scan_input_not_found")
                return {"ok": False, "error": "Không tìm thấy ô nhập mã quét"}

            # 4) Gõ từng mã → Enter (hoặc click nút >>)
            entered = 0
            failed_codes = []
            for code in codes:
                code = code.strip()
                if not code:
                    continue
                scan_input.fill(code)
                page.wait_for_timeout(200)
                # Nhấn Enter hoặc click nút >> để submit mã.
                try:
                    scan_input.press("Enter")
                except Exception:
                    # Fallback: click nút >> bên cạnh ô input.
                    try:
                        _click_first(page, ["button:has-text('>>')", "button:near(input)"])
                    except Exception:
                        pass
                
                # Chờ một chút để web xử lý mã
                page.wait_for_timeout(random.uniform(400, 800))
                
                # Nếu có popup cảnh báo lỗi cho mã này (VD: "Kiện hàng đã tồn tại"), bấm OK để bỏ qua ngay
                try:
                    for btn in page.locator("button:has-text('OK'), button:has-text('ĐỒNG Ý'), button:has-text('Đóng')").all():
                        if btn.is_visible():
                            print(f"[scan-import] Đóng thông báo lỗi cho mã {code}")
                            if code not in failed_codes:
                                failed_codes.append(code)
                            btn.click(force=True)
                            page.wait_for_timeout(300)
                except Exception:
                    pass

                entered += 1
                if entered % 50 == 0:
                    print(f"[scan-import] {carrier}: đã nhập {entered}/{len(codes)} mã")

            print(f"[scan-import] {carrier}: nhập xong {entered} mã, chờ xử lý...")
            page.wait_for_timeout(2000)

            # 5.5) Tự động xóa các kiện trùng / lỗi trên bảng để nút TẠO được hiển thị
            try:
                # Đóng các toast/modal cảnh báo chung nếu có
                for _ in range(2):
                    for btn in page.locator("button:has-text('OK'), button:has-text('ĐỒNG Ý'), button:has-text('Đóng')").all():
                        if btn.is_visible():
                            btn.click(force=True)
                            page.wait_for_timeout(500)
                
                # Tìm các dòng báo lỗi và xóa
                rows = page.locator("tbody tr").all()
                for row in rows:
                    if not row.is_visible(): continue
                    txt = row.inner_text().lower()
                    # Nhận diện lỗi: trùng, tồn tại, đã lấy hàng, lỗi, đã bàn giao, khác...
                    if any(k in txt for k in ["trùng", "tồn tại", "đã lấy hàng", "lỗi", "đã bàn giao", "khác", "không hợp lệ"]):
                        # Ghi nhận mã bị lỗi bằng cách quét xem mã nào có trong text của dòng này
                        for code in codes:
                            if code.strip().lower() in txt and code.strip() not in failed_codes:
                                failed_codes.append(code.strip())

                        del_btn = row.locator("[icon='trash'], .nb-trash, i.fa-trash, button:has-text('Xóa'), button[title='Xóa']").first
                        if del_btn.is_visible():
                            print("[scan-import] Xóa 1 mã bị trùng/lỗi khỏi danh sách.")
                            del_btn.click(force=True)
                            entered -= 1  # Giảm số lượng thực tế import thành công
                            page.wait_for_timeout(500)
                            # Đóng popup xác nhận xóa (nếu hệ thống có hỏi "Bạn có chắc...")
                            for confirm_btn in page.locator("button:has-text('ĐỒNG Ý'), button:has-text('Xác nhận'), button:has-text('Xoá')").all():
                                if confirm_btn.is_visible():
                                    confirm_btn.click(force=True)
                                    page.wait_for_timeout(500)
            except Exception as e:
                print("[scan-import] Bỏ qua lỗi khi cố gắng xóa kiện trùng:", e)

            _save_screenshot(page, "after_scan_input")
            
            # 5.6) Điền Ghi chú
            if stamp:
                try:
                    note_area = page.locator("textarea[placeholder*='ghi chú'], textarea[placeholder*='Ghi chú']").first
                    if note_area.is_visible():
                        note_area.fill(stamp)
                        page.wait_for_timeout(300)
                except Exception:
                    pass

            # 6) Bấm TẠO
            url_before = page.url
            _click_first(page, [
                "button:has-text('TẠO')",
                ":text('TẠO')",
                "button:has-text('Tạo')",
            ])

            # 7) Chờ popup "Tạo phiên bàn giao thành công" (nb-dialog) rồi bấm OK.
            #    QUAN TRỌNG: phải đóng popup trước khi đọc mã phiên, nếu không
            #    page.inner_text('body') chỉ đọc text popup, không lấy được mã.
            page.wait_for_timeout(2000)
            try:
                for btn in page.locator("button:has-text('OK'), button:has-text('Đồng ý'), button:has-text('ĐỒNG Ý')").all():
                    if btn.is_visible():
                        btn.click(force=True)
                        page.wait_for_timeout(500)
                        break
            except Exception:
                pass

            # Chờ URL đổi sang /#/tpl-sessions/{id}/view (OPS redirect sau khi tạo).
            ops_session_id = ""
            try:
                page.wait_for_function(f"() => location.href !== {url_before!r}", timeout=10000)
            except Exception:
                pass

            # Chờ trang chi tiết render xong mã phiên (VD MVECCE2US39U trong card
            # bên phải). Cần chờ đủ lâu vì Angular render bất đồng bộ.
            page.wait_for_timeout(4000)
            _save_screenshot(page, "before_extract_session")

            ops_session_id = _extract_session_id(page)

            body = page.inner_text("body")
            if "không có quyền" in body.lower() or "access denied" in body.lower():
                shot = _save_screenshot(page, "no_permission")
                return {"ok": False, "error": "Không có quyền truy cập",
                        "screenshot_file": shot, "codes_entered": entered, "failed_codes": failed_codes}

            # 8) Bấm BÀN GIAO 3PL hoặc BÀN GIAO
            try:
                clicked = _click_first(page, [
                    "button:has-text('BÀN GIAO 3PL')",
                    "button:has-text('Bàn giao 3PL')",
                    "button:has-text('BÀN GIAO')",
                    "button:has-text('Bàn giao')"
                ])
                if clicked:
                    page.wait_for_timeout(2000)
                    # Nếu có popup hỏi xác nhận bàn giao
                    _click_first(page, [
                        "button:has-text('XÁC NHẬN')",
                        "button:has-text('Xác nhận')",
                        "button:has-text('ĐỒNG Ý')",
                        "button:has-text('Đồng ý')"
                    ])
                    page.wait_for_timeout(2000)
            except Exception as click_err:
                print(f"[scan_import] Lỗi khi bấm Bàn giao 3PL: {click_err}")
            # 9) Lấy số lượng thực tế từ giao diện (bởi vì OPS có thể tự động loại bỏ mã trùng)
            actual_entered = entered
            try:
                # Tìm phần tử chứa "Số lượng kiện hàng"
                # Thường OPS hiển thị: "Số lượng kiện hàng bàn giao: 70" hoặc tương tự
                info_text = page.locator("body").inner_text()
                import re
                m_count = re.search(r"(?:Số lượng kiện hàng bàn giao|Số kiện hàng|Số lượng|Tổng số kiện)[\s:]*(\d+)", info_text, re.IGNORECASE)
                if m_count:
                    actual_entered = int(m_count.group(1))
                    
                # Đối chiếu mã xem mã nào bị OPS âm thầm gạch bỏ (silently dropped)
                # Nếu mã không có trong text của trang View, chắc chắn nó đã bị loại!
                for code in codes:
                    if code not in info_text and code not in failed_codes:
                        failed_codes.append(code.strip())
            except Exception as e:
                print(f"[scan_import] Không đọc được số lượng thực tế: {e}")

            _save_screenshot(page, "scan_complete")
            return {"ok": True, "error": "", "ops_session_id": ops_session_id,
                    "codes_entered": actual_entered, "screenshot_file": "", "failed_codes": failed_codes}
        except Exception as e:
            shot = _save_screenshot(page, "scan_error")
            return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:400]}",
                    "screenshot_file": shot, "codes_entered": 0, "failed_codes": failed_codes if 'failed_codes' in locals() else []}
        finally:
            browser.close()


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

            # 3) Bấm nút IMPORT (nút xanh lá góc phải — Nebular nbButton)
            _click_first(page, [
                "button:has-text('IMPORT')",
                "[nbButton]:has-text('IMPORT')",
                "text=/^\\s*IMPORT\\s*$/i",
                ":text('IMPORT')",
            ])
            page.wait_for_timeout(1500)

            # 4) Upload file — set trực tiếp vào input[type='file'] ẩn.
            #    Cách này đáng tin cậy hơn click nút "Chọn file" (có thể là <a>,
            #    <label>, <span> — không phải <button>).
            file_input = page.locator("input[type='file']").first
            if file_input.count() > 0:
                file_input.set_input_files(excel_path)
            else:
                # Fallback: click nút "Chọn file" qua file chooser.
                with page.expect_file_chooser() as fc_info:
                    _click_first(page, [
                        ":text('Chọn file')",
                        "text=/Chọn file/i",
                        "button:has-text('Chọn file')",
                        "a:has-text('Chọn file')",
                        "label:has-text('Chọn file')",
                    ])
                fc = fc_info.value
                fc.set_files(excel_path)
            page.wait_for_timeout(2000)

            # 5) Chờ file upload xử lý xong.
            #    Component ngx-import-file tự upload khi file được set.
            try:
                page.wait_for_selector(
                    "nb-icon[icon='checkmark-circle-2'], table tbody tr, .upload-state-icon",
                    timeout=15000,
                )
            except Exception:
                pass
            page.wait_for_timeout(2000)
            _save_screenshot(page, "after_upload")

            # 5b) Đóng dialog import (nút × góc phải trên) để lộ form chính.
            try:
                close_btn = _find_first(page, [
                    "button.close",
                    "nb-card-header button",
                    ".nb-close",
                    "button:has-text('×')",
                    "a.close",
                ])
                if close_btn:
                    close_btn.click()
                    page.wait_for_timeout(1000)
                else:
                    # Fallback: bấm ESC để đóng dialog.
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(1000)
            except Exception:
                page.keyboard.press("Escape")
                page.wait_for_timeout(1000)

            # 6) Chọn Đối tác vận chuyển (dropdown Nebular nb-select)
            _select_partner(page, partner_name)
            page.wait_for_timeout(500)

            # 7) Bấm TẠO (URL trước khi bấm, để so sánh sau)
            url_before = page.url
            _click_first(page, [
                "button:has-text('TẠO')",
                ":text('TẠO')",
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
            ops_session_id = _extract_session_id(page)

            body = page.inner_text("body")
            if "không có quyền" in body.lower() or "access denied" in body.lower():
                # Chụp cả trường hợp không quyền để Admin xem lại.
                shot = _save_screenshot(page, "no_permission")
                return {"ok": False, "error": "Không có quyền truy cập (kiểm tra tài khoản)",
                        "screenshot_file": shot}
            return {"ok": True, "error": "", "ops_session_id": ops_session_id,
                    "screenshot_file": ""}
        except Exception as e:  # noqa: BLE001
            shot = _save_screenshot(page, "error")
            return {"ok": False,
                    "error": f"{type(e).__name__}: {str(e)[:400]}",
                    "screenshot_file": shot}
        finally:
            browser.close()


def _extract_session_id(page, retries=4) -> str:
    """Đọc mã phiên OPS từ URL hoặc nội dung trang sau khi bấm TẠO."""
    import re
    ops_session_id = ""
    # Pattern chung cho nhiều hãng: 
    # Đa số các mã phiên OPS hiện nay đều bắt đầu bằng MVEC... hoặc CCE...
    pattern = r"(MVEC[A-Z0-9]{4,20}|[A-Z]{2,6}CCE[A-Z0-9]{4,20}|JTE[A-Z0-9]{6,20}|BEX[A-Z0-9]{6,20})"
    
    for _ in range(retries):
        new_url = page.url
        m = re.search(pattern, new_url)
        if m:
            return m.group(1)
            
        try:
            # Đọc toàn bộ chữ trên màn hình (hỗ trợ xuyên Shadow DOM)
            body = page.inner_text("body")
            # Xóa các chữ thường dính liền vào mã phiên do UI (ví dụ badge "Mới", nút "Sao chép")
            body_clean = body.replace("Mới", " ").replace("Sao chép", " ").replace("Copy", " ")
            
            # (?<![A-Za-z]) ngăn match bên trong mã tracking như ORMVEC...
            m2 = re.search(r"(?<![A-Za-z])(" + pattern + r")(?![A-Za-z0-9])", body_clean)
            if m2:
                return m2.group(1)
        except Exception:
            pass
            
        page.wait_for_timeout(1500)
        
    return ""


def _save_screenshot(page, tag: str) -> str:
    """Lưu screenshot vào OPS_LOGS_DIR. Trả về TÊN FILE (không full path)."""
    try:
        os.makedirs(config.OPS_LOGS_DIR, exist_ok=True)
        name = f"ops_{tag}_{int(time.time() * 1000)}.png"
        path = os.path.join(config.OPS_LOGS_DIR, name)
        page.screenshot(path=path, full_page=True)
        return name
    except Exception:  # noqa: BLE001
        return ""


def _login(page):
    """Login vào OPS qua Keycloak SSO (auth.vnfai.com).

    OPS dùng Keycloak: khi chưa login, truy cập OPS sẽ redirect sang
    https://auth.vnfai.com/auth/realms/{tenant}/protocol/openid-connect/auth?...
    Form login Keycloak: input#username, input#password, input#kc-login (submit).
    """
    page.goto(f"{config.OPS_URL}/", wait_until="networkidle")
    page.wait_for_timeout(2000)

    current_url = page.url
    # Nếu URL vẫn trên OPS domain (có hash routing) → đã login.
    if config.OPS_URL.split("//")[1].split("/")[0] in current_url and "auth" not in current_url:
        # Kiểm tra thêm: nếu trang đã render được app (có sidebar/menu) → OK.
        try:
            body = page.inner_text("body")
            if "Log In" not in body and "log in" not in body.lower():
                return  # Đã login thành công, session cookie còn hiệu lực.
        except Exception:
            return

    # Keycloak redirect: URL chuyển sang auth.vnfai.com hoặc tương tự.
    # Đợi form login Keycloak xuất hiện.
    page.wait_for_timeout(1500)

    # Tìm form login Keycloak (input#username hoặc input[name='username']).
    user_input = _find_first(page, [
        "input#username",
        "input[name='username']",
        "input[id='username']",
    ])
    pass_input = _find_first(page, [
        "input#password",
        "input[name='password']",
        "input[type='password']",
    ])

    if user_input and pass_input:
        user_input.fill(config.OPS_USER)
        pass_input.fill(config.OPS_PASS)
        page.wait_for_timeout(300)
        # Nút submit Keycloak: input#kc-login (type=submit, KHÔNG phải <button>).
        _click_first(page, [
            "input#kc-login",
            "input[name='login']",
            "input[type='submit']",
            "button[type='submit']",
            "button:has-text('Log In')",
            "button:has-text('Đăng nhập')",
        ])
        # Chờ redirect về OPS sau login thành công.
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(3000)
    else:
        raise RuntimeError(
            f"Không tìm thấy form login Keycloak. URL hiện tại: {page.url}"
        )


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
    """Mở dropdown 'Đối tác vận chuyển' và chọn option chứa partner_name.

    OPS dùng Nebular UI → dropdown là nb-select (custom component, KHÔNG phải
    native <select>). Mở bằng cách click trigger, rồi chọn option trong overlay.
    """
    _save_screenshot(page, "before_partner")

    # 1) Tìm nb-select chứa label "Đối tác vận chuyển" hoặc nằm gần nó.
    #    Thử nhiều cách tìm trigger.
    selectors_trigger = [
        # Nebular: nb-select hiển thị placeholder hoặc giá trị hiện tại.
        "nb-select",
        ".select-button",
        # Tìm theo label gần: div chứa text + nb-select bên trong.
        "nb-select:near(:text('vận chuyển'), 300)",
    ]
    clicked = False
    for sel in selectors_trigger:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                loc.click(timeout=5000)
                page.wait_for_timeout(600)
                clicked = True
                break
        except Exception:
            continue

    if not clicked:
        # Fallback: click phần tử có text gợi ý dropdown.
        try:
            _click_first(page, [
                ":text('Chọn đối tác')",
                ":text('đối tác vận chuyển')",
                "[placeholder*='đối tác']",
            ])
            page.wait_for_timeout(600)
            clicked = True
        except Exception:
            pass

    if not clicked:
        _save_screenshot(page, "partner_not_found")
        raise RuntimeError(f"Không tìm thấy dropdown 'Đối tác vận chuyển'")

    _save_screenshot(page, "partner_dropdown_open")

    # 2) Chọn option trong overlay.
    option_selectors = [
        f"nb-option:has-text('{partner_name}')",
        f".option-list nb-option:has-text('{partner_name}')",
        f"nb-option:text-is('{partner_name}')",
        f":text('{partner_name}')",
    ]
    for sel in option_selectors:
        try:
            opt = page.locator(sel).first
            if opt.count() > 0:
                opt.click(timeout=5000)
                return
        except Exception:
            continue

    # 3) Nếu không match chính xác, tìm option chứa từ khoá.
    #    VD partner_name = "J&T Express" → tìm option có chứa "J&T" hoặc "JT".
    keyword = partner_name.split()[0].replace("&", "")  # "J&T" → "JT"
    try:
        opts = page.locator("nb-option").all()
        for opt in opts:
            text = opt.inner_text().strip()
            if keyword.lower() in text.lower().replace("&", "") or partner_name.lower() in text.lower():
                opt.click(timeout=3000)
                return
    except Exception:
        pass

    _save_screenshot(page, "partner_option_not_found")
    raise RuntimeError(
        f"Không chọn được đối tác '{partner_name}'. "
        f"Kiểm tra screenshot trong ops_logs/ để xem dropdown."
    )
