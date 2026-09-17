# Spec — BQA CLI Recovery (Case 1)

> Ngày 16/09/2026 · Phạm vi: tự khôi phục lỗi của CLI. Chức năng dùng AI giải challenge không thuộc đặc tả này.

## Mục tiêu

Khi một lệnh `ctf` thất bại, CLI tự chuyển incident đã được khử bí mật cho BQA. BQA là tên hiển thị của phiên `agy` bền vững theo source checkout. BQA có thể sửa trực tiếp source hiện hành, viết regression test, chạy kiểm tra rồi lệnh gốc được thử lại đúng một lần trong process Python mới.

## Luồng

1. `cli.main()` ghi nhận argv gốc và bọc dispatch bằng recovery boundary.
2. Lỗi chưa bắt, hoặc `SystemExit` khác `0`, tạo `RecoveryIncident` gồm command đã che bí mật, exit code/exception, traceback, repository state và cookie shape.
3. Không recovery cho help/version, menu tương tác, `KeyboardInterrupt`, hoặc process đã mang `CTF_BQA_RETRY=1`.
4. `BqaRecovery` lưu/tải session theo source root tại `~/.config/ctf_toolkit/bqa_sessions.json`, sau đó gọi `agy` trong source root. Lần đầu gửi bootstrap prompt đầy đủ; lần sau resume conversation đã lưu.
5. Trong TTY, BQA hiển thị trạng thái không cần nhập liệu; non-TTY ghi các trạng thái text. Process `agy` chạy bằng `Popen` để UI có thể cập nhật khi chờ.
6. Sau khi BQA kết thúc, runner kiểm `compileall`, `pytest --collect-only`, và các file test mà BQA đã tạo/sửa. Nếu kiểm tra đạt, runner gọi `python -m ctf_downloader.cli` với argv gốc, môi trường `CTF_BQA_RETRY=1`.
7. Retry không bao giờ gọi BQA lần hai. Lỗi cuối giữ exit code của retry và in trạng thái recovery.

## Bảo mật dữ liệu

- Prompt, session store và log không được chứa giá trị cookie, token, password, flag, `Authorization`, hoặc `Set-Cookie`.
- `CookieShape` chỉ giữ input kind (`header`, `json`, `raw-token`, `empty`), tên cookie, số segment hợp lệ/lỗi và lỗi parse JSON nếu có.
- Command được redaction cho các option `-c`, `--cookie`, `-t`, `--token`, `--password`, `-f`, `--flag`, `--cf-clearance` và giá trị kề sau chúng.
- BQA nhận source root, revision/diff summary và incident; nó không nhận raw response body hoặc credential.

## Phiên BQA

`BqaSessionStore` map canonical source root tới conversation ID, source revision và thời gian dùng cuối. Khi source revision thay đổi hoặc resume thất bại, runner tạo bootstrap session mới. Command runner dùng `agy --mode accept-edits --output-format json --print`; conversation ID được lấy từ JSON result và lần tiếp theo dùng `--conversation <id>`.

Bootstrap prompt yêu cầu BQA: đọc source trước khi sửa; chỉ sửa `ctf_downloader/`, `tests/`, và tài liệu cần thiết; không dùng credential; thêm regression test; chạy test liên quan; và in JSON result gồm conversation ID, test commands cùng kết quả.

## UX

TTY in các trạng thái: `Need help? BQA is investigating this failure…`, `collecting diagnostics`, `updating platform support`, `running regression tests`, và `retrying your command`. Đây là status tự động, không phải câu hỏi chờ xác nhận. Ctrl-C gửi terminate tới child BQA rồi trả mã 130.

## Tiêu chí chấp nhận

- Lỗi pull có exit code khác 0 gọi BQA một lần với incident đã che secrets.
- Retry không tự gọi BQA.
- Cookie header/JSON/raw token tạo shape đúng mà không lộ value.
- Session lần hai resume conversation đã lưu; session stale hoặc resume thất bại bootstrap lại.
- BQA không chạy cho help/version/menu/Ctrl-C.
- Các kiểm tra sau BQA chạy thành công mới cho phép retry.
- Không có code nào cho AI giải challenge trong change này.
