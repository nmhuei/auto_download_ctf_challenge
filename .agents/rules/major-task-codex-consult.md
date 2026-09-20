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
