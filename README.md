# Scan Ecom — Hệ thống quét mã vận đơn cho vận hành Ecom

Quét mã vận đơn từ nhãn bằng **máy quét 2D**, tự động tổng hợp và phân loại theo
**đơn vị vận chuyển (ĐVVC)**, chống trùng, xuất Excel — xem online realtime.

## Kiến trúc

```
[Máy quét 2D] → gõ như bàn phím → [Scanner Agent (chạy ngầm)]
                                        │ POST /api/scans (X-API-Key)
                                        ▼
                          [Backend FastAPI + PostgreSQL]  ──(SSE realtime)──▶  [Web App]
```

- **`agent/`** — Scanner Agent chạy trên máy có máy quét. Bắt mã **toàn cục**
  (không cần focus ô nhập), đẩy lên server. Xem [agent/README.md](agent/README.md).
- **`backend/`** — API + phục vụ luôn web tĩnh. Vào Docker.
- **`web/`** — giao diện quản lý (KPI, bảng, tìm/sửa/xoá, xuất Excel, realtime).

## Tính năng
- ✅ Tự nhận diện ĐVVC theo prefix mã (SPX, GHN, J&T, Best, GHTK, Viettel Post,
  Ninja Van…); mã lạ → **Other**. Sửa luật **ngay trên web** (nút ⚙️ Quản lý ĐVVC).
- ✅ Tổng hợp **Total** + phân chia theo ĐVVC (thẻ KPI realtime).
- ✅ **Chống quét trùng có cửa sổ ân hạn** (`DUP_GRACE_SECONDS`, mặc định 60s):
  quét lại trong khoảng này → bỏ qua êm; quá đó → đánh dấu **trùng ×N** trên dòng
  gốc + email Admin + agent kêu cảnh báo. **Không xoá dòng.**
- ✅ **Cột Trạng thái lấy hàng**: "✔ Đã lấy hàng" / "ĐVVC chưa lấy hàng". Bấm để
  đổi tay, hoặc dùng **🔎 Tra trạng thái** (Playwright tự đọc trang hãng — hiện SPX).
- ✅ **Lọc thời gian**: Hôm nay / Hôm trước / Tuần này / Tuần trước / Tháng này /
  Tháng trước / Quý / Năm / Tất cả (giờ VN). Áp cho KPI, bảng và khi xuất file.
- ✅ **Xuất Excel** (đầy đủ cột) + **📤 Xuất file import** (đúng format 1 cột "Mã"
  để import lên hệ thống nội bộ; chỉ lấy đơn ĐÃ lấy hàng, giới hạn số lượng cấu hình được).
- ✅ Tìm kiếm, lọc theo ĐVVC, xoá (có **mật khẩu xoá** kiểm ở server).
- ✅ Realtime qua SSE; agent có **cửa sổ giao diện** + **hàng đợi offline** khi mất mạng.

> Ghi chú: cột **NCC / Ghi chú** đã bỏ khỏi giao diện (theo yêu cầu vận hành);
> schema DB vẫn giữ cột để dùng lại sau nếu cần.

## Chạy thử local (Docker)

```bash
cd scan-ecom
cp .env.example .env          # sửa API_KEY, mật khẩu DB, SMTP…
docker compose up --build
```

Mở http://localhost:8000 → thấy web app. Thử API:

```bash
# Thêm 1 mã (đổi API_KEY cho khớp .env)
curl -X POST http://localhost:8000/api/scans \
  -H "X-API-Key: doi-thanh-chuoi-ngau-nhien" \
  -H "Content-Type: application/json" \
  -d '{"code":"SPXVN123456789","source_agent":"test"}'

# Quét lại đúng mã đó -> 409 trùng + email Admin
# Tổng hợp:
curl http://localhost:8000/api/summary
```

## Deploy lên Dokploy

1. Push repo này lên Git (GitHub/GitLab).
2. Trong Dokploy: **Create → Compose**, trỏ tới repo, chọn file `docker-compose.yml`.
3. Vào tab **Environment** của app, dán nội dung `.env` (đổi `API_KEY`, mật khẩu DB,
   `SMTP_*`, `ADMIN_EMAIL`). Đặt `CORS_ORIGINS` = domain thật (vd `https://scan.congty.com`).
