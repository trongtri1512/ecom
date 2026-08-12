# Scan Ecom — Hệ thống quét mã vận đơn cho vận hành Ecom

Quét mã vận đơn từ nhãn bằng **máy quét 2D**, gộp mã theo **sọt vật lý** (thùng/cần
xé), tự động **bàn giao lên imv.ops** khi đủ điều kiện, xem online realtime.

## Kiến trúc

```
[Máy quét 2D] --gõ như bàn phím--> [Agent Windows (chạy ngầm)]
                                          │  POST /api/scans (X-API-Key)
                                          ▼
                       [Backend FastAPI + PostgreSQL]
                          │           │              │
                          │           │              └─> [Playwright + Chromium]
                          │           │                   tự upload lên imv.ops
                          │           ▼
                          │      [Web UI /]     [Admin /admin]
                          │      quản lý mã     cấu hình + log
                          ▼
                    [SSE realtime -> web & agent]
```

3 thành phần chính, cùng repo:

- **`agent/`** — Scanner Agent .exe trên máy Windows có máy quét. Bắt mã **toàn cục**
  (không cần focus ô nhập), có cửa sổ giao diện, gộp sọt, hàng đợi offline. Xem
  [agent/README.md](agent/README.md).
- **`backend/`** — FastAPI + Postgres + Playwright + phục vụ luôn web tĩnh. Đóng
  Docker.
- **`web/`** — Trang quản lý chính `/` (bảng mã + KPI + sọt) và Admin `/admin`
  (tab ngang: ĐVVC, Test post OPS, Đồng bộ ngược, Sọt, Log OPS).

## Tính năng chính

### Quản lý mã vận đơn
- ✅ Tự nhận diện **ĐVVC theo prefix** (SPX/J&T/Best/GHN/GHTK/Viettel Post/Ninja Van).
  1 hãng có thể có **nhiều prefix**. Sửa ngay trên trang Admin, không cần code.
- ✅ **KPI 1 hàng ngang gọn** — chỉ hiện ĐVVC có đơn (=0 tự ẩn).
- ✅ **Cột "Vị trí"** — hiển thị "📦 Sọt N" nếu mã đã vào sọt, "— Chưa" nếu chưa.
- ✅ **Chống quét trùng có cửa sổ ân hạn** (`DUP_GRACE_SECONDS`, mặc định 60s):
  quét lại trong khoảng này → bỏ qua êm; quá đó → đánh dấu **trùng ×N** trên dòng
  gốc + email Admin + agent kêu cảnh báo. Không xoá dòng.
- ✅ **Sửa mã**, **xoá** (có mật khẩu `DELETE_PASSWORD`), **xoá hàng loạt** (checkbox).
- ✅ **Lọc thời gian**: Hôm nay / Hôm trước / Tuần này / Tuần trước / Tháng này /
  Tháng trước / Quý / Năm / Tất cả (giờ VN).
- ✅ **Xuất Excel** đầy đủ + **Xuất file import OPS** có modal preview trước khi tải.

### Sọt (Basket) — gộp mã theo lô vật lý
- ✅ **Agent tự quản `current_basket_seq`** — mỗi POST `/scans` gửi kèm
  `basket_seq`, server gán `basket_id` NGAY khi INSERT (không phụ thuộc timing,
  không lẫn giữa nhiều agent, mã đúng sọt kể cả khi mất mạng).
- ✅ Header agent hiện label **"🟢 Đang quét vào Sọt N"** — nhân viên biết rõ
  đang gộp vào sọt nào.
- ✅ Bấm **"✅ Hoàn thành sọt"** trên agent → chốt sọt hiện tại → **agent tự
  tăng seq** cho sọt kế tiếp. Sync lại từ server khi restart app
  (`GET /api/baskets/next`).
