# Spec — Browser Extension Bridge (Cầu nối Browser vượt Cloudflare)

> Ngày 30/08/2026 · Branch: `feat/browser-extension-bridge` · Thiết kế kiến trúc chuyển hướng mạng từ CLI qua Browser Extension Manifest V3

---

## 1. Mục tiêu & Phạm vi (Goals & Non-Goals)

### Mục tiêu (Goals)
1. **Loại bỏ 100% rào cản Cloudflare / WAF**: Tận dụng phiên duyệt web thực của người dùng (nơi đã đăng nhập, đã giải Turnstile/Managed Challenge) trên Chrome/Brave/Firefox để thực hiện mọi request mạng.
2. **Trong suốt với người dùng CLI**: Toàn bộ quy trình làm việc (`ctf pull`, `ctf sync`, `ctf submit`, `ctf instance`, `ctf rank`, `ctf watch`) giữ nguyên 100% giao diện và trải nghiệm dòng lệnh (Rich UI, progress bar, storage management).
3. **Extension đóng vai trò trung gian nhẹ (Headless/Relay)**: Extension không can thiệp vào UI giải bài hay lưu trữ file cục bộ; nhiệm vụ duy nhất là nhận lệnh forward `fetch()` từ CLI, thực hiện trên trình duyệt và trả kết quả về.
4. **Tự động chuyển đổi Transport (Adaptive Fallback)**: Khi Extension không bật hoặc không có Cloudflare, CLI vẫn có thể chạy direct HTTP qua `requests`/`curl_cffi`. Khi phát hiện Cloudflare hoặc có cấu hình ưu tiên Bridge, CLI chuyển sang `BrowserBridgeTransport`.

### Phi mục tiêu (Non-Goals)
- Không biến Extension thành giao diện giải CTF đầy đủ (toàn bộ logic tạo workspace, quản lý file, template `solve.py` vẫn nằm ở CLI).
- Không yêu cầu cài đặt native messaging host phức tạp vào registry của hệ điều hành.

---

## 2. Kiến trúc Hệ thống & Luồng Dữ liệu (Architecture & Data Flow)

```
┌─────────────────────────────────────────────────────────────┐
│                       TRÌNH DUYỆT                           │
│     (Chrome / Brave / Firefox — User đã login & pass CF)    │
│                                                             │
│   ┌─────────────────────────────────────────────────────┐   │
│   │            CTF Operations Bridge Extension          │   │
│   │              (Manifest V3 Service Worker)           │   │
│   │                                                     │   │
│   │  • Kết nối ws://127.0.0.1:18888/ws                  │   │
│   │  • Thực hiện fetch(url, {credentials: 'include'})   │   │
│   │  • Stream binary chunks base64 về CLI               │   │
│   │  • Broadcast cookie thay đổi cho CLI                │   │
│   └────────────────────────▲────────────────────────────┘   │
└────────────────────────────┼────────────────────────────────┘
                             │ WebSocket JSON Protocol
┌────────────────────────────┼────────────────────────────────┐
│   ┌────────────────────────▼────────────────────────────┐   │
│   │         Local Bridge Server (Background Daemon)     │   │
│   │              (Port 18888 - Token Protected)         │   │
│   └────────────────────────▲────────────────────────────┘   │
│                            │                                │
│   ┌────────────────────────┴────────────────────────────┐   │
│   │               ctf_downloader Core Engine            │   │
│   │                                                     │   │
│   │  • AdaptiveSession (BrowserBridgeTransport)         │   │
│   │  • PullService / SubmitService / InstanceService    │   │
│   │  • WorkspaceRepo / Storage / UI Terminal            │   │
│   └─────────────────────────────────────────────────────┘   │
│                        MÁY LOCAL                            │
└─────────────────────────────────────────────────────────────┘
```

---

## 3. Giao thức WebSocket RPC (JSON Protocol)

Giao thức truyền thông hai chiều giữa Extension và Local Bridge Server trên `ws://127.0.0.1:18888/ws`:

### A. Handshake & Authentication
Ngay khi mở kết nối WebSocket, Extension gửi:
```json
{
  "type": "HANDSHAKE",
  "token": "<SHARED_BRIDGE_TOKEN>",
  "client": "chrome-extension",
  "version": "1.0.0"
}
```
Server phản hồi:
```json
{
  "type": "HANDSHAKE_ACK",
  "status": "ok",
  "server_version": "3.0.0"
}
```

### B. Forward Request (CLI → Extension)
CLI muốn thực hiện HTTP request (GET/POST/PUT/DELETE):
```json
{
  "type": "REQUEST_FORWARD",
  "id": "req_8f1a2c3d",
  "method": "GET",
  "url": "https://mirror-ctf.compfest.id/api/v1/challenges",
  "headers": {
    "Accept": "application/json",
    "CSRF-Token": "..."
  },
  "body": null,
  "timeout_ms": 30000,
  "binary": false
}
```

### C. Forward Response (Extension → CLI)
Extension hoàn thành `fetch()` và trả về:
```json
{
  "type": "RESPONSE_FORWARD",
  "id": "req_8f1a2c3d",
  "status_code": 200,
  "status_text": "OK",
  "headers": {
    "content-type": "application/json"
  },
  "body": "{\"success\": true, \"data\": [...]}",
  "is_base64": false,
  "error": null
}
```

### D. Binary Stream / Chunking (Tải Attachment)
Đối với file attachment lớn (zip, raw binary, exe):
- `binary: true` trong request.
- Extension tải dạng `ArrayBuffer`, gửi chunk base64 hoặc payload toàn vẹn:
```json
{
  "type": "RESPONSE_FORWARD",
  "id": "req_binary_001",
  "status_code": 200,
  "headers": { "content-type": "application/zip", "content-length": "1048576" },
  "body": "<BASE64_STRING>",
  "is_base64": true,
  "error": null
}
```

