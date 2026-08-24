# ⚡ CTF Challenge Downloader & Workspace Builder

Công cụ tự động hóa tải toàn bộ challenge từ các trang CTF (CTFd, rCTF, custom CTF platform), tự động trích xuất link nguồn/file đính kèm (bao gồm cả link bên thứ 3 như Google Drive, Dropbox, Mediafire, GitHub, Discord...), phân loại và tạo cấu trúc thư mục làm bài chuyên nghiệp.

---

## 🌟 Tính năng nổi bật

- **Tự động phân loại & tổ chức thư mục**:
  - Phân loại challenge theo danh mục (`Web`, `Pwn`, `Crypto`, `Forensics`, `Reverse`, `Misc`, ...).
  - Tạo từng thư mục riêng biệt cho từng challenge.
  - Tự động tạo `README.md` (chứa toàn bộ đề bài, points, author, hints, tags, lệnh netcat/kết nối).
  - Tự động tạo file `solve.py` mẫu (đã tích hợp sẵn thư viện `pwn`, `requests` hoặc `Crypto` và host:port tương ứng).
  - Lưu trữ `metadata.json` chứa dữ liệu thô của challenge.
  - Tạo `SUMMARY.md` và `challenges.json` tổng quan ở thư mục gốc.

- **Nhận diện & tải toàn bộ file liên quan**:
  - Tải file đính kèm chính thức từ nền tảng CTF.
  - **Tự động trích xuất link bên thứ 3 trong đề bài**:
    - **Google Drive**: Tự động lấy file ID, bypass xác nhận file lớn / quét virus và tải trực tiếp.
    - **Dropbox**: Tự động chuyển đổi thành link tải trực tiếp (`dl=1`).
    - **MediaFire**: Tự động cào link tải trực tiếp.
    - **GitHub / GitLab**: Tải raw files & releases.
    - **Discord CDN**: Tải attachments đính kèm trong tin nhắn.
    - Tải mọi file trực tiếp (`.zip`, `.pcap`, `.elf`, `.exe`, `.py`, `.c`, `.tar.gz`, ...).

- **Hỗ trợ đa nền tảng**:
  - **CTFd** (hầu hết các giải CTF hiện nay) - hỗ trợ cả Session Cookie và API Token, tự động xử lý CSRF nonce.
  - **rCTF** (nền tảng của redpwn).
  - **Generic / HTML Scraper** (tự động cào dữ liệu cho các trang web CTF tùy biến).

- **Hiệu năng cao & An toàn**:
  - Tải đa luồng (Multi-threading).
  - Thanh tiến trình trực quan với `rich`.
  - Tự động kiểm tra file đã tải để tránh tải lại (có cờ `--force` nếu muốn tải đè).
  - Chuẩn hóa tên file/thư mục an toàn trên cả Linux, Windows, macOS.

---

## 🚀 Cài đặt

Yêu cầu Python 3.8+.

```bash
# Cài đặt các thư viện cần thiết
pip install -r requirements.txt
```

---

## 🔑 Cách lấy Cookie hoặc Token từ trình duyệt

### Cách 1: Lấy Session Cookie (Khuyên dùng)
1. Mở trình duyệt và đăng nhập vào trang CTF.
2. Nhấn `F12` để mở **Developer Tools** (Công cụ nhà phát triển).
3. Vào tab **Application** (Chrome/Edge) hoặc **Storage** (Firefox) -> Chọn **Cookies** -> Chọn domain trang CTF.
4. Tìm cookie có tên `session` (hoặc copy toàn bộ cookie).
5. Copy giá trị của `session` (ví dụ: `session=.eJw1...` hoặc toàn bộ chuỗi).

### Cách 2: Lấy API Token (Trên CTFd)
1. Đăng nhập vào trang CTFd -> Bấm vào tên tài khoản ở góc trên bên phải -> Chọn **Settings**.
2. Cuộn xuống phần **Access Tokens** -> Tạo một token mới.
3. Copy mã token bắt đầu bằng `ctfd_...`.

---

## 💻 Hướng dẫn sử dụng

### 1. Chế độ tương tác từng bước (Interactive Wizard)
Chỉ cần chạy lệnh sau, công cụ sẽ hỏi bạn URL, Cookie, thư mục lưu, số luồng:
```bash
python main.py -i
# hoặc chỉ cần chạy không tham số:
python main.py
```

### 2. Chế độ dòng lệnh (CLI Commands)

#### Tải toàn bộ với Cookie:
```bash
python main.py -u https://ctf.example.com -c "session=.eJw1z..." -o ./my_ctf
```

