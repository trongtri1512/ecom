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

    def get_summary_today(self):
        """Lấy thống kê HÔM NAY từ server: {total, by_carrier, carrier_order}.

        Trả None nếu không gọi được (mất mạng / server lỗi).
        """
        try:
            r = self.session.get(self.cfg["url"] + "/api/summary?period=day", timeout=8)
            if r.status_code == 200:
                return r.json()
        except requests.RequestException:
            pass
        return None

    def get_sessions_today(self):
        """Danh sách mã phiên đã import từ ops_logs. None nếu lỗi."""
        try:
            r = self.session.get(self.cfg["url"] + "/api/ops/logs?limit=30", timeout=8)
            if r.status_code == 200:
                items = r.json().get("items", [])
                return [i for i in items if "import" in i.get("action", "") and i.get("session_id")]
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

    def close_basket(self):
        """Chốt 1 sọt cho máy này. Trả record sọt vừa tạo, hoặc None nếu lỗi."""
        try:
            r = self.session.post(self.cfg["url"] + "/api/baskets/close",
                                  params={"agent_name": self.cfg["name"]}, timeout=10)
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
    """Cửa sổ trên máy quét: TRÁI (~1/3) danh sách mã vừa quét, PHẢI (~2/3) thống
    kê số lượng theo ĐVVC trong NGÀY (lấy từ server, tự làm mới định kỳ).

    tkinter phải chạy trên MAIN THREAD. Thread quét đẩy sự kiện vào hàng đợi;
    việc gọi server lấy summary chạy ở thread nền để không treo giao diện.
    """

    COLORS = {
        "ok": "#16a34a", "ignored": "#f59e0b", "dup": "#ef4444",
        "err": "#ef4444", "net": "#3b82f6",
    }
    LABELS = {
        "ok": "✔ Đã thêm", "ignored": "• Quét lại (<1p)", "dup": "✖ TRÙNG",
        "err": "! Lỗi", "net": "⇄ Chờ gửi",
    }
    BG = "#0f172a"
    PANEL = "#1e293b"

    def __init__(self, cfg, state, event_queue):
        self.cfg = cfg
        self.state = state
        self.q = event_queue
        self.count_session = 0
        self._summary = None          # summary ngày mới nhất (dict) từ server
        self._summary_lock = threading.Lock()
        self._kpi_widgets = {}        # tên ĐVVC -> label giá trị
        self._logo_imgs = {}          # tên ĐVVC -> ImageTk (cache logo)
        self._sessions = {}           # by_carrier: {carrier: [{session_id,count,...}]}
        self._last_session_ids = set()  # để phát hiện phiên mới, hiện thông báo
        self._baskets = []            # danh sách sọt hôm nay của máy này

        self.root = tk.Tk()
        self.root.title("Scan Ecom — Máy quét")
        self.root.geometry("980x720")
        self.root.configure(bg=self.BG)
        self.root.minsize(820, 600)

        # ---- Header ----
        top = tk.Frame(self.root, bg=self.PANEL)
        top.pack(fill="x")
        tk.Label(top, text="📦 Scan Ecom", fg="#e2e8f0", bg=self.PANEL,
                 font=("Segoe UI", 14, "bold")).pack(side="left", padx=12, pady=10)
        self.status_lbl = tk.Label(top, text="● Đang kết nối…", fg="#94a3b8", bg=self.PANEL,
                                   font=("Segoe UI", 10))
        self.status_lbl.pack(side="right", padx=12)
        tk.Label(top, text=f"Máy: {cfg['name']}", fg="#94a3b8", bg=self.PANEL,
                 font=("Segoe UI", 9)).pack(side="right", padx=12)

        # ---- Thân: 2 cột (trái 1/3, phải 2/3) ----
        body = tk.Frame(self.root, bg=self.BG)
        body.pack(fill="both", expand=True, padx=12, pady=10)
        body.columnconfigure(0, weight=1)   # trái ~1/3
        body.columnconfigure(1, weight=2)   # phải ~2/3
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
        tk.Label(head, text="SỐ LƯỢNG THEO ĐƠN VỊ VẬN CHUYỂN (HÔM NAY)",
                 fg="#94a3b8", bg=self.BG, font=("Segoe UI", 9, "bold")).pack(side="left", pady=(0, 4))

        # Thẻ Tổng (nổi bật)
        total_card = tk.Frame(right, bg="#2563eb")
        total_card.pack(fill="x", pady=(2, 10))
        tk.Label(total_card, text="TỔNG ĐƠN HÔM NAY", fg="#dbeafe", bg="#2563eb",
                 font=("Segoe UI", 10)).pack(anchor="w", padx=14, pady=(10, 0))
        self.total_lbl = tk.Label(total_card, text="0", fg="white", bg="#2563eb",
                                  font=("Segoe UI", 30, "bold"))
        self.total_lbl.pack(anchor="w", padx=14, pady=(0, 10))

        # Lưới thẻ theo ĐVVC (2 cột)
        self.kpi_grid = tk.Frame(right, bg=self.BG)
        self.kpi_grid.pack(fill="x")
        self.kpi_grid.columnconfigure(0, weight=1)
        self.kpi_grid.columnconfigure(1, weight=1)
        # ---- Khối "Mã phiên hôm nay" ----
        tk.Label(right, text="MÃ PHIÊN OPS HÔM NAY", fg="#94a3b8", bg=self.BG,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(14, 4))
        self.sessions_box = tk.Frame(right, bg=self.PANEL)
        self.sessions_box.pack(fill="x")
        
        # Style cho Treeview (Dark theme)
        style = ttk.Style()
        style.theme_use("default")
        style.configure("Treeview", background="#1e293b", foreground="#e2e8f0", fieldbackground="#1e293b",
                        rowheight=24, borderwidth=0, font=("Segoe UI", 9))
        style.configure("Treeview.Heading", background="#334155", foreground="#e2e8f0", 
                        font=("Segoe UI", 9, "bold"), borderwidth=0)
        style.map("Treeview", background=[("selected", "#3b82f6")])

        cols = ("stt", "carrier", "session_id", "type", "qty", "qty_out", "status", "creator", "date")
        self.tree_sessions = ttk.Treeview(self.sessions_box, columns=cols, show="headings", height=5)
        self.tree_sessions.pack(side="left", fill="x", expand=True)
        
        self.tree_sessions.heading("stt", text="#")
        self.tree_sessions.heading("carrier", text="Nhà vận chuyển")
        self.tree_sessions.heading("session_id", text="Mã phiên bàn giao")
        self.tree_sessions.heading("type", text="Loại phiên")
        self.tree_sessions.heading("qty", text="Số kiện hàng")
        self.tree_sessions.heading("qty_out", text="SL đơn xuất")
        self.tree_sessions.heading("status", text="Trạng thái")
        self.tree_sessions.heading("creator", text="Người tạo")
        self.tree_sessions.heading("date", text="Ngày tạo")

        self.tree_sessions.column("stt", width=30, anchor="center")
        self.tree_sessions.column("carrier", width=110, anchor="w")
        self.tree_sessions.column("session_id", width=140, anchor="w")
        self.tree_sessions.column("type", width=80, anchor="center")
        self.tree_sessions.column("qty", width=80, anchor="center")
        self.tree_sessions.column("qty_out", width=80, anchor="center")
        self.tree_sessions.column("status", width=80, anchor="center")
        self.tree_sessions.column("creator", width=80, anchor="center")
        self.tree_sessions.column("date", width=120, anchor="center")
        
        sb_sessions = tk.Scrollbar(self.sessions_box, command=self.tree_sessions.yview)
        sb_sessions.pack(side="right", fill="y")
        self.tree_sessions.config(yscrollcommand=sb_sessions.set)

        # ---- Khối "Các sọt hôm nay" + nút Hoàn thành sọt ----
        basket_head = tk.Frame(right, bg=self.BG)
        basket_head.pack(fill="x", pady=(14, 4))
        tk.Label(basket_head, text="CÁC SỌT HÔM NAY (MÁY NÀY)",
                 fg="#94a3b8", bg=self.BG, font=("Segoe UI", 9, "bold")).pack(side="left")
        tk.Button(basket_head, text="✅ Hoàn thành sọt", command=self._close_basket,
                  bg="#16a34a", fg="white", relief="flat",
                  font=("Segoe UI", 10, "bold"), padx=12, pady=4).pack(side="right")
        self.baskets_box = tk.Frame(right, bg=self.PANEL)
        self.baskets_box.pack(fill="both", expand=True)
        self.baskets_empty = tk.Label(self.baskets_box,
                                      text="  (chưa có sọt nào — bấm 'Hoàn thành sọt' để chốt sọt đầu tiên)",
                                      fg="#64748b", bg=self.PANEL,
                                      font=("Segoe UI", 9, "italic"))
        self.baskets_empty.pack(anchor="w", padx=12, pady=8)

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
        s = self.state["sender"].get_summary_today()
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
        if not s:
            return
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
                    self.listbox.insert(0, f"{time.strftime('%H:%M:%S')}  📤 Bàn giao 3PL {carrier} ({count} đơn) - {sid}")
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

        # Phát hiện phiên MỚI so với lần render trước -> hiện thông báo (bíp)
        current_ids = set()
        for idx, s in enumerate(data):
            session_id = s.get("session_id", "")
            current_ids.add(session_id)
            
            stt = idx + 1
            carrier = s.get("carrier", "")
            type_str = "Phiên giao"
            qty = s.get("count", 0)
            status = "Thành công" if s.get("level") == "success" else "Lỗi"
            creator = "Hệ thống"
            date_str = (s.get("created_at") or "")[:19].replace("T", " ")

            self.tree_sessions.insert("", "end", values=(
                stt, carrier, session_id, type_str, qty, qty, status, creator, date_str
            ))

        new_ids = current_ids - self._last_session_ids
        self._last_session_ids = current_ids

        # Thông báo phiên mới (nếu có, và không phải lần render đầu tiên)
        if new_ids and self._last_session_ids != new_ids:
            for sid in new_ids:
                # sid format: YYYYMMDD-HHMMSS-CARRIER
                parts = sid.split("-")
                carrier = parts[-1] if len(parts) >= 3 else sid
                self.listbox.insert(0, f"{time.strftime('%H:%M:%S')}  📤 Import OPS   {carrier}: {sid}")
                self.listbox.itemconfig(0, fg="#3b82f6")

    # --- Sọt ---
    def _close_basket(self):
        """Bấm nút Hoàn thành sọt: gọi API server, thông báo kết quả."""
        def do():
            result = self.state["sender"].close_basket()
            if result is None:
                # về main thread để cảnh báo (đơn giản: log vào listbox)
                self.root.after(0, lambda: self.listbox.insert(0,
                    f"{time.strftime('%H:%M:%S')}  ✖ Lỗi     Không chốt được sọt (mất mạng?)"))
                self.root.after(0, lambda: self.listbox.itemconfig(0, fg="#ef4444"))
                return
            name = result.get("name", "Sọt ?")
            total = result.get("total", 0)
            by = result.get("by_carrier", {})
            detail = " | ".join(f"{k}:{v}" for k, v in by.items()) or "(rỗng)"
            self.root.after(0, lambda: self.listbox.insert(0,
                f"{time.strftime('%H:%M:%S')}  ✅ Chốt {name}  Total {total} — {detail}"))
            self.root.after(0, lambda: self.listbox.itemconfig(0, fg="#22c55e"))
            self.root.after(0, self._pull_all)  # cập nhật danh sách sọt ngay
        threading.Thread(target=do, daemon=True).start()

    def _render_baskets(self):
        with self._summary_lock:
            data = list(self._baskets or [])
        for w in self.baskets_box.winfo_children():
            w.destroy()
        if not data:
            self.baskets_empty = tk.Label(self.baskets_box,
                text="  (chưa có sọt nào — bấm 'Hoàn thành sọt' để chốt sọt đầu tiên)",
                fg="#64748b", bg=self.PANEL, font=("Segoe UI", 9, "italic"))
            self.baskets_empty.pack(anchor="w", padx=12, pady=8)
            return
        for b in data:
            row = tk.Frame(self.baskets_box, bg=self.PANEL)
            row.pack(fill="x", padx=10, pady=3)
            tk.Label(row, text=b.get("name", "Sọt ?"), fg="#e2e8f0", bg=self.PANEL,
                     font=("Segoe UI", 10, "bold"), width=10, anchor="w").pack(side="left")
            tk.Label(row, text=f"Total {b.get('total', 0)}", fg="#4ade80", bg=self.PANEL,
                     font=("Segoe UI", 10, "bold"), width=12, anchor="w").pack(side="left")
            by = b.get("by_carrier", {})
            detail = "   ".join(f"{k}: {v}" for k, v in by.items()) or "(rỗng)"
            tk.Label(row, text=detail, fg="#cbd5e1", bg=self.PANEL,
                     font=("Consolas", 10), anchor="w").pack(side="left", fill="x", expand=True)
            closed = (b.get("closed_at") or "")[11:19]  # HH:MM:SS UTC
            tk.Label(row, text=closed, fg="#64748b", bg=self.PANEL,
                     font=("Consolas", 9)).pack(side="right", padx=6)

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
        t = time.strftime("%H:%M:%S")
        label = self.LABELS.get(result, result)
        self.listbox.insert(0, f"{t}  {label:14s} {code}")
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
    state = {"cfg": cfg, "catcher": catcher, "oq": oq, "sender": sender, "window": None}

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
