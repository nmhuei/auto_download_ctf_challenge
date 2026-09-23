# Kiến trúc & Quy chuẩn Giao diện Cockpit (UI Structure & Design System)

Tài liệu này lưu trữ toàn bộ cấu trúc giao diện tương tác (TUI Cockpit), luồng điều hướng, bản đồ phím tắt và các template giao diện trực quan của `auto_download_ctf_challenge`.
Mọi cập nhật hoặc refactor liên quan đến UI **bắt buộc phải cập nhật tài liệu này** để duy trì tính nhất quán.

---

## 1. Luồng Render & Kiến trúc Màn hình Chính (Cockpit Flow)

Khi người dùng khởi chạy `ctf` hoặc sau khi hoàn thành bất kỳ lệnh thao tác nào quay trở về màn hình chính, nếu workspace đã tải bài thi (`total > 0`) và `SolverService.scan()` có dữ liệu, hệ thống tự động render bảng Solver Radar trực tiếp phía trên bảng hành động theo cấu trúc phân tầng:

```
┌──────────────────────────────────────────────────────────────┐
│ [Khối 1] AppHeader Banner (Định danh giải, timestamp UTC)    │
├──────────────────────────────────────────────────────────────┤
│ [Khối 2] Workspace Summary (Tên giải, tỷ lệ giải, meter bar) │
├──────────────────────────────────────────────────────────────┤
│ [Khối 3] SuperBQA Solver Radar Overview Panel (Thanh meter)  │
├──────────────────────────────────────────────────────────────┤
│ [Khối 4] Full Challenge Matrix Table (Phân loại theo Category│
│          Kèm Display ID, trạng thái, hoarded flag, container)│
├──────────────────────────────────────────────────────────────┤
│ [Khối 5] Category Sessions & Worker Daemon Running Banner    │
├──────────────────────────────────────────────────────────────┤
│ [Khối 6] Bảng Hành động Tác chiến (Table.grid trong Padding) │
│          [1] Đổi giải  [2] Nộp Flag  [3] Solver  [4] Rank... │
└──────────────────────────────────────────────────────────────┘
```

---

## 2. Bảng Phím tắt & Điều phối Hành động (Action Map)

Các mục menu được thiết kế **tối giản, loại bỏ toàn bộ chữ rườm rà trong dấu ngoặc đơn `()`**, tập trung 100% vào danh mục tác chiến:

| Phím | Tên Mục Hiển Thị | Hàm Xử Lý Nội Bộ | Danh Sách Alias / Lối Tắt | Chức Năng Tác Chiến |
| :---: | :--- | :--- | :--- | :--- |
| **`[1]`** | `🎯 Đổi giải CTF & Auth` | `hub_workspace_targets()` | `1`, `ws`, `target`, `workspace` | Đổi giải đấu, clone giải mới, nhập Cookie/Token, Platform Doctor |
| **`[2]`** | `🚩 Nộp Flag & Kho cờ` | `hub_flag_submission()` | `2`, `flag`, `submit`, `nop` | Nộp flag đơn lẻ theo ID, auto-submit toàn bộ cờ đã nhặt |
| **`[3]`** | `⚡ AI Solver SuperBQA` | `_menu_solver()` | `3`, `solve`, `solver`, `bqa`, `eat` | Bật AGYworker giải theo ID/all, Live Radar, xem logs, dừng worker |
| **`[4]`** | `🏆 Live Scoreboard & Rank` | `_menu_ranking()` | `4`, `rank`, `ranking`, `scoreboard`, `bxh` | Xem bảng điểm live, khoảng cách top, tự sync `RANKING.md` |
| **`[5]`** | `🌐 Quản lý Container` | `_menu_container_manager()` | `5`, `instance`, `container`, `dock` | Khởi động IP:Port, kiểm tra thời gian sống, gia hạn, hủy container |
| **`[T]`** | `🎨 Đổi màu Theme` | `_menu_theme()` | `T`, `t`, `theme`, `color`, `style` | Đổi dải màu (Cyberpunk, Matrix, Dracula, Amber, ExOdia) |
| **`[0]`** | `🚪 Thoát` | Thoát chương trình | `0`, `q`, `exit`, `quit`, `thoat` | Trở về shell hệ thống |

> **Lối tắt trực tiếp từ Prompt**: Người dùng có thể gõ `info <id>` hoặc `card <id>` (ví dụ `info 12`) để mở trực tiếp Action Card (README, hints, connection info) của bài đó mà không làm xáo trộn phím hành động mặc định.

---

## 3. Template Giao diện 1: Màn hình Chính Cockpit (Main View)