- ✅ Server **gán `basket_id` cho từng mã** — sọt chứa danh sách mã chính xác.
- ✅ Trang Admin tab **📦 Sọt** hiển thị tất cả sọt trong ngày:
  - **Trạng thái OPS** (badge): ⏳ Chưa bàn giao · ✅ Đã bàn giao · ⚠ Bàn giao
    1 phần · ❌ Thất bại
  - **Mã phiên OPS** đã cấp cho từng ĐVVC (VD `SPX: MVECCEJIPX3K`)
  - Nút **📤 Bàn giao OPS** (chưa bàn giao) hoặc **📋 Xem nhật ký** (đã done)
  - Modal **Nhật ký sọt**: danh sách mã phiên + bảng mã lỗi + lý do CỤ THỂ
    ("đã tồn tại", "OPS âm thầm loại", …) + nút Copy mã lỗi
- ✅ **Backfill** basket_id cho sọt cũ chưa gán (nút trên Admin, idempotent).

### Auto import lên imv.ops (Playwright)
- ✅ Playwright chạy trên VPS: login Keycloak SSO → mở `/tpl-sessions/new/{template_id}`
  → gõ từng mã (OPS tự nhận diện ĐVVC) → bấm **TẠO** → đọc mã phiên **`MVECCE...`** →
  đóng browser (**KHÔNG** tự bấm "Bàn giao 3PL", user tự làm khi hàng thực tế sẵn sàng).
- ✅ Cấu hình per-carrier trong Admin: bật/tắt auto, `AUTO_IMPORT_BATCH`, `template_id`,
  tên đối tác OPS.
- ✅ **Log OPS** ghi vào DB + screenshot khi lỗi. Xem/xoá qua Admin tab **📜 Log**.
- ✅ **Đồng bộ ngược** thủ công: nếu tự tạo phiên trên OPS, dán mã phiên + list mã
  vào Admin để đánh dấu đã xử lý (không double-post).

### Agent Windows (.exe)
- ✅ Bắt mã **toàn cục** — cắm máy quét vào là quét được, không cần focus ô nhập.
- ✅ **Cửa sổ giao diện** riêng: danh sách mã vừa quét · thống kê ĐVVC hôm nay ·
  các sọt · các mã phiên OPS đã tạo.
- ✅ **Hàng đợi offline** SQLite — mất mạng vẫn quét được, tự đẩy lại khi có mạng.
- ✅ **Icon khay hệ thống** — chạy ngầm, khởi động cùng máy (auto-start .bat).
- ✅ **Bíp** phản hồi khi quét: OK / trùng / lỗi.

## Chạy thử local (Docker)

```bash
cd scan-ecom
cp .env.example .env          # sửa API_KEY, mật khẩu DB, OPS_USER/PASS…
docker compose up --build
```

Mở:
- **http://localhost:8000** — trang quản lý mã (không cần login).
- **http://localhost:8000/admin** — trang admin (login `admin/123456` mặc định).

## Deploy lên Dokploy / server thật

1. Push repo lên Git.
2. Trên Dokploy: **Create → Compose**, trỏ tới repo, chọn `docker-compose.yml`.
3. Tab **Environment** dán nội dung `.env` (đổi `API_KEY`, `DELETE_PASSWORD`,
   `OPS_USER`, `OPS_PASS`, `CORS_ORIGINS`=domain thật).
