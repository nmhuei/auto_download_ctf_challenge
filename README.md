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

Tự tạo tài khoản trên platform (GZCTF/CTFd/rCTF) và lưu auth:

```bash
ctf register -u https://ctf.example.com --tempmail
```

`--tempmail` tự sinh email tạm (mail.tm) + mật khẩu random mạnh, verify email nếu platform bắt buộc.

Cloudflare được xử lý adaptive: traffic thường vẫn dùng `requests`; khi phát hiện Cloudflare, tool chuyển sang browser TLS/HTTP fingerprint bằng `curl_cffi`. Nếu Managed Challenge/Turnstile vẫn chặn, mở site bằng browser rồi truyền `cf_clearance`: `ctf register --cf-clearance <value>` hoặc với lệnh có `-c`, dùng `-c "cf_clearance=xxx; session=yyy"`. Tool không tự bypass CAPTCHA và không replay mù POST/submit.

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
| `git` | Branch/push/merge lifecycle riêng cho từng giải | `ctf git status -w my_ctf` · `ctf git finish -w my_ctf` |
| `note` | Ghi chú cho challenge | `ctf note 12 "đã thử SSTI, bị WAF chặn"` |
| `tag` | Gắn label cho challenge | `ctf tag 12 hard todo` |
| `sync` | Đồng bộ metadata động (points/solves) | `ctf sync --verify` |
| `history` | Lịch sử submit flag (mặc định 100 entry mới nhất) | `ctf history --tail 20` · `ctf history --all` · `ctf history --prune 'FLAG{...}'` · `ctf history --clear` |
| `sniper` | Preload flag, nộp ngay giờ G | `ctf sniper --start-at "2026-09-01T08:00:00+07:00"` |
| `serve` | Dashboard web local (POST submit qua gate CLI) | `ctf serve --port 8689` |
| `open` | Mở thư mục challenge trong file manager | `ctf open 12` |
| `config` | Xem/đặt cấu hình toàn cục — global là mặc định, workspace `.ctf/config.json` override | `ctf config auto-sync off` |
| `register` | Tạo tài khoản + lưu auth map | `ctf register -u https://ctf.example.com --tempmail` |
| `menu` | Interactive console đầy đủ | `ctf menu` |

Đường dẫn mặc định cho mọi workspace có thể cấu hình một lần:

```bash
ctf config workspace-root ~/Workspace/CTF
```

`pull` khi không truyền `-o`, cùng với `workspaces`, `storage` và `git init`, đều dùng `workspace-root` này.

### Git workflow theo từng giải

Khởi tạo một shared Git repo (làm một lần) và gắn remote:

```bash
ctf git init -d ~/Workspace/CTF --remote-url git@github.com:user/ctf-workspaces.git
# Nếu ~/Workspace/CTF đã có dữ liệu cũ và muốn đưa chúng vào main:
ctf git init -d ~/Workspace/CTF --remote-url git@github.com:user/ctf-workspaces.git --import-existing
```

Sau đó `ctf pull` mặc định tạo/checkout branch `ctf/<ten-giai>`, chỉ commit thư mục workspace của giải và tự push branch lên `origin` khi remote đã cấu hình:

```bash
ctf pull -u https://ctf.example.com -o ~/Workspace/CTF/Example_CTF_2026
ctf git push -w ~/Workspace/CTF/Example_CTF_2026
ctf git status -w ~/Workspace/CTF/Example_CTF_2026
```

Khi giải kết thúc:

```bash
ctf git finish -w ~/Workspace/CTF/Example_CTF_2026
```

`finish` tạo final checkpoint, merge event branch vào `main` bằng `--no-ff`, push `main`, rồi mới xóa branch event local/remote. Nếu working tree có thay đổi ngoài workspace, merge conflict, hoặc push `main` thất bại thì branch event được giữ nguyên. Dùng `pull --no-git` để tắt workflow cho một lượt, hoặc `--no-git-push` để chỉ commit local.

### Giao diện UCS_ExOdia

Brand CLI dùng **UCS_ExOdia** theo hướng brutalist + cyber minimal. Màu brand chạy từ teal/cyan → blue → violet → fuchsia → amber; mỗi stage `detect / pull / workspace / submit / watch / sniper / rank / automate` có một micro-gradient riêng. Màu semantic của dữ liệu vẫn dùng theme Amber Refit (`#FFB000`, ✔ solved, ✗ error, ! warning), nên màu trang trí không làm lẫn nghĩa trạng thái.

