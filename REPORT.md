# Báo cáo phân tích & kiểm thử repo — CTF Challenge Downloader & Workspace Builder

> **Ngày:** 24/08/2026 · **Repo:** `/home/light/Workspace/Project/auto_download_ctf_challenge` · **Package:** `ctf-toolkit` (setup.py khai báo v2.0.0)
> **Phạm vi nền tảng:** Linux (tool hiện chỉ chạy/nhắm tới Linux — các vấn đề tương thích Windows/macOS được đánh giá ở mức thông tin, không ưu tiên vá)
> **Phương pháp:** 5 agent phân tích song song theo phạm vi tách biệt — (1) Core & CLI, (2) Feature modules, (3) Kiểm thử thực tế, (4) Docs/tests/git state, (5) Platforms/downloaders/utils — sau đó hợp nhất, khử trùng lặp.

---

## 1. Tổng quan repo

Bộ công cụ Python tự động hóa vòng đời thi CTF: **tải challenge từ platform → dựng workspace chuẩn → quản lý container động → nộp flag → theo dõi tiến độ → xem bảng xếp hạng live**.

```
Entrypoints (wrapper + console_scripts)
├── main.py / ctf.py ──────► ctf_downloader.cli:main   (CLI hợp nhất, 7 nhóm lệnh)
├── submit.py ─────────────► FlagSubmitter             (nộp flag độc lập)
├── manage.py ─────────────► CTFDashboard + wizard     (dashboard/quản lý)
├── instance.py ───────────► InstanceManager           (container động, có --sync)
└── rank.py ───────────────► RankingManager            (scoreboard live — mới, chưa commit)

Lõi ctf_downloader/
├── cli.py · config.py · core.py · interactive_menu.py
├── platforms/    base, detector, ctfd, gzctf, rctf, custom_rest, generic_html
├── downloaders/  manager, http_downloader, gdrive, mediafire, dropbox
├── extractors/   link_extractor, text_parser
├── generator/    workspace_builder, summary_generator
├── dashboard.py · submitter.py · instance_manager.py · ranking.py
└── utils/        http_client, sanitize, logger
```

**Pipeline chính:** URL + Cookie/Token → `PlatformDetector` chọn adapter → `authenticate()` → `fetch_challenges()` → tải file song song (ThreadPoolExecutor) → `WorkspaceBuilder` dựng cây thư mục + solve template → `SummaryGenerator` ghi `SUMMARY.md` + `challenges.json` (index trung tâm mà dashboard/submitter/instance/ranking đọc lại).

**Kiến trúc platform adapter:**

| Platform | Endpoints chính | Auth | Container | Scoreboard |
|---|---|---|---|---|
| CTFd | `/api/v1/challenges[/{id}]`, `/attempt`, whale plugin ×3 tầng fallback | Cookie session hoặc `Token ctfd_…` + CSRF nonce scrape | Có (whale) | `/api/v1/scoreboard` |
| GZCTF | `/api/account/profile`, `/api/game/{id}[/details|/challenges/{id}|/scoreboard]` | Cookie `GZCTF_Token` | Native DynamicContainer | `/api/game/{id}/scoreboard` |
| rCTF | `/api/v1/challs`, `/submit`, `/leaderboard/now` | Bearer token (teamToken exchange) | Không | `/api/v1/leaderboard/now` |
| Custom REST | `/api/auth/me`, `/api/challenges[/{id}]/submit` | Cookie Next.js | Không | Không |
| Generic HTML | Scrape heuristic BeautifulSoup | Không | Không | Không |

Điểm cộng thiết kế: dataclass `Challenge` rõ ràng, ghi `.tmp` rồi move (atomic-ish), guard idempotent ("không ghi đè file có sẵn"), exception theo-challenge không làm chết cả batch.

**Output mẫu thực tế:** workspace `PTIT_CTF_2026/` (35 challenge, 5 category, sinh 22/08 bằng code layout cũ phẳng — chưa có `challenge/ solver/ writeup/`).

---

