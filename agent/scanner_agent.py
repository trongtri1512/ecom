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


HERE = _app_dir()
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
    """Cửa sổ hiển thị trên máy quét: mã vừa quét, trạng thái, nút điều khiển.

    tkinter phải chạy trên MAIN THREAD. Các thread khác (quét, gửi) đẩy sự kiện
    vào hàng đợi; cửa sổ tự đọc định kỳ để cập nhật -> an toàn luồng.
    """

    COLORS = {
        "ok": "#16a34a",       # xanh - thêm mới thành công
        "ignored": "#f59e0b",  # vàng - quét lại <1p, bỏ qua
        "dup": "#ef4444",      # đỏ - trùng >1p
        "err": "#ef4444",
        "net": "#3b82f6",      # xanh dương - mất mạng, đã xếp hàng
    }
    LABELS = {
        "ok": "✔ Đã thêm",
        "ignored": "• Quét lại (<1p)",
        "dup": "✖ TRÙNG",
        "err": "! Lỗi",
        "net": "⇄ Chờ gửi (mất mạng)",
    }

    def __init__(self, cfg, state, event_queue):
        self.cfg = cfg
        self.state = state
        self.q = event_queue
        self.count_session = 0

        self.root = tk.Tk()
        self.root.title("Scan Ecom — Máy quét")
        self.root.geometry("560x460")
        self.root.configure(bg="#0f172a")
        self.root.minsize(460, 360)

        # Header: tên máy + server
        top = tk.Frame(self.root, bg="#1e293b")
        top.pack(fill="x")
        tk.Label(top, text="📦 Scan Ecom", fg="#e2e8f0", bg="#1e293b",
                 font=("Segoe UI", 14, "bold")).pack(side="left", padx=12, pady=10)
        self.status_lbl = tk.Label(top, text="● Đang kết nối…", fg="#94a3b8", bg="#1e293b",
                                   font=("Segoe UI", 10))
        self.status_lbl.pack(side="right", padx=12)

        info = tk.Frame(self.root, bg="#0f172a")
        info.pack(fill="x", padx=12, pady=(8, 4))
        tk.Label(info, text=f"Máy: {cfg['name']}    Server: {cfg['url']}",
                 fg="#94a3b8", bg="#0f172a", font=("Segoe UI", 9)).pack(side="left")

        # Đếm phiên
        self.count_lbl = tk.Label(self.root, text="Đã quét (phiên này): 0",
                                  fg="#e2e8f0", bg="#0f172a", font=("Segoe UI", 11, "bold"))
        self.count_lbl.pack(anchor="w", padx=12, pady=(2, 6))

        # Danh sách mã vừa quét
        listwrap = tk.Frame(self.root, bg="#0f172a")
        listwrap.pack(fill="both", expand=True, padx=12)
        self.listbox = tk.Listbox(listwrap, bg="#111c30", fg="#e2e8f0",
                                  font=("Consolas", 11), borderwidth=0,
                                  highlightthickness=0, selectbackground="#334155",
                                  activestyle="none")
        self.listbox.pack(side="left", fill="both", expand=True)
        sb = tk.Scrollbar(listwrap, command=self.listbox.yview)
        sb.pack(side="right", fill="y")
        self.listbox.config(yscrollcommand=sb.set)

        # Nút điều khiển
        btns = tk.Frame(self.root, bg="#0f172a")
        btns.pack(fill="x", padx=12, pady=10)
        self.toggle_btn = tk.Button(btns, text="⏸ Tạm dừng", command=self._toggle,
                                    bg="#263449", fg="#e2e8f0", relief="flat",
                                    font=("Segoe UI", 10), padx=10, pady=4)
        self.toggle_btn.pack(side="left")
        tk.Button(btns, text="🌐 Mở web quản lý", command=self._open_web,
                  bg="#263449", fg="#e2e8f0", relief="flat",
                  font=("Segoe UI", 10), padx=10, pady=4).pack(side="left", padx=8)
        tk.Button(btns, text="Ẩn xuống khay", command=self._hide,
                  bg="#263449", fg="#e2e8f0", relief="flat",
                  font=("Segoe UI", 10), padx=10, pady=4).pack(side="right")

        # Đóng (X) = ẩn xuống khay, KHÔNG thoát agent.
        self.root.protocol("WM_DELETE_WINDOW", self._hide)
        self._poll()

    def _toggle(self):
        c = self.state["catcher"]
        c.enabled = not c.enabled
        self.toggle_btn.config(text="▶ Bật lại" if not c.enabled else "⏸ Tạm dừng")

    def _open_web(self):
        import webbrowser
        webbrowser.open(self.cfg["url"])

    def _hide(self):
        self.root.withdraw()  # ẩn cửa sổ; agent vẫn chạy ngầm

    def show(self):
        self.root.deiconify()
        self.root.lift()

    def _poll(self):
        # Đọc sự kiện quét từ hàng đợi, cập nhật danh sách.
        try:
            while True:
                ev = self.q.get_nowait()
                self._add_row(ev)
        except Exception:  # queue.Empty
            pass
        # Cập nhật trạng thái kết nối + hàng đợi offline.
        pending = self.state["oq"].count()
        if pending > 0:
            self.status_lbl.config(text=f"⚠ Chờ gửi lại: {pending} mã", fg="#f59e0b")
        else:
            self.status_lbl.config(text="● Sẵn sàng", fg="#22c55e")
        self.root.after(400, self._poll)

    def _add_row(self, ev):
        code, result = ev["code"], ev["result"]
        t = time.strftime("%H:%M:%S")
        label = self.LABELS.get(result, result)
        self.listbox.insert(0, f"{t}   {label:22s} {code}")
        self.listbox.itemconfig(0, fg=self.COLORS.get(result, "#e2e8f0"))
        # Giữ tối đa 300 dòng.
        if self.listbox.size() > 300:
            self.listbox.delete(300, "end")
        if result in ("ok",):
            self.count_session += 1
            self.count_lbl.config(text=f"Đã quét (phiên này): {self.count_session}")

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
        # Đẩy sang cửa sổ GUI (nếu có).
        ui_events.put({"code": code, "result": result})

    catcher = ScanCatcher(cfg, handle_code)
    state = {"cfg": cfg, "catcher": catcher, "oq": oq, "window": None}

    # Chạy nền: gửi lại mã offline.
    threading.Thread(target=sender.flush_loop, daemon=True).start()

    # Nghe bàn phím toàn cục.
    listener = keyboard.Listener(on_press=catcher.on_press)
    listener.start()

    print(f"Scanner Agent đang chạy. Server: {cfg['url']}  |  Máy: {cfg['name']}")

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
