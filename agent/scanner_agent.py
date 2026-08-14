#!/usr/bin/env python3
"""Scanner Agent — chạy ngầm, bắt mã vận đơn TOÀN CỤC rồi đẩy lên server.

Nguyên lý: máy quét mã vạch 2D hoạt động như bàn phím — nó "gõ" nội dung mã
rồi nhấn Enter. Agent này nghe bàn phím ở tầng hệ điều hành (pynput) nên bắt
được mã DÙ con trỏ đang ở ứng dụng nào (giải quyết bài toán quên focus ô nhập).

Phân biệt "quét" với "gõ tay": máy quét gõ rất nhanh (~10-30ms/ký tự). Nếu các
ký tự đến liên tiếp trong ngưỡng thời gian rất ngắn và kết thúc bằng Enter →
coi là 1 mã quét. Gõ tay chậm hơn nhiều nên bị loại.

Có: system tray icon, tiếng bíp (thành công/trùng), hàng đợi offline (gửi lại
khi có mạng) để không mất mã.
"""
import configparser
import os
import queue
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import requests

try:
    from pynput import keyboard
except Exception as exc:  # noqa: BLE001
    print("Thiếu pynput. Chạy: pip install -r requirements.txt")
    raise

# pystray + Pillow là tuỳ chọn (icon khay). Không có vẫn chạy được ở chế độ console.
try:
    import pystray
    from PIL import Image, ImageDraw
    HAS_TRAY = True
except Exception:  # noqa: BLE001
    HAS_TRAY = False

# ImageTk để hiển thị logo ĐVVC trong tkinter (tuỳ chọn).
try:
    from PIL import Image as _PILImage, ImageTk
    HAS_IMAGETK = True
except Exception:  # noqa: BLE001
    HAS_IMAGETK = False

# tkinter (giao diện cửa sổ) — có sẵn trong Python chuẩn trên Windows.
try:
    import tkinter as tk
    from tkinter import ttk
    HAS_GUI = True
except Exception:  # noqa: BLE001
    HAS_GUI = False


def _app_dir():
    """Thư mục chứa config.ini / queue.db.

    - Khi chạy dạng .exe (PyInstaller): lấy thư mục CHỨA file .exe, để người dùng
      sửa config.ini cạnh .exe được (không phải thư mục temp bundle).
    - Khi chạy .py bình thường: lấy thư mục chứa script.
    """
    if getattr(sys, "frozen", False):  # đang chạy trong bundle PyInstaller
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


# ----------------------------- Phiên bản & Auto Update -----------------------------
def _read_version() -> str:
    """Đọc số version từ file VERSION (nguồn duy nhất, khớp GitHub Actions tag).

    Thứ tự tìm:
      1. sys._MEIPASS/VERSION — khi chạy .exe PyInstaller (bundle qua spec).
      2. dirname(__file__)/VERSION — khi chạy trực tiếp `python scanner_agent.py`.
      3. _app_dir()/VERSION — fallback (thư mục cạnh .exe).
    Thiếu file → 0.0.0 (chắc chắn được coi là cũ, tự update lần check kế).
    """
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(os.path.join(meipass, "VERSION"))
    candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION"))
    candidates.append(os.path.join(_app_dir(), "VERSION"))
    for p in candidates:
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    return f.read().strip() or "0.0.0"
            except Exception:
                pass
    return "0.0.0"


CURRENT_VERSION = _read_version()


# Múi giờ Việt Nam để hiển thị thời gian từ server (server lưu UTC).
VN_TZ = timezone(timedelta(hours=7))


def _now_vn(fmt: str = "%H:%M:%S") -> str:
    """Giờ hiện tại theo VN (+7), KHÔNG phụ thuộc timezone máy local.

    Máy Windows đặt sai timezone (VD +8, UTC) vẫn hiển thị đúng giờ VN.
    """
    return datetime.now(timezone.utc).astimezone(VN_TZ).strftime(fmt)


def _fmt_vn_time(iso_str: str, fmt: str = "%H:%M:%S") -> str:
    """Parse ISO UTC string từ server -> hiển thị giờ VN (+7).

    - Nếu chuỗi có timezone info (có +00:00, Z, +07:00) -> parse chính xác.
    - Nếu không có tz -> giả định UTC (backend luôn dùng UTC).
    - Lỗi parse -> fallback về cắt chuỗi thô.
    """
    if not iso_str:
        return ""
    try:
        # datetime.fromisoformat hỗ trợ +00:00 nhưng không hỗ trợ 'Z' trên Py<3.11.
        s = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(VN_TZ).strftime(fmt)
    except Exception:
        # Fallback thô nếu ISO malformed.
        return iso_str[11:19] if len(iso_str) >= 19 else iso_str


def _agent_root_dir() -> str:
    """Thư mục gốc chứa source code py (thường là parent của dist/ khi chạy dạng .exe).

    - Khi chạy dạng .exe (sys.frozen = True): sys.executable nằm tại D:/Tool/agent/dist/ScanEcomAgent.exe.
      Parent của dist là D:/Tool/agent (thư mục chứa source code py, build_exe.ps1...).
    - Khi chạy .py bình thường: lấy thư mục chứa script (D:/Tool/agent).
    """
    if getattr(sys, "frozen", False):
        exec_dir = os.path.dirname(sys.executable)
        parent_dir = os.path.dirname(exec_dir)
        if os.path.exists(os.path.join(parent_dir, "build_exe.ps1")) or os.path.exists(os.path.join(parent_dir, "scanner_agent.py")):
            return parent_dir
        return exec_dir
    return os.path.dirname(os.path.abspath(__file__))


def _parse_version(v_str: str) -> tuple[int, ...]:
    """Parse version string '1.1.0' -> (1, 1, 0) để so sánh."""
    import re
    parts = re.findall(r"\d+", str(v_str))
    return tuple(int(p) for p in parts) if parts else (0,)


def _update_log_path() -> str:
    """File log cạnh .exe cho quá trình auto-update. User dễ tìm khi debug."""
    return os.path.join(_app_dir(), "update.log")