## 2. Tool khả thi & kết quả chạy thử

### 2.1 Môi trường

- OS: Kali Linux (7.0.12+kali-amd64) · Python 3.13.14
- Dependencies: requests 2.34.2 ✅ · beautifulsoup4 4.15.0 ✅ · rich ✅ · urllib3 2.7.0 ✅ · gdown 6.1.0 ✅ (đã cài sẵn dù thiếu trong requirements.txt)

### 2.2 Danh sách 11 tool khả thi — tất cả chạy thành công

| # | Lệnh | Exit | Ghi chú |
|---|---|---|---|
| 1 | `python3 main.py --help` (+ subcommand help) | 0 | Usage đầy đủ 7 nhóm lệnh |
| 2 | `python3 ctf.py --help` | 0 | Giống hệt main.py (wrapper dư) |
| 3 | `ctf` / `ctfcli` / `ctf-tool --help` | 0 | Cả 3 console script hoạt động |
| 4 | `python3 submit.py --help` | 0 | `-u/-c/-t/-w/--id/-n/-f/--auto/-i` |
| 5 | `python3 manage.py --help` | 0 | `-w/-c/-t/-u/-s/-C/--container/-A/-i` |
| 6 | `python3 instance.py --help` | 0 | `--start/--stop/--extend/--status/--sync/-l/-i` |
| 7 | `python3 rank.py --help` | 0 | `-w/-u/-c/-t/-n/--no-docs` |
| 8 | `main.py -v` | 0 | In `ctf-toolkit 2.0.0` |
| 9 | `main.py workspaces` | 0 | Quét 14 workspace thật trong `~/Workspace/CTF` |
| 10 | `main.py status -w PTIT_CTF_2026` | 0 | Dashboard GZCTF: 35 challs, 1 solved |
| 11 | `instance.py -w PTIT_CTF_2026 -l` | 0 | Liệt kê 22 dynamic container (offline) |

Chức năng offline khác đều đúng: sanitize bỏ dấu tiếng Việt (`Tiger Bạc` → `Tiger_Bac`), chống path traversal (`../../etc/passwd?.png` → `_.._etc_passwd`), html→markdown, extract link gdrive/direct, `SummaryGenerator` + `WorkspaceBuilder` ghi đúng cấu trúc vào /tmp.

### 2.3 Test suite & import module

- **Test suite: 10/10 PASSED** (unittest + pytest đều OK, ~7s, thuần mock không cần mạng).
- **Import module: 28/28 thành công**, không module nào lỗi.
- Coverage tốt: cookie parser, link extractor, gdrive/dropbox converter, sanitize, text parser, parser CTFd/GZCTF (mock), workspace builder end-to-end, flag resolution.
- **Coverage lệch/yếu:** RankingManager chỉ test khởi tạo (feature mới nhất!), rCTF không có test, InstanceManager không test gì, auto-submit flow và downloader thật chưa test, không có CI config.

---

## 3. Trạng thái Git chưa commit

Branch `main`, 1 commit duy nhất (`0f0439e`). Working tree: **14 file modified (~588+/82−) + 2 untracked** — chứa trọn 2 cụm tính năng lớn chưa commit:

1. **Live Ranking/Scoreboard:** `rank.py`, `ctf_downloader/ranking.py`, `fetch_scoreboard()` trên cả 3 platform, subcommand `rank`.
2. **Refactor layout workspace:** per-challenge chuyển sang `challenge/ · script/ · solver/ · writeup/`, kèm vá tương thích lan toả (dashboard, submitter, summary, core, instance sync).

Rủi ro: dễ mất 2 file untracked khi `git clean`; diff trộn 2 concern khó review/revert riêng; workspace cũ (PTIT_CTF_2026) sẽ lẫn lộn hai thế hệ layout nếu chạy lại tool; test ranking gần như rỗng trong khi feature mới nhất chính là nó.

---

## 4. Phát hiện vấn đề (hợp nhất từ 5 agent, đã khử trùng lặp)

