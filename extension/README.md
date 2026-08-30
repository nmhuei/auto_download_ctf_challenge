# ⚡ CTF Operations Bridge Extension

Browser Extension Manifest V3 làm cầu nối trong suốt chuyển tiếp mọi network request từ CLI toolkit qua context của trình duyệt nhằm vượt 100% rào cản Cloudflare Turnstile / WAF.

## 🚀 Hướng dẫn Cài đặt (10 giây):

1. Mở trình duyệt (Brave / Chrome / Chromium / Edge / Arc).
2. Truy cập: `chrome://extensions/`
3. Bật công tắc **Developer mode** (Góc trên bên phải).
4. Nhấn nút **Load unpacked** (Tải tiện ích đã giải nén).
5. Chọn thư mục `extension/` này trong dự án.
6. Extension sẽ tự động kết nối nền tới CLI Bridge (`ws://127.0.0.1:18888/ws`).

Badge trên icon sẽ hiện 🟢 **ON** khi đã kết nối sẵn sàng.
