# Spec — Event Window: tự nhận diện thời gian giải & auto-sync trong window

> Ngày 24/08/2026 · Phụ thuộc kiến trúc rebuild (services/registry/WorkspaceRepo) · Nguồn: 3 agent nghiên cứu (CTFtime API, platform time sources, watch/sync cơ chế)

## 1. Mục tiêu

Tool tự biết giải **bắt đầu/kết thúc lúc nào** (từ platform server hoặc CTFtime), và chỉ **tự động cập nhật dữ liệu trong khoảng đó** theo policy user chọn đúng một lần. Không auto-sync ngoài window trừ khi user yêu cầu.

## 2. Nguồn thời gian — thứ tự ưu tiên

| # | Nguồn | Cách lấy | Confidence | Ghi chú |
|---|---|---|---|---|
| 1 | **Manual** (`--start/--end` hoặc wizard nhập tay) | user cung cấp | HIGH (override) | Ghi đè mọi nguồn khác |
| 2 | **Platform API** | Xem bảng dưới | HIGH | Admin chính giải đặt, cập nhật khi delay |
| 3 | **CTFtime** | fuzzy-match tên giải | MEDIUM | Organizer nhập, có thể trễ |
| 4 | Không xác định được | hỏi user nhập tay; từ chối thì mode `manual` (không watch) | — | |

**Xung đột**: tự động dùng nguồn cao hơn; chênh lệch >5 phút với nguồn thấp hơn → cảnh báo + log cả hai.

### Endpoint per platform (đã verify từ source)

| Platform | Field | Format ⚠️ | Auth |
|---|---|---|---|
| GZCTF | `GET /api/game/{id}` → `start`, `end` | **epoch milliseconds**; giá trị ≤0 hoặc năm <2000 = chưa đặt lịch | Public |
| rCTF | `GET /api/v1/integrations/client/config` → `startTime`, `endTime` | epoch ms; có thể vắng mặt (env chưa set); fallback `<meta name="rctf-config">` trong HTML | Public |
| CTFd | HTML mọi trang: biến JS `window.init` chứa `'start'`, `'end'` | **unix GIÂY** dạng chuỗi số hoặc `null` | Public (theme chính thức; theme custom có thể thiếu → confidence MEDIUM) |
| custom/generic | Heuristic JSON keys (`start_time`/`startTime`/`begin_time`…) hoặc regex ISO/epoch trong HTML | Nhận diện ms(13 số)/s(10 số)/ISO | LOW — bắt buộc user confirm trước khi dùng tự động |

**Bẫy đơn vị**: GZCTF/rCTF = ms, CTFd = giây + chuỗi null. Phân biệt bằng độ dài chữ số; mọi kết quả chuẩn hoá về `datetime aware UTC`.

## 3. Data model

```python
@dataclass
class EventTimes:
    start_utc: Optional[datetime]   # aware UTC
    end_utc: Optional[datetime]
    confidence: str                 # high | medium | low
    source: str                     # "gzctf:/api/game/{id}" | "ctftime:{id}" | "manual"

class BaseCTFPlatform:
    def fetch_event_times(self) -> Optional[EventTimes]:
        """Không bao giờ raise; default None."""
```

- `platforms/ctftime_resolver.py` (MỚI): gọi thẳng `requests` (không thêm dependency), UA bắt buộc dạng `ctf-downloader/1.0 (+contact)` — UA mặc định requests/curl bị CTFtime chặn 403.
  - `fetch_window(days_back=7, days_ahead=30)`: GET `/api/v1/events/?limit=200&start=<unix>&finish=<unix>` (1 request).
  - `resolve_event_times(title_hint, url_hint=None)`: chuẩn hoá title (lowercase, bỏ năm/punctuation/stopword ctf-quals-finals-open-online) → similarity = max(SequenceMatcher, 0.5*ratio+0.7*jaccard_tokens) → ngưỡng 0.60; URL domain trùng khớp tuyệt đối → score 1.0; top-1 hơn top-2 <0.15 → trả list để wizard hỏi user chọn (tối đa 5).
  - Cache `{platform_url → ctftime_id}` vào challenges.json; lần sau đi thẳng `GET /events/{id}/`.
  - KHÔNG tin tuyệt đối: COMPFEST case chứng minh organizer quên update finish sau extension → luôn ưu tiên platform server khi có xung đột.

