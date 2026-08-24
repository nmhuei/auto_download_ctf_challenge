# Changelog

Toàn bộ thay đổi đáng chú ý của dự án được ghi lại tại đây.

Format dựa trên [Keep a Changelog](https://keepachangelog.com/vi/1.1.0/).

## [3.0.0] - 2026-08-24

Rebuild kiến trúc trên nhánh `rebuild/architecture` — Layered Monolith + Registry.

### Features

- **pull**: tải tăng dần `--update`/`--refresh-meta` — chỉ tải bài mới, cập nhật metadata bài cũ
- **watch**: tự nhận diện event window + auto-sync trong window + instance keep-alive
- **sniper**: submit hẹn giờ first-blood, có window-guard và blacklist
- **register**: tự đăng ký GZCTF/CTFd + xác thực tempmail, an toàn 1 tài khoản/lần
- **serve**: web dashboard đọc-only chạy local, thuần stdlib
- **doctor**: health-check platform — URL/auth/capabilities/window/flag-format
- **storage**: StorageManager (du scan/archive tar.gz/git-push), CLI `ctf storage du/archive` có xác nhận/xoá an toàn, export zip strip-secrets
- **sync**: đồng bộ workspace 2 chiều giữ local-state + kiểm tra drift
- **writeup**: export pack — INDEX.md + file per-challenge + zip
- **notes**: ghi chú/tag cho CTF + lọc trạng thái `--label`/`--search`
- **status**: trạng thái đa chiều solve/flag/writeup/container + attribution sync; header đếm ngược event window LIVE/countdown/ended
- **cli**: nối wire các lệnh sync/export-pack/history/sniper/serve
- **ui**: widgets theo mẫu btop — gradient meter per-cell, braille graph, footer bar; layer output-discipline (printer/diagnostics/style/theme)

### Refactor

Kiến trúc (phase 1-7):

- tách `Challenge`/`CTFInfo`/`Verdict` sang `models.py`; chia sẻ storage/fileio + constants
- `WorkspaceRepo` hợp nhất truy cập state file, áp dụng cho 4 module
- `session_factory` + `auth_service` dùng chung
- platform registry (decorator cho 5 platform) + detection registry-driven với `platform_resolver`
- downloader registry; CLI mỏng + entrypoint shims
- services pull/status/submit/instance/rank + facades

UI:

- thiết kế lại status/workspaces/storage/watch/help/doctor theo palette phosphor — panel/table/theme thống nhất, glyph semantic, bỏ rainbow
- watch panel layout btop: header clock + notices + mini-scoreboard + footer; pull progress spinner tạm thời + gợi ý chẩn đoán; status dashboard meter gradient

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

### Docs

- spec kiến trúc rebuild (Phương án A — Layered Monolith + Registry) + implementation plan 12 tasks/7 phases
- spec Event Window + Challenge Status Model đa chiều; Instance Keep-Alive auto-extend (8 state, ràng buộc thực chiến)
- sync packaging v3.0.0 + README minimal usage/install/setup; DoD cleanup phase 7