#### Tải bằng file chứa cookie:
```bash
# Lưu cookie vào file cookie.txt
python main.py -u https://ctf.example.com -c cookie.txt -o ./my_ctf
```

#### Tải bằng CTFd API Token:
```bash
python main.py -u https://ctf.example.com -t ctfd_xxxxxxxxxxxxxxxxxxxx -o ./my_ctf
```

#### Chỉ tải một số Category cụ thể (Ví dụ: Web và Pwn):
```bash
python main.py -u https://ctf.example.com -c "session=..." -C Web Pwn -j 8
```

#### Bỏ qua một Category không muốn tải:
```bash
python main.py -u https://ctf.example.com -c "session=..." -E Misc Forensics
```

#### Không tải file từ bên thứ 3 (chỉ tải file đính kèm chính thức):
```bash
python main.py -u https://ctf.example.com -c "session=..." --no-third-party
```

---

### 3. Cập nhật Bảng xếp hạng & Ranking Live (`rank.py`)
```bash
# Xem bảng xếp hạng hiện tại và vị trí của đội bạn:
python3 rank.py -w ./my_ctf

# Xem top 30 đội đứng đầu và tự động ghi vào RANKING.md:
python3 main.py rank -w ./my_ctf -n 30
```

### 4. Quản lý Dynamic Container / Instance (`instance.py`)
```bash
# Khởi động instance mới (tự động nạp URL vào metadata.json):
python3 instance.py -w ./my_ctf --id <CHALL_ID> --start

# Kiểm tra trạng thái và URL hiện tại:
python3 instance.py -w ./my_ctf --id <CHALL_ID> --status

# Gia hạn thời gian sống của container:
python3 instance.py -w ./my_ctf --id <CHALL_ID> --extend
```

### 5. Nộp Flag Tự Động & Chấm điểm (`submit.py`)
```bash
# Quét toàn bộ workspace và nộp các flag mới tìm được:
python3 submit.py -w ./my_ctf --auto

# Nộp flag cho một bài cụ thể:
python3 submit.py -w ./my_ctf --id <CHALL_ID> -f "FLAG{...}"
```

### 6. Trạng thái đa chiều (`ctf status`)
Mỗi challenge có block `status` trong `metadata.json` gồm 4 trục độc lập —
dashboard và SUMMARY.md render bằng bộ icon thống nhất:

| Trục | Giá trị → Icon |
|---|---|
| Solve | unsolved `·` · working 🛠️ · solved_by_me 🧑✅ · solved_by_team 👥✅ · solved_other 🌐✅ |
| Flag | none ∅ · found_unverified ❓ · hoarded 🏴 · submitted_correct 🚩✔ · submitted_wrong ⛔ |
| Writeup | none `-` · skeleton 📄 · draft 📝 · complete 📚 |
| Container | running 🐳▶ · stopped 🐳⏸ |

```text
[🧑✅][🚩✔][📚] 17. flask-jail (400 pts)
```

- Kết quả submit (đúng/sai), container start/stop và sync solve attribution
  từ server (GZCTF / CTFd / rCTF) tự động cập nhật trạng thái — chỉ nâng, không hạ.
- Header dashboard hiển thị: 📊 Progress · 💰 Points · 🏴 Hoarded · 📝 Drafts · 📦 Files · ⏱️ Window.
- Workspace cũ (layout phẳng, chưa có block `status`) được migrate-on-read ngay khi mở.

---

## 📂 Cấu trúc thư mục được tạo ra

```text
my_ctf/
├── SUMMARY.md                       # Bảng tổng kết toàn bộ giải đấu, điểm số, danh mục, link từng bài
├── challenges.json                  # Dữ liệu JSON tổng hợp toàn bộ giải
│
├── Web/                             # Thư mục thể loại Web
│   ├── Super_Secure_Login/
│   │   ├── challenge/               # 📦 Source code, file đính kèm, attachments (src.zip, Dockerfile...)
│   │   │   ├── src.zip
│   │   │   └── Dockerfile
│   │   ├── solver/                  # 💻 Script exploit & giải tự động
│   │   │   └── solve.py
│   │   ├── writeup/                 # 📄 Writeup, phân tích kỹ thuật & PoC
│   │   │   └── README.md
│   │   └── metadata.json            # ⚙️ Metadata đồng bộ từ platform
│   └── ...
│
├── Pwn/                             # Thư mục thể loại Pwn
│   ├── Ret2Libc_Easy/
│   │   ├── challenge/               # 📦 Binaries, libc, challenge assets
│   │   │   ├── vuln
│   │   │   └── libc.so.6
│   │   ├── solver/                  # 💻 Script exploit pwntools
│   │   │   └── solve.py
│   │   ├── writeup/                 # 📄 Writeup hướng dẫn khai thác
│   │   │   └── README.md
│   │   └── metadata.json
│   └── ...
│
├── Crypto/
├── Reverse/
└── Forensics/
```