def _upd_log(msg: str) -> None:
    """Ghi 1 dòng vào update.log kèm timestamp VN. In ra stdout nếu console."""
    try:
        stamp = _now_vn("%Y-%m-%d %H:%M:%S")
    except Exception:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{stamp}] {msg}\n"
    try:
        with open(_update_log_path(), "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass
    try:
        print(line, end="")
    except Exception:
        pass


def check_and_apply_update(server_url: str, api_key: str) -> bool:
    """Kiểm tra và thực hiện cập nhật tự động mã nguồn từ server.

    1. GET /api/agent/version -> Lấy latest_version.
    2. So sánh nếu latest_version > CURRENT_VERSION:
       - Tải file zip mã nguồn từ download_url.
       - Giải nén đè trực tiếp các file code mới vào _agent_root_dir() (D:/Tool/agent/).
         KHÔNG đụng vào dist/ (đảm bảo dist/config.ini và dist/queue.db nguyên vẹn).
       - Sinh updater.bat tự động chạy build_exe.ps1 để build lại .exe vào dist/ScanEcomAgent.exe và bật lại.
       - Thoát ứng dụng hiện tại.
    """
    import shutil
    import subprocess
    import tempfile
    import zipfile

    try:
        _upd_log("=" * 60)
        _upd_log(f"BAT DAU auto-update. CURRENT_VERSION={CURRENT_VERSION}")
        _upd_log(f"  sys.executable = {sys.executable}")
        _upd_log(f"  sys.frozen = {getattr(sys, 'frozen', False)}")
        _upd_log(f"  _MEIPASS = {getattr(sys, '_MEIPASS', 'N/A')}")
        _upd_log(f"  _app_dir() = {_app_dir()}")
        _upd_log(f"  _agent_root_dir() = {_agent_root_dir()}")

        url = f"{server_url}/api/agent/version"
        headers = {"X-API-Key": api_key}
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code != 200:
            _upd_log(f"HTTP {res.status_code} tu {url} -> abort")
            return False

        data = res.json()
        latest_ver = data.get("latest_version", "1.0.0")
        download_url = data.get("download_url")
        _upd_log(f"Server tra: latest_version={latest_ver}, download_url={download_url}")

        if not download_url or _parse_version(latest_ver) <= _parse_version(CURRENT_VERSION):
            _upd_log(f"Da la ban moi nhat hoac hon. Khong update.")
            return False

        # Anti-loop: nếu vừa update sang latest_ver trong 10 phút mà agent vẫn
        # thấy mình cũ hơn -> chắc chắn bundle sai (MEIPASS thiếu VERSION hoặc
        # spec cũ). Bỏ qua để user chú ý, không loop vô hạn.
        marker = os.path.join(tempfile.gettempdir(), f"scanecom_upd_{latest_ver}.mark")
        try:
            if os.path.exists(marker):
                age = time.time() - os.path.getmtime(marker)
                if age < 600:  # 10 phút
                    _upd_log(f"SKIP (anti-loop): vua update sang v{latest_ver} cach {int(age)}s "
                             f"nhung CURRENT_VERSION van la v{CURRENT_VERSION}. Nghi build hong "
                             f"bundle VERSION. Xoa {marker} de thu lai.")
                    return False
        except Exception:
            pass
        # Đánh dấu để lần sau detect loop.
        try:
            with open(marker, "w") as f:
                f.write(f"{latest_ver}\n{CURRENT_VERSION}\n{time.time()}")
        except Exception:
            pass

        _upd_log(f"Phat hien v{latest_ver} > v{CURRENT_VERSION}. Bat dau tai zip...")

        dl_full_url = download_url if download_url.startswith("http") else f"{server_url}{download_url}"
        z_res = requests.get(dl_full_url, headers=headers, timeout=60)
        if z_res.status_code != 200:
            _upd_log(f"Loi tai zip: HTTP {z_res.status_code}")
            return False
        _upd_log(f"Tai zip xong: {len(z_res.content)} bytes")

        temp_dir = tempfile.mkdtemp(prefix="scanecom_update_")
        zip_path = os.path.join(temp_dir, "agent_src.zip")
        with open(zip_path, "wb") as f:
            f.write(z_res.content)

        extract_dir = os.path.join(temp_dir, "extracted")
        os.makedirs(extract_dir, exist_ok=True)
        with zipfile.ZipFile(zip_path, "r") as zip_ref:
            zip_ref.extractall(extract_dir)

        # Verify VERSION trong zip
        zip_ver_path = os.path.join(extract_dir, "VERSION")
        if os.path.exists(zip_ver_path):
            try:
                with open(zip_ver_path, "r", encoding="utf-8") as f:
                    zip_ver = f.read().strip()
                _upd_log(f"VERSION trong zip = {zip_ver}")
                if zip_ver != latest_ver:
                    _upd_log(f"CANH BAO: VERSION trong zip ({zip_ver}) KHAC latest_version API ({latest_ver})!")
            except Exception as e:
                _upd_log(f"Loi doc VERSION trong zip: {e}")
        else:
            _upd_log("CANH BAO: KHONG co file VERSION trong zip!")

        target_root = _agent_root_dir()
        _upd_log(f"Giai nen & ghi de vao target_root = {target_root}")

        copied_files = []
        for item in os.listdir(extract_dir):
            src_item = os.path.join(extract_dir, item)
            dst_item = os.path.join(target_root, item)

            # Bỏ qua thư mục dist và file config/db của client
            if item.lower() in ("dist", ".venv", "config.ini", "queue.db"):
                _upd_log(f"  SKIP: {item}")
                continue

            try:
                if os.path.isdir(src_item):
                    if os.path.exists(dst_item):
                        shutil.rmtree(dst_item, ignore_errors=True)
                    shutil.copytree(src_item, dst_item)
                    copied_files.append(f"dir:{item}")
                else:
                    shutil.copy2(src_item, dst_item)
                    copied_files.append(item)
            except Exception as e:
                _upd_log(f"  LOI copy {item}: {e}")

        _upd_log(f"Copy xong {len(copied_files)} items: {', '.join(copied_files)}")

        # Verify source VERSION sau khi copy
        src_ver_path = os.path.join(target_root, "VERSION")
        if os.path.exists(src_ver_path):
            try:
                with open(src_ver_path, "r", encoding="utf-8") as f:
                    src_ver = f.read().strip()
                _upd_log(f"target_root/VERSION sau khi copy = {src_ver}")
            except Exception as e:
                _upd_log(f"Loi doc target_root/VERSION: {e}")
        else:
            _upd_log(f"CANH BAO: target_root/VERSION KHONG ton tai sau copy!")

        bat_path = os.path.join(tempfile.gettempdir(), "scanecom_updater.bat")
        current_pid = os.getpid()
        if getattr(sys, "frozen", False):
            exec_path = sys.executable
            exec_name = os.path.basename(exec_path)
            # LƯU Ý: taskkill /F /IM chỉ giết đúng file .exe cùng tên. Vòng lặp
            # để chắc chắn KHÔNG có instance nào của agent còn sót — kể cả các
            # bản mở trước đó cũng bị dọn (tránh multi-instance chồng nhau).
            # rf""" = raw + f-string: mọi \ giữ nguyên, không escape.
            bat_script = rf"""@echo off
chcp 65001 > nul
echo [UPDATE] Dang dung TAT CA instance {exec_name} cu (PID hien tai: {current_pid})...
:killloop
tasklist /FI "IMAGENAME eq {exec_name}" 2>NUL | find /I "{exec_name}" >NUL
if not errorlevel 1 (
    taskkill /F /IM "{exec_name}" >NUL 2>&1
    timeout /t 1 /nobreak > nul
    goto killloop
)
echo [UPDATE] Da dong het instance cu.

cd /d "{target_root}"

echo [UPDATE] Dang tu dong build lai {exec_name} tu code source py moi...
powershell -ExecutionPolicy Bypass -File build_exe.ps1
if errorlevel 1 (
    echo [ERROR] Build that bai. Khong khoi dong lai. Kiem update.log de biet chi tiet.
    pause
    exit /b 1
)

REM Build ra tai target_root\dist\exec_name. Nhung .exe GOC dang chay co the
REM o cho khac (VD user chep .exe portable). Copy .exe vua build DE LEN dung
REM cho file goc de restart chay ban moi.
set "BUILT_EXE={target_root}\dist\{exec_name}"
set "ORIG_EXE={exec_path}"

if not exist "%BUILT_EXE%" (
    echo [ERROR] Khong tim thay .exe sau build: %BUILT_EXE%
    pause
    exit /b 1
)

if /I not "%BUILT_EXE%"=="%ORIG_EXE%" (
    echo [UPDATE] Copy .exe moi de len file goc: %ORIG_EXE%
    copy /Y "%BUILT_EXE%" "%ORIG_EXE%" >NUL
    if errorlevel 1 (
        echo [ERROR] Copy that bai. Khoi dong ban build tam thoi tai %BUILT_EXE%.
        start "" "%BUILT_EXE%"
        del "%~f0"
        exit /b 0
    )
)

echo [UPDATE] Build thanh cong! Dang khoi dong lai Agent tai %ORIG_EXE%...
start "" "%ORIG_EXE%"

del "%~f0"
"""
        else:
            py_executable = sys.executable
            main_script = os.path.join(target_root, "scanner_agent.py")
            bat_script = f"""@echo off
chcp 65001 > nul
echo [UPDATE] Dang dung Python agent cu (PID {current_pid})...
taskkill /F /PID {current_pid} >NUL 2>&1
timeout /t 2 /nobreak > nul

cd /d "{target_root}"
echo [UPDATE] Dang khoi dong lai Agent Python v{latest_ver}...
start "" "{py_executable}" "{main_script}"
del "%~f0"
"""

        with open(bat_path, "w", encoding="utf-8") as f:
            f.write(bat_script)

        subprocess.Popen(["cmd.exe", "/c", bat_path], creationflags=subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0)
        print(f"[AutoUpdate] Đã khởi chạy updater. Agent v{CURRENT_VERSION} sẽ bị taskkill từ updater.")
        # KHÔNG dùng sys.exit(0) — nó chỉ raise SystemExit ở thread hiện tại,
        # tk mainloop + pystray + pynput listener trên các thread khác vẫn sống.
        # Dùng os._exit(0) để terminate CẢ process ngay lập tức, an toàn vì
        # updater.bat cũng sẽ taskkill để phòng trường hợp bản .exe khác cấu
        # trúc thread khiến _exit không kịp kill hết.
        os._exit(0)
        return True

    except Exception as e:
        print(f"[AutoUpdate] Lỗi nâng cấp: {e}")
        return False



HERE = _app_dir()
CONFIG_PATH = os.path.join(HERE, "config.ini")
QUEUE_DB = os.path.join(HERE, "queue.db")
LOGOS_DIR = os.path.join(HERE, "logos")


def _slugify_carrier(name: str) -> str:
    """Đổi tên ĐVVC -> tên file logo: thường, bỏ dấu VN, thay ký tự lạ bằng '-'.

    Ví dụ: "Best Express"->"best-express", "J&T"->"jt", "Viettel Post"->"viettel-post".
    """
    import re
    import unicodedata
    s = unicodedata.normalize("NFD", name)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")  # bỏ dấu
    s = s.lower().replace("đ", "d")
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s


def find_logo_path(carrier: str):
    """Tìm file logo cho ĐVVC trong thư mục logos/ (thử png/jpg). None nếu không có."""
    slug = _slugify_carrier(carrier)
    for ext in (".png", ".jpg", ".jpeg", ".webp"):
        p = os.path.join(LOGOS_DIR, slug + ext)
        if os.path.exists(p):
            return p
    return None


# ----------------------------- Cấu hình -----------------------------
def load_config():
    if not os.path.exists(CONFIG_PATH):
        print(f"Chưa có {CONFIG_PATH}. Hãy copy config.ini.example -> config.ini và điền.")
        sys.exit(1)
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_PATH, encoding="utf-8")
    return {
        "url": cfg.get("server", "url").rstrip("/"),
        "api_key": cfg.get("server", "api_key"),
        "name": cfg.get("agent", "name", fallback="scanner"),
        "show_window": cfg.getboolean("agent", "show_window", fallback=True),
        "inter_key_timeout": cfg.getfloat("scanner", "inter_key_timeout", fallback=0.05),
        "min_length": cfg.getint("scanner", "min_length", fallback=6),
        "beep": cfg.getboolean("scanner", "beep", fallback=True),
    }


# ----------------------------- Tiếng bíp / cảnh báo -----------------------------
def beep(kind: str):
    """kind: 'ok' | 'dup' | 'err'. Best-effort, khác nhau để phân biệt bằng tai.

    'dup' (mã TRÙNG sau >1 phút) kêu to/dài/lặp để nhân viên nghe rõ ngay tại máy.
    """
    try:
        if sys.platform.startswith("win"):
            import winsound
            if kind == "ok":
                winsound.Beep(1200, 90)
            elif kind == "dup":
                # Cảnh báo trùng: 3 hồi trầm dài, dễ nhận biết.
                for _ in range(3):
                    winsound.Beep(700, 250)
                    time.sleep(0.06)
            else:
                winsound.Beep(300, 300)
        elif sys.platform == "darwin":
            # macOS: dùng âm thanh hệ thống. Trùng -> gọi 'afplay' âm cảnh báo 3 lần.
            if kind == "dup":
                for _ in range(3):
                    os.system("afplay /System/Library/Sounds/Sosumi.aiff >/dev/null 2>&1 || printf '\\a'")
            else:
                os.system("afplay /System/Library/Sounds/Tink.aiff >/dev/null 2>&1 || printf '\\a'")
        else:
            # Linux: ký tự chuông (lặp cho 'dup').
            n = 3 if kind == "dup" else 1
            for _ in range(n):
                sys.stdout.write("\a"); sys.stdout.flush(); time.sleep(0.15)
    except Exception:  # noqa: BLE001
        pass


