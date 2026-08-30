# Changelog

Toàn bộ thay đổi đáng chú ý của dự án được ghi lại tại đây.

Format dựa trên [Keep a Changelog](https://keepachangelog.com/vi/1.1.0/).

## [Unreleased]

### Changed

- **ui** (UI v2): meter gradient per-cell → ramp `▰▱` 3 mốc amber (non-TTY/terminal hẹp fallback plain không màu); AppHeader slab → **Phosphor Radar** 4 dòng (scanline `░░▒▒▓▓` full-width · title `CTF·TOOLKIT v3◢` căn giữa · `▍` lệnh + context · `▸` timestamp mép phải); palette amber-only → **Amber Refit** (retune fg.base/muted/faint/solved, giữ accent #FFB000); selection ❯ reverse #14100A-on-#FFB000 + phím số `[n]`, item giải xong strike; lỗi render theo error-tree `├─`/`└─ ACTION REQUIRED`; status overview tách 2 panel TIẾN ĐỘ + GIẢI responsive (≥96 cột xếp ngang, hẹp xếp dọc, sparkline braille nhịp giải trong panel GIẢI)
- **ui** (splash): logo dual-tier in ĐÚNG MỘT LẦN khi vào `ctf menu`, trước radar AppHeader đầu tiên của phiên — terminal ≥ 80 cột nhận bản `big` 78×13 tên đầy đủ CTF-TOOLKIT trong khung box-drawing kèm scanline, hẹp hơn tự rơi về bản pagga HUD rail 46×6; art nhúng nguyên văn dạng text (không gọi pyfiglet lúc runtime), màu áp ở lớp render qua theme token — các lệnh framed giữ nguyên radar 4 dòng

### Fixes

- **cloudflare/adaptive transport** (2026-08-30): shared HTTP session tự nhận diện Cloudflare Challenge Page qua `cf-mitigated: challenge` (marker HTML chỉ fallback), lazily chuyển sang `curl_cffi` browser TLS/HTTP fingerprint (`impersonate=chrome`) nhưng giữ `requests.Session` làm transport mặc định. GET/HEAD/OPTIONS challenge chỉ replay đúng 1 lần; trước mutation đầu tiên trên mỗi origin, tool HEAD-preflight an toàn để kích hoạt browser transport/cf_clearance TRƯỚC side effect; Managed Challenge còn tồn tại thì raise `CloudflareChallengeError` trước POST, còn challenge chỉ xuất hiện ở POST thì tuyệt đối không replay. Cookie bridge hai chiều giữ `cf_clearance`, register có `--cf-clearance`, auth map persist clearance cùng platform session cookie; CTFd submit/start/stop/extend short-circuit khi Cloudflare chặn thay vì thử thêm mutation endpoint fallback. Binary streaming qua curl_cffi dùng `contextlib.closing` (không giả định response context-manager), worker download kế thừa CF state, Range accelerator tắt khi browser transport active, curl transport error normalize về `requests.RequestException`. Fix credential leak cross-origin: platform cookie được scope theo host (`localhost.local` cho localhost), Authorization/API-key strip theo origin, và browser backend dùng session cô lập cho foreign origin để cùng host khác port cũng không nhận `session`/`cf_clearance`; force-browser mode cũng đi qua cùng policy wrapper thay vì raw curl session. Exact minimum `curl_cffi==0.7.4` đã được test với preflight, no-replay, binary stream và foreign-origin isolation. Final full regression: **1628 passed, 1 skipped, 65 subtests** ở cả randomized xdist và sequential 85/85 files; fresh-wheel smoke từ ngoài source tree chứng minh 2 HEAD preflight → 1 POST, clearance persisted và foreign same-host/different-port nhận 0 cookie/auth/API-key.
- **packaging/reproducibility** (2026-08-30): clean-wheel smoke phát hiện `build/lib/.../http_downloader.py` stale 0-byte có mtime mới hơn source khiến setuptools incremental build đóng gói wheel hỏng dù source tests xanh. `ForceBuildPy` giờ luôn recopy package modules từ source; thêm `scripts/verify_wheel_contents.py` build wheel offline và so byte-for-byte toàn bộ `ctf_downloader/**/*.py` với source (76 modules), gate này được nối vào `verify_quality.py`; `requirements-dev.txt` khai báo rõ `build`, `setuptools`, `wheel` để PEP517 no-isolation reproducible. Wheel fixed đã cài vào fresh venv từ ngoài source tree và console `ctf register --help` chạy được với `--cf-clearance`.
- **deep-debug / auto-register** (2026-08-30): đối chiếu source upstream GZCTF develop + rCTF hiện tại và sửa wire contract thật: GZCTF đọc cả camelCase/PascalCase `/api/config`, mã hoá password **và flag submit** bằng X25519→SHA-256→AES-GCM khi có `apiPublicKey`, HashPow hash field `challenge` (không phải ticket `id`) và luôn trả answer 8-byte/16-hex kể cả difficulty=0, fresh captcha ticket cho từng protected action, xử lý `LoggedIn`/email/admin confirmation; thêm rCTF v2 register/config/captcha/rate-limit/email-verify + lưu auth/team token; CTFd nhận hidden nonce và fail-closed trước POST nếu captcha/required field lạ. Register one-account guard chuyển thành durable reservation **trước network POST**; tempmail bỏ mail không liên quan, verify CTFd/GZ/rCTF theo đúng link/API, 429 tôn trọng Retry-After nhưng timeout POST không blind-retry. `--email`/`--tempmail` loại trừ nhau; wheel metadata đồng bộ dependency `cryptography`.
- **deep-debug / execution paths** (2026-08-30): sniper hoàn attempt trên 429 và hiểu typed verdict (`already_solved`, deferred event, auth/event/cheat/not-found blocked); one-shot rank không còn biến `_http_status` 500/429/304 hoặc `_error` thành scoreboard rỗng thành công; sync corrupt/persist partial failure trả `ok=False`; Git finish retry sau remote-branch-delete failure idempotent, không merge lần hai/xoá branch diverged/hồi sinh branch đã merged; doctor không false-green khi event ended/empty/inverted và có workspace flag-format fallback; HTTP auth bỏ substring heuristic gây `mytoken123`→`Token`, giữ scheme explicit; GZ solve-attribution successful-empty trả `net_clean` để cache TTL không poll mạng liên tục; test random-order sửa leak descriptor `staticmethod`; multiprocessing tests dùng `spawn` để tránh fork-from-thread warnings trên Python 3.13.
- **verification/final gates** (2026-08-30): hoàn tất hardening plan bằng full randomized parallel + sequential regression (**1532 pass, 1 skip, 62 subtests** ở cả hai mode); thêm `requirements-dev.txt`, `pyproject.toml` và `scripts/verify_quality.py` cho compileall + generated-doc freshness + Ruff correctness + scoped mypy reliability-core + Bandit high-severity + pip-audit; `git diff --check` sạch; random/xdist còn bắt và sửa thêm test-isolation HOME/workspace, PID-reuse false-positive khi venv path chứa chữ `ctf`, compatibility probe signature cũ, Mega mock path chưa materialize và Rich character-wrap làm hỏng path copy/paste.
- **verification/download** (2026-08-30): thêm `--verify-downloads fast|normal|strict` xuyên suốt pull/downloader (`fast` presence, `normal` ETag/Last-Modified/size, `strict` thêm SHA-256), persist final validator/hash metadata; redirect attachment được validate từng hop, public→private/loopback chặn mặc định và chỉ mở bằng `--allow-private-redirects`, cross-origin không mang Authorization/API-key style headers; khôi phục `requests.Session` làm mặc định ổn định để policy retry HEAD/GET/OPTIONS không bị `curl_cffi` cài ngẫu nhiên bypass; resume `.part` lưu ETag/Last-Modified + gửi `If-Range`, reject `Content-Range` lệch, finalize bằng `os.replace`; accelerator file lớn kiểm `Content-Range` từng segment và khi parallel fail quay lại pipeline GET đã validate thay vì ghi response fallback ad-hoc; HTTP downloader chặn scheme ngoài http/https và URL nhúng user/password nhưng vẫn cho phép private/loopback phục vụ CTF lab.
- **watch/instance verification** (2026-08-30): scoreboard conditional ETag/304 giữ snapshot cũ + adaptive idle, 429 dùng Retry-After/one-shot backoff, 401/403 surface auth expiry, 5xx/transport đi qua task backoff; fault matrix phủ suspend/backward clock; chuẩn hoá CTFd Whale v1/legacy/generic và GZCTF start/stop/extend/status, network/auth không còn bị báo nhầm `stopped`; PID-reuse detector bỏ argv[0] khỏi marker để venv/project path có chữ `ctf` không giả mạo watch sống.
- **cli/docs verification** (2026-08-30): argparse là nguồn chân lý cho long-option index tự sinh trong README/man qua `scripts/generate_cli_option_index.py`; test consistency khóa parser↔bash↔zsh↔README↔man và bổ sung completion cho `--verify-downloads`/`--allow-private-redirects`.
- **submit/platform contracts** (2026-08-30): verdict mở rộng `already_solved`/auth/event/cheat/not-found thay vì ép về correct/unknown; trạng thái không phán quyết không vào blacklist/history, `already_solved` chỉ nâng solve-state chứ không coi candidate vừa gửi là flag đúng; per-challenge cross-process gate re-check history/status sau lock rồi mới POST, ngăn hai process cùng submit/penalty; thêm contract tests ASIS/CTFd/GZCTF/rCTF và multiprocessing race test.
- **workspace/history hygiene** (2026-08-30): `resolve_platform_url()` không còn os.walk xuyên workspace con khi cwd chỉ là thư mục cha thiếu `challenges.json`; `history --prune` chuyển substring-delete nguy hiểm thành exact match, loại trừ với `--clear`, đồng bộ bash/zsh/README/man; ignore workspace tải về, cookie dump và core dump ở repo root.
- **ui** (synthesis uiv2): footer các surface render-một-lần-rồi-thoát (status/workspaces/hoard --list/storage/sync/export-pack/history/config view, `ctf --help`, doctor) bỏ gợi ý phím ảo `↑↓/?/q` không bao giờ được xử lý — thay bằng lệnh thật `ctf sync · ctf submit · ctf menu` (watch giữ footer `q/p/r` vì là vòng interactive thật); menu interactive dùng AppHeader Phosphor Radar thay Banner B cũ, đồng bộ nhận diện với mọi lệnh framed; switcher workspace cắt/pad đúng theo cell width kèm `…` (an toàn wide-char East-Asian — hết tên dính solved-count hay mất ngăn cách giữa các trường); workspace giải 100% mang token strike `done` cả trong bảng `ctf workspaces` như menu switcher (ws rỗng 0/0 không tính); `_tty_columns` non-TTY mặc định 80 cols thay vì 10^6 — pipe ra terminal hẹp không còn ép overview xếp ngang tới trần (`COLUMNS` đặt rõ vẫn được tôn trọng)
- **ui** (Amber Refit): màu bold green/yellow legacy phụ thuộc theme terminal map về token semantic luôn đi kèm glyph vai trò — dòng nhận diện platform (label/confidence `✔`/`!`), wizard submit, SubmitService (dữ liệu `info`, flag-format `literal`); sweep nốt pull/instance/rank service và platforms custom_rest/rctf/ctfd/gzctf; map tiếp cli_commands (thông báo huỷ pull → token error, mã hex cứng `#EAC54F` → token warn từ theme) và status_service (tên hiển thị hoard remove/note/tag `[bold cyan]` → dạng lồng `[bold][info]` — rich không resolve compound tag) — chỉ đổi markup, không đổi text/ngữ nghĩa thông điệp
- **storage/sync** (hunter-c16): fileio bỏ mkdir hồi sinh thư mục challenge đã bị xoá giữa lúc sync — ghi chuyển thành skip + tín hiệu, hết metadata.json zombie mất id/name và ghost `removed_from_server`; `update_status` so snapshot trong lock, không đổi gì thì không ghi/không stamp `updated_at` (`--sync` chạy lại không còn rewrite file); `save_submit_history` đọc-lại-trong-lock rồi merge theo khóa entry — hai tiến trình submit song song hết lost update; early-return lỗi của sync_workspace trả đủ shape keys như nhánh thành công
- **storage/services**: caller tiêu thụ tín hiệu skip của tầng storage — builder cảnh báo rõ tên + đường dẫn khi metadata.json bị skip thay vì nuốt im lặng; note/tag/gỡ-flag phân loại noop ("không có gì thay đổi") vs persist thất bại (cảnh báo lỗi, `hoard --remove` exit 1) thay vì báo success giả; solve attribution chỉ đếm updated khi persist thật (hết phantom count); mở lockfile bọc `FileNotFoundError` ở cả 3 helper — process xếp hàng nhận tín hiệu skip sạch thay vì nổ TOCTOU
- **watch** (hunter-c17): deadline PollScheduler + WindowGuard neo wall-clock thay vì monotonic — suspend qua đêm hết làm guard kẹt pha/countdown sai hay poll/keepalive renewal chết đói khi resume (giờ hệ thống nhảy ngược bất thường chỉ cảnh báo một lần, không kẹt vĩnh viễn); `acquire_lock` đối chiếu pid trong lockfile trước unlink — hai process cùng đọc stale-lock không còn lần lượt xoá nhầm lock tươi của nhau; adaptive scoreboard hai chiều — activity quay lại trả interval về cơ sở trước đó thay vì kẹt mức backoff đến hết giải; `_pid_alive` kiểm tra thêm `/proc/<pid>/cmdline` phát hiện PID reuse; exception `_resolve_window` bọc riêng — không còn thoát khỏi run() bỏ rơi lock trên đĩa; đang grace hiển thị "Hết giờ — grace còn Xm" (feed + header) thay vì đếm ngược "0m00s" gây hiểu nhầm
- **services** (hunter-c18): RANKING.md nhúng `my_rank`/`my_score`/`pos` nguyên văn từ JSON server vỡ bảng markdown — mọi giá trị gốc server escape tại sink (`md_cell` + strip_ansi footer/badge, code-span GFM không vỡ khi tên team chứa backtick); register rate-limit TOCTOU (cfg load 1 lần, ghi stale sau network dài — 2 CLI song song tạo 2 tài khoản) chuyển đọc-mutate-ghi trong cùng khóa flock, re-check trên state mới nhất ngay trước mutate, thua race không đè timestamp/auth; verify-hook chỉ HTTP 200 = verified (bỏ `or True` khiến mọi status đều pass), nhánh captcha re-raise vẫn ghi attempt (hết bypass rate-limit khi chạy lại); submit README workspace ghi qua storage atomic thay vì `open()` thô + except-pass, auto-submit break candidate còn lại của challenge vừa solved (hết penalty risk), identifier rỗng/toàn-space trả None thay vì khớp partial-match đầu tiên; score chuỗi/rác (`"300"`/`"abc"`) qua `_score_int` parse an toàn thay vì max() lexicographic/TypeError
- **cli/menu** (review-c18 follow-up): `ctf config` set và menu lưu workspace bỏ load-stale-save — `update_global_config(mutator)` đọc-mutate-ghi trong khóa flock (seed defaults deepcopy — bản setdefault alias dict từng làm bẩn defaults cấp module vĩnh viễn); `_commit_attempt` tri-state ok/preempted/unpersisted — thư mục config biến mất giữa chừng không còn bị báo thành công giả, OSError persist không lan qua run() che exit-code (credentials đã tạo phía server vẫn in/lưu, lỗi log warning)
- **platforms** (deferred-triage): rctf `last_verdict` gói `models.Verdict` Literal thay str tự do; detection tầng 2 parse chuỗi cookie-hint `-c` thành TÊN cookie (cặp name=value tách theo `;`/newline, bỏ quote, lowercase) — hết substring false-match nguyên blob, fallback hành vi cũ khi không parse được cặp nào; pin literal PlatformSpec default throttle = DEFAULT_THROTTLE submit_service (custom_rest/generic_html) khóa nguồn mặc định chống drift
- **storage/watch**: `_load_json_object` bỏ exception dư trong tuple catch (JSONDecodeError ⊂ ValueError); `acquire_lock` chụp inode lockfile trước khi đọc pid và đối chiếu ngay trước khi trả True — thu hẹp cửa sổ TOCTOU re-read↔unlink khi process khác thay tay lockfile giữa lúc verify (lệch inode bỏ lượt, kỳ sau thử lại; protocol pid giữ nguyên)
- **cli/auth** (open-code batch-3): `hoard` target toàn số ưu tiên tra theo TÊN case-insensitive trước khi coi là id — challenge tên "1337" từng không bao giờ hoard được theo tên (`--id` tường minh vẫn thắng, cache rỗng giữ behavior offline); quy ước key auth-map (ws-dir-thật → abs path, else URL rstripped) định nghĩa MỘT NƠI ở `auth_service.auth_key` — RegisterService chỉ delegate, read-side `AuthService.lookup_auth_entry` compat cả hai quy ước key cũ, không migration dữ liệu user
- **storage/utils/watch** (open-code deferred): `locked_update_json` gặp JSON hợp lệ nhưng non-dict (list/str/int) backup `.bak` cạnh file rồi mới thay `{}` thay vì nuốt im lặng (mirror read-path); flag_format thêm scanner dup-alternation — từ chối nhánh alternation trùng hệt kiểu `(a|a)+$` exponential mà scanner nested-quantifier cũ bỏ sót, intro nhóm (`(?:`/`(?=`/`(?P<name>`) strip để so nhánh nguyên văn, gate chung cho search/matches/validate_flag; wizard event window tách scoreboard thành prompt thứ 4 độc lập (policy riêng, hết gắn cứng scoreboard=notices)
- **ui**: headline error-tree diagnostics đổi `"red bold"`/`"yellow bold"` (tên màu chuẩn rich phụ thuộc theme terminal) sang hex token ERROR/WARN theo pattern ACCENT sẵn có — err_console không gắn Theme nên token name vốn không resolve; test khóa quét toàn bộ `ui/*.py` cấm tên màu chuẩn rich (tokenize + ast, PALETTE miễn trừ có kiểm)
- **pull/builder** (hunter-c19): đích tải tính MỘT LẦN trước khi download qua `resolve_challenge_dir()` duy nhất (sanitize category/name + guard owner/-id dùng chung builder) — hai challenge sanitize trùng tên (`web:1` vs `web/1`) hết rót attachment chung một challenge/ đè nhau im lặng; `--update`/`--refresh-meta` đổi category/tên: field user-owned (status/submitted_flag/instance_info) phục hồi vào metadata THƯ MỤC MỚI (trước đây ghi nhầm file cũ — mất trạng thái, id xuất hiện 2 nơi), bản cũ tombstone `superseded_by` qua `repo.update_metadata` atomic, index run_update/sync_solve_attribution/verify/sync_workspace bỏ qua tombstone; README/NOTE/solve.py/writeup section ghi atomic `locked_write_text` — challenge README là DERIVED refresh=True luôn viết lại (exists-guard cũ làm stale vĩnh viễn sau --update), file USER-OWNED giữ exists-guard, producer raise giữ bản cũ tốt thay vì đè bằng trang lỗi
- **downloader/sanitize** (hunter-c19): consent preflight chạy main thread TRƯỚC thread pool — probe HEAD mỗi URL đúng một lần, hỏi GỘP file vượt ngưỡng trong MỘT prompt, worker chỉ đọc quyết định sẵn có (`input()` từ worker từng chồng prompt lên nhau), Mega qua gate như đường http, size cache tái dùng bỏ HEAD trùng trước GET; skip-if-exists theo PRESENCE trừ khi force — điều kiện cũ đòi Content-Length khai báo + khớp kích thước khiến server chunked/unknown-length tải lại ĐÈ file hoàn chỉnh đã có (resume `.part` giữ luồng riêng có kiểm); retry `continue` ngay → backoff exponential 0.5×2^(n-1)s cap 8s + jitter ≤0.25s ở cả 4 điểm retry; so downloaded_bytes vs Content-Length bỏ qua khi có Content-Encoding — gzip/deflate/br trả bytes đã giải nén nhỏ hơn CL gây fail ảo rồi retry đến chết dù file đủ; `sanitize_filename` cap theo BYTE utf-8 (NAME_MAX 255 byte) thay vì char — tên emoji dài cắt theo ký tự từng vượt trần OSError lúc ghi, phần rơi giữa multi-byte decode bỏ qua không đứt codepoint

## [3.0.0] - 2026-08-24

Rebuild kiến trúc trên nhánh `rebuild/architecture` — Layered Monolith + Registry.

### Features

- **pull**: tải tăng dần `--update`/`--refresh-meta` — chỉ tải bài mới, cập nhật metadata bài cũ
- **watch**: tự nhận diện event window + auto-sync trong window + instance keep-alive
- **sniper**: submit hẹn giờ first-blood, có window-guard và blacklist
- **register**: tự đăng ký GZCTF/CTFd/rCTF + xác thực tempmail, an toàn 1 tài khoản/lần
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