### 🔴 Nghiêm trọng (High) — 7

| # | Vấn đề | Vị trí |
|---|---|---|
| H1 | **Auto-submit có thể spam flag rác lên platform**: regex flag có nhánh `[a-zA-Z0-9_\-]+\{...\}` khớp *mọi* chuỗi `word{...}` (sha256{abc}, tên hàm trong code block…), kết hợp auto-submit mọi candidate không dedupe, không giới hạn số lần sai → nguy cơ bị trừ điểm/khoá tài khoản | `submitter.py:164,205-213` |
| H2 | **Credentials plaintext**: cookie/token lưu thô vào `~/.config/ctf_toolkit/config.json`, menu [9] in nguyên cookie/token ra console; đồng thời mọi CLI nhận cookie/token qua argv (lộ shell history, `ps`, `/proc/*/cmdline`) | `interactive_menu.py:249-258,445-448`; toàn bộ entrypoint |
| H3 | **Bug thuộc tính vô hiệu hoá tính năng**: `dl.output_dir` không tồn tại (thật là `dl.config.output_dir`) → sau khi tải xong, workspace mới không bao giờ được tự động active/lưu, người dùng thấy thông báo "tải thất bại" sai sự thật | `interactive_menu.py:186` |
| H4 | **Resubmit vô hạn**: `_update_local_workspace` bọc `except Exception: pass` — nếu ghi metadata.json lỗi thì flag đã submit trên platform nhưng local vẫn `solved_by_me=false` → lần scan sau submit lại mãi | `submitter.py:252-253` |
| H5 | **Nuốt exception im lặng toàn bộ downloaders**: http_downloader/gdrive/mediafire/dropbox đều `except Exception: return None` — không thể biết download fail vì DNS/403/disk full | `http_downloader.py:57-58,91-92`, `gdrive.py:74-75,86-87`, `mediafire.py:43-44`, `dropbox.py:43-44` |
| H6 | **Link Mega chết âm thầm**: classify gắn `mega` + `is_downloadable=True` nhưng DownloadManager không có handler → link mega đi nhánh HTTP thường, lưu nhầm HTML hoặc fail | `link_extractor.py:117-124` vs `manager.py:66-75` |
| H7 | **Session/credential leak qua stdout**: logger chỉ là wrapper rich ra stdout, không redaction/file/level; log `resp.text` server thô (`gzctf.py:287,300,313`), username/email; redirect output = lộ session | `utils/logger.py`, các platform |

### 🟠 Trung bình (Medium) — 20