## 4. SyncPolicy — wizard hỏi đúng 1 lần

Lần đầu `ctf pull` thành công mà workspace chưa có config:

1. `⏱️ Tự động cập nhật trong lúc giải diễn ra? [Y/n]`
2. Nếu Y: `Chế độ: (1) chỉ trong window giải (2) luôn cập nhật (3) thủ công [1]`
3. `Nhận báo challenge mới/hint mới? [terminal/mặc định]`

Flag override mọi prompt: `ctf watch --once --no-scoreboard --start ... --end ...`. Đổi ý: `ctf config auto-sync off`.

### Lưu trữ

```jsonc
// workspace/.ctf/config.json        — user sở hữu, ghi 1 lần bởi wizard
{ "version": 1,
  "auto_sync": { "enabled": true, "mode": "window",     // window|always|manual
    "policy": {"notices": true, "scoreboard": true, "challenge_rescan": true},
    "intervals_sec": {"notices": 15, "scoreboard": 60, "challenges": 120},
    "grace_seconds": 300, "auto_exit_on_end": true } }

// workspace/.ctf/watch_state.json   — runtime, atomic + lockfile chống chạy đôi
{ "version": 1, "session_id": "uuid",
  "window": {"start": "...ISO...", "end": "...", "source": "platform"},
  "last_synced_at": {"notices": "...", "scoreboard": "...", "challenges": "..."},
  "etag_cache": {...}, "seen_notice_ids": [], "backoff": {"multiplier": 1.0} }
```

EventWindow cũng mirror vào `challenges.json.ctf_info.event_window` cho SUMMARY/dashboard hiển thị.

## 5. Watch service (`services/watch_service.py` — MỚI)

**Mô hình chính**: lệnh `ctf watch` foreground, rich Live UI (icon theo design system: 🩸 blood · ✨ challenge mới · 💡 hint · 📢 thông báo). `--once` chạy đúng 1 vòng rồi exit — entrypoint cho cron/systemd bọc ngoài (tool KHÔNG tự sinh unit/crontab).

- **PollScheduler stdlib-only**: dict task→deadline_monotonic; jitter ±20%; backoff ×2 cap 600s; 429 tôn trọng Retry-After; ETag/304 cache per endpoint.
- **Interval**: notices 15s · scoreboard 60s (adaptive tăng 120s nếu 3 kỳ không đổi) · challenges re-scan 120s (+burst 20-30s trong 2 phút nếu tổng số bài đổi).
- **WindowGuard**: monotonic cho mọi sleep nội bộ; wall-clock chỉ so với start/end; wall < start → pause + in đếm ngược; wall > end+grace → final sync (scoreboard + rank cuối) rồi exit 0 (hoặc idle nếu `auto_exit_on_end=false`). Đồng hồ hệ thống nhảy → phát hiện qua lệch Date header server, cảnh báo NTP.
- **Signal**: SIGINT/SIGTERM → cùng `_shutdown()`: stop Live, flush state atomic, exit 130/0. Checkpoint per-type sau mỗi tick thành công → crash-safe; wake-from-sleep tick ngay (deadline đã quá).
- **Lock**: `watch_state.json.lock` chứa pid; stale-pid → chiếm lại; live-pid → thoát với cảnh báo "watch đang chạy".

## 6. Tích hợp

- `pull_service`: sau khi dựng workspace lần đầu → chạy wizard Event Window (nếu policy=window/always và đang trong window → gợi ý chạy `ctf watch`).
- `status_service`: dashboard header hiển thị ⏱️ window + trạng thái (🔴 LIVE · ⏳ countdown · ✅ ended) khi có event_window.
- Icon bắt buộc theo design system đã ghi ledger (mục "YÊU CẦU DESIGN").

## 7. Testing

- Unit: ctftime_resolver (mock HTTP: match title, multi-candidate, UA header assert), fetch_event_times từng platform (epoch ms vs s, epoch-0=null, meta-tag fallback), PollScheduler (jitter bounds, backoff cap), WindowGuard (clock skew, grace, pause-before-start).
- Integration: watch_service vòng lặp mock platform — tick đúng thứ tự, checkpoint per-type, Ctrl-C mô phỏng raise KeyboardInterrupt giữa tick → state flush.
- Gate: full suite hiện tại phải vẫn xanh; test mới trong `test_event_window.py`.