### E. Heartbeat & Cookie Broadcast
- Heartbeat: Mỗi 15s gửi ping-pong để giữ kết nối sống.
- Cookie Broadcast: Khi Extension phát hiện cookie `cf_clearance` hoặc `session` mới từ tab CTF, nó tự động gửi `COOKIE_UPDATE` để Bridge cập nhật vào cấu hình local.

---

## 4. Cấu trúc Thư mục & Module

### A. Thư mục `extension/` (Browser Extension)
```text
extension/
├── manifest.json            # Manifest V3 (Brave/Chrome/Firefox compatible)
├── background/
│   ├── service_worker.js    # WebSocket connection, message routing, fetch handler
│   ├── cookie_tracker.js    # Lắng nghe chrome.cookies.onChanged cho domain CTF
│   └── bridge_client.js     # Quản lý vòng đời kết nối WebSocket + auto-reconnect
├── popup/
│   ├── popup.html           # Giao diện hiển thị trạng thái kết nối & target URL
│   ├── popup.css            # Style tối giản đồng bộ tông phosphor / dark theme
│   └── popup.js             # Hiển thị status badge (Connected/Disconnected)
├── icons/
│   ├── icon16.png
│   ├── icon48.png
│   └── icon128.png
└── README.md                # Hướng dẫn Load Unpacked vào trình duyệt trong 10s
```

### B. Thư mục `ctf_downloader/bridge/` (Python Subsystem)
```text
ctf_downloader/bridge/
├── __init__.py
├── constants.py             # Default port (18888), token path, protocol types
├── server.py                # Asyncio WebSocket server chạy loopback 127.0.0.1
├── daemon.py                # Quản lý PID file, auto-start bridge daemon khi cần
├── transport.py             # BrowserBridgeTransport (Adapter tương thích requests.Session)
└── client.py                # Sync/Async IPC client gửi request qua Local Bridge
```

---

## 5. Tích hợp vào `AdaptiveSession` (`ctf_downloader/utils/http_client.py`)

Trong `AdaptiveSession`:
1. Mở rộng cơ chế `_route_request()`:
   - Thử nghiệm theo thứ tự: Direct Request (`requests`) → Browser Fingerprint (`curl_cffi`).
   - Nếu nhận về Cloudflare Challenge (403/503/429 chứa marker `cf-chl-`, `just a moment...`) hoặc nếu cờ `--bridge` / cấu hình `bridge.enabled=true` được bật:
   - **Tự động chuyển tiếp request sang `BrowserBridgeTransport`**.
2. `BrowserBridgeTransport` thực hiện:
   - Kiểm tra daemon `BridgeServer` đã chạy chưa (nếu chưa, tự start daemon nền).
   - Kiểm tra có Extension kết nối không. Nếu Extension đang kết nối, gửi `REQUEST_FORWARD` và đợi response.
   - Nếu không có Extension kết nối sau thời gian chờ (grace period), hiển thị hướng dẫn ngắn gọn cho user:
     `[!] Cần bật Extension trên trình duyệt để vượt Cloudflare cho request này.`
   - Đóng gói kết quả trả về thành `requests.Response` chuẩn (`status_code`, `headers`, `content`, `text`, `json()`).

---

## 6. Bảo mật & Cô lập Môi trường (Security & Isolation)

1. **Loopback Only**: Server Bridge chỉ bind vào `127.0.0.1` (hoặc `::1`), tuyệt đối không bind `0.0.0.0`.
2. **Token Authentication**:
   - Sinh ngẫu nhiên token bảo mật cryptographically secure (32 bytes hex) lưu tại `~/.config/ctf_toolkit/bridge_token`.
   - Extension lưu token này (hoặc tự bắt tay trong lần pairing đầu tiên).
3. **Origin & CORS Safety**:
   - Chỉ cho phép WebSocket connection có origin từ extension (`chrome-extension://...`, `moz-extension://...`).

---

## 7. Kế hoạch Kiểm thử & Đảm bảo Chất lượng (Test Strategy)

1. **`test_bridge_protocol.py`**:
   - Test Handshake, message serialization/deserialization, error formats.
2. **`test_bridge_server.py`**:
   - Test WebSocket server lifecycle, xác thực token, xử lý nhiều request đồng thời, timeout handling.
3. **`test_bridge_transport.py`**:
   - Test tích hợp `BrowserBridgeTransport` với `AdaptiveSession`:
     - Giả lập mock extension response → verify `requests.Response` đầu ra khớp status, headers, json body.
     - Giả lập binary payload base64 → verify file download nhị phân đúng hash SHA-256.
4. **`test_cli_bridge_integration.py`**:
   - Test lệnh `ctf pull`, `ctf sync`, `ctf submit` khi chạy qua Bridge Transport.
5. **Toàn bộ test suite hiện có (41+ tests)** phải tiếp tục PASS 100%.

---

## 8. Trải nghiệm Người dùng (UX Walkthrough)

1. **Cài đặt 1 lần**:
   - Mở Chrome/Brave vào `chrome://extensions` → Bật *Developer mode* → Chọn *Load unpacked* → Chọn thư mục `extension/` của dự án.
   - Popup hiển thị: 🟢 `CTF Operations Bridge: Connected (Port 18888)`.
2. **Thực thi lệnh CLI**:
   - Người dùng gõ:
     ```bash
     ctf pull -u https://mirror-ctf.compfest.id -o /home/light/Workspace/CTF/COMPFEST_18 --update
     ```
   - CLI thông báo:
     `[*] Cloudflare detected — routed request through Browser Extension Bridge ✔`
   - Toàn bộ đề bài và file được tải về sạch sẽ, không bao giờ gặp lỗi 403.