```text
┌────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ ◈ FIELD KIT COCKPIT · ASIS_CTF_2026                                                               14:15 UTC+7     │
└────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
  ASIS CTF 2026 · CTFD · asis-ctf-2026
  ▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▱▱  50/61 · 82.0% · 8450/10300 pts

╭────────────────────────────────────────────  SUPERBQA SOLVER · RADAR  ─────────────────────────────────────────────╮
│ ▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▱▱▱▱  50/61 solved · 82.0% · 27 hoarded · 0 workers running                                        │
╰────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
    ID  CHALLENGE             STATE        OUTCOME     SRC  INST  PHASE
┌┐ CRYPTO ────────────────────────────────────────────────────────  11/12 ▰▰▰▰▰▰▰▰▰▱  92%
  ✔  1  Crypto Challenge 1    ✔ completed  ✔+★          ✓    ✓    ★ flag{d15e…7697}
  ✔  2  Crypto Challenge 10   · idle       ✔+★          ✓    ✓    ★ flag{e419…aa7f}
  ·  3  Crypto Challenge 9    · idle       –            ✓    ✓    ready
┌┐ PWN ────────────────────────────────────────────────────────────   9/12 ▰▰▰▰▰▰▰▱▱▱  75%
  ✔  4  Pwn Challenge 1       · idle       ✔+★          ✓    ✓    ★ flag{e883…2a65}
  ·  5  Pwn Challenge 8       · idle       –            ✓    ✓    ready
┌┐ WEB ────────────────────────────────────────────────────────────   8/12 ▰▰▰▰▰▰▱▱▱▱  67%
  ✔  6  Web Challenge 1       · idle       ✔ platform   ✓    ✓    ✔ platform
  ✔  7  Web Challenge 12      · idle       ✔+★          ✓    ✓    ★ flag{5647…6b7c}
  ·  8  Web Challenge 6       · idle       –            ✓    ✓    ready
  📁 Category Sessions: crypto (71ee66e4...), misc (75192b20...)

  ACTIONS
  [1]  🎯 Đổi giải CTF & Auth
  [2]  🚩 Nộp Flag & Kho cờ
  [3]  ⚡ AI Solver SuperBQA
  [4]  🏆 Live Scoreboard & Rank
  [5]  🌐 Quản lý Container
  [0]  🚪 Thoát

  ❯ Select action (1-5, 0 [T=Theme]): 
```

---

## 4. Template Giao diện 2: Bảng xếp hạng Trực tiếp (Live Scoreboard View)

Tích hợp trực tiếp từ [`RankService.display_and_update()`](../ctf_downloader/services/rank_service.py):

```text
╭───────────────  BẢNG XẾP HẠNG · CTFD LIVE  ────────────────╮
│     #    TEAM / USER                  SCORE          GAP   │
│   ◆ 1    Kalmarunionen                12850            -   │
│   ◆ 2    Tea Deliverers               11420    -1430 pts   │
│   ◆ 3    Balsn                        10890     -530 pts   │
│     4    The Few Chosen                9840    -1050 pts   │
│     5    Our Team (YOU)                8920     -920 pts   │
│     6    DiceGang                      8500     -420 pts   │
│     7    Project Sekai                 7800     -700 pts   │
│ rank 5/250 · gap 920 pts                                   │
╰────────────────────────────────────────────────────────────╯

  ◈ LIVE SCOREBOARD & RANKING
  [1] Làm mới bảng xếp hạng
  [2] Thay đổi số đội hiển thị (Top 15)
  [0] Quay lại Menu chính

  ❯ Lựa chọn thao tác (0-2) [default 0]: 
```

---

## 5. Template Giao diện 3: Thẻ Tác chiến Chi tiết Bài thi (Action Card)

Khi gõ `info <id>` hoặc chọn bài trong Hub Nộp cờ:

```text
╭ ACTION CARD ────────────────────────────────────────────────────────────────────────────────────────────╮
│ Challenge  : Crypto Challenge 1                                                                         │
│ Metadata   : ID: 101  ·  Category: Crypto  ·  Points: 200  ·  Solves: 42                                │
│ Connection : nc ctf.example.com 1337                                                                    │
│ Status     : ✔ SOLVED (★ flag{d15e…7697} lưu trong README.md)                                          │
╰─────────────────────────────────────────────────────────────────────────────────────────────────────────╯

  ◈ TACTICAL ACTION: Crypto Challenge 1
  [1] Xem mô tả đề bài, Hints & File đính kèm
  [2] Dynamic Instance: Bật / Tắt / Gia hạn Container
  [3] Nộp Flag cho bài này
  [4] Chạy SuperBQA AI Solver tự động giải
  [0] Quay lại

  ❯ Select card action (0-4): 
```

---

## 6. Quy chuẩn Lập trình Giao diện (Design System Standards - Rule 6)

1. **Zero-Hardcoding**:
   - Tuyệt đối không dùng mã màu ANSI cứng (như `\033[32m`) hoặc hex code trực tiếp.
   - Luôn sử dụng semantic tokens từ [`ctf_downloader/ui/theme.py`](../ctf_downloader/ui/theme.py):
     `ACCENT`, `ACCENT_DEEP`, `FG_BASE`, `FG_MUTED`, `FG_FAINT`, `INFO`, `SUCCESS`, `WARN`, `ERROR`, `SOLVED`.
2. **Căn lề tự động bằng Rich Table.grid**:
   - Mọi danh sách thao tác, danh sách bài thi phải dùng `Table.grid(padding=(0, 1))` bọc bởi `Padding(grid, (0, 2))`.
   - Cột key luôn là `[Key]` (`bold ACCENT`), nhãn thoát `[0]` luôn ở cuối (`bold FG_FAINT`).
   - Mục đang active/chọn gần nhất: được highlight toàn bộ bằng màu xanh lá (`bold SUCCESS`), không gắn thêm text `● active` hay `[default ...]` ở prompt để giữ giao diện tối giản, thanh lịch.
3. **Dynamic Console Theme Recoloring**:
   - Mọi console thứ cấp (như `RankService._rank_console`) phải được cập nhật qua theme stack (`push_theme(..., inherit=False)`) khi người dùng đổi theme.
