# Scanner Agent — bắt mã vận đơn chạy ngầm

Agent này chạy trên **máy có gắn máy quét mã vạch 2D**. Nó bắt mã **toàn cục**
(dù con trỏ đang ở app nào), tự nhận diện, rồi đẩy lên server. Có icon khay hệ
thống, tiếng bíp phản hồi, và hàng đợi offline khi mất mạng.

## 1. Cài đặt

```bash
cd agent
python3 -m venv .venv
# Windows: .venv\Scripts\activate     |  macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

cp config.ini.example config.ini   # Windows: copy config.ini.example config.ini
```

Sửa `config.ini`:
- `url` = địa chỉ backend đã deploy trên Dokploy (vd `https://scan.congty.com`).
- `api_key` = **khớp** biến `API_KEY` của backend.
- `name` = tên bàn/quầy để phân biệt nguồn quét.

## 2. Chạy thử

```bash
python scanner_agent.py
```

Mở app bất kỳ (Notepad, Word…), quét thử 1 mã → nghe bíp "tít" (thành công).
Quét lại đúng mã đó → nghe "tít-tít" (trùng, đã bị chặn + gửi email Admin).
Vào web app xem mã đã lên danh sách.

> **macOS:** cần cấp quyền **Đầu vào (Input Monitoring)** và **Trợ năng
> (Accessibility)** cho Terminal/Python trong *System Settings → Privacy & Security*.
> **Windows:** không cần quyền đặc biệt.

## 🟢 Cách nhanh nhất cho Windows: đóng thành file .exe

Không muốn cài Python trên từng máy quét? Build 1 lần thành `ScanEcomAgent.exe`
rồi chép chạy trên mọi máy Windows.

**Build (làm 1 lần) — cách đơn giản nhất, KHÔNG cần cài sẵn Python:**

Mở **PowerShell** tại thư mục `agent`, chạy:
```powershell
powershell -ExecutionPolicy Bypass -File build_exe.ps1
```
Script tự làm hết: **cài Python bằng winget nếu máy chưa có** → tạo môi trường →
cài thư viện → build ra **`dist\ScanEcomAgent.exe`**.

> Nếu winget vừa cài Python xong mà báo "chưa thấy Python": đóng PowerShell, mở
> lại, chạy lệnh trên lần nữa (Windows cần nạp lại PATH).

*(Cách cũ nếu đã có sẵn Python: nhấp đúp **`build_exe.bat`**.)*

