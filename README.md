# wghcbot — Bot + Mini App quản lý AmneziaWG

Quản lý VPN AmneziaWG qua Telegram: **bot menu** và **Mini App** (web UI trong Telegram).
Dashboard khách, tạo/gia hạn/khoá config theo tháng, nhắc hết hạn, cảnh báo node bị chặn,
doanh thu, sao lưu.

## ⚠️ Chạy Ở ĐÂU

Chạy **trên chính VPS cài AmneziaWG** (cần iptables, `manage_amneziawg.sh`, đọc config, /proc).
Không chạy trên Railway/máy khác. Hai service: `awgbot` (bot) + `awgpanel` (Mini App, FastAPI).

## Cài / cập nhật

```bash
git clone https://github.com/maumucboy/wghcbot /root/wghcbot   # lần đầu
cd /root/wghcbot
mkdir -p /root/awgbot
cp .env.example /root/awgbot/bot.env
nano /root/awgbot/bot.env      # điền BOT_TOKEN, BOT_PASS, PANEL_URL
chmod 600 /root/awgbot/bot.env
bash deploy-vps.sh
```

Cập nhật: `cd /root/wghcbot && git pull && bash deploy-vps.sh`

## Cloudflare (cho Mini App)

- Bản ghi A `panel.<domain>` → IP VPS, **Proxied (mây cam)**.
- SSL/TLS → Overview → **Full** (chấp nhận cert tự ký ở origin).
- Panel chạy TCP 443; AWG chạy UDP 443 — không đụng nhau.

## Cấu hình (`bot.env`)

| Biến | Ý nghĩa |
|---|---|
| `BOT_TOKEN` | token BotFather |
| `ADMIN_ID` | Telegram ID được phép dùng |
| `BOT_PASS` | mật khẩu `/start` của bot |
| `AWG_ENDPOINT_DOMAIN` | tên miền endpoint ghi vào config khách |
| `AWG_PORT` | cổng AWG (443) |
| `NODE_NAME` | tên node |
| `PANEL_URL` | `https://panel.<domain>` — nút Mini App trong bot |

Giá gói sửa ngay trong **tab Cài** của Mini App (lưu vào DB).

## Tính năng

- Dashboard: online/offline, traffic, hạn, CPU/RAM; tìm kiếm + lọc
- Tạo khách theo gói 1/3/6/12 tháng + dùng thử 3 ngày → gửi `.conf` + QR vào chat
- Gia hạn (cộng dồn), khoá/mở (iptables, không đụng key), xoá, ghi chú/liên hệ
- Khoá tự động khi hết hạn, nhắc trước N ngày
- Cảnh báo khi online tụt mạnh (nghi IP bị chặn)
- Doanh thu tháng, biểu đồ; sao lưu về Telegram
- Mini App: giao diện web đầy đủ, tab Khách/Tạo/Doanh thu/Cài
- Tự nạp khách tạo trước bot

## Router khách (🛰 trên Dashboard)

Bot đăng nhập modem Keenetic/Netcraze của khách qua **KeenDNS** (RCI API) để reconnect tunnel — dùng khi đổi IP server.

- **Thêm router**: gửi `địa_chỉ_KeenDNS tên [mật_khẩu]`. Bỏ mật khẩu → bot sinh mật khẩu, bạn tạo user `hcbot` (quyền web) trên router lúc pre-config.
- **Reconnect tất cả** / **Chỉ cái đang treo** (handshake > 5 phút), **Thử lại lỗi**.
- **Watchdog** (bật/tắt): 5 phút/lần, tự reconnect tunnel treo (mỗi router tối đa 1 lần/15 phút), báo admin khi router mất truy cập.
- Tunnel tự dò theo endpoint `AWG_ENDPOINT_DOMAIN`. Mật khẩu mã hoá (Fernet), key ở `/root/awgbot/router.key` (600) — **sao lưu file này**, mất key = phải nhập lại mật khẩu router.
- Biến tuỳ chọn: `ROUTER_LOGIN` (mặc định `hcbot`), `ROUTER_PARALLEL` (8).