---

## 🛠️ Danh sách tham số đầy đủ

| Tham số | Viết tắt | Ý nghĩa | Mặc định |
| :--- | :--- | :--- | :--- |
| `--url` | `-u` | Đường dẫn trang CTF (ví dụ `https://ctf.example.com`) | Bắt buộc |
| `--cookie` | `-c` | Chuỗi cookie hoặc đường dẫn đến file cookie | `None` |
| `--token` | `-t` | Token API (CTFd token hoặc Bearer token) | `None` |
| `--output` | `-o` | Thư mục đầu ra để lưu challenge | `./ctf_challenges` |
| `--threads` | `-j` | Số luồng tải song song | `4` |
| `--category` | `-C` | Danh sách category muốn tải (cách nhau bởi dấu cách) | Tất cả |
| `--exclude` | `-E` | Danh sách category muốn loại trừ | Không có |
| `--no-third-party`| | Tắt tự động tải từ link bên thứ 3 (GDrive, Dropbox, ...) | `False` |
| `--no-template` | | Tắt tạo file mẫu `solve.py` | `False` |
| `--force` | `-f` | Buộc tải lại toàn bộ file dù đã tồn tại | `False` |
| `--timeout` | | Thời gian timeout mỗi request (giây) | `30` |
| `--interactive` | `-i` | Bật giao diện hướng dẫn từng bước | `False` |

---

## 🚩 Công cụ tự động nộp Flag (`submit.py`)

Công cụ hỗ trợ nộp flag tự động lên nền tảng **CTFd**, **GZ::CTF**, và **rCTF**, đồng thời tự động cập nhật trạng thái `- [x] Solved` trong file `README.md` và `metadata.json` của challenge.

### 1. Nộp theo tên hoặc ID của challenge:
```bash
# Nộp theo tên bài:
python3 submit.py -u https://jeo.infosecptit.org/games/6/challenges -c "GZCTF_Token=..." --name "Tiger Bạc" -f "PTITCTF{Vu_Duc_Luong}"

# Nộp theo ID bài:
python3 submit.py -u https://jeo.infosecptit.org/games/6/challenges -c "GZCTF_Token=..." --id 18 -f "PTITCTF{Vu_Duc_Luong}"
```

### 2. Tự động quét Workspace và nộp hàng loạt (Auto Scan & Submit):
Khi bạn giải xong bài và điền flag vào `README.md` (khung `FLAG{...}`) hoặc tạo file `flag.txt` trong thư mục bài, chỉ cần chạy lệnh:
```bash
python3 submit.py -w ./PTIT_CTF_2026 -c "GZCTF_Token=..." --auto
```
Công cụ sẽ tự động duyệt toàn bộ workspace, tìm tất cả các bài đã có flag nhưng chưa nộp, submit lên hệ thống và cập nhật trạng thái solved.

### 3. Giao diện tương tác nộp flag:
```bash
python3 submit.py -i
```

---

## 🏗️ Kiến trúc source code (`ctf_downloader/`)

Toàn bộ business logic sống trong **services** (use-case) và **storage** (đọc/ghi
workspace). Các module ở tầng ngoài (CLI, entrypoint script, facade) chỉ là lớp
mỏng delegate xuống — không chứa logic nhân bản.

