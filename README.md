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
- ✅ Tự nhận diện ĐVVC theo định dạng mã (SPX, GHN, J&T, Best, GHTK, Viettel Post,
  Ninja Van…); mã lạ → **Other**. Sửa luật trong [backend/app/carriers.py](backend/app/carriers.py).
- ✅ Tổng hợp **Total** + phân chia theo ĐVVC (thẻ KPI realtime).
- ✅ **Chống quét trùng có cửa sổ ân hạn**: quét lại trong `DUP_GRACE_SECONDS`
  (mặc định 60s) kể từ lần đầu → **bỏ qua êm** (tránh báo nhầm khi lỡ quét đúp).
  Quá thời gian đó mới tính **trùng** → chặn + **email Admin** + agent **kêu cảnh báo tại máy**.
- ✅ **Xuất Excel / CSV**.
- ✅ Tìm kiếm, lọc theo ĐVVC, sửa NCC/ghi chú, xoá.
- ✅ Cột **NCC** để trống — điền tay bây giờ, để dành auto-map sau.
- ✅ Realtime qua SSE; agent có **hàng đợi offline** khi mất mạng.

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
Dropdown thời gian trên web: **Hôm nay / Tuần này / Tháng này / Quý này / Năm nay**
(tính theo giờ VN). Áp dụng cho KPI Total, bảng danh sách và cả khi Xuất Excel/CSV.

## API tóm tắt
| Method | Path | Mô tả |
|--------|------|-------|
| POST | `/api/scans` | Agent gửi mã (cần `X-API-Key`). 201 mới / 409 trùng |
| GET | `/api/scans?q=&carrier=&period=&limit=&offset=` | Danh sách (period: day/week/month/quarter/year) |
| GET | `/api/summary?period=` | Total + theo ĐVVC (lọc kỳ) |
| PATCH | `/api/scans/{id}` | Sửa NCC/ghi chú/ĐVVC |
| DELETE | `/api/scans/{id}` | Xoá |
| GET | `/api/export?format=xlsx\|csv&period=` | Xuất file (lọc kỳ) |
| POST | `/api/reclassify` | Phân loại lại toàn bộ |
| GET | `/api/carriers` · POST · PATCH `/{id}` · DELETE `/{id}` | Quản lý luật ĐVVC |
| GET | `/api/stream` | SSE realtime |