# ----------------------------- Hàng đợi offline -----------------------------
class OfflineQueue:
    """Lưu mã vào SQLite khi mất mạng, gửi lại khi có mạng — không mất mã.

    Từ 2026-08: thêm cột basket_seq để khi gửi lại vẫn đúng sọt agent đang quét
    lúc lỡ mất mạng.
    """

    def __init__(self, path):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS pending ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, scanned_at TEXT, "
            "basket_seq INTEGER NOT NULL DEFAULT 0)"
        )
        # Migration nhẹ: thêm cột basket_seq nếu bản cũ chưa có.
        try:
            cols = [r[1] for r in self._conn.execute("PRAGMA table_info(pending)")]
            if "basket_seq" not in cols:
                self._conn.execute("ALTER TABLE pending ADD COLUMN basket_seq INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass
        self._conn.commit()

    def add(self, code, scanned_at, basket_seq=0):
        with self._lock:
            self._conn.execute(
                "INSERT INTO pending(code, scanned_at, basket_seq) VALUES (?, ?, ?)",
                (code, scanned_at, int(basket_seq or 0))
            )
            self._conn.commit()

    def all(self):
        with self._lock:
            return self._conn.execute(
                "SELECT id, code, scanned_at, basket_seq FROM pending ORDER BY id"
            ).fetchall()

    def remove(self, row_id):
        with self._lock:
            self._conn.execute("DELETE FROM pending WHERE id = ?", (row_id,))
            self._conn.commit()

    def count(self):
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM pending").fetchone()[0]


# ----------------------------- Gửi lên server -----------------------------
class Sender:
    def __init__(self, cfg, oq: OfflineQueue):
        self.cfg = cfg
        self.oq = oq
        self.session = requests.Session()
        self.session.headers.update({"X-API-Key": cfg["api_key"]})

    def _post(self, code, scanned_at, basket_seq=None):
        """Trả 'ok' | 'ignored' | 'dup' | 'net' | 'err'.

        - ok      : mã mới, đã lưu (201).
        - ignored : quét lại trong <1 phút -> server bỏ qua êm (201, status ignored).
        - dup     : quét lại sau >1 phút -> mã TRÙNG (409) -> cảnh báo tại máy.

        `basket_seq`: agent tự quản. Có -> server gán ngay basket_id cho mã này.
        """
        payload = {"code": code, "scanned_at": scanned_at, "source_agent": self.cfg["name"]}
        if basket_seq:
            payload["basket_seq"] = int(basket_seq)
        try:
            r = self.session.post(
                self.cfg["url"] + "/api/scans",
                json=payload,
                timeout=8,
            )
        except requests.RequestException:
            return "net"
        if r.status_code == 201:
            try:
                if r.json().get("status") == "ignored":
                    return "ignored"
            except Exception:  # noqa: BLE001
                pass
            return "ok"
        if r.status_code == 409:
            return "dup"
        return "err"

    def send(self, code, basket_seq=None):
        """Gửi 1 mã kèm basket_seq. Nếu mất mạng -> đưa vào hàng đợi offline."""
        scanned_at = datetime.now(timezone.utc).isoformat()
        result = self._post(code, scanned_at, basket_seq=basket_seq)
        if result == "net":
            # Lưu cả basket_seq để khi gửi lại vẫn đúng sọt.
            self.oq.add(code, scanned_at, basket_seq=basket_seq or 0)
            print(f"[offline] Mất mạng, xếp hàng: {code} (chờ gửi lại)")
        return result

    def get_next_basket_seq(self):
        """Sync với server: sọt kế tiếp = sọt cuối +1 (mặc định 1)."""
        try:
            r = self.session.get(self.cfg["url"] + "/api/baskets/next",
                                 params={"agent_name": self.cfg["name"]}, timeout=5)
            if r.status_code == 200:
                return int(r.json().get("current_seq", 1))
        except requests.RequestException:
            pass
        return 1

    def get_summary_today(self):
        """Lấy thống kê HÔM NAY từ server (chỉ lấy order của các hãng)."""
        try:
            r = self.session.get(self.cfg["url"] + "/api/summary?period=day", timeout=8)
            if r.status_code == 200:
                return r.json()
        except requests.RequestException:
            pass
        return None

    def get_current_basket(self):
        """Lấy thống kê sọt HIỆN TẠI của máy này từ server."""
        try:
            r = self.session.get(self.cfg["url"] + "/api/baskets/current", 
                                 params={"agent_name": self.cfg["name"]}, timeout=8)
            if r.status_code == 200:
                return r.json()
        except requests.RequestException:
            pass
        return None

    def get_sessions_today(self):
        """Danh sách bàn giao hôm nay theo (Sọt, ĐVVC) - filter theo máy này."""
        try:
            r = self.session.get(
                self.cfg["url"] + "/api/ops/sessions",
                params={"agent_name": self.cfg["name"]},
                timeout=8,
            )
            if r.status_code == 200:
                return r.json().get("items", [])
        except requests.RequestException:
            pass
        return None

    def import_basket(self, basket_id: int):
        """Bàn giao sọt cụ thể lên OPS."""
        try:
            r = self.session.post(self.cfg["url"] + f"/api/ops/import-basket?basket_id={basket_id}", timeout=15)
            if r.status_code == 200:
                return r.json()
        except requests.RequestException:
            pass
        return None

    def import_now(self, carrier: str):
        """Kích hoạt đẩy thủ công đơn lên OPS cho 1 ĐVVC. (API: /api/ops/import-now)."""
        try:
            url = f"{self.cfg['url']}/api/ops/import-now?carrier={carrier}&limit=100&require_picked=true"
            r = self.session.post(url, timeout=45)
            if r.status_code == 200:
                return r.json()
            return {"status": "error", "error": f"HTTP {r.status_code}"}
        except Exception as e:
            return {"status": "error", "error": str(e)}
        return None

    def close_basket(self, basket_seq=None):
        """Chốt 1 sọt cho máy này. Nếu gửi basket_seq -> chốt đúng sọt đó (agent-side)."""
        try:
            params = {"agent_name": self.cfg["name"]}
            if basket_seq:
                params["basket_seq"] = int(basket_seq)
            r = self.session.post(self.cfg["url"] + "/api/baskets/close",
                                  params=params, timeout=10)
            if r.status_code == 200:
                return r.json()
        except requests.RequestException:
            pass
        return None

    def list_baskets_today(self):
        """Danh sách sọt hôm nay của máy này. Trả list, hoặc None nếu lỗi."""
        try:
            r = self.session.get(self.cfg["url"] + "/api/baskets",
                                 params={"agent_name": self.cfg["name"], "period": "day"},
                                 timeout=8)
            if r.status_code == 200:
                return r.json().get("items", [])
        except requests.RequestException:
            pass
        return None

    def flush_loop(self):
        """Chạy nền: định kỳ gửi lại các mã đang xếp hàng khi có mạng."""
        while True:
            time.sleep(5)
            rows = self.oq.all()
            for row_id, code, scanned_at, basket_seq in rows:
                res = self._post(code, scanned_at, basket_seq=basket_seq if basket_seq else None)
                if res in ("ok", "ignored", "dup", "err"):
                    # mọi kết quả trừ 'net' đều coi là đã xử lý -> bỏ khỏi hàng đợi.
                    self.oq.remove(row_id)
                    print(f"[flush] Gửi lại {code} (sọt {basket_seq}): {res}")
                else:
                    break  # vẫn mất mạng -> để lần sau


# ----------------------------- Bắt mã toàn cục -----------------------------
class ScanCatcher:
    """Gom ký tự do máy quét gõ; kết thúc ở Enter.

    NHIỀU MÁY QUÉT trên cùng 1 máy tính (không cần phân biệt nguồn):
    - Mọi máy quét đều "gõ" vào chung dòng bàn phím của Windows; agent nghe chung
      nên nhận được mã từ TẤT CẢ máy quét, gộp vào cùng 1 danh sách. OK cho nhu cầu này.
    - Rủi ro: nếu 2 người bấm quét gần như ĐỒNG THỜI, ký tự 2 mã có thể xen kẽ ->
      tạo mã rác. Giảm rủi ro bằng 2 cơ chế:
        1) Mỗi máy quét tự kết thúc mã bằng Enter -> flush ngay khi gặp Enter.
        2) Nếu buffer có ký tự nhưng im lặng quá lâu (mất Enter / xen kẽ dở dang)
           -> tự bỏ buffer để không dính sang mã kế tiếp.
      Trong thực tế nên nhắc nhân viên quét LẦN LƯỢT (chênh nhau >0.2s) là an toàn tuyệt đối.
    """

    def __init__(self, cfg, on_code):
        self.cfg = cfg
        self.on_code = on_code
        self.buffer = []
        self.last_time = 0.0
        self.enabled = True
        # Ngưỡng tự-bỏ buffer khi mất Enter (giây). Lớn hơn nhiều so với tốc độ quét.
        self.stale_timeout = max(0.5, cfg["inter_key_timeout"] * 20)

    def _flush(self):
        code = "".join(self.buffer).strip()
        self.buffer = []
        if code and len(code) >= self.cfg["min_length"]:
            self.on_code(code)

    def on_press(self, key):
        if not self.enabled:
            return
        now = time.monotonic()
        gap = now - self.last_time
        self.last_time = now

        # Ký tự đến sau một khoảng nghỉ dài (gõ tay lạc, hoặc mã trước mất Enter)
        # -> coi buffer cũ là rác, bắt đầu mã mới sạch sẽ.
        if gap > self.stale_timeout and self.buffer:
            self.buffer = []

        try:
            if key == keyboard.Key.enter:
                self._flush()
                return
            ch = key.char  # ký tự in được
            if ch is not None:
                self.buffer.append(ch)
        except AttributeError:
            # phím đặc biệt (shift, ctrl…) -> bỏ qua
            pass


# ----------------------------- Tray icon -----------------------------
def make_icon_image(color):
    img = Image.new("RGB", (64, 64), (30, 41, 59))
    d = ImageDraw.Draw(img)
    # vẽ vài vạch như barcode
    for i, x in enumerate(range(10, 54, 6)):
        w = 3 if i % 2 == 0 else 2
        d.rectangle([x, 12, x + w, 52], fill=color)
    return img


def run_tray(state):
    """state: dict chia sẻ để điều khiển agent."""
    def toggle(icon, item):
        state["catcher"].enabled = not state["catcher"].enabled
        icon.icon = make_icon_image((34, 197, 94) if state["catcher"].enabled else (239, 68, 68))
        icon.title = "Scan Ecom — " + ("Đang bật" if state["catcher"].enabled else "Tạm dừng")

    def open_web(icon, item):
        import webbrowser
        webbrowser.open(state["cfg"]["url"])

    def show_status(icon, item):
        n = state["oq"].count()
        icon.notify(f"Đang chờ gửi lại: {n} mã", "Scan Ecom")

    def open_window(icon, item):
        win = state.get("window")
        if win is not None:
            # gọi trên thread GUI cho an toàn
            win.root.after(0, win.show)

    def quit_app(icon, item):
        icon.stop()
        os._exit(0)

    items = []
    if state.get("window") is not None:
        items.append(pystray.MenuItem("Mở cửa sổ", open_window, default=True))
    items += [
        pystray.MenuItem(lambda item: "Tạm dừng" if state["catcher"].enabled else "Bật lại", toggle),
        pystray.MenuItem("Mở web quản lý", open_web),
        pystray.MenuItem("Trạng thái hàng đợi", show_status),
        pystray.MenuItem("Thoát", quit_app),
    ]
    menu = pystray.Menu(*items)
    icon = pystray.Icon("scan_ecom", make_icon_image((34, 197, 94)), "Scan Ecom — Đang bật", menu)
    state["tray_icon"] = icon
    icon.run()


# ----------------------------- Cửa sổ giao diện (tkinter) -----------------------------
class AgentWindow:
    """Cửa sổ trên máy quét: TRÁI (~1/3) danh sách mã vừa quét, PHẢI (~2/3) thống
    kê số lượng theo ĐVVC trong NGÀY (lấy từ server, tự làm mới định kỳ).

    tkinter phải chạy trên MAIN THREAD. Thread quét đẩy sự kiện vào hàng đợi;
    việc gọi server lấy summary chạy ở thread nền để không treo giao diện.
    """

    COLORS = {
        "ok": "#16a34a", "ignored": "#f59e0b", "dup": "#ef4444",
        "err": "#ef4444", "net": "#3b82f6",
    }
    # Chỉ icon, không chữ. Xanh ✓ = OK, vàng • = quét lại <1p, đỏ ✖ = trùng.
    LABELS = {
        "ok": "✔", "ignored": "•", "dup": "✖",
        "err": "!", "net": "⇄",
    }
    BG = "#0f172a"
    PANEL = "#1e293b"

    def __init__(self, cfg, state, event_queue):
        self.cfg = cfg
        self.state = state
        self.q = event_queue
        self.count_session = 0
        self._summary = None          # summary sọt hiện tại (dict) từ server
        self._summary_today = None    # summary toàn bộ hôm nay
        self._summary_lock = threading.Lock()
        self._kpi_widgets = {}        # tên ĐVVC -> label giá trị
        self._logo_imgs = {}          # tên ĐVVC -> ImageTk (cache logo)
        self._sessions = {}           # by_carrier: {carrier: [{session_id,count,...}]}
        self._last_session_ids = set()  # để phát hiện phiên mới, hiện thông báo
        self._baskets = []            # danh sách sọt hôm nay của máy này
        # Số sọt hiện tại (agent-side): mọi mã quét sẽ tự gán vào sọt này.
        # Sync với server lúc khởi động (sọt cuối +1, fallback 1).
        self.current_basket_seq = 1
        try:
            self.current_basket_seq = state["sender"].get_next_basket_seq()
            print(f"[startup] Sync với server: sọt hiện tại = {self.current_basket_seq}")
        except Exception as e:
            print(f"[startup] Không sync được sọt (dùng seq=1): {e}")

        self.root = tk.Tk()
        self.root.title(f"Scan Ecom — Máy quét (v{CURRENT_VERSION})")
        self.root.geometry("1280x760")
        self.root.configure(bg=self.BG)
        self.root.minsize(1100, 640)

        # ---- Header ----
        top = tk.Frame(self.root, bg=self.PANEL)
        top.pack(fill="x")
        tk.Label(top, text="📦 Scan Ecom", fg="#e2e8f0", bg=self.PANEL,
                 font=("Segoe UI", 14, "bold")).pack(side="left", padx=12, pady=10)
        # Label nổi bật: đang quét vào sọt số mấy
        self.current_basket_lbl = tk.Label(
            top, text=f"🟢 Đang quét vào Sọt {self.current_basket_seq}",
            fg="#4ade80", bg=self.PANEL, font=("Segoe UI", 12, "bold"))
        self.current_basket_lbl.pack(side="left", padx=20)

        self.status_lbl = tk.Label(top, text="● Đang kết nối…", fg="#94a3b8", bg=self.PANEL,
                                   font=("Segoe UI", 10))
        self.status_lbl.pack(side="right", padx=12)
        # Nút "Kiểm tra cập nhật" nằm ngay cạnh label version (bên phải label).
        # Pack theo thứ tự right→left: nút pack sau sẽ nằm BÊN TRÁI cái pack trước,
        # nên để nút cùng padx=0 rồi đến label version.
        self.btn_check_update = tk.Button(
            top, text="🔄 Kiểm tra cập nhật", command=self._check_update_now,
            bg="#0f172a", fg="#93c5fd", relief="flat", cursor="hand2",
            font=("Segoe UI", 9), padx=8, pady=2,
            activebackground="#1e293b", activeforeground="#dbeafe",
        )
        self.btn_check_update.pack(side="right", padx=(0, 12))
        tk.Label(top, text=f"Máy: {cfg['name']} | v{CURRENT_VERSION}", fg="#94a3b8", bg=self.PANEL,
                 font=("Segoe UI", 9)).pack(side="right", padx=(12, 4))

        # ---- Thân: 3 cột (mã 1/8, KPI 4/8, bàn giao/sọt 3/8) ----
        body = tk.Frame(self.root, bg=self.BG)
        body.pack(fill="both", expand=True, padx=12, pady=10)
        # weight + minsize để cột KHÔNG NHẢY khi nội dung thay đổi (VD switch
        # tab "Sọt hôm nay" <-> "Mã phiên OPS" có nhiều cột khác nhau).
        body.columnconfigure(0, weight=1, minsize=140)   # trái: danh sách mã vừa quét
        body.columnconfigure(1, weight=4, minsize=520)   # giữa: KPI (cố định để không co)
        body.columnconfigure(2, weight=3, minsize=420)   # phải: bàn giao/sọt/phiên OPS
        body.rowconfigure(0, weight=1)

        # TRÁI: danh sách mã vừa quét
        left = tk.Frame(body, bg=self.BG)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        tk.Label(left, text="MÃ VỪA QUÉT", fg="#94a3b8", bg=self.BG,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(0, 4))
        listwrap = tk.Frame(left, bg=self.BG)
        listwrap.pack(fill="both", expand=True)
        self.listbox = tk.Listbox(listwrap, bg="#111c30", fg="#e2e8f0",
                                  font=("Consolas", 10), borderwidth=0,
                                  highlightthickness=0, selectbackground="#334155",
                                  activestyle="none")
        self.listbox.pack(side="left", fill="both", expand=True)
        sb = tk.Scrollbar(listwrap, command=self.listbox.yview)
        sb.pack(side="right", fill="y")
        self.listbox.config(yscrollcommand=sb.set)

        # PHẢI: thống kê theo ĐVVC trong ngày
        right = tk.Frame(body, bg=self.BG)
        right.grid(row=0, column=1, sticky="nsew")
        head = tk.Frame(right, bg=self.BG)
        head.pack(fill="x")
        self.lbl_kpi_title = tk.Label(head, text="SỐ LƯỢNG THEO ĐƠN VỊ VẬN CHUYỂN (SỌT 1)",
                 fg="#94a3b8", bg=self.BG, font=("Segoe UI", 9, "bold"))
        self.lbl_kpi_title.pack(side="left", pady=(0, 4))

        # Thẻ Tổng (nổi bật, 2 cột)
        total_card = tk.Frame(right, bg="#2563eb")
        total_card.pack(fill="x", pady=(2, 10))
        
        col1 = tk.Frame(total_card, bg="#2563eb")
        col1.pack(side="left", fill="both", expand=True, padx=14, pady=10)
        self.lbl_total_title = tk.Label(col1, text="TỔNG ĐƠN (SỌT 1)", fg="#dbeafe", bg="#2563eb",
                 font=("Segoe UI", 10))
        self.lbl_total_title.pack(anchor="w")
        self.total_lbl = tk.Label(col1, text="0", fg="white", bg="#2563eb",
                                  font=("Segoe UI", 30, "bold"))
        self.total_lbl.pack(anchor="w")

        col2 = tk.Frame(total_card, bg="#1d4ed8")
        col2.pack(side="right", fill="both", expand=True, padx=14, pady=10)
        tk.Label(col2, text="TỔNG HÔM NAY (TOÀN HỆ THỐNG)", fg="#dbeafe", bg="#1d4ed8",
                 font=("Segoe UI", 10)).pack(anchor="w")

        # Số tổng + mini breakdown ĐVVC nằm cùng hàng: số bên trái to, list bên phải chia 2 cột.
        row_today = tk.Frame(col2, bg="#1d4ed8")
        row_today.pack(fill="x", anchor="w")
        self.total_today_lbl = tk.Label(row_today, text="0", fg="white", bg="#1d4ed8",
                                        font=("Segoe UI", 30, "bold"))
        self.total_today_lbl.pack(side="left", anchor="w")
        # 2 cột breakdown: cột trái + cột phải, mỗi cột 1 Label multi-line.
        breakdown_wrap = tk.Frame(row_today, bg="#1d4ed8")
        breakdown_wrap.pack(side="left", anchor="s", padx=(12, 0), pady=(0, 6))
        self.today_breakdown_col1 = tk.Label(
            breakdown_wrap, text="", fg="#dbeafe", bg="#1d4ed8",
            font=("Segoe UI", 9), justify="left", anchor="nw"
        )
        self.today_breakdown_col1.grid(row=0, column=0, sticky="nw", padx=(0, 16))
        self.today_breakdown_col2 = tk.Label(
            breakdown_wrap, text="", fg="#dbeafe", bg="#1d4ed8",
            font=("Segoe UI", 9), justify="left", anchor="nw"
        )
        self.today_breakdown_col2.grid(row=0, column=1, sticky="nw")

        # Lưới thẻ theo ĐVVC (2 cột) — nằm ngay dưới ô Tổng, chiếm hết chiều cao còn lại
        self.kpi_grid = tk.Frame(right, bg=self.BG)
        self.kpi_grid.pack(fill="both", expand=True)
        self.kpi_grid.columnconfigure(0, weight=1)
        self.kpi_grid.columnconfigure(1, weight=1)

        # Nút "Hoàn thành sọt" — hành động chính, nằm cuối cột giữa.
        # Text hiện SỐ SỌT luôn để user thấy rõ đang chốt sọt nào.
        close_btn_wrap = tk.Frame(right, bg=self.BG)
        close_btn_wrap.pack(fill="x", pady=(12, 0))
        self.btn_close_basket = tk.Button(
            close_btn_wrap,
            text=f"✅ Hoàn thành Sọt {self.current_basket_seq}",
            command=self._close_basket,
            bg="#16a34a", fg="white", relief="flat",
            font=("Segoe UI", 11, "bold"), padx=16, pady=8)
        self.btn_close_basket.pack(side="right")

        # ---- CỘT PHẢI: 2 tab-button toggle + Treeview + nút "Kết thúc ngày" gọn ----
        far_right = tk.Frame(body, bg=self.BG)
        far_right.grid(row=0, column=2, sticky="nsew", padx=(10, 0))
        tk.Label(far_right, text="BÀN GIAO & SỌT", fg="#94a3b8", bg=self.BG,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(0, 4))

        # Đáy cột: nút "Kết thúc ngày" gọn, align PHẢI (không full-width).
        eos_wrap = tk.Frame(far_right, bg=self.BG)
        eos_wrap.pack(side="bottom", fill="x", pady=(8, 0))
        tk.Button(eos_wrap, text="🌙 Kết thúc ngày",
                  command=self._close_basket_end_of_shift,
                  bg="#334155", fg="#cbd5e1", relief="flat",
                  font=("Segoe UI", 9), padx=12, pady=5).pack(side="right")

        # Tab-button toggle (2 nút "Sọt hôm nay" / "Mã phiên OPS")
        tabbar = tk.Frame(far_right, bg=self.BG)
        tabbar.pack(fill="x", pady=(0, 4))
        self._active_tab = "baskets"
        self.btn_tab_baskets = tk.Button(
            tabbar, text="📦 Sọt hôm nay",
            command=lambda: self._switch_tab("baskets"),
            bg="#2563eb", fg="white", relief="flat",
            font=("Segoe UI", 9, "bold"), padx=12, pady=6)
        self.btn_tab_baskets.pack(side="left", padx=(0, 4))
        self.btn_tab_sessions = tk.Button(
            tabbar, text="📤 Phiên OPS",
            command=lambda: self._switch_tab("sessions"),
            bg="#1e293b", fg="#94a3b8", relief="flat",
            font=("Segoe UI", 9), padx=12, pady=6)
        self.btn_tab_sessions.pack(side="left")

        # 2 frame content, chỉ 1 cái hiện tại 1 thời điểm.
        self.baskets_tab = tk.Frame(far_right, bg=self.PANEL)
        self.baskets_tab.pack(fill="both", expand=True)
        self.sessions_tab = tk.Frame(far_right, bg=self.PANEL)
        # sessions_tab chưa pack — sẽ pack khi _switch_tab.

        # Style cho Treeview (Dark theme)
        style = ttk.Style()
        style.theme_use("default")
        style.configure("Treeview", background="#1e293b", foreground="#e2e8f0", fieldbackground="#1e293b",
                        rowheight=24, borderwidth=0, font=("Segoe UI", 9))
        style.configure("Treeview.Heading", background="#334155", foreground="#e2e8f0", 
                        font=("Segoe UI", 9, "bold"), borderwidth=0)
        style.map("Treeview", background=[("selected", "#3b82f6")])

        # --- Treeview Sessions: 5 cột ---
        # Sọt | ĐVVC | SL | Mã phiên giao | Thời gian
        cols = ("seq", "carrier", "count", "session_id", "time")
        self.tree_sessions = ttk.Treeview(self.sessions_tab, columns=cols, show="headings")
        self.tree_sessions.pack(side="left", fill="both", expand=True)

        self.tree_sessions.heading("seq", text="Sọt")
        self.tree_sessions.heading("carrier", text="ĐVVC")
        self.tree_sessions.heading("count", text="SL")
        self.tree_sessions.heading("session_id", text="Mã phiên giao")
        self.tree_sessions.heading("time", text="Thời gian")

        self.tree_sessions.column("seq", width=48, anchor="center", stretch=tk.NO)
        self.tree_sessions.column("carrier", width=100, anchor="w", stretch=tk.NO)
        self.tree_sessions.column("count", width=44, anchor="center", stretch=tk.NO)
        self.tree_sessions.column("session_id", width=140, anchor="w", stretch=tk.YES)
        self.tree_sessions.column("time", width=110, anchor="center", stretch=tk.NO)
        
        sb_sessions = tk.Scrollbar(self.sessions_tab, command=self.tree_sessions.yview)
        sb_sessions.pack(side="right", fill="y")
        self.tree_sessions.config(yscrollcommand=sb_sessions.set)

        # --- Label / Treeview Baskets ---
        self.baskets_empty = tk.Label(self.baskets_tab,
                                      text="  (chưa có sọt nào — bấm 'Hoàn thành sọt' để chốt sọt đầu tiên)",
                                      fg="#64748b", bg=self.PANEL, font=("Segoe UI", 9, "italic"))
        self.baskets_empty.pack(anchor="w", padx=12, pady=12)

        # ---- Nút điều khiển ----
        btns = tk.Frame(self.root, bg=self.BG)
        btns.pack(fill="x", padx=12, pady=(0, 10))
        self.count_lbl = tk.Label(btns, text="Máy này (phiên): 0", fg="#e2e8f0", bg=self.BG,
                                  font=("Segoe UI", 10, "bold"))
        self.count_lbl.pack(side="left")
        self.toggle_btn = tk.Button(btns, text="⏸ Tạm dừng", command=self._toggle,
                                    bg="#263449", fg="#e2e8f0", relief="flat",
                                    font=("Segoe UI", 10), padx=10, pady=4)
        self.toggle_btn.pack(side="right", padx=(8, 0))
        tk.Button(btns, text="🌐 Web quản lý", command=self._open_web,
                  bg="#263449", fg="#e2e8f0", relief="flat",
                  font=("Segoe UI", 10), padx=10, pady=4).pack(side="right", padx=8)
        tk.Button(btns, text="Ẩn xuống khay", command=self._hide,
                  bg="#263449", fg="#e2e8f0", relief="flat",
                  font=("Segoe UI", 10), padx=10, pady=4).pack(side="right")

        self.root.protocol("WM_DELETE_WINDOW", self._hide)  # X = ẩn xuống khay
        self._poll()
        self._start_summary_loop()

    # --- điều khiển ---
    def _toggle(self):
        c = self.state["catcher"]
        c.enabled = not c.enabled
        self.toggle_btn.config(text="▶ Bật lại" if not c.enabled else "⏸ Tạm dừng")

    def _open_web(self):
        import webbrowser
        webbrowser.open(self.cfg["url"])

    def _hide(self):
        self.root.withdraw()

    def show(self):
        self.root.deiconify()
        self.root.lift()

    # --- lấy summary + sessions ngày từ server (thread nền) ---
    def _start_summary_loop(self):
        def loop():
            while True:
                self._pull_all()
                time.sleep(5)  # tự làm mới mỗi 5 giây
        threading.Thread(target=loop, daemon=True).start()

    def _pull_all(self):
        s_today = self.state["sender"].get_summary_today()
        if s_today is not None:
            with self._summary_lock:
                self._summary_today = s_today

        s = self.state["sender"].get_current_basket()
        if s is not None:
            with self._summary_lock:
                self._summary = s
        sess = self.state["sender"].get_sessions_today()
        if sess is not None:
            with self._summary_lock:
                self._sessions = sess
        bs = self.state["sender"].list_baskets_today()
        if bs is not None:
            with self._summary_lock:
                self._baskets = bs

    def refresh_summary_now(self):
        """Gọi ngay sau khi quét để số cập nhật nhanh (không đợi 5s)."""
        threading.Thread(target=self._pull_all, daemon=True).start()

    def _render_summary(self):
        with self._summary_lock:
            s = self._summary
            s_today = self._summary_today

        if s_today:
            self.total_today_lbl.config(text=str(s_today.get("total", 0)))
            # Mini breakdown theo ĐVVC chia 2 cột (bên phải số tổng): chỉ hiện hãng > 0.
            by_today = s_today.get("by_carrier") or {}
            order = s_today.get("carrier_order") or list(by_today.keys())
            parts = [f"{name}: {by_today.get(name, 0)}" for name in order if by_today.get(name, 0) > 0]
            # Chia đôi: cột 1 = nửa đầu (làm tròn lên), cột 2 = phần còn lại.
            mid = (len(parts) + 1) // 2
            self.today_breakdown_col1.config(text="\n".join(parts[:mid]) if parts else "")
            self.today_breakdown_col2.config(text="\n".join(parts[mid:]) if len(parts) > mid else "")

        if not s:
            return

        # Agent-side seq là nguồn thống nhất — server đôi khi tính lệch nếu có
        # sọt cũ chốt rỗng hoặc backfill. Nhãn trên cùng "Đang quét vào Sọt N"
        # và tiêu đề khối thống kê phải cùng 1 số.
        seq = self.current_basket_seq
        self.lbl_kpi_title.config(text=f"SỐ LƯỢNG THEO ĐƠN VỊ VẬN CHUYỂN (SỌT {seq})")
        self.lbl_total_title.config(text=f"TỔNG ĐƠN (SỌT {seq})")
        
        self.total_lbl.config(text=str(s.get("total", 0)))
        order = s.get("carrier_order", [])
        by = s.get("by_carrier", {})
        # Tạo/ cập nhật thẻ cho từng ĐVVC (logo bên trái, tên + số bên phải).
        for i, name in enumerate(order):
            if name not in self._kpi_widgets:
                card = tk.Frame(self.kpi_grid, bg=self.PANEL)
                card.grid(row=i // 2, column=i % 2, sticky="nsew", padx=4, pady=4, ipady=6)

                # Logo (nếu có file trong logos/), nếu không -> badge chữ.
                logo_img = self._load_logo(name)
                if logo_img is not None:
                    lg = tk.Label(card, image=logo_img, bg=self.PANEL)
                    lg.image = logo_img  # giữ tham chiếu, tránh bị GC
                    lg.pack(side="left", padx=(10, 8), pady=6)
                else:
                    badge = tk.Label(card, text=self._abbrev(name), fg="#e2e8f0",
                                     bg="#334155", font=("Segoe UI", 10, "bold"),
                                     width=4, height=2)
                    badge.pack(side="left", padx=(10, 8), pady=6)

                textcol = tk.Frame(card, bg=self.PANEL)
                textcol.pack(side="left", fill="both", expand=True)
                
                # Tên ĐVVC
                tk.Label(textcol, text=name, fg="#94a3b8", bg=self.PANEL,
                         font=("Segoe UI", 9)).pack(anchor="w")
                
                # Container cho Số lượng + Nút Bàn Giao
                val_row = tk.Frame(textcol, bg=self.PANEL)
                val_row.pack(fill="x", expand=True)
                
                val = tk.Label(val_row, text="0", fg="#e2e8f0", bg=self.PANEL,
                               font=("Segoe UI", 20, "bold"))
                val.pack(side="left", anchor="w")
                
                # Nút Bàn giao 3PL
                btn = tk.Button(val_row, text="Bàn giao 3PL",
                                command=lambda c=name: self._post_carrier(c),
                                bg="#3b82f6", fg="black", cursor="hand2",
                                font=("Segoe UI", 8, "bold"), relief="flat", padx=6)
                btn.pack(side="right", anchor="e", padx=(0, 10), pady=(4, 0))
                
                self._kpi_widgets[name] = val
            self._kpi_widgets[name].config(text=str(by.get(name, 0)))

    def _post_carrier(self, carrier: str):
        """Xử lý khi ấn nút Bàn giao 3PL cho 1 ĐVVC."""
        from tkinter import messagebox
        def worker():
            res = self.state["sender"].import_now(carrier)
            def show_result():
                if res and res.get("status") == "ok":
                    count = res.get("count", 0)
                    sid = res.get("session_id", "")
                    self.listbox.insert(0, f"{_now_vn('%H:%M:%S')}  📤 Bàn giao 3PL {carrier} ({count} đơn) - {sid}")
                    messagebox.showinfo("Bàn giao thành công", f"Đã đẩy {count} đơn của {carrier} lên OPS.\nMã phiên: {sid}")
                elif res and res.get("status") == "empty":
                    messagebox.showwarning("Không có đơn", res.get("message", "Không có mã nào chờ lấy hàng."))
                else:
                    err = res.get("error", "Lỗi không xác định") if res else "Không kết nối được server"
                    messagebox.showerror("Lỗi bàn giao", f"Lỗi khi đẩy {carrier}:\n{err}")
            self.root.after(0, show_result)
        
        threading.Thread(target=worker, daemon=True).start()


    def _abbrev(self, name: str) -> str:
        """Chữ viết tắt cho badge fallback (khi không có logo)."""
        parts = name.split()
        if len(parts) >= 2:
            return (parts[0][0] + parts[1][0]).upper()
        return name[:3].upper()

    def _load_logo(self, name: str):
        """Load + resize logo cho 1 ĐVVC (cache lại). None nếu không có/không load được."""
        if not HAS_IMAGETK:
            return None
        if name in self._logo_imgs:
            return self._logo_imgs[name]
        path = find_logo_path(name)
        img = None
        if path:
            try:
                pil = _PILImage.open(path).convert("RGBA")
                pil.thumbnail((44, 44))
                img = ImageTk.PhotoImage(pil)
            except Exception:  # noqa: BLE001
                img = None
        self._logo_imgs[name] = img  # cache cả None để khỏi thử lại
        return img

    def _render_sessions(self):
        """Vẽ khối 'Mã phiên OPS hôm nay' — mỗi phiên 1 dòng trong Treeview."""
        with self._summary_lock:
            data = list(self._sessions or [])
        
        # Xóa dữ liệu cũ
        for row in self.tree_sessions.get_children():
            self.tree_sessions.delete(row)
        
        if not data:
            return

        # Phát hiện phiên MỚI so với lần render trước -> hiện thông báo (bíp).
        # Server (endpoint /api/ops/sessions mới) trả:
        #   {seq, carrier, count, session_id, time}
        current_ids = set()
        for s in data:
            session_id = s.get("session_id", "")
            current_ids.add(session_id)

            seq = s.get("seq", "-")
            carrier = s.get("carrier", "")
            count = s.get("count", 0)
            time_str = _fmt_vn_time(s.get("time") or "", "%d/%m %H:%M")

            self.tree_sessions.insert("", "end", values=(
                f"Sọt {seq}", carrier, count, session_id, time_str
            ))

        new_ids = current_ids - self._last_session_ids
        self._last_session_ids = current_ids

        # Thông báo phiên mới (nếu có, và không phải lần render đầu tiên)
        if new_ids and self._last_session_ids != new_ids:
            for sid in new_ids:
                # sid format: YYYYMMDD-HHMMSS-CARRIER
                parts = sid.split("-")
                carrier = parts[-1] if len(parts) >= 3 else sid
                self.listbox.insert(0, f"{_now_vn('%H:%M:%S')}  📤 Import OPS   {carrier}: {sid}")
                self.listbox.itemconfig(0, fg="#3b82f6")

    # --- Cập nhật ---
    def _check_update_now(self, silent_if_no_update: bool = False):
        """Kiểm tra cập nhật + hiện dialog (dùng cho cả nút bấm và auto-check khi mở app).

        Flow: query API version ở thread nền → bounce về main thread hiện dialog
        → user confirm → spawn updater ở thread nền (updater sẽ os._exit).

        silent_if_no_update:
          - False (mặc định — nút bấm): đã mới nhất -> hiện "Đã là bản mới nhất".
          - True (auto-check khi mở app): đã mới nhất -> im lặng, chỉ hiện khi có bản mới.
        """
        def query():
            try:
                r = self.state["sender"].session.get(
                    self.cfg["url"] + "/api/agent/version", timeout=8
                )
                if r.status_code != 200:
                    if not silent_if_no_update:
                        from tkinter import messagebox
                        self.root.after(0, lambda: messagebox.showerror(
                            "Lỗi kết nối", f"Server trả về HTTP {r.status_code}"))
                    return
                data = r.json()
                self.root.after(0, lambda: show_result(data))
            except Exception as ex:
                if not silent_if_no_update:
                    from tkinter import messagebox
                    self.root.after(0, lambda: messagebox.showerror(
                        "Lỗi", f"Không kiểm được cập nhật: {ex}"))

        def show_result(data: dict):
            latest = data.get("latest_version") or ""
            changelog = (data.get("changelog") or "").strip()
            released_at = data.get("released_at") or ""

            if _parse_version(latest) <= _parse_version(CURRENT_VERSION):
                if silent_if_no_update:
                    return
                from tkinter import messagebox
                messagebox.showinfo(
                    "Đã là bản mới nhất",
                    f"Phiên bản hiện tại: v{CURRENT_VERSION}\n"
                    f"Phiên bản trên server: v{latest}\n\n"
                    f"Không cần cập nhật.")
                return

            # Có phiên bản mới -> hiện dialog custom kèm changelog dạng scrollable.
            self._show_update_dialog(latest, changelog, released_at)

        threading.Thread(target=query, daemon=True).start()

    def _show_update_dialog(self, latest: str, changelog: str, released_at: str):
        """Dialog gọn: header 1 dòng + changelog cuộn + 2 nút Yes/No dưới đáy."""
        dlg = tk.Toplevel(self.root)
        dlg.title("Có phiên bản mới")
        dlg.configure(bg=self.PANEL)
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.geometry("440x300")
        dlg.resizable(False, False)
        # Center trên cửa sổ chính
        dlg.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width() // 2) - 220
        y = self.root.winfo_y() + (self.root.winfo_height() // 2) - 150
        dlg.geometry(f"+{max(x, 20)}+{max(y, 20)}")

        # 1. NÚT PHẢI PACK TRƯỚC (side="bottom") — luôn ở đáy, không bị đè.
        btns = tk.Frame(dlg, bg=self.PANEL)
        btns.pack(side="bottom", fill="x", padx=14, pady=(6, 12))

        def do_update():
            dlg.destroy()
            threading.Thread(
                target=lambda: check_and_apply_update(self.cfg["url"], self.cfg["api_key"]),
                daemon=True
            ).start()

        def skip():
            dlg.destroy()

        tk.Button(btns, text="Để sau", command=skip,
                  bg="#334155", fg="#e2e8f0", relief="flat",
                  font=("Segoe UI", 10), padx=14, pady=5).pack(side="right", padx=(6, 0))
        tk.Button(btns, text="✓ Cập nhật ngay", command=do_update,
                  bg="#16a34a", fg="white", relief="flat",
                  font=("Segoe UI", 10, "bold"), padx=14, pady=5).pack(side="right")

        # 2. Header + info gọn (1 dòng)
        tk.Label(dlg, text="🎉 Có phiên bản Agent mới",
                 fg="#4ade80", bg=self.PANEL, font=("Segoe UI", 12, "bold")
                 ).pack(anchor="w", padx=14, pady=(12, 4))

        subtitle = f"v{CURRENT_VERSION}  →  v{latest}"
        if released_at:
            subtitle += f"   ·   {_fmt_vn_time(released_at, '%d/%m/%Y %H:%M')}"
        tk.Label(dlg, text=subtitle, fg="#94a3b8", bg=self.PANEL,
                 font=("Segoe UI", 9)).pack(anchor="w", padx=14, pady=(0, 6))

        # 3. Changelog scrollable — điền phần còn lại giữa header và nút.
        log_frame = tk.Frame(dlg, bg=self.BG)
        log_frame.pack(fill="both", expand=True, padx=14, pady=(0, 6))
        log_text = tk.Text(log_frame, wrap="word", bg=self.BG, fg="#e2e8f0",
                           font=("Consolas", 9), borderwidth=0, highlightthickness=0,
                           padx=8, pady=6, height=6)
        log_text.pack(side="left", fill="both", expand=True)
        sb = tk.Scrollbar(log_frame, command=log_text.yview)
        sb.pack(side="right", fill="y")
        log_text.config(yscrollcommand=sb.set)
        log_text.insert("1.0", changelog or "(Không có ghi chú thay đổi)")
        log_text.config(state="disabled")

    def _switch_tab(self, name: str):
        """Chuyển giữa 2 frame content 'baskets' / 'sessions' (thay ttk.Notebook)."""
        if name == self._active_tab:
            return
        if name == "baskets":
            self.sessions_tab.pack_forget()
            self.baskets_tab.pack(fill="both", expand=True)
            self.btn_tab_baskets.config(bg="#2563eb", fg="white",
                                        font=("Segoe UI", 9, "bold"))
            self.btn_tab_sessions.config(bg="#1e293b", fg="#94a3b8",
                                         font=("Segoe UI", 9))
        else:
            self.baskets_tab.pack_forget()
            self.sessions_tab.pack(fill="both", expand=True)
            self.btn_tab_sessions.config(bg="#2563eb", fg="white",
                                         font=("Segoe UI", 9, "bold"))
            self.btn_tab_baskets.config(bg="#1e293b", fg="#94a3b8",
                                        font=("Segoe UI", 9))
        self._active_tab = name

    # --- Sọt ---
    MIN_CODES_PER_BASKET = 10  # yêu cầu tối thiểu để chốt 1 sọt

    def _close_basket(self):
        """Bấm nút Hoàn thành sọt: chốt sọt hiện tại, TĂNG seq cho sọt kế tiếp.

        Thứ tự đúng để tránh race condition:
        1. Check total >= MIN.
        2. Check `_closing_in_progress` (chống double-click) — nếu đang chốt, bỏ qua.
        3. TĂNG current_basket_seq NGAY (trước khi gọi API) — mọi mã quét từ giờ
           sẽ vào sọt mới. Sọt cũ được "đóng băng".
        4. Gọi API close_basket(basket_seq=closing_seq) ở thread nền.
        5. Nếu API fail -> rollback seq về closing_seq.
        """
        from tkinter import messagebox

        # 1. Check total
        current_total = 0
        with self._summary_lock:
            if self._summary:
                current_total = int(self._summary.get("total") or 0)

        if current_total < self.MIN_CODES_PER_BASKET:
            messagebox.showwarning(
                "Chưa đủ mã để chốt sọt",
                f"Sọt {self.current_basket_seq} hiện có {current_total} mã.\n"
                f"Cần ít nhất {self.MIN_CODES_PER_BASKET} mã mới được chốt.\n\n"
                f"Vui lòng tiếp tục quét thêm."
            )
            return

        # 2. Chống double-click
        if getattr(self, "_closing_in_progress", False):
            return
        self._closing_in_progress = True

        # 3. TĂNG seq NGAY. Từ giây này, handle_code() đọc current_basket_seq
        #    sẽ ra sọt MỚI -> mã quét trong lúc API chạy không bị gán vào sọt
        #    đang chốt (race condition cũ).
        closing_seq = self.current_basket_seq
        self.current_basket_seq = closing_seq + 1
        new_seq = self.current_basket_seq
        self.current_basket_lbl.config(text=f"🟢 Đang quét vào Sọt {new_seq}")
        self.btn_close_basket.config(text=f"✅ Hoàn thành Sọt {new_seq}")

        def do():
            try:
                result = self.state["sender"].close_basket(basket_seq=closing_seq)
                if result is None:
                    # 5. API fail -> rollback seq
                    self.current_basket_seq = closing_seq
                    self.root.after(0, lambda: self.current_basket_lbl.config(
                        text=f"🟢 Đang quét vào Sọt {closing_seq}"))
                    self.root.after(0, lambda: self.btn_close_basket.config(
                        text=f"✅ Hoàn thành Sọt {closing_seq}"))
                    self.root.after(0, lambda: self.listbox.insert(0,
                        f"{_now_vn('%H:%M:%S')}  ✖ Lỗi     Không chốt được sọt (mất mạng?) — đã hoàn tác"))
                    self.root.after(0, lambda: self.listbox.itemconfig(0, fg="#ef4444"))
                    return
                name = result.get("name", f"Sọt {closing_seq}")
                total = result.get("total", 0)
                by = result.get("by_carrier", {})
                detail = " | ".join(f"{k}:{v}" for k, v in by.items()) or "(rỗng)"
                self.root.after(0, lambda: self.listbox.insert(0,
                    f"{_now_vn('%H:%M:%S')}  ✅ Chốt {name}  Total {total} — {detail}"))
                self.root.after(0, lambda: self.listbox.itemconfig(0, fg="#22c55e"))
                self.root.after(0, self._pull_all)  # cập nhật danh sách sọt ngay
            finally:
                self._closing_in_progress = False
        threading.Thread(target=do, daemon=True).start()

    def _close_basket_end_of_shift(self):
        """Chốt sọt cuối ca: bypass MIN_CODES_PER_BASKET + THOÁT agent sau khi chốt.

        Dùng khi kết thúc ngày và sọt cuối chỉ có vài mã (< 10).
        Confirm 2 bước để tránh bấm nhầm:
          1. "Xác nhận chốt sọt cuối ca?" -> Yes/No.
          2. Chốt sọt (bypass rule). Nếu API OK -> hiện thông báo tổng kết +
             thoát agent sau 2 giây.
        """
        from tkinter import messagebox

        current_total = 0
        with self._summary_lock:
            if self._summary:
                current_total = int(self._summary.get("total") or 0)

        if getattr(self, "_closing_in_progress", False):
            return

        # Confirm
        seq = self.current_basket_seq
        msg = (f"Chốt sọt cuối ca?\n\n"
               f"Sọt hiện tại: Sọt {seq} — {current_total} mã.\n\n")
        if current_total == 0:
            msg += "⚠ Sọt đang RỖNG. Nếu chốt sẽ tạo sọt trống trên hệ thống.\n\n"
        elif current_total < self.MIN_CODES_PER_BASKET:
            msg += (f"⚠ Chỉ có {current_total} mã (dưới ngưỡng {self.MIN_CODES_PER_BASKET}).\n"
                    f"Chỉ bấm khi thật sự kết thúc ca hôm nay.\n\n")
        msg += "Sau khi chốt, agent sẽ TỰ THOÁT."

        if not messagebox.askyesno("Chốt sọt cuối ca", msg, icon="question"):
            return

        self._closing_in_progress = True
        closing_seq = self.current_basket_seq
        # KHÔNG tăng seq — agent sẽ thoát, không quét thêm nữa.

        def do():
            try:
                result = self.state["sender"].close_basket(basket_seq=closing_seq)
                if result is None:
                    self.root.after(0, lambda: messagebox.showerror(
                        "Lỗi", "Không chốt được sọt (mất mạng?). Vui lòng thử lại."))
                    return
                name = result.get("name", f"Sọt {closing_seq}")
                total = result.get("total", 0)
                by = result.get("by_carrier", {})
                detail = " | ".join(f"{k}:{v}" for k, v in by.items()) or "(rỗng)"
                self.root.after(0, lambda: self.listbox.insert(0,
                    f"{_now_vn('%H:%M:%S')}  🌙 Chốt cuối ca {name}  Total {total} — {detail}"))
                self.root.after(0, lambda: self.listbox.itemconfig(0, fg="#a855f7"))
                # Thông báo tổng kết ngày (lấy từ summary today).
                s_today = None
                with self._summary_lock:
                    s_today = self._summary_today
                today_total = int(s_today.get("total", 0)) if s_today else total
                self.root.after(0, lambda: messagebox.showinfo(
                    "Đã chốt sọt cuối ca",
                    f"✅ Chốt {name} với {total} mã.\n\n"
                    f"📊 Tổng ngày: {today_total} mã.\n\n"
                    f"Agent sẽ đóng sau 2 giây."))
                # Thoát agent hoàn toàn (kill mọi thread/tray).
                self.root.after(2000, lambda: os._exit(0))
            finally:
                self._closing_in_progress = False
        threading.Thread(target=do, daemon=True).start()

    def _render_baskets(self):
        with self._summary_lock:
            data = list(self._baskets or [])
        
        if not hasattr(self, 'tree_baskets'):
            # Vùng Tree (bỏ nút "Bàn giao Sọt đã chọn lên OPS" — user không dùng
            # tính năng này từ agent nữa, thao tác qua trang Admin web).
            tree_frame = tk.Frame(self.baskets_tab, bg=self.PANEL)
            tree_frame.pack(fill="both", expand=True, padx=12, pady=10)
            
            # id là cột ẩn để lưu basket_id
            cols = ("id", "seq", "total", "details", "time")
            self.tree_baskets = ttk.Treeview(tree_frame, columns=cols, show="headings")
            self.tree_baskets.pack(side="left", fill="both", expand=True)
            
            self.tree_baskets.heading("id", text="ID")
            self.tree_baskets.heading("seq", text="Sọt")
            self.tree_baskets.heading("total", text="Tổng số")
            self.tree_baskets.heading("details", text="Chi tiết theo ĐVVC")
            self.tree_baskets.heading("time", text="Giờ chốt")
            
            self.tree_baskets.column("id", width=0, stretch=tk.NO) # Ẩn cột ID
            self.tree_baskets.column("seq", width=48, anchor="center", stretch=tk.NO)
            self.tree_baskets.column("total", width=54, anchor="center", stretch=tk.NO)
            self.tree_baskets.column("details", width=200, anchor="w")
            # Cột thời gian: đủ rộng cho "HH:MM:SS" (đã format ngắn khi render).
            self.tree_baskets.column("time", width=90, anchor="center", stretch=tk.NO)
            
            sb = tk.Scrollbar(tree_frame, command=self.tree_baskets.yview)
            sb.pack(side="right", fill="y")
            self.tree_baskets.config(yscrollcommand=sb.set)

        # Xóa dữ liệu cũ
        for row in self.tree_baskets.get_children():
            self.tree_baskets.delete(row)

        if not data:
            if not self.baskets_empty.winfo_ismapped():
                self.baskets_empty.pack(anchor="w", padx=12, pady=12)
            # Ẩn nút và tree nếu trống
            self.tree_baskets.master.pack_forget()
            return
            
        if self.baskets_empty.winfo_ismapped():
            self.baskets_empty.pack_forget()
        
        if not self.tree_baskets.master.winfo_ismapped():
            self.tree_baskets.master.pack(fill="both", expand=True, padx=12, pady=10)
            
        for b in data:
            b_id = b.get("id")
            seq = b.get('name', 'Sọt ?')
            total = b.get("total", 0)
            
            # Formatting by_carrier dict to string
            by_carrier = b.get("by_carrier", {})
            details_arr = [f"{k}: {v}" for k, v in by_carrier.items() if v > 0]
            details = " | ".join(details_arr) if details_arr else "Trống"
            
            # Vì tab bên phải hẹp: chỉ hiện HH:MM:SS (baskets đều là hôm nay).
            # Server trả UTC -> convert +7 để đúng giờ VN.
            time_str = _fmt_vn_time(b.get("closed_at") or "", "%H:%M:%S")
            self.tree_baskets.insert("", "end", values=(b_id, seq, total, details, time_str))

    def _post_basket(self):
        """Khi bấm nút Bàn giao sọt đã chọn lên OPS."""
        from tkinter import messagebox
        selection = self.tree_baskets.selection()
        if not selection:
            messagebox.showwarning("Chưa chọn sọt", "Vui lòng click chọn một sọt trong danh sách trước!")
            return
            
        item = self.tree_baskets.item(selection[0])
        b_id = item["values"][0]
        seq = item["values"][1]
        
        if not messagebox.askyesno("Xác nhận", f"Bạn có chắc chắn muốn đẩy {seq} lên hệ thống OPS không?"):
            return
            
        def worker():
            res = self.state["sender"].import_basket(b_id)
            def show_result():
                if res and res.get("status") == "done":
                    results = res.get("results", [])
                    if not results:
                        messagebox.showinfo("Hoàn tất", f"Đã đẩy {seq}. (Trống hoặc không có mã cần đẩy)")
                        return
                    
                    msg = f"Kết quả đẩy {seq}:\n\n"
                    for r in results:
                        if r.get("status") == "ok":
                            msg += f"✅ {r['carrier']}: Thành công {r['count']} đơn. Phiên: {r['session_id']}\n"
                        else:
                            msg += f"❌ {r['carrier']}: LỖI - {r.get('error')}\n"
                    messagebox.showinfo("Kết quả bàn giao", msg)
                elif res and res.get("status") == "empty":
                    messagebox.showwarning("Không có đơn", res.get("message", "Sọt rỗng hoặc đã đẩy hết rồi."))
                else:
                    err = res.get("error", "Lỗi không xác định") if res else "Không kết nối được server"
                    messagebox.showerror("Lỗi", f"Lỗi hệ thống:\n{err}")
            self.root.after(0, show_result)
        
        threading.Thread(target=worker, daemon=True).start()

    # --- vòng lặp cập nhật GUI ---
    def _poll(self):
        try:
            while True:
                ev = self.q.get_nowait()
                self._add_row(ev)
        except Exception:  # queue.Empty
            pass
        # trạng thái kết nối + hàng đợi offline
        pending = self.state["oq"].count()
        if pending > 0:
            self.status_lbl.config(text=f"⚠ Chờ gửi lại: {pending} mã", fg="#f59e0b")
        else:
            self.status_lbl.config(text="● Sẵn sàng", fg="#22c55e")
        self._render_summary()
        self._render_sessions()
        self._render_baskets()
        self.root.after(500, self._poll)

    def _add_row(self, ev):
        code, result = ev["code"], ev["result"]
        # Bỏ giờ để hiển thị gọn — chỉ icon + mã.
        label = self.LABELS.get(result, result)
        self.listbox.insert(0, f"{label}  {code}")
        self.listbox.itemconfig(0, fg=self.COLORS.get(result, "#e2e8f0"))
        if self.listbox.size() > 300:
            self.listbox.delete(300, "end")
        if result == "ok":
            self.count_session += 1
            self.count_lbl.config(text=f"Máy này (phiên): {self.count_session}")
        # Sau mỗi lần quét -> cập nhật số ngày ngay.
        if result in ("ok", "dup"):
            self.refresh_summary_now()

    def run(self):
        self.root.mainloop()


# ----------------------------- main -----------------------------
def main():
    import queue as _queue

    cfg = load_config()
    oq = OfflineQueue(QUEUE_DB)
    sender = Sender(cfg, oq)

    # Hàng đợi sự kiện để đẩy kết quả quét sang cửa sổ GUI (an toàn luồng).
    ui_events = _queue.Queue()

    def handle_code(code):
        # Lấy sọt hiện tại từ window (nếu có) — gửi kèm để server gán basket_id ngay.
        win = state.get("window")
        seq = win.current_basket_seq if win else None
        result = sender.send(code, basket_seq=seq)
        if result == "ok":
            print(f"[ok] {code}")
            if cfg["beep"]:
                beep("ok")
        elif result == "ignored":
            # Quét lại trong <1 phút: im lặng hoàn toàn (không bíp, không log ồn).
            print(f"[bỏ qua <1p] {code}")
        elif result == "dup":
            print(f"[TRÙNG >1p] {code} — đã báo email Admin")
            if cfg["beep"]:
                beep("dup")  # cảnh báo to tại máy local
        elif result == "net":
            if cfg["beep"]:
                beep("ok")  # đã xếp hàng, coi như nhận
        else:
            print(f"[lỗi] {code}")
            if cfg["beep"]:
                beep("err")
        # Đẩy sang cửa sổ GUI (nếu có).
        ui_events.put({"code": code, "result": result})

    catcher = ScanCatcher(cfg, handle_code)
    state = {"cfg": cfg, "catcher": catcher, "oq": oq, "sender": sender, "window": None}

    # Chạy nền: gửi lại mã offline.
    threading.Thread(target=sender.flush_loop, daemon=True).start()

    # Chạy nền: kiểm tra tự động cập nhật mã nguồn & build lại .exe từ server.
    def _auto_update_loop():
        # Đợi 5s cho GUI tk mainloop kịp sẵn sàng (nếu có window).
        time.sleep(5)
        first_check = True
        while True:
            try:
                win = state.get("window")
                if win is not None:
                    # Có GUI: hiện dialog kèm changelog để user Yes/No.
                    # first_check: khi vừa mở app, luôn kiểm tra (im lặng nếu đã mới nhất).
                    win._check_update_now(silent_if_no_update=True)
                else:
                    # Không có GUI (chỉ tray hoặc console): update im lặng như cũ.
                    check_and_apply_update(cfg["url"], cfg["api_key"])
            except Exception as ex:
                print(f"[AutoUpdate Error] {ex}")
            first_check = False
            time.sleep(30 * 60)

    threading.Thread(target=_auto_update_loop, daemon=True).start()

    # Nghe bàn phím toàn cục.
    listener = keyboard.Listener(on_press=catcher.on_press)
    listener.start()

    print(f"Scanner Agent v{CURRENT_VERSION} đang chạy. Server: {cfg['url']}  |  Máy: {cfg['name']}")

    # --- Điều phối GUI + Tray ---
    if HAS_GUI:
        # Cửa sổ chạy trên MAIN THREAD; tray chạy thread nền.
        window = AgentWindow(cfg, state, ui_events)
        state["window"] = window
        if not cfg.get("show_window", True):
            window._hide()  # khởi động thẳng xuống khay
        if HAS_TRAY:
            threading.Thread(target=run_tray, args=(state,), daemon=True).start()
        window.run()  # blocking (mainloop). Đóng X -> ẩn xuống khay.
    elif HAS_TRAY:
        run_tray(state)  # không có GUI -> chỉ tray (blocking)
    else:
        try:
            listener.join()
        except KeyboardInterrupt:
            print("\nĐã thoát.")


if __name__ == "__main__":
    main()
