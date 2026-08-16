# FAI OPS API Reverse Engineering Notes

## Endpoints đã identify (từ APK + browser)

| Method | Path | Mục đích |
|---|---|---|
| GET | `/TPL/partners` | Danh sách đối tác vận chuyển |
| POST | `/TPL/validate-package` | Validate 1 mã kiện, tự detect carrier |
| POST | `/TPL/sessions` | Tạo phiên bàn giao |
| GET | `/TPL/sessions?...` | List phiên (filter status, date) |
| GET | `/TPL/sessions/{id}` | Chi tiết 1 phiên |
| GET | `/TPL/sessions/{id}?isPrint=true` | Xem/in phiếu |
| POST | `/TPL/sessions/{id}/handon` | BÀN GIAO 3PL |
| GET | `/TPL/rma-types`, `/TPL/condition-package-types` | Master data |
| GET | `/TPL/session-statuses`, `/TPL/tpls` | Master data |
| POST | `/package-sealing` | Đóng gói niêm phong |
| POST | `/Auth/refresh-token` | Refresh token (base URL dev) |

## Base URL
- Prod: `https://ops-api.vnfai.com`
- Dev: `https://ops-api.fai.devtest.vn`

## Auth
- Keycloak realm: `imv`
- Client (web UI): `wh-cce` (public client, Authorization Code flow)
- Client (Service Account): `0c0d6ea3-746c-483b-8d15-f7248d109c29` / secret `624e6988-...`
  - Client Credentials flow OK, nhưng aud=`ext-api` không dùng được cho `ops-api.vnfai.com`
- Endpoint token: `https://auth.vnfai.com/auth/realms/imv/protocol/openid-connect/token`

## Headers custom mỗi request

| Header | Ví dụ | Ghi chú |
|---|---|---|
| `Authorization` | `Bearer <access_token>` | JWT từ Keycloak |
| `X-Tenant` | `541` | Tenant ID, khớp `wid` trong JWT |
| `X-Device` | `4d29328b72d5f44e87ab4cdcfc61da80f8132f994fddd19cc119392b6235045f` | 64 hex = SHA256, có vẻ device fingerprint cố định |
| `X-Timezone-Id` | `Asia/Saigon` | |
| `X-Timestamp` | `1786789945720` | Unix ms |
| `X-RequestId` | UUID v4 | Random per-request |
| `X-Content-MD5` | base64 MD5 body | Rỗng nếu GET không body |
| `X-Signature` | `FXHdVdGb7eAf5gIoTLxfRNCD78SfrMPvN3QphFmsfU8=` | HMAC-SHA256 base64 (44 chars) |

## Signature (X-Signature)

Đã brute force các canonical string đơn giản với 2 sample, KHÔNG match:
- Secret candidates: tenant, device, session_state, sub, uid, wid, cid, client_id, session_state không dấu, ""
- Component permutations: method+path+ts+reqid+tenant+device+body_md5 (length 2-6)
- Separators: `\n`, `|`, `:`, ``

**Kết luận**: cần decompile APK (`vn.aipacific.fai` v1.26.3 dev) để lấy chính xác hoặc hỏi FAI.

APK có hàm `hmacSha256Base64` trong classes*.dex.

## JWT decoded (browser flow)
```json
{
  "iss": "https://auth.vnfai.com/auth/realms/imv",
  "aud": "warehouse",
  "azp": "wh-cce",
  "sub": "01e3aceb-6e44-416e-9cb4-ad220f56e9fc",
  "session_state": "906b50e3-5721-4b26-a670-2181b8411db6",
  "uid": 11677, "wid": 541, "cid": 77,
  "scope": "openid fai"
}
```

## Payload samples captured

### POST /TPL/validate-package
```json
{"code":"SPXVN064276259448","tplId":null,"partnerId":null,"saleChannelTPLId":null,"tplSessionType":2}
```

### POST /TPL/sessions
```json
{
  "tplId": 1658,
  "partnerId": 1231,
  "saleChannelTPLId": "326c0000-ea6a-567f-dc18-08db695ee995",
  "note": null,
  "packageCodeOrBillOfLanding": null,
  "keyword": "",
  "packages": [{"id": 46657092}],
  "tplSessionType": 2
}
```
- `tplId`, `partnerId`, `saleChannelTPLId` lấy từ GET /TPL/partners (chưa capture).
- `packages[].id` = internal ID trả về từ validate-package (chưa capture response).
- `tplSessionType: 2` = phiên bàn giao.

### X-Content-MD5 verification
- Format: **hex 32 chars** (không phải base64).
- Formula: `md5(body).hexdigest()` — đã verify khớp.

## Cần thêm để implement REST
- [ ] Response schema của `POST /TPL/validate-package` (để biết `packages[].id` structure)
- [ ] Body + response của `POST /TPL/sessions/{id}/handon` (chưa có)
- [ ] Body + response của `GET /TPL/partners` (biết tplId/partnerId/saleChannelTPLId của J&T, GHN, Best...)
- [ ] Công thức chính xác `X-Signature` (HMAC-SHA256 base64, secret nằm đâu?)

## Cảnh báo bảo mật (báo FAI)
- Telegram bot token hardcode trong APK: `8479553696:AAG3dpCAggRfU6tAYfHUAht27tZjhqDwOUM`
