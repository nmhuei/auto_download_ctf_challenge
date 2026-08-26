# Changelog

Toàn bộ thay đổi đáng chú ý của dự án được ghi lại tại đây.

Format dựa trên [Keep a Changelog](https://keepachangelog.com/vi/1.1.0/).

## [Unreleased]

### Changed

- **ui** (UI v2): meter gradient per-cell → ramp `▰▱` 3 mốc amber (non-TTY/terminal hẹp fallback plain không màu); AppHeader slab → **Phosphor Radar** 4 dòng (scanline `░░▒▒▓▓` full-width · title `CTF·TOOLKIT v3◢` căn giữa · `▍` lệnh + context · `▸` timestamp mép phải); palette amber-only → **Amber Refit** (retune fg.base/muted/faint/solved, giữ accent #FFB000); selection ❯ reverse #14100A-on-#FFB000 + phím số `[n]`, item giải xong strike; lỗi render theo error-tree `├─`/`└─ ACTION REQUIRED`; status overview tách 2 panel TIẾN ĐỘ + GIẢI responsive (≥96 cột xếp ngang, hẹp xếp dọc, sparkline braille nhịp giải trong panel GIẢI)

## [3.0.0] - 2026-08-24

Rebuild kiến trúc trên nhánh `rebuild/architecture` — Layered Monolith + Registry.

### Features

- **pull**: tải tăng dần `--update`/`--refresh-meta` — chỉ tải bài mới, cập nhật metadata bài cũ
- **watch**: tự nhận diện event window + auto-sync trong window + instance keep-alive
- **sniper**: submit hẹn giờ first-blood, có window-guard và blacklist
- **register**: tự đăng ký GZCTF/CTFd + xác thực tempmail, an toàn 1 tài khoản/lần
- **serve**: web dashboard chạy local, thuần stdlib; dashboard POST `/api/submit` đi qua gate SubmitService (flag-format/blacklist/throttle của CLI) — CSRF-lite header bắt buộc, rate-limit 1 submit/5s
- **doctor**: health-check platform — URL/auth/capabilities/window/flag-format
- **storage**: StorageManager (du scan/archive tar.gz/git-push), CLI `ctf storage du/archive` có xác nhận/xoá an toàn, export zip strip-secrets
- **sync**: đồng bộ workspace 2 chiều giữ local-state + kiểm tra drift
- **writeup**: export pack — INDEX.md + file per-challenge + zip
- **notes**: ghi chú/tag cho CTF + lọc trạng thái `--label`/`--search`
- **status**: trạng thái đa chiều solve/flag/writeup/container + attribution sync; header đếm ngược event window LIVE/countdown/ended
- **cli**: nối wire các lệnh sync/export-pack/history/sniper/serve
- **open**: `ctf open` mở thư mục challenge
- **config**: subcommand `ctf config` xem/đặt auto-sync on/off (persist ~/.config/ctf_toolkit/config.json)
- **hoard**: `--list` bảng hàng chờ-submit + `--remove`
- **watch**: tick cập nhật solve attribution theo thời gian thực (spec-gap §4)
- **completions**: bash/zsh cho CLI
- **ui**: widgets theo mẫu btop — gradient meter per-cell, braille graph, footer bar; layer output-discipline (printer/diagnostics/style/theme)

### Refactor

Kiến trúc (phase 1-7):

- tách `Challenge`/`CTFInfo`/`Verdict` sang `models.py`; chia sẻ storage/fileio + constants
- `WorkspaceRepo` hợp nhất truy cập state file, áp dụng cho 4 module
- `session_factory` + `auth_service` dùng chung
- platform registry (decorator cho 5 platform) + detection registry-driven với `platform_resolver`
- downloader registry; CLI mỏng + entrypoint shims
- services pull/status/submit/instance/rank + facades
- legacy: bỏ `input()`/đọc-trực-tiếp state — qua repo + readline pattern; dropbox/mediafire dùng chung `session_factory`; RANKING.md ghi qua WorkspaceRepo atomic write
- watch: dọn `_refresh_live`, bound `_TARGET_LOCKS`

UI:

- thiết kế lại status/workspaces/storage/watch/help/doctor theo palette phosphor — panel/table/theme thống nhất, glyph semantic, bỏ rainbow
- watch panel layout btop: header clock + notices + mini-scoreboard + footer; pull progress spinner tạm thời + gợi ý chẩn đoán; status dashboard meter gradient
- phủ phosphor nốt sniper/register/rank/dashboard/logger-token; chuẩn hoá token amber-only + ramp meter 3-stop; batch polish NICE synthesis-v6; glyph SUMMARY.md đồng bộ ROW_GLYPHS

i18n:

- đầu ra CLI nhất quán tiếng Việt toàn diện — menu/platforms/downloader/instance/submit (batch 2b/2c)

### Fixes

- chống mất dữ liệu: `locked_update_json` lockfile riêng + tmp unique, symlink-consistent; summary an toàn None; guard ReDoS
- status: dual-write lock, safe_int overflow, assessor anti-inflation, guard symlink/nan; flag_format anchored dùng re.M; ctfd by_me fail-safe
- watch: auto-exit cuối window, clock-skew active, cảnh báo source-conflict, lock O_EXCL
- download: resume 416 reset, gdrive quota-fp đúng thứ tự, builder defensive hints/category, domain suffix-match
- rctf: leaderboard phân trang >100 đội (truyền limit/offset bắt buộc)
- register: gzctf hashpow wire-format đúng upstream + type-None không nhầm captcha
- storage: dir-exclude top-level, parse_event_end chuẩn hoá UTC, sync_via_repo dùng update_metadata
- misc: builder json-guard, shortcut content-range 416, ctfd window-scan brace-match
- khôi phục behavior cache empty-challenges
- pull/download (hunter-c9): race ghi `.part`, collision tên file, byte-limit, consent pass-through, tổng kết ghi atomic
- platforms (hunter-c10 + review-3): null-shape không xoá nhầm dữ liệu, contract attribution, rctf kind strict, hashpow difficulty, swap cache TTL-safe
- auth/register/watch/status/storage/fileio (hunter-c7/c8 + review round): residuals R1-R6, backoff sống sót qua lỗi tạm thời, url-key so khớp exact, auto-sync precedence
- watch/keep-alive/sniper/menu (hunter-c11-c13): HTTP 304 clear streak, checkpoint isolate, give-up có thể phục hồi + reset counters sau restart, ctrl-c sạch, index/isdecimal guard
- sync/archive/builder (hunter-c15 + review-6): archive atomic, sync idempotent, delay hữu hạn, lock dọn trong finally, removed_local chỉ xoá khi persist thành công
- escaping/style-injection: rich markup từ solver_names/base_dir/logger interpolation, strip ANSI dữ liệu rank, escape markdown SUMMARY/RANKING, summary ghi có lock; markup=True chỉ tại call-site trang trí chủ ý sau default-escape
- attribution: TTL cache + synced_at chỉ stamp khi metadata thật sự đổi
- cli/serve/export-pack: pull non-tty thiếu --url thoát exit 2 sạch (hết traceback EOFError), serve Content-Length bất hợp lý trả 400 thay vì OverflowError, suffix subdir trùng tên, guard cid None, clamp --poll, bỏ in trùng cảnh báo/tổng kết
- ui (synthesis-v6 must-fix): doctor span, hoard chrome, config phosphor; rank i18n residual + pager footer non-tty

### Performance

- **history**: dựng challenge index một lần thay vì rescan toàn workspace mỗi entry — `ctf history` 2053ms → 30ms (mặc định `--tail 100` entry mới nhất, `--all` in hết); sửa crash rich khi timestamp epoch số
- **assessor**: difflib early-gate bit-exact + memo theo nội dung (blake2b/LRU) — chấm lại writeup không đổi gần như free, verdict giữ nguyên
- **status**: truyền metadata xuyên suốt compute_status → read_status — bỏ double-read metadata.json mỗi challenge trên mọi đường scan/render/scan-all

### Docs

- spec kiến trúc rebuild (Phương án A — Layered Monolith + Registry) + implementation plan 12 tasks/7 phases
- spec Event Window + Challenge Status Model đa chiều; Instance Keep-Alive auto-extend (8 state, ràng buộc thực chiến)
- sync packaging v3.0.0 + README minimal usage/install/setup; DoD cleanup phase 7
- man page `ctf(1)` + completions bash/zsh; README/man đồng bộ CLI thực tế qua 3 vòng (phosphor, open/hoard --list/--remove/config, history --tail/--all, serve POST /api/submit)
- spec đồng bộ theo spec-audit — i18n, glyph 2 tầng, known-deviations

### Tests

- chuỗi boundary-test hunter cycle 7→15 (archive parallel, sync idempotent, sniper finite/dedup, builder locked-write, keep-alive restart...)
- gap coverage UI widgets/meter/pager; khớp precedence `ctf config` với soft-wrap rich
- guard hiệu năng: history (bounded read + tail cap), assessor (tương đương difflib + hợp đồng memo), status meta passthrough (spy đúng 1 lượt đọc/challenge)
- FLAKY_TESTS.md: ghi nhận race môi trường ở smoke hoard
