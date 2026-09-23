# ⚡ CTF Toolkit

<p align="center">
  <strong>Unified CTF Operations Cockpit & Autonomous Multi-Agent Solver Engine</strong><br>
  <em>Tác chiến CTF toàn diện: Tải đề, Quản lý Container, Giải tự động SuperBQA, Đồng bộ Burp Suite & Nộp Flag.</em>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10%2B-blue?style=flat-square&logo=python" alt="Python">
  <img src="https://img.shields.io/badge/Architecture-Phosphor%20Cockpit-success?style=flat-square" alt="Cockpit">
  <img src="https://img.shields.io/badge/Skills-ctf--ask%20%7C%20ctf--toolkit%20%7C%20ctf--crypto-purple?style=flat-square" alt="Skills">
  <img src="https://img.shields.io/badge/Tests-2050%2B%20Passed-green?style=flat-square" alt="Tests">
</p>

---

## 🚀 Quick Start (Cài đặt & Khởi động trong 30s)

```bash
# 1. Cài đặt trực tiếp hoặc qua pipx (khuyên dùng)
git clone https://github.com/nmhuei/auto_download_ctf_challenge.git && cd auto_download_ctf_challenge
pip install -e .

# 2. Khởi động Trung tâm Tác chiến Cockpit TUI
ctf menu
```

Hoặc tải giải đấu ngay từ dòng lệnh:
```bash
# Tự động bắt cookie từ Burp Suite proxy và tải toàn bộ challenge
ctf pull -u https://ctf.example.com --from-burp -o ./workspace_ctf
```

---

## 🎯 Tính Năng Cốt Lõi

| Phân hệ | Mô tả năng lực tác chiến |
|---|---|
| 🕹️ **Tactical Cockpit TUI** | Bàn điều khiển 5-Hub (`ctf menu`) với 10 bảng màu Cyberpunk/Matrix/Amber, Live Radar & Scoreboard trực tiếp. |
| 🤖 **SuperBQA Autosolver** | Worker pool giải bài tự động đa luồng theo danh mục (`ctf solve`), tự phục hồi khi dính filter an toàn AI. |
| 🧠 **Bộ Kỹ năng AI (Skills)** | Tích hợp sẵn `ctf-ask` (tham vấn toán học hình thức), `ctf-toolkit`, `ctf-crypto` trong `.agents/skills/`. |
| 🔌 **Burp Suite Auto-Sync** | Tự động đọc và đồng bộ cookie/token trực tiếp từ Burp Suite proxy đang chạy (`ctf auth --from-burp`). |
| 🐳 **Dynamic Instances** | Tự động spawn, gia hạn (`< 5m`) và điều phối container động cho Web/Pwn (`ctf instance`). |
| 📦 **Git Lifecycle & Pack** | Tự động tạo nhánh event, checkpoint khi có flag mới, nén attachment lớn bằng XZ (`ctf git`). |
| 🏁 **Flag Hoarder & Sniper** | Lưu flag cục bộ (`ctf hoard`), hẹn giờ bắn flag thần tốc vào thời khắc quyết định (`ctf sniper`). |

---

## ⚡ Cheatsheet Lệnh Tác Chiến

```bash
# === 1. TẢI ĐỀ & ĐỒNG BỘ AUTH ===
ctf pull -u <URL> --from-burp -o ./my_ctf     # Tải đề tự động lấy cookie từ Burp Suite
ctf auth --show                              # Xem credentials và trạng thái proxy hiện tại
ctf register -u <URL> --tempmail             # Tự tạo tài khoản qua email tạm (mail.tm)

# === 2. GIẢI BÀI TỰ ĐỘNG (SUPERBQA) ===
ctf solve -w ./my_ctf --workers 3            # Chạy pool 3 worker giải bài nền
ctf solve --status                           # Theo dõi tiến độ, logs và candidate flags

# === 3. CONTAINER ĐỘNG & SCOREBOARD ===
ctf instance start --id 12 -w ./my_ctf       # Khởi chạy container động cho challenge 12
ctf rank -n 20                               # Bảng xếp hạng trực tiếp dạng TUI
ctf submit --id 12 -f "FLAG{...}"            # Nộp flag (hoặc 'ctf hoard' để lưu trữ bí mật)

# === 4. GIT LIFECYCLE CHO MỖI GIẢI ===
ctf git init -d ~/Workspace/CTF --remote-url git@github.com:user/ctf-vault.git
ctf git push -w ./my_ctf                     # Đẩy checkpoint bài giải lên GitHub/GitLab
ctf git finish -w ./my_ctf                   # Merge nhánh giải đấu vào main và dọn dẹp
```

---

## 🧠 Bộ Kỹ Năng Tích Hợp Cho AI Agents (`.agents/skills/`)

Dự án trang bị sẵn 3 kỹ năng tác chiến chuẩn mực cho các AI Agent (Antigravity CLI / AGY / Codex / Claude Code):