```text
ctf_downloader/
├── cli.py                  # Parser + dispatch (không input(), không logic)
├── cli_commands.py         # Handler mỏng: parse → service → render → exit code
├── cli_legacy.py           # argparse nguyên văn của submit/manage/instance/rank.py cũ
├── config.py               # DownloaderConfig + validate (urlnorm)
├── core.py                 # Facade CTFDownloader → PullService
├── dashboard.py            # Facade CTFDashboard → StatusService
├── instance_manager.py     # Facade InstanceManager → InstanceService
├── ranking.py              # Facade RankingManager → RankService
├── submitter.py            # Facade FlagSubmitter → SubmitService
├── interactive_menu.py     # Wizard tương tác (gọi services)
│
├── services/               # 💡 Toàn bộ use-case (logic duy nhất của từng việc)
│   ├── pull_service.py     #    Pull/download toàn bộ giải
│   ├── status_service.py   #    Scan workspace + stats + render cây challenge + scan-all
│   ├── submit_service.py   #    Submit flag: format gate, blacklist, throttle theo registry
│   ├── rank_service.py     #    Live scoreboard + ghi RANKING.md / SUMMARY.md
│   ├── instance_service.py #    Container: list/start/stop/extend/sync + interactive_pick
│   ├── auth_service.py     #    Resolve cookie/token cho một workspace
│   ├── platform_resolver.py#    Chọn adapter platform (khai báo rõ hoặc auto-detect)
│   └── session_factory.py  #    Tạo requests session có auth
│
├── storage/                # 💾 Đọc/ghi workspace & config (atomic + lockfile)
│   ├── workspace_repo.py   #    challenges.json, metadata.json, RANKING/SUMMARY patch...
│   ├── fileio.py           #    Ghi atomic (.tmp + rename), locked_update_json
│   ├── global_config.py    #    Config toàn cục (~/.ctf_downloader)
│   └── constants.py        #    Hằng số chia sẻ (LIVE_RANK_PREFIX, anchor SUMMARY...)
│
├── platforms/              # 🔌 Adapter nền tảng CTF — đăng ký bằng 1 decorator
│   ├── registry.py         #    Nguồn chân lý: PlatformSpec (throttle, markers, probes...)
│   ├── base.py             #    BasePlatform: authenticate/fetch_challenges/submit_flag...
│   ├── detection.py        #    Pipeline auto-detect 4 tầng (marker→cookie→probe→fallback)
│   ├── detector.py         #    Wrapper tương thích ngược
│   ├── capabilities.py     #    PlatformInfo + PLATFORM_TYPES (sinh tự từ registry)
│   ├── gzctf.py ctfd.py rctf.py custom_rest.py generic_html.py
│
├── downloaders/            # Tải file (http, gdrive, dropbox, mediafire, mega...) + manager
├── extractors/             # Trích xuất link bên thứ 3 từ đề bài
├── generator/              # Dựng workspace: README/metadata/solve.py/SUMMARY
└── utils/                  # logger, sanitize, urlnorm, flag_format, http_client

main.py / ctf.py / submit.py / manage.py / instance.py / rank.py   # shim ≤10 dòng
```

### ➕ Thêm một platform mới = 1 file mới (+ 1 dòng import)

Registry là nguồn dữ liệu duy nhất — bạn **không cần sửa** danh sách hardcode nào.
Tạo `ctf_downloader/platforms/my_platform.py`:

```python
from .base import BasePlatform
from .registry import register


def probe_api(origin, session, info, done):
    """Probe tầng 3 (tuỳ chọn): nhận diện qua API đặc trưng."""
    ...


@register("my_platform", label="My Platform", throttle=5.0,
          html_markers=("Powered by MyPlatform",),   # tầng 1: chuỗi trong HTML
          cookie_hints=("MP_Token",),                # tầng 2: tên cookie
          probes=(probe_api,),                       # tầng 3: probe API
          supports_container=True, supports_scoreboard=True)
class MyPlatform(BasePlatform):
    def authenticate(self): ...
    def fetch_challenges(self): ...
    def submit_flag(self, challenge_id, flag): ...
```

Rồi thêm 1 dòng import để kích hoạt decorator trong
`ctf_downloader/platforms/registry.py`:

```python
from . import ctfd, custom_rest, generic_html, gzctf, my_platform, rctf
```

Xong. Ngay lập tức platform mới:
- xuất hiện trong `capabilities.PLATFORM_TYPES` (sinh tự từ registry),
- có throttle riêng đọc bởi `submit_service` (mặc định 5.0s nếu không khai báo),
- dựng được adapter theo tên khi workspace khai báo `"platform": "my_platform"`.

> **Caveat auto-detect:** pipeline nhận diện 4 tầng trong
> `ctf_downloader/platforms/detection.py` duyệt các tuple ưu tiên cứng
> `_MARKER_PRIORITY` / `_COOKIE_PRIORITY` / `_PROBE_PRIORITY`
> (hiện chỉ `"rctf"`, `"ctfd"`, `"gzctf"`). Platform **hoàn toàn mới** muốn được
> auto-detect qua markers/cookie_hints/probes của chính nó phải thêm key của nó
> vào các tuple đó; đăng ký registry (throttle/capabilities/adapter theo tên)
> thì không cần.

> Fixture kiểm chứng hành vi này: `test_arch_phase4.py::TestOneFilePlatformFixture`.

---

## 🧪 Kiểm thử (Unit Tests)

Chạy bộ test suite tích hợp để kiểm tra mọi tính năng:
```bash
python3 test_suite.py
```

