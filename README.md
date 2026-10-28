# wghcbot — Bot quản lý AmneziaWG

Bot Telegram quản lý VPN AmneziaWG: dashboard khách, tạo/gia hạn/khoá config theo tháng,
nhắc hết hạn, cảnh báo node bị chặn, doanh thu, sao lưu.

## ⚠️ Bot chạy Ở ĐÂU

Bot **phải chạy trên chính VPS cài AmneziaWG**, vì nó cần quyền local của server:

- gọi `manage_amneziawg.sh` (tạo/xoá peer)
- `iptables` (khoá/mở khách theo IP tunnel)
- đọc `/etc/amnezia/amneziawg/awg0.conf`, các file `.conf`/`.png`
- đọc CPU/RAM từ `/proc`

**Không chạy trên Railway hay máy khác** — máy khác không với tới iptables/script/config
của server VPN. Muốn host ngoài VPS thì phải viết lại cho chạy qua SSH (chưa làm).

## Cài trên VPS

```bash
git clone https://github.com/maumucboy/wghcbot /root/wghcbot
cd /root/wghcbot
mkdir -p /root/awgbot
cp .env.example /root/awgbot/bot.env
nano /root/awgbot/bot.env    # điền BOT_TOKEN, BOT_PASS, tên miền
chmod 600 /root/awgbot/bot.env
bash deploy-vps.sh
```

Cập nhật sau này:

```bash
cd /root/wghcbot && git pull && bash deploy-vps.sh
```

## Cấu hình (`bot.env`)

| Biến | Ý nghĩa |
|---|---|
| `BOT_TOKEN` | token BotFather |
| `ADMIN_ID` | Telegram ID được phép dùng |
| `BOT_PASS` | mật khẩu nhập ở `/start` |
| `AWG_ENDPOINT_DOMAIN` | tên miền endpoint ghi vào config khách |
| `AWG_PORT` | cổng AWG (443) |
| `NODE_NAME` | tên node hiển thị |

Giá gói sửa trong `bot.py` (`PLAN_PRICES`), rồi `systemctl restart awgbot`.

## Tính năng

- Dashboard: online/offline, traffic, số ngày còn lại, CPU/RAM
- Tạo khách theo gói 1/3/6/12 tháng + dùng thử 3 ngày → gửi `.conf` + QR
- Gia hạn (cộng dồn), khoá/mở (iptables, không đụng key), xoá, ghi chú
- Khoá tự động khi hết hạn, nhắc trước 3 ngày
- Cảnh báo khi số khách online tụt mạnh (nghi IP bị chặn)
- Doanh thu tháng, sao lưu về Telegram
- Tự nạp khách tạo trước bot vào danh sách
