---
description: Quy tắc gọi Codex CLI tham vấn sau mỗi mục lớn và đọc lại khi cronjob kích hoạt
globs: ["*"]
always_on: true
---

# Major Task Codex Consultation & Design Rules

## 1. Codex CLI Collaborative Consultation
- **Khi nào áp dụng**: Sau khi hoàn thành bất kỳ mục lớn nào (major feature / bugfix / refactor).
- **Hành động**: Gọi `codex exec "<prompt>"` hoặc tương tác với `codex cli` để:
  1. Phản biện và review code độc lập.
  2. Cùng thảo luận và đối chiếu các ý tưởng giải pháp tiếp theo.
  3. Ghi nhận các đóng góp và chốt phương án trước khi chuyển sang giai đoạn mới.

## 2. Cronjob Periodic Refresh
- Mỗi khi cronjob (chu kỳ 20 phút) gửi thông báo, luôn đọc lại quy tắc này để đảm bảo nhất quán và không quên workflow.

## 3. Strict Anti-Hardcoding & Modular UI
- **Tuyệt đối không hardcode** màu sắc ANSI, hex codes hay styling trực tiếp trong logic code.
- Mọi styling phải sử dụng semantic tokens từ hệ thống Theme (`ctf_downloader.ui.theme`).
- Thiết kế module hóa cao: các tính năng mới phải được chia thành các service hoặc helper độc lập, dễ mở rộng và dễ viết unit test.
- Hỗ trợ đa dạng Theme (ExOdia Cyan, Matrix Emerald, Cyberpunk Neon, Amber Tactical) mà vẫn giữ 100% tương thích ngược với cấu hình hiện tại.

## 4. Full Autonomous Authority (Toàn quyền tự quyết & Tự động hóa)
- Agent có toàn quyền quyết định phương án thiết kế, tái cấu trúc và tối ưu hóa tốt nhất theo tiêu chí đơn giản, hiện đại, tiện dụng.
- Tự hành động xuyên suốt từ lập kế hoạch, refactor, kiểm thử thực tế, debug chi tiết đến hoàn thiện mà không cần dừng lại xin xác nhận hay chờ approve từ user.

## 5. Solver Testing & Prompt Generation Guideline (Kiểm thử Solver & Prompt)
- Khi kiểm thử tính năng giải bài (solver/AGYworker) hoặc sinh prompt: chỉ cần đảm bảo luồng hoạt động ổn định, sinh prompt chuẩn xác, không kích hoạt/dính bộ lọc an toàn (cybersecurity filter/censorship) là coi như ĐẠT. Không cần ép agent phải giải hoàn chỉnh ra flag toàn bộ bài đó trong quá trình test.

## 6. Tactical UI Uniformity & Design System Standard (Quy chuẩn Đồng bộ Giao diện)
- **Đồng bộ bố cục (Layout Consistency)**: Toàn bộ các menu, sub-hub, settings hoặc bảng hiển thị phải tuân thủ layout thống nhất của Cockpit:
  - Header / Panel định danh: Sử dụng `app_header` hoặc `Panel(..., border_style=ACCENT)` có tiêu đề rõ ràng, icon đại diện, subtitle/description xúc tích.
  - Danh sách thao tác: Sử dụng Rich `Table.grid(padding=(0, 1))` bọc ngoài bởi `Padding(grid, (0, 2))`. Tuyệt đối không dùng string padding thủ công (như `" " * 15`, `\t` hay cột dummy `Text("  ")`).
  - Cột chuẩn trong Grid: `[Key]` (phím số/chữ in hoa, `bold ACCENT`), `[Swatch]` (dải màu nếu là theme/color preview), `[Title]` (`FG_BASE` hoặc `bold ACCENT` nếu active), `[Badge]` (`● active` màu `SUCCESS`), `[Description]` (`FG_MUTED`).
  - Tùy chọn thoát/quay lại: Luôn tích hợp `[0]` thẳng vào bảng/grid, định dạng `bold FG_FAINT`, nhãn `Quay lại Menu chính` (`FG_MUTED`), không in rời rạc hay lệch lề.
- **Dải màu trực quan (Color Ramps & Visual Swatches)**:
  - Khi hiển thị theme hay bảng màu, luôn render các dải màu trực quan `[■■■■■■]` bằng mã màu thực tế của palette để người dùng preview trước khi chọn.
  - Cung cấp live spectrum breakdown (Core tokens + Category tokens) trong panel showcase.
- **Thao tác nhanh & Linh hoạt**:
  - Cho phép người dùng nhập cả số thứ tự (`1-10`) hoặc tên/alias trực tiếp (`dracula`, `tokyo`, `cyberpunk`, `q`, `exit`).