## 8. Phi mục tiêu

- Không push-notification desktop (v1 chỉ terminal).
- Không tự sinh systemd unit/crontab.
- Không submit flag tự động theo lịch (sniper mode — backlog P2 riêng).

## 9. Instance Keep-Alive (bổ sung theo yêu cầu user)

Hai cơ chế cốt lõi của instance: (1) **bật** (start) đã có; (2) **duy trì** (extend) cần tự động hoá:

### Lệnh
- `ctf instance --id N --auto-extend` — giữ sống 1 container
- `ctf instance --auto-extend-all` — giữ sống mọi container running của workspace
- `ctf watch` — tick keep-alive là một task trong scheduler (interval 60s)

### Cơ chế per-platform
| Platform | Dữ liệu | Cửa sổ extend | Giới hạn |
|---|---|---|---|
| GZCTF | `expectStopAt` (challenge detail context.closeTime) | chỉ nhận trong RenewalWindow ~10' cuối (mặc định); gọi sớm → 400 | +ExtensionDuration/lần (mặc định 120'); vô hạn lượt |
| CTFd whale | GET container `remaining_time` | renew bất kỳ lúc nào (PATCH) | `docker_max_renew_count` mặc định 5 lần |

### Hành vi WatchKeepAlive.tick()
1. Đọc `expectStopAt`/`remaining_time` mọi container đang tracking → dashboard hiển thị đếm ngược ⏱️.
2. Khi remaining <= RenewalWindow (GZCTF) hoặc < 10' (whale): tự gọi extend, ghi log "🔄 Extended flask-jail +120m".
3. Whale: đếm renew_count; còn ≤1 lượt → icon 🔴 cảnh báo; hết lượt mà remaining thấp → 📢 "container sẽ chết sau X phút — không extend được nữa".
4. Container chết bất ngờ (status Destroyed khi poll): cập nhật metadata.instance_info.status + 📢 báo.
5. Sau end-of-window (EventWindow hết): dừng auto-extend trừ khi practice_mode=true.

### Testing
Mock platform: tick đúng thời điểm cửa sổ (không sớm hơn — tránh 400 GZCTF); whale hết lượt → không gọi PATCH nữa; poll phát hiện Destroyed → sync local.

### Ràng buộc thực chiến (kinh nghiệm user — BẮT BUỘC tuân thủ)

**R-A. Restart ĐỔI FLAG với một số bài dynamic.**
Với challenge dynamic mà flag sinh theo instance (không theo teamToken), tạo lại container = flag cũ chết.
→ Quy tắc: TUYỆT ĐỐI không auto-restart âm thầm khi `status.flag.value` đã có (hoarded/submitted):
  (1) nếu chưa submit đúng → cảnh báo + xác nhận "restart sẽ ĐỔI FLAG của bài này, flag bạn đang giữ sẽ hết hiệu lực";
  (2) nếu user đồng ý (hoặc --yes): restart xong set `status.flag.state = found_unverified` + xoá `flag.value`, note "🔄 flag đã rotate do restart";
  (3) nếu đang auto-mode không có người (watch), chỉ restart khi flag.value == null; có flag → dừng ở mức 📢 critical chờ user.
Điều này ghi đè mọi mặc định khác trong mục 9.

**R-B. Lỗi 502 qua entry KHÔNG có nghĩa là container đã chết.**
Một số platform trả 502 từ lớp limit/proxy của instance trong khi backend vẫn chạy bình thường.
→ Quy tắc health-check: 502/503/504 từ entry KHÔNG được tính là dấu hiệu chết; chỉ tin (a) trạng thái API chính thức (status Destroyed / expectStopAt quá khứ), hoặc (b) TCP connect fail NHIỀU lần liên tiếp (≥3 lần cách nhau ≥30s). Trước khi kết luận dead phải cross-check status API; nếu API vẫn báo Running + còn hạn → chỉ hiển thị ⚠️ "502 tạm thời (limit)" chứ không restart.
