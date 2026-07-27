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

## 3. Chạy ngầm tự khởi động cùng máy

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

## 4. Icon khay hệ thống
- **Tạm dừng / Bật lại** bắt mã.
- **Mở web quản lý**.
- **Trạng thái hàng đợi** (bao nhiêu mã đang chờ gửi lại khi mất mạng).
- **Thoát**.

## Cơ chế phân biệt quét vs gõ tay
Máy quét gõ rất nhanh (~10–30ms/ký tự). Agent chỉ gom các ký tự đến trong
ngưỡng `inter_key_timeout` (mặc định 0.05s) và kết thúc bằng Enter. Gõ tay chậm
hơn nên không bị nhận nhầm. Chỉnh `inter_key_timeout` / `min_length` trong
`config.ini` nếu cần.