4. Gắn **Domain** cho service `app`, container port **8000**. Bật HTTPS (Let's Encrypt).
5. **Deploy**. Xong, mở domain là ra web app.
6. Trên máy quét Windows: cài `ScanEcomAgent.exe` + `config.ini` (xem
   [agent/README.md](agent/README.md)).

- ✅ **Tự động Cập nhật & Auto-Build Agent trên Windows**: Agent tự động check version mới, tải `.zip` mã nguồn, giải nén đè vào `D:\Tool\agent\` (giữ nguyên config & queue DB trong `dist/`), tự kích hoạt `build_exe.ps1` build lại `.exe` và khởi chạy lại app mượt mà.
- ✅ **Quản lý Release Agent từ Admin Web**: Admin có thể bấm nút **"⚡ Phát hành từ folder agent/"** hoặc upload file `.zip` thủ công để đẩy phiên bản mới xuống tất cả các máy Agent.

## Trang Admin `/admin`

7 tab ngang:

| Tab | Chức năng |
|-----|-----------|
| 📊 **Dashboard** | KPI hôm nay + 5 shortcut nhanh |
| 🚚 **Đơn vị vận chuyển** | Bảng 2 nhóm cột (🔍 Nhận diện prefix + 📤 Auto import) — mỗi hàng có nút **📤 Post** ngay |
| 📤 **Test post OPS** | Chọn ĐVVC + số đơn tối đa → post ngay lên OPS để kiểm cấu hình |
| 🔄 **Đồng bộ ngược** | Dán mã phiên OPS + list mã đã bàn giao thủ công → cập nhật DB |
| 📦 **Sọt** | Bảng các sọt hôm nay, mỗi sọt có nút Bàn giao OPS + nút Backfill basket_id |
| 📜 **Log OPS** | Log mọi lần post OPS (thành công/lỗi) + screenshot debug |
| 🤖 **Quản lý Agent** | Phát hành nhanh bản Agent mới từ server hoặc upload file ZIP + xem lịch sử phiên bản |

## Cấu hình `.env` (tóm tắt)

```env
# --- Auth & bảo mật ---
API_KEY=xxx                     # agent gửi qua header X-API-Key
DELETE_PASSWORD=1512            # xác nhận khi xoá mã trên web
POSTGRES_PASSWORD=xxx           # mật khẩu Postgres

# --- App ---
APP_PORT=8000
CORS_ORIGINS=http://your-domain
DUP_GRACE_SECONDS=60            # cửa sổ ân hạn quét trùng

# --- Auto import imv.ops ---
AUTO_IMPORT_ENABLED=true
AUTO_IMPORT_BATCH=100
OPS_URL=https://imv.ops.vnfai.com
OPS_USER=your-ops-user
OPS_PASS=your-ops-pass
# Fallback nếu chưa cấu hình trên Admin (JSON 1 dòng):
OPS_CARRIER_MAP={"SPX":{"template_id":2,"partner":"SPX Express"}, ...}

# --- Email (tuỳ chọn, báo mã trùng) ---
MAIL_ENABLED=false
SMTP_HOST=smtp.gmail.com
SMTP_USER=xxx
SMTP_PASS=xxx
ADMIN_EMAIL=xxx
```

## Nhận diện ĐVVC (prefix đã chốt từ mã thật)

| ĐVVC | Prefix |
|------|--------|
| SPX | `SPXVN` |
| J&T | `8621` |
| Best Express | `TTVN` hoặc `BE` |
| GHN | `GYAB` |

Sửa/thêm trên trang Admin → tab **🚚 Đơn vị vận chuyển**. Sửa xong bấm **💾 Lưu tất cả** +
**↻ Phân loại lại** trên trang chính.

## Luồng nghiệp vụ chuẩn (khuyến nghị)

```
1. Quét mã vào Agent → bảng chính hiện "— Chưa" (chưa vào sọt)
2. Đóng gói 1 thùng xong → nhân viên bấm "✅ Hoàn thành sọt" trên Agent
3. Web hiện "📦 Sọt 1" cho các mã của sọt đó
4. Admin vào /admin → tab Sọt → bấm "📤 Bàn giao OPS" cho sọt cần bàn giao
5. Playwright tự tạo phiên trên OPS → trả mã MVECCE... vào cột Vị trí không đổi,
   nhưng mã phiên hiện trong tab Log OPS + tab Sọt (có badge "đã bàn giao")
6. Khi tài xế ĐVVC đến, admin tự vào OPS bấm "BÀN GIAO 3PL" cho phiên đó
   (hệ thống KHÔNG tự bấm để tránh bàn giao khi hàng chưa ra khỏi kho)
```

## API tóm tắt

| Method | Path | Mô tả |
|--------|------|-------|
| POST | `/api/scans` | Agent gửi mã (`code`, `source_agent`, `basket_seq`). Cần `X-API-Key`. 201 mới / 409 trùng |
| GET | `/api/scans?q=&carrier=&period=&limit=&offset=` | Danh sách (có `basket_seq`) |
| GET | `/api/summary?period=` | Tổng + phân bố ĐVVC |
| PATCH | `/api/scans/{id}` | Sửa ĐVVC / `code` (sửa mã cần `X-Delete-Password`) |
| DELETE | `/api/scans/{id}` | Xoá (cần `X-Delete-Password`) |
| POST | `/api/scans/bulk-delete` | Xoá hàng loạt theo `ids` (cần `X-Delete-Password`) |
| GET | `/api/export?format=xlsx\|csv&period=` | Xuất bảng đầy đủ |
| GET | `/api/export/import-file?carrier=&period=&limit=&only_picked=` | Xuất Excel format import |
| GET | `/api/export/import-file/preview?...` | Preview trước khi xuất |
| POST | `/api/reclassify` | Phân loại lại toàn bộ mã |
| GET | `/api/carriers` · POST · PATCH `/{id}` · DELETE `/{id}` | Quản lý luật ĐVVC + config auto import |
| POST | `/api/baskets/close?agent_name=&basket_seq=` | Chốt sọt (agent gửi seq đang mở) |
| GET | `/api/baskets?agent_name=&period=` | Danh sách sọt (kèm ops_status, ops_sessions, ops_errors) |
| GET | `/api/baskets/next?agent_name=` | Trả seq kế tiếp — agent sync khi khởi động |
| POST | `/api/baskets/backfill` | Gán basket_id cho sọt cũ (idempotent) |
| POST | `/api/ops/import-now?carrier=&limit=&require_picked=` | Post 1 batch lên OPS (test) |
| POST | `/api/ops/import-basket?basket_id=` | Bàn giao 1 sọt lên OPS |
| POST | `/api/ops/sync-manual` | Đồng bộ ngược mã phiên OPS về DB |
| GET | `/api/ops/logs?limit=` | Danh sách log OPS |
| GET | `/api/ops/logs/{id}/screenshot` | Screenshot lỗi (PNG) |
| DELETE | `/api/ops/logs/{id}` · `/api/ops/logs` | Xoá 1 / tất cả log |
| DELETE | `/api/sessions/carrier/{name}` | Xoá tất cả phiên của 1 ĐVVC (khôi phục về chưa import) |
| GET | `/api/agent/version` | Kiểm tra phiên bản Agent mới nhất hiện tại |
| GET | `/api/agent/download-source` | Tải file .zip mã nguồn Agent mới nhất |
| GET | `/api/agent/releases` | Danh sách lịch sử các bản phát hành Agent |
| POST | `/api/agent/releases/pack-current` | Nén thư mục agent/ trên server và phát hành bản mới |
| POST | `/api/agent/releases/upload` | Upload file .zip mã nguồn Agent thủ công để phát hành |
| GET | `/api/stream` | SSE realtime (scan / update / delete / basket_close / ops_log / auto_import / agent_release) |
| GET | `/admin` | Trang Admin (Basic Auth: `admin/123456`) |

## Ghi chú kỹ thuật

- **Playwright** cần Chromium — Dockerfile dùng base image
  `mcr.microsoft.com/playwright/python:v1.48.0-noble` (đã có sẵn browser).
- **Migration nhẹ** tự chạy khi container start — thêm cột mới vào bảng cũ mà
  không mất dữ liệu.
- **`session_id`** trong bảng `scans` = mã phiên OPS đã gán (ví dụ `MVECCEJIPX3K`).
  Rỗng = chưa bàn giao.
- **`basket_id`** = ID sọt chứa mã đó. NULL = chưa vào sọt.
- **Đóng trình duyệt khi đang post OPS**: an toàn — Playwright chạy trên VPS,
  không phụ thuộc client. Kết quả xem lại trong tab Log OPS.