| # | Vấn đề | Vị trí |
|---|---|---|
| M1 | Retry cả POST trong urllib3 Retry → nguy cơ submit flag trùng khi 500/429 | `http_client.py:70` |
| M2 | GZCTF xác định đúng/sai flag bằng so sánh số lượng bloods trước/sau — race, dễ kết quả sai | `gzctf.py:216-251` |
| M3 | Detector fallback luôn trả `CTFdPlatform`; `GenericHTMLPlatform` tồn tại nhưng không bao giờ được chọn | `detector.py:118` |
| M4 | GZCTF brute-force probe game id 1..9 khi không truyền game_id — 8 request thừa, nguy cơ gắn nhầm game | `gzctf.py:48-60` |
| M5 | Thiếu action khi truyền `--id` → âm thầm START container (side effect tốn kém) | `instance.py:130-131`, `cli.py:276` |
| M6 | Read-modify-write metadata.json/challenges.json/SUMMARY.md/solve.py không lock, không atomic write → mất cập nhật nếu 2 process xen kẽ | `instance_manager.py:190-279`, `submitter.py:225-253` |
| M7 | Crash tiềm ẩn khi `"raw": null` trong metadata.json | `dashboard.py:128,151`; nuốt im lặng ở `instance_manager.py:176` |
| M8 | Resolve challenge bằng partial substring match → nhập "web" có thể submit flag cho sai challenge | `submitter.py:117-119` |
| M9 | Regex patch `PORT = \d+` / `Target Connection:` thay MỌI occurrence trong solve.py/README | `instance_manager.py:227,245-246` |
| M10 | Parse entry `host:port` bằng `split(':')` — vỡ với IPv6/nhiều dấu `:`, một chỗ nuốt im lặng khiến solve.py không cập nhật | `instance_manager.py:117,244` |
| M11 | `validate()` rebuild URL từ scheme+netloc+path — xoá sạch query string (ngoài `token=`) | `config.py:47` |
| M12 | Một `requests.Session` dùng chung giữa các thread của ThreadPoolExecutor — thread-safety không bảo đảm | `core.py:21-26,136` |
| M13 | Auth lỗi bị nuốt: ranking vẫn gọi fetch_scoreboard sau authenticate() raise; submitter không check giá trị trả về | `ranking.py:52-55`, `submitter.py:139` |
| M14 | Dependencies lệch nhau: `gdown` chỉ có ở setup.py, `urllib3` chỉ có ở requirements.txt, rich `>=13` vs `>=12` | `setup.py` vs `requirements.txt` |
| M15 | Version lệch: `__init__.py` = 1.0.0, setup.py/CLI = 2.0.0 | `__init__.py:5`, `cli.py:59` |
| M16 | Trùng lặp logic quy mô lớn: instance.py ≈ cli.handle_instance (kèm wizard), quét workspace nhân bản ×3 → hành vi đã lệch (chỉ instance.py có `--sync`) | `instance.py:39-131` vs `cli.py:206-287` |
| M17 | CTFd submit_flag mất nguyên nhân lỗi (`except Exception as e` không dùng `e`) | `ctfd.py:266-267` |
| M18 | Gap điểm tới #1 tính theo `standings[0]` — nếu scoreboard chưa sort thì gap âm, in `--5 pts` | `ranking.py:115-116` |
| M19 | File chunked không có content-length → check skip-if-exists vô dụng, re-download mỗi lần; file `.tmp` sót khi crash | `http_downloader.py` |
| M20 | Coverage test lệch nghiêm trọng so với risk: RankingManager (feature mới nhất) gần như không test, rCTF/InstanceManager/auto-submit/downloader thật = 0 test | `test_suite.py` |

### 🟡 Thấp (Low) — 19

| # | Vấn đề | Vị trí |
|---|---|---|
| L1 | Hardcode context cá nhân: default URL `jeo.infosecptit.org/games/6`, prefix `PTITCTF`, `~/Workspace/CTF` rải rác, Top 30 RANKING.md | `submit.py:52,68`, `submitter.py:164`, `ranking.py:161` |
| L2 | Tên thư mục giữ emoji/comma/`!` (`🪟`, `Approve_Please,_Genie!`) — chấp nhận được trong phạm vi Linux-only của tool; chỉ thành vấn đề nếu sau này mở rộng sang Windows/macOS | `sanitize_folder_name` |
| L3 | Dead code: unreachable sau `return` (submitter.py:68-69), `if not args.list` luôn True (instance.py:115), else cuối main() không tới (cli.py:367-368), import thừa nhiều file, `ThreadPoolExecutor` dead import trong manager.py | nhiều nơi |
| L4 | Placeholder mismatch: submitter thay literal `FLAG{...}` còn template sinh ra là `FLAG{{...}}` → không bao giờ được thay | `submitter.py:244` vs `workspace_builder.py:336` |
| L5 | Copy attachment flatten basename — 2 file trùng tên → file thứ 2 bị skip không cảnh báo | `workspace_builder.py:42-49` |
| L6 | `total_points += chall.points` TypeError nếu points=None; category=None tạo key lạ trong bảng | `summary_generator.py:28,54` |
| L7 | `my_rank` gắn hậu tố `th` cứng → "1th/2th/3th" | `ctfd.py:483`, `rctf.py:215`, `gzctf.py:383` |
| L8 | Timestamp `datetime.now()` không timezone | `ranking.py:138`, `instance_manager.py:187` |
| L9 | Template injection nhẹ: challenge.name/description chèn thẳng f-string tạo solve.py — tên chứa `'''`/newline làm skeleton lỗi cú pháp | `workspace_builder.py:219,246-289` |
| L10 | Generic HTML regex points bắt số đầu tiên bất kỳ ("Top 1 of 200 teams" → points=1) | `generic_html.py:74` |
| L11 | `host:port` connection pattern quá rộng — sinh connection info giả từ URL mất scheme | `link_extractor.py:255-256` |
| L12 | Token heuristic: chứa chữ "token" → `Token …`, không thì `Bearer …` — mong manh với platform khác | `http_client.py` |
| L13 | Đánh dấu solved "giả thành công" khi README không chứa marker nào | `manage.py:124-131` |
| L14 | `get_auth_for_workspace` bất đối xứng: truyền `-c` thì bỏ qua token đã lưu | `cli.py:16-20` |
| L15 | Menu tương tác: ID challenge trùng số thứ tự danh sách → chọn nhầm theo index | `interactive_menu.py:338-346` |
| L16 | Dashboard dùng print() thuần trong khi package có rich — UI không nhất quán | `dashboard.py` |
| L17 | Progress bar `'█' * int(8*rate//100)` overflow nếu completion rate >100% | `manage.py:31`, `cli.py:201` |
| L18 | README thiếu: GZCTF trong mục đa nền tảng, cờ `--sync`, giải thích `script/`+`NOTE.md`, schema metadata.json/RANKING.md; không CHANGELOG/license | `README.md` |
| L19 | Dashboard logic solved thừa/no-op (`elif not is_solved ... is_solved=False`); metadata thắng marker README do break sớm | `dashboard.py:40-41` |

