# Antigravity Agent Guidelines for auto_download_ctf_challenge

## Collaborative Architecture & Review Rule
1. **Codex CLI Consultation**:
   - Sau khi hoàn thành bất kỳ mục lớn nào (major milestone/feature), hãy gọi `codex cli` (`/home/light/.local/bin/codex exec "..."`) lên hỗ trợ review code độc lập, cross-validate kiến trúc và cùng thảo luận ý tưởng đưa ra phương án cải tiến tiếp theo.
2. **Cronjob Routine Check**:
   - Mỗi khi cronjob (20 phút) kích hoạt, hãy đọc lại quy tắc này (`AGENTS.md` / `.agents/rules/major-task-codex-consult.md`) để duy trì đúng định hướng và không quên các nguyên tắc kiến trúc.
3. **No Hardcoding & Modular Design**:
   - Tuyệt đối không hardcode bất kỳ màu sắc, đường dẫn hoặc cấu hình tĩnh nào trực tiếp vào logic xử lý.
   - Tách nhỏ các module (chia nhỏ trách nhiệm, single responsibility).
   - Màu sắc và giao diện phải đi qua Theme system (`ctf_downloader/ui/theme.py`), hỗ trợ mở rộng theme (như Cyberpunk, Matrix Emerald, Amber Radar, ExOdia Cyan) mà không sửa đổi code lõi.
4. **Full Autonomous Authority (Toàn quyền tự quyết & Tự động hóa)**:
   - Agent có toàn quyền quyết định phương án thiết kế, tái cấu trúc và tối ưu hóa tốt nhất theo tiêu chí đơn giản, hiện đại, tiện dụng.
   - Tự hành động xuyên suốt từ lập kế hoạch, refactor, kiểm thử thực tế, debug chi tiết đến hoàn thiện mà không cần dừng lại xin xác nhận hay chờ approve từ user.
5. **Solver Testing & Prompt Generation Guideline (Kiểm thử Solver & Prompt)**:
   - Khi kiểm thử tính năng giải bài (solver/AGYworker) hoặc sinh prompt: chỉ cần đảm bảo luồng hoạt động ổn định, sinh prompt chuẩn xác, không kích hoạt/dính bộ lọc an toàn (cybersecurity filter/censorship) là coi như ĐẠT. Không cần ép agent phải giải hoàn chỉnh ra flag toàn bộ bài đó trong quá trình test.
6. **Tactical UI Uniformity & Design System Standard (Quy chuẩn Đồng bộ Giao diện Tác Chiến)**:
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
