# Logo Đơn vị vận chuyển

Bỏ file logo (PNG, nền trong suốt càng đẹp) vào thư mục này, đặt tên đúng như bảng
dưới. Agent sẽ tự hiển thị logo bên cạnh số lượng trên giao diện. **Không có file
thì tự dùng badge chữ** — không lỗi gì cả, chỉ là không có hình.

| ĐVVC (tên trong hệ thống) | Tên file cần đặt |
|---------------------------|------------------|
| SPX                       | `spx.png`        |
| J&T                       | `jt.png`         |
| Best Express              | `best-express.png` |
| GHN                       | `ghn.png`        |
| GHTK                      | `ghtk.png`       |
| Viettel Post              | `viettel-post.png` |
| Ninja Van                 | `ninja-van.png`  |
| Other                     | `other.png` (tuỳ chọn) |

## Cách đặt tên (nếu bạn thêm hãng mới)
Lấy tên ĐVVC → viết thường → bỏ dấu tiếng Việt → thay khoảng trắng và ký tự đặc
biệt (`&`, `/`…) bằng dấu gạch ngang `-`. Ví dụ:
- "Best Express" → `best-express.png`
- "J&T" → `jt.png`
- "Viettel Post" → `viettel-post.png`

## Gợi ý
- Kích thước ~ 200×200 px trở lên, nền trong suốt (PNG) cho đẹp.
- Định dạng hỗ trợ: PNG, JPG. PNG nền trong được ưu tiên.
- Tải logo chính thức từ trang thương hiệu / bộ nhận diện của từng hãng.

## Khi chạy bằng file .exe
Logo **KHÔNG** đóng gói vào .exe (để bạn đổi logo mà không phải build lại). Hãy
để thư mục `logos/` **nằm cạnh file `ScanEcomAgent.exe`**, giống như `config.ini`:
```
C:\ScanEcom\
 ├── ScanEcomAgent.exe
 ├── config.ini
 └── logos\
      ├── spx.png
      ├── jt.png
      ├── best-express.png
      └── ghn.png
```
Thêm/đổi logo xong chỉ cần **khởi động lại agent** (không cần build lại .exe).
