# ⚡ CTF Toolkit

Unified CTF CLI: tải challenge, submit flag, quản lý container động, scoreboard & dashboard — hỗ trợ CTFd, GZCTF, rCTF.

## 1. Install

```bash
git clone <repo> && cd auto_download_ctf_challenge
pip install .
```

Hoặc dùng [pipx](https://pipx.pypa.io/) (cô lập môi trường, không đụng system Python):

```bash
pipx install .
```

## 2. Set up ban đầu

Tự tạo tài khoản trên platform (GZCTF/CTFd) và lưu auth:

```bash
ctf register -u https://ctf.example.com --tempmail
```

`--tempmail` tự sinh email tạm (mail.tm) + mật khẩu random mạnh, verify email nếu platform bắt buộc.

Nếu đã có sẵn tài khoản, dán cookie/token thủ công vào lệnh bất kỳ (`-c "session=xxx"` hoặc `-t <token>`).

Kiểm tra sức khoẻ platform trước giờ giải:

```bash
ctf doctor -u https://ctf.example.com
```

Credentials được lưu trong **auth map** tại `~/.config/ctf_toolkit/config.json`, map theo URL/workspace — các lệnh sau đó không cần truyền lại cookie (chỉ cần `-w <workspace>`).

## 3. Cách sử dụng cơ bản

| Lệnh | Mô tả | Ví dụ |
|---|---|---|
| `pull` | Tải toàn bộ challenge + build workspace | `ctf pull -u https://ctf.example.com -c "session=xxx" -o ./my_ctf` |
| `status` | Cây challenge, points, tiến độ solve | `ctf status -w my_ctf -u` |
| `workspaces` | Quét các workspace trên máy | `ctf workspaces -d ~/Workspace/CTF` |
| `instance` | Bật/tắt/gia hạn container động | `ctf instance start --id 34 -w my_ctf` |
| `submit` | Nộp flag lên platform | `ctf submit --id 16 -f "FLAG{...}"` · `ctf submit --auto` |
| `hoard` | Lưu flag local, KHÔNG nộp | `ctf hoard 16 "FLAG{...}"` |
| `rank` | Scoreboard live + cập nhật RANKING.md | `ctf rank -n 20` |
| `watch` | Auto-sync challenge/scoreboard trong event window | `ctf watch --once` |
| `doctor` | Health-check platform (auth/capabilities/window) | `ctf doctor -u https://ctf.example.com` |
| `storage` | Báo cáo dung lượng + archive tar.gz | `ctf storage archive my_ctf` |
| `note` | Ghi chú cho challenge | `ctf note 12 "đã thử SSTI, bị WAF chặn"` |
| `tag` | Gắn label cho challenge | `ctf tag 12 hard todo` |
| `sync` | Đồng bộ metadata động (points/solves) | `ctf sync --verify` |
| `export-pack` | Đóng gói writeup các bài solved | `ctf export-pack --out ./packs` |
| `history` | Lịch sử submit flag | `ctf history --all` |
| `sniper` | Preload flag, nộp ngay giờ G | `ctf sniper --start-at "2026-09-01T08:00:00+07:00"` |
| `serve` | Dashboard web read-only | `ctf serve --port 8689` |
| `register` | Tạo tài khoản + lưu auth map | `ctf register -u https://ctf.example.com --tempmail` |
| `menu` | Interactive console đầy đủ | `ctf menu` |

### Giao diện PHOSPHOR

Output TUI dùng theme **phosphor** (progress bar `█░`, icon trạng thái: ✔ solved · ⛁ có attachment · ⎘ có note · ✎ draft flag):

```text
▐██ CTF·TOOLKIT  │  status · PTIT_CTF_2026                           22:05 UTC+7
╭────────────────  Vòng loại PTIT CTF 2026  ─────────────────╮
│                                                            │
│  TIẾN ĐỘ                           ĐIỂM                    │
│  ░░░░░░░░░░░░░░░░░░░░░░            500 / 8135              │
│  1/35 solved · 2.9%                hoarded 0 · drafts 1    │
│                                                            │
╰──────────────────── gzctf · B23DCCE070 ────────────────────╯

── CRYPTO                                              0/7 ░░░░░░░░░░ 0/1880
  ·  12  SSSH                      180 pts    9 giải   ⛁ ⎘
  ·   5  Signaling                 108 pts   20 giải   ⎘

── REVERSE                                           1/7 █░░░░░░░░░ 500/1587
  ✔  18  Tiger Bạc                 500 pts    0 giải   ✎

↑↓ di chuyển · ? help · q thoát
```

Chạy `ctf <lệnh> --help` để xem đầy đủ tuỳ chọn của từng lệnh.

## 4. Cây workspace output

```
my_ctf/
├── challenges.json          # metadata tổng (points/solves/solved/status)
├── SUMMARY.md               # tổng quan giải
├── Web/
│   └── <challenge-name>/
│       ├── README.md        # đề bài, hints, connection info
│       ├── metadata.json    # dữ liệu thô + status/notes/tags/flag
│       ├── solve.py         # template giải mẫu (pwntools/requests)
│       └── <attachment>
├── Pwn/
├── Crypto/
└── ...
```

Docs thiết kế chi tiết: [`docs/superpowers/specs`](docs/superpowers/specs).
