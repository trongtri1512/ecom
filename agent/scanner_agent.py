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
from datetime import datetime, timezone

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


HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.ini")
QUEUE_DB = os.path.join(HERE, "queue.db")


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
    """Lưu mã vào SQLite khi mất mạng, gửi lại khi có mạng — không mất mã."""

    def __init__(self, path):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS pending ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, scanned_at TEXT)"
        )
        self._conn.commit()

    def add(self, code, scanned_at):
        with self._lock:
            self._conn.execute(
                "INSERT INTO pending(code, scanned_at) VALUES (?, ?)", (code, scanned_at)
            )
            self._conn.commit()

    def all(self):
        with self._lock:
            return self._conn.execute(
                "SELECT id, code, scanned_at FROM pending ORDER BY id"
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

    def _post(self, code, scanned_at):
        """Trả 'ok' | 'ignored' | 'dup' | 'net' | 'err'.

        - ok      : mã mới, đã lưu (201).
        - ignored : quét lại trong <1 phút -> server bỏ qua êm (201, status ignored).
        - dup     : quét lại sau >1 phút -> mã TRÙNG (409) -> cảnh báo tại máy.
        """
        try:
            r = self.session.post(
                self.cfg["url"] + "/api/scans",
                json={"code": code, "scanned_at": scanned_at, "source_agent": self.cfg["name"]},
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

    def send(self, code):
        """Gửi 1 mã. Nếu mất mạng -> đưa vào hàng đợi offline."""
        scanned_at = datetime.now(timezone.utc).isoformat()
        result = self._post(code, scanned_at)
        if result == "net":
            self.oq.add(code, scanned_at)
            print(f"[offline] Mất mạng, xếp hàng: {code} (chờ gửi lại)")
        return result

    def flush_loop(self):
        """Chạy nền: định kỳ gửi lại các mã đang xếp hàng khi có mạng."""
        while True:
            time.sleep(5)
            rows = self.oq.all()
            for row_id, code, scanned_at in rows:
                res = self._post(code, scanned_at)
                if res in ("ok", "ignored", "dup", "err"):
                    # mọi kết quả trừ 'net' đều coi là đã xử lý -> bỏ khỏi hàng đợi.
                    self.oq.remove(row_id)
                    print(f"[flush] Gửi lại {code}: {res}")
                else:
                    break  # vẫn mất mạng -> để lần sau


# ----------------------------- Bắt mã toàn cục -----------------------------
class ScanCatcher:
    """Gom ký tự do máy quét gõ; kết thúc ở Enter hoặc khi im lặng đủ lâu."""

    def __init__(self, cfg, on_code):
        self.cfg = cfg
        self.on_code = on_code
        self.buffer = []
        self.last_time = 0.0
        self.enabled = True

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

        # Nếu ký tự đến chậm (gõ tay) -> reset buffer, bắt đầu lại.
        if gap > self.cfg["inter_key_timeout"] and self.buffer:
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

    def quit_app(icon, item):
        icon.stop()
        os._exit(0)

    menu = pystray.Menu(
        pystray.MenuItem(lambda item: "Tạm dừng" if state["catcher"].enabled else "Bật lại", toggle),
        pystray.MenuItem("Mở web quản lý", open_web),
        pystray.MenuItem("Trạng thái hàng đợi", show_status),
        pystray.MenuItem("Thoát", quit_app),
    )
    icon = pystray.Icon("scan_ecom", make_icon_image((34, 197, 94)), "Scan Ecom — Đang bật", menu)
    icon.run()


# ----------------------------- main -----------------------------
def main():
    cfg = load_config()
    oq = OfflineQueue(QUEUE_DB)
    sender = Sender(cfg, oq)

    def handle_code(code):
        result = sender.send(code)
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

    catcher = ScanCatcher(cfg, handle_code)

    # Chạy nền: gửi lại mã offline.
    threading.Thread(target=sender.flush_loop, daemon=True).start()

    # Nghe bàn phím toàn cục.
    listener = keyboard.Listener(on_press=catcher.on_press)
    listener.start()

    print(f"Scanner Agent đang chạy. Server: {cfg['url']}  |  Máy: {cfg['name']}")
    print("Quét mã ở bất kỳ đâu — không cần focus vào cửa sổ nào. Ctrl+C để thoát (console).")

    if HAS_TRAY:
        state = {"cfg": cfg, "catcher": catcher, "oq": oq}
        run_tray(state)  # blocking; menu Thoát sẽ os._exit
    else:
        try:
            listener.join()
        except KeyboardInterrupt:
            print("\nĐã thoát.")


if __name__ == "__main__":
    main()