1. **`ctf-ask`**: Kỹ năng tham vấn chuyên gia toán học rời rạc / đại số (Astra High / Codex) với **Giao thức 5 bước tự phục hồi khi dính Filter an toàn** và cơ chế sandbox vật lý cô lập tuyệt đối.
2. **`ctf-toolkit`**: Kỹ năng điều phối tổng lực, prompt routing Telex thông minh, kiểm soát container và quản lý worker pool.
3. **`ctf-crypto`**: Kỹ năng chuyên sâu giải mật mã hiện đại (Lattice CVP/SVP, RSA Coppersmith, PRNG, AES DFA).

---

<!-- BEGIN GENERATED CLI OPTIONS -->
### Chỉ mục tuỳ chọn CLI (tự sinh)

> Nguồn chân lý: ctf_downloader.cli.build_unified_parser(). Chạy python3 scripts/generate_cli_option_index.py sau khi đổi parser.

| Lệnh | Long options |
| --- | --- |
| `ctf pull` | `--allow-private-redirects` · `--burp-port` · `--category` · `--cookie` · `--exclude` · `--force` · `--from-burp` · `--git-base` · `--git-remote` · `--insecure` · `--interactive` · `--no-git` · `--no-git-push` · `--no-template` · `--no-third-party` · `--output` · `--proxy` · `--refresh-meta` · `--save-cookie` · `--threads` · `--timeout` · `--token` · `--update` · `--url` · `--verify-downloads` |
| `ctf status` | `--category` · `--container` · `--label` · `--search` · `--set` · `--solved` · `--solver` · `--unsolved` · `--watch` · `--workspace` |
| `ctf solve` | `--active` · `--attach` · `--bg` · `--cancel` · `--detach` · `--distill` · `--foreground` · `--ids` · `--logs` · `--new-session` · `--reset-sessions` · `--stale-timeout` · `--status` · `--stop` · `--timeout` · `--workers` · `--workspace` |
| `ctf note` | `--remove` · `--workspace` |
| `ctf tag` | `--remove` · `--workspace` |
| `ctf workspaces` | `--dir` |
| `ctf instance` | `--auto-extend` · `--auto-extend-all` · `--cookie` · `--id` · `--interactive` · `--list` · `--name` · `--token` · `--workspace` · `--yes` |
| `ctf submit` | `--auto` · `--cookie` · `--flag` · `--flag-format` · `--force` · `--id` · `--interactive` · `--name` · `--token` · `--url` · `--workspace` |
| `ctf hoard` | `--all` · `--flag` · `--id` · `--list` · `--name` · `--remove` · `--workspace` |
| `ctf rank` | `--cookie` · `--no-docs` · `--token` · `--top` · `--url` · `--workspace` |
| `ctf watch` | `--cookie` · `--end` · `--no-scoreboard` · `--once` · `--start` · `--token` · `--workspace` |
| `ctf register` | `--cf-clearance` · `--email` · `--password` · `--tempmail` · `--url` · `--username` · `--workspace` |
| `ctf doctor` | `--cookie` · `--insecure` · `--runtime` · `--token` · `--url` · `--workspace` |
| `ctf menu` | `--cookie` · `--token` · `--workspace` |
| `ctf storage` | `--base-dir` · `--threshold-mb` |
| `ctf storage archive` | `--git-remote` · `--out` · `--yes` |
| `ctf sync` | `--apply` · `--insecure` · `--pull` · `--pull-status` · `--verify` · `--workspace` |
| `ctf history` | `--all` · `--clear` · `--limit` · `--prune` · `--tail` · `--workspace` |
| `ctf sniper` | `--poll` · `--retry-wrong` · `--start-at` · `--workspace` |
| `ctf serve` | `--port` · `--workspace` |
| `ctf open` | `--workspace` |
| `ctf git init` | `--base` · `--dir` · `--import-existing` · `--no-push` · `--remote` · `--remote-url` |
| `ctf git status` | `--workspace` |
| `ctf git push` | `--message` · `--no-pack` · `--no-push` · `--threshold` · `--workspace` |
| `ctf git pack` | `--all` · `--keep-original` · `--threshold` · `--workspace` |
| `ctf git unpack` | `--keep-xz` · `--workspace` |
| `ctf git finish` | `--base` · `--keep-remote` · `--no-push` · `--remote` · `--workspace` |
| `ctf config` | — |
| `ctf auth` | `--burp-port` · `--clear` · `--cookie` · `--from-burp` · `--show` · `--token` · `--url` · `--workspace` |
| `ctf bridge` | — |
| `ctf platform list` | `--workspace` |
| `ctf platform show` | `--workspace` |
| `ctf platform probe` | `--key` · `--label` · `--save` · `--scope` · `--workspace` |
| `ctf platform add` | `--scope` · `--workspace` |
| `ctf platform remove` | `--scope` · `--workspace` |
| `ctf pack` | `--all` · `--keep-original` · `--threshold` · `--workspace` |
| `ctf unpack` | `--keep-xz` · `--workspace` |
<!-- END GENERATED CLI OPTIONS -->

---

<p align="center">
  <sub>Built with ⚡ by nmhuei & Antigravity Agentic Team · UCS_ExOdia Phosphor Engine</sub>
</p>