---

## 5. Khuyến nghị ưu tiên

**P0 — làm ngay trước khi commit**
1. Sửa H3 (`dl.output_dir` → `dl.config.output_dir`) — 1 dòng, khôi phục cả một luồng tính năng.
2. Siết auto-submit (H1): loại nhánh `[a-zA-Z0-9_-]+\{` khỏi regex, thêm confirm/dry-run và giới hạn số flag nộp mỗi lần chạy.
3. Bỏ POST khỏi `allowed_methods` retry (M1); sửa placeholder `FLAG{{...}}` (L4).
4. Tách working tree thành 2 commit theo concern (ranking / layout refactor) và commit sớm để không mất 2 file untracked.

**P1 — tuần này**
5. Thay `except Exception: pass` bằng log warning ở downloaders + submitter (H4/H5); thêm cơ chế đánh dấu "flag đã submit" bền vững.
6. Chuyển secret khỏi argv/config plaintext → env var hoặc keyring; redact token khi log (H2/H7).
7. Bổ sung test cho RankingManager (fetch_scoreboard mock cho cả 3 platform), InstanceManager sync, auto-submit filter.
8. Thêm handler Mega hoặc loại khỏi downloadable list (H6); sửa detector fallback sang GenericHTML (M3).

**P2 — dọn dẹp**
9. Consolidate entrypoint: xoá wrapper trùng (main.py/ctf.py), gộp logic instance/workspace-scan vào 1 module.
10. Đồng bộ deps/version giữa setup.py ↔ requirements.txt ↔ __init__.py.
11. Lock/atomic write cho metadata.json & challenges.json; chuẩn hoá parse host:port hỗ trợ IPv6.

## 6. Kết luận

Repo ở tình trạng **hoạt động thực tế đã được chứng minh** (workspace PTIT_CTF_2026 35 bài, 14 workspace local, 10/10 test pass, 28/28 module import OK) với kiến trúc phân lớp hợp lý và tài liệu khá đầy đủ. Điểm yếu xuyên suốt là: (a) xử lý lỗi kiểu nuốt-im-lặng, (b) sync file bằng regex thay vì cấu trúc, (c) quản lý secret thô sơ, (d) auto-submit thiếu van an toàn. Hai cụm tính năng đang nằm trong working tree nên được commit sớm sau khi vá P0.