4. Gắn **Domain** cho service `app`, container port **8000**. Bật HTTPS (Let's Encrypt).
5. **Deploy**. Xong, mở domain là ra web app.
6. Trên máy có máy quét: cấu hình `agent/config.ini` với `url` = domain vừa gắn,
   `api_key` khớp, rồi chạy agent (xem [agent/README.md](agent/README.md)).

## Chỉnh luật nhận diện ĐVVC (ngay trên web, không cần code)
Bấm nút **⚙️ Quản lý ĐVVC** trên web để **thêm / sửa / xoá** hãng bằng cách gõ
**prefix** (chuỗi đầu mã, vd `SPXVN`, `8621`, `GYAB`) — không cần đụng code hay
deploy lại. Bấm **💾 Lưu & phân loại lại toàn bộ** để áp dụng cho cả mã cũ.

Prefix đã chốt từ mã thật: **SPX**=`SPXVN`, **J&T**=`8621`, **Best Express**=`TTVN`/`BE`,
**GHN**=`GYAB`. Luật lưu trong DB (bảng `carrier_rules`); giá trị mặc định seed từ
[backend/app/carriers.py](backend/app/carriers.py) khi bảng còn rỗng.

## Lọc theo thời gian
Dropdown: **Hôm nay / Hôm trước / Tuần này / Tuần trước / Tháng này / Tháng trước /
Quý này / Năm nay / Tất cả** (giờ VN). Áp cho KPI, bảng và khi xuất file.

## Tra trạng thái lấy hàng (Playwright)
Nút **🔎 Tra trạng thái** gọi `POST /api/track/run` — server chạy Playwright (Chromium
trong Docker) mở trang tracking của hãng, đọc DOM tìm dòng
**"Đơn vị vận chuyển lấy hàng thành công"** → cập nhật cột trạng thái. Hiện hỗ trợ
**SPX** (đã xác thực bằng mã thật); J&T/Best/GHN để khung trong
[backend/app/tracker.py](backend/app/tracker.py), bổ sung selector sau.

> Lưu ý: dùng trang tracking công khai (không phải API chính thức) — nên chạy từ
> server ở VN, tốc độ có giới hạn (delay giữa các mã để tránh bị chặn).

## Xuất file import lên hệ thống nội bộ
Nút **📤 Xuất file import** (chọn 1 ĐVVC + số lượng) → `GET /api/export/import-file`
xuất Excel đúng format (2 sheet: "Dữ Liệu" cột **Mã** + "Diễn Giải"), chỉ gồm các
đơn **đã lấy hàng** trong ngày, tối đa `limit` đơn (mặc định 100). File này import
thẳng lên hệ thống TPL nội bộ.

## API tóm tắt
| Method | Path | Mô tả |
|--------|------|-------|
| POST | `/api/scans` | Agent gửi mã (cần `X-API-Key`). 201 mới / 409 trùng |
| GET | `/api/scans?q=&carrier=&period=&limit=&offset=` | Danh sách (lọc kỳ) |
| GET | `/api/summary?period=` | Total + theo ĐVVC (lọc kỳ) |
| PATCH | `/api/scans/{id}` | Sửa ĐVVC / `pickup_status` (picked\|pending) |
| DELETE | `/api/scans/{id}` | Xoá (cần header `X-Delete-Password`) |
| GET | `/api/export?format=xlsx\|csv&period=` | Xuất bảng đầy đủ (lọc kỳ) |
| GET | `/api/export/import-file?carrier=&limit=` | Xuất Excel format import (đơn đã lấy) |
| POST | `/api/track/run` | Tra trạng thái lấy hàng (Playwright, chạy nền) |
| POST | `/api/reclassify` | Phân loại lại toàn bộ |
| GET | `/api/carriers` · POST · PATCH `/{id}` · DELETE `/{id}` | Quản lý luật ĐVVC |
| GET | `/api/stream` | SSE realtime |
