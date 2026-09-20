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