**Chạy trên máy quét:**
1. Chép **`ScanEcomAgent.exe`** + **`config.ini`** (đã sửa `url`, `api_key`) vào
   CÙNG một thư mục (ví dụ `C:\ScanEcom\`).
2. Nhấp đúp `ScanEcomAgent.exe` → chạy ngầm, hiện **icon barcode ở khay đồng hồ**.
   (Không có cửa sổ đen. `config.ini` và `queue.db` nằm cạnh file .exe.)

**Tự chạy khi mở máy:** chạy **`install_autostart.bat`** 1 lần — nó tạo shortcut
trong thư mục Startup. Từ lần bật máy sau, agent tự chạy ngầm.
(Gỡ: `Win+R` → `shell:startup` → xoá `ScanEcomAgent.lnk`.)

> Cần cài Python chỉ ở **máy dùng để build**. Máy quét chỉ cần file .exe.

## 3. Chạy ngầm tự khởi động cùng máy (nếu chạy bằng Python, không dùng .exe)

### Windows
Tạo file `start_agent.vbs` (chạy ẩn, không hiện cửa sổ đen):
```vbs
Set sh = CreateObject("WScript.Shell")
sh.Run "cmd /c cd /d C:\đường\dẫn\agent && .venv\Scripts\python.exe scanner_agent.py", 0
```
Nhấn `Win+R` → gõ `shell:startup` → chép shortcut của `start_agent.vbs` vào đó.
Máy khởi động là agent tự chạy ngầm (icon barcode ở khay đồng hồ).

### macOS (launchd)
Tạo `~/Library/LaunchAgents/com.imv.scanecom.plist`:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict>
  <key>Label</key><string>com.imv.scanecom</string>
  <key>ProgramArguments</key>
  <array>
    <string>/đường/dẫn/agent/.venv/bin/python</string>
    <string>/đường/dẫn/agent/scanner_agent.py</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict></plist>
```
```bash
launchctl load ~/Library/LaunchAgents/com.imv.scanecom.plist
```

## 4. Cửa sổ giao diện
Khi chạy (nếu `show_window = true`), agent mở cửa sổ chia 2 phần:
- **Bên trái (~1/3): Mã vừa quét** — danh sách realtime, tô màu: 🟢 đã thêm ·
  🟡 quét lại <1p · 🔴 trùng.
- **Bên phải (~2/3): Số lượng theo ĐVVC HÔM NAY** — thẻ Tổng nổi bật + từng ĐVVC
  (SPX/J&T/Best/GHN…). Số liệu lấy từ **server** nên gồm **tất cả mã trong ngày**
  (kể cả máy khác quét), **tự làm mới mỗi 5 giây** và ngay sau mỗi lần quét.
- Nút: ⏸ Tạm dừng · 🌐 Web quản lý · Ẩn xuống khay.
- Bấm **X = thu nhỏ xuống khay** (agent vẫn chạy ngầm), mở lại bằng nhấp đúp icon khay.

## 5. Icon khay hệ thống
- **Mở cửa sổ** (nhấp đúp icon).
- **Tạm dừng / Bật lại** bắt mã.
- **Mở web quản lý**.
- **Trạng thái hàng đợi** (bao nhiêu mã đang chờ gửi lại khi mất mạng).
- **Thoát**.

## Cơ chế phân biệt quét vs gõ tay
Máy quét gõ rất nhanh (~10–30ms/ký tự). Agent chỉ gom các ký tự đến trong
ngưỡng `inter_key_timeout` (mặc định 0.05s) và kết thúc bằng Enter. Gõ tay chậm
hơn nên không bị nhận nhầm. Chỉnh `inter_key_timeout` / `min_length` trong
`config.ini` nếu cần.

## Nhiều máy quét trên cùng 1 máy tính
Cắm **2 (hoặc nhiều) máy quét** vào cùng 1 máy tính vẫn chạy bình thường — **chỉ
cần 1 agent**. Lý do: mọi máy quét đều hoạt động như bàn phím, "gõ" vào chung
dòng bàn phím của Windows; agent nghe chung nên nhận mã từ **tất cả** máy quét và
gộp vào **cùng 1 danh sách** (không phân biệt nguồn — đúng nhu cầu hiện tại).

- Mỗi mã được máy quét tự kết thúc bằng Enter → agent tách mã chính xác.
- Nếu 2 người bấm quét **đúng cùng một khoảnh khắc**, ký tự 2 mã có thể xen kẽ →
  hiếm, nhưng agent có cơ chế tự-bỏ buffer khi mất Enter để hạn chế. **Khuyến nghị
  vận hành:** quét lần lượt (chênh nhau ~0.2 giây) là an toàn tuyệt đối.

## Luồng dữ liệu (data flow)
```
[Máy quét 1] --+
[Máy quét 2] --+  "gõ" ký tự + Enter (như bàn phím)
               |
               v
   +------------------------------+
   | ScanEcomAgent (1 tiến trình) |  gom ký tự -> thành 1 mã vận đơn
   +------------------------------+
               |  POST /api/scans  { code, scanned_at, source_agent } + header X-API-Key
               |  (mất mạng -> lưu queue.db, tự gửi lại khi có mạng)
               v
   Server (VD http://103.73.67.64:8000)
     - tự nhận diện ĐVVC theo prefix
     - chống trùng: <60s bỏ qua êm | >60s = trùng -> chặn + email + agent kêu cảnh báo
     - lưu PostgreSQL
               |  đẩy sự kiện realtime (SSE)
               v
   Web app: KPI theo ĐVVC, bảng danh sách, xuất Excel — nhiều người xem cùng lúc
```
Nhiều máy quét (kể cả ở nhiều máy tính khác nhau) đều đẩy về **cùng 1 server**,
xem chung **1 danh sách** realtime.
