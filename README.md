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
| `hoard` | Lưu flag local, KHÔNG nộp | `ctf hoard 16 "FLAG{...}"` · `ctf hoard --list` |
| `rank` | Scoreboard live + cập nhật RANKING.md | `ctf rank -n 20` |
| `watch` | Auto-sync challenge/scoreboard trong event window | `ctf watch --once` |
| `doctor` | Health-check platform (auth/capabilities/window) | `ctf doctor -u https://ctf.example.com` |
| `storage` | Báo cáo dung lượng + archive tar.gz | `ctf storage archive my_ctf` |
| `note` | Ghi chú cho challenge | `ctf note 12 "đã thử SSTI, bị WAF chặn"` |
| `tag` | Gắn label cho challenge | `ctf tag 12 hard todo` |
| `sync` | Đồng bộ metadata động (points/solves) | `ctf sync --verify` |
| `export-pack` | Đóng gói writeup các bài solved | `ctf export-pack --out ./packs` |
| `history` | Lịch sử submit flag (mặc định 100 entry mới nhất) | `ctf history --tail 20` · `ctf history --all` |
| `sniper` | Preload flag, nộp ngay giờ G | `ctf sniper --start-at "2026-09-01T08:00:00+07:00"` |
| `serve` | Dashboard web local (POST submit qua gate CLI) | `ctf serve --port 8689` |
| `open` | Mở thư mục challenge trong file manager | `ctf open 12` |
| `config` | Xem/đặt cấu hình toàn cục — global là mặc định, workspace `.ctf/config.json` override | `ctf config auto-sync off` |
| `register` | Tạo tài khoản + lưu auth map | `ctf register -u https://ctf.example.com --tempmail` |
| `menu` | Interactive console đầy đủ | `ctf menu` |

### Giao diện Amber Refit

Output TUI dùng theme **Amber Refit** (lineage Phosphor Field Kit) — accent `#FFB000`, meter `▰▱`, glyph trạng thái: ✔ solved · ◆ working · ⛁ container · ✎ draft writeup · ⎘ có file:

```text
░░▒▒▓▓░░▒▒▓▓░░▒▒▓▓░░▒▒▓▓░░▒▒▓▓░░▒▒▓▓░░▒▒▓▓░░▒▒▓▓░░▒▒▓▓░░▒▒▓▓░░▒▒▓▓░░▒▒▓▓░░▒▒▓▓░░▒▒▓▓░░▒▒▓▓░░▒▒▓▓░░▒▒
···································· CTF·TOOLKIT v3◢····································
▍status  ·  PTIT_CTF_2026
▸                                                                            22:05 UTC+7

╭─────────────  TIẾN ĐỘ  ─────────────╮╭───────────────────  GIẢI  ────────────────────╮
│ ▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱ 1/35         ││ +0 flags 24h ⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀                     │
│ 1/35 solved · 2.9%                  │╰───────────────────────────────────────────────╯
│ 500/8135 pts · hoarded 0 · drafts 1 │
╰── Vòng loại PTIT CTF 2026 · gzctf ──╯

┌┐ CRYPTO ───────────────────────────────────────────────────  0/7 ▱▱▱▱▱▱▱▱▱▱ 0/1880
  ·  12  SSSH                      180 pts    9 giải   ⛁ ⎘
  ·   5  Signaling                 108 pts   20 giải   ⎘

┌┐ REVERSE ────────────────────────────────────────────────  1/7 ▰▱▱▱▱▱▱▱▱▱ 500/1587
  ✔  18  Tiger Bạc                 500 pts    0 giải   ✎

[số] chọn · ❯ mục mặc định · ? help · q thoát
```

AppHeader "Phosphor Radar" 4 dòng: scanline `░░▒▒▓▓` full-width · title `CTF·TOOLKIT v3◢` căn giữa · `▍` lệnh + context · `▸` timestamp mép phải. Hai panel overview TIẾN ĐỘ + GIẢI xếp ngang khi terminal ≥ 96 cột, xếp dọc khi hẹp hơn; nhịp giải 24h (sparkline braille) nằm trong panel GIẢI. Meter gradient amber chỉ bật trên TTY ≥ 60 cột — non-TTY/terminal hẹp fallback plain `▰▱` không màu. Chọn bằng phím số `[n]`; mục mặc định/đang dùng đánh dấu `❯`.

Mở `ctf menu`, một splash logo lớn hiện đúng một lần trước radar đầu tiên của phiên: terminal ≥ 80 cột nhận bản `big` trong khung box-drawing, hẹp hơn tự rơi về bản pagga rail gọn. Các lệnh framed khác vẫn giữ radar 4 dòng như mẫu trên.

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
