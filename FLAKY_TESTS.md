# FLAKY_TESTS.md — note về fail không-deterministic

Quy ước: mỗi mục ghi HIỆN TƯỢNG / TÁI HIỆU / GỐC RỄ / CÁCH TRÁNH.
Chỉ ghi khi đã điều tra có kiểm soát (không đoán).

---

## 2026-08-25 · `ctf hoard --list` exit=2 khi chạy song song với full pytest

**HIỆN TƯỢNG** (từ merge-readiness audit): smoke `ctf hoard --list` báo
exit=2 đúng 2 lần CHỈ khi chạy nền trong lúc full pytest suite đang chạy;
standalone luôn exit=0.

**TÁI HIỆU** (không tái hiện được — 0/748):
- Lần 1: loop `ctf hoard --list` ×400 chạy liên tục trong khi full suite
  (1179 tests) chạy 2 pass liên tiếp → 400/400 exit=0.
- Lần 2: loop xen kẽ `-w ~/Workspace/CTF` và repo-root ×348 trong khi
  **3** pytest full suite chạy đồng thời → 348/348 exit=0.
- Đặt tài nguyên cứng (`ulimit -v`, `ulimit -n`) → exit=1 (ImportError),
  không bao giờ 2.

**GỐC RỄ — kết luận môi trường/harness, không phải bug code:**
đường `--list` chỉ đọc, không khoá (`iter_challenges`/`read_metadata`
nuốt mọi exception → `{}`; `read_status` migrate-on-read thuần bộ nhớ);
exit code duy nhất nó phát ra là `sys.exit(1)` (workspace thiếu) hoặc 0
(`ctf_downloader/cli_commands.py:_render_hoard_list`). Exit=2 trong process
chỉ đến từ argparse usage-error — deterministic theo argv, không thể flaky.
=> 2 lần exit=2 của audit sinh ra NGOÀI process lệnh (wrapper/harness ghi
nhận exit code, hoặc argv lệch lúc gọi), không tái hiện được từ code.

**CÁCH TRÁNH:**
- Smoke `ctf hoard --list` (và các lệnh xem nói chung) nên chạy standalone
  hoặc SAU khi suite kết thúc; nếu chạy nền song song mà thấy exit != 0,
  bắt buộc log stderr + re-run standalone trước khi coi là fail thật.
- Harness audit cần chụp stderr từng lần gọi thay vì chỉ exit code.