```text
UCS_ExOdia // status · ~/Workspace/CTF/PTIT_CTF_2026 ▰▰▰ ▰▰▰ ▰▰▰ ▰▰▰ ▰▰▰ ▰▰▰ ▰▰▰ ▰▰▰  22:49 UTC+7 · v3
╭──────────────  TIẾN ĐỘ · Vòng loại PTIT CTF 2026 · gzctf  ──────────────╮
│ ▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱ 1/35 · 2.9%   500/8135 pts · hoarded 0 · drafts 0 │
╰──────────────────────────────────────────────────────────────────────────╯
┌┐ CRYPTO ─────────────────────────────────────────────  0/7 ▱▱▱▱▱▱▱▱▱▱ 0/1880
  ·  12  SSSH                      180 pts    9 giải   ⛁ ⎘
  ·   5  Signaling                 108 pts   20 giải   ⎘
┌┐ REVERSE ──────────────────────────────────────────  1/7 ▰▱▱▱▱▱▱▱▱▱ 500/1587
  ✔  18  Tiger Bạc                 500 pts    0 giải   ⎘
```

AppHeader của lệnh thường chỉ còn **1 dòng**: `UCS_ExOdia // <command> · <context>` + spectral rail 8 stage + version; từ 100 cột trở lên mới thêm timestamp để ưu tiên giữ context ở terminal hẹp. Header tự co rail theo width, truncate context bằng `…`, không wrap. `status` dùng **một** panel `TIẾN ĐỘ`; activity/window chỉ xuất hiện khi có tín hiệu thật, không dựng panel `GIẢI` rỗng hay sparkline `+0`. Các category nối liền nhau, bỏ blank separator. Capture thật PTIT 35 challenge vừa đúng 45 dòng ở terminal 120×45; 80 và 60 cột đều không overflow.

Mở `ctf` hoặc `ctf menu`, full splash **UCS_ExOdia** xuất hiện đúng một lần: terminal ≥ 80 cột dùng splash 7 dòng (6 dòng logo brutalist + 1 footer brand/rail), terminal < 80 tự rơi về compact 2 dòng. `ctf --help` dùng full brand một lần; các subcommand dùng AppHeader 1 dòng để hạn chế scroll.

Chạy `ctf <lệnh> --help` để xem đầy đủ tuỳ chọn của từng lệnh.

<!-- BEGIN GENERATED CLI OPTIONS -->
### Chỉ mục tuỳ chọn CLI (tự sinh)

> Nguồn chân lý: ctf_downloader.cli.build_unified_parser(). Chạy python3 scripts/generate_cli_option_index.py sau khi đổi parser.

| Lệnh | Long options |
| --- | --- |
| `ctf pull` | `--allow-private-redirects` · `--category` · `--cookie` · `--exclude` · `--force` · `--git-base` · `--git-remote` · `--interactive` · `--no-git` · `--no-git-push` · `--no-template` · `--no-third-party` · `--output` · `--refresh-meta` · `--threads` · `--timeout` · `--token` · `--update` · `--url` · `--verify-downloads` |
| `ctf status` | `--category` · `--container` · `--label` · `--search` · `--solved` · `--unsolved` · `--workspace` |
| `ctf note` | `--remove` · `--workspace` |
| `ctf tag` | `--remove` · `--workspace` |
| `ctf workspaces` | `--dir` |
| `ctf instance` | `--auto-extend` · `--auto-extend-all` · `--cookie` · `--id` · `--interactive` · `--list` · `--name` · `--token` · `--workspace` · `--yes` |
| `ctf submit` | `--auto` · `--cookie` · `--flag` · `--flag-format` · `--force` · `--id` · `--interactive` · `--name` · `--token` · `--url` · `--workspace` |
| `ctf hoard` | `--all` · `--flag` · `--id` · `--list` · `--name` · `--remove` · `--workspace` |
| `ctf rank` | `--cookie` · `--no-docs` · `--token` · `--top` · `--url` · `--workspace` |
| `ctf watch` | `--cookie` · `--end` · `--no-scoreboard` · `--once` · `--start` · `--token` · `--workspace` |
| `ctf register` | `--cf-clearance` · `--email` · `--password` · `--tempmail` · `--url` · `--username` · `--workspace` |
| `ctf doctor` | `--cookie` · `--runtime` · `--token` · `--url` · `--workspace` |
| `ctf menu` | `--cookie` · `--token` · `--workspace` |
| `ctf storage` | `--base-dir` · `--threshold-mb` |
| `ctf storage archive` | `--git-remote` · `--out` · `--yes` |
| `ctf sync` | `--verify` · `--workspace` |
| `ctf history` | `--all` · `--clear` · `--limit` · `--prune` · `--tail` · `--workspace` |
| `ctf sniper` | `--poll` · `--retry-wrong` · `--start-at` · `--workspace` |
| `ctf serve` | `--port` · `--workspace` |
| `ctf open` | `--workspace` |
| `ctf git init` | `--base` · `--dir` · `--import-existing` · `--no-push` · `--remote` · `--remote-url` |
| `ctf git status` | `--workspace` |
| `ctf git push` | `--message` · `--no-push` · `--workspace` |
| `ctf git finish` | `--base` · `--keep-remote` · `--no-push` · `--remote` · `--workspace` |
| `ctf config` | — |
| `ctf bridge` | — |
<!-- END GENERATED CLI OPTIONS -->

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
