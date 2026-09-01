"""Stable user-facing diagnostics for local OS/resource failures."""
from __future__ import annotations

import errno
from dataclasses import dataclass


@dataclass(frozen=True)
class LocalFailureDiagnostic:
    code: str
    summary: str
    hint: str
    retryable: bool = False


def diagnose_os_error(exc: BaseException) -> LocalFailureDiagnostic:
    """Classify filesystem/process-resource failures without string guessing."""
    if isinstance(exc, PermissionError):
        return LocalFailureDiagnostic(
            "permission-denied",
            "Không có quyền truy cập/ghi tài nguyên local",
            "Kiểm tra quyền ghi/owner/chmod/chown hoặc chọn thư mục thuộc user hiện tại.",
        )
    if isinstance(exc, FileNotFoundError):
        return LocalFailureDiagnostic(
            "path-missing",
            "File/thư mục cần thiết không tồn tại",
            "Kiểm tra đường dẫn và bảo đảm thư mục cha chưa bị xoá/di chuyển.",
        )

    if not isinstance(exc, OSError):
        return LocalFailureDiagnostic(
            "unknown-local-error",
            f"Lỗi local không phân loại ({type(exc).__name__})",
            "Xem exception gốc/debug log trước khi thử lại.",
        )

    err = getattr(exc, "errno", None)
    if err == errno.ENOSPC:
        return LocalFailureDiagnostic(
            "disk-full",
            "Thiết bị lưu trữ đã hết chỗ trống",
            "Giải phóng dung lượng (df -h) hoặc chọn output trên filesystem khác.",
        )
    if hasattr(errno, "EDQUOT") and err == errno.EDQUOT:
        return LocalFailureDiagnostic(
            "quota-exceeded",
            "User/filesystem quota đã hết",
            "Kiểm tra quota và giải phóng dữ liệu hoặc đổi output location.",
        )
    if err in (errno.EACCES, errno.EPERM):
        return LocalFailureDiagnostic(
            "permission-denied",
            "Không có quyền truy cập/ghi tài nguyên local",
            "Kiểm tra quyền ghi/owner/chmod/chown hoặc chọn thư mục thuộc user hiện tại.",
        )
    if err == errno.EROFS:
        return LocalFailureDiagnostic(
            "read-only-filesystem",
            "Filesystem đang ở chế độ read-only",
            "Remount filesystem ở chế độ ghi hoặc chọn output writable khác.",
        )
    if err == errno.EMFILE:
        return LocalFailureDiagnostic(
            "process-fd-limit",
            "Process đã mở quá nhiều file/socket",
            "Đóng bớt worker/socket hoặc tăng ulimit -n cho process.",
            True,
        )
    if err == errno.ENFILE:
        return LocalFailureDiagnostic(
            "system-fd-limit",
            "Hệ thống đã đạt giới hạn file descriptor",
            "Giảm concurrency hoặc tăng giới hạn file descriptor toàn hệ thống.",
            True,
        )
    if err == errno.ENAMETOOLONG:
        return LocalFailureDiagnostic(
            "path-too-long",
            "Đường dẫn hoặc tên file vượt giới hạn filesystem",
            "Rút ngắn workspace/output path hoặc filename.",
        )
    if err == errno.EBUSY:
        return LocalFailureDiagnostic(
            "resource-busy",
            "File/device đang bận hoặc bị process khác giữ",
            "Đóng process đang dùng tài nguyên rồi thử lại.",
            True,
        )
    if err == errno.EXDEV:
        return LocalFailureDiagnostic(
            "cross-device-rename",
            "Atomic rename đi qua hai filesystem khác nhau",
            "Giữ file tạm và file đích trên cùng filesystem.",
        )
    if err == errno.EIO:
        return LocalFailureDiagnostic(
            "io-error",
            "Filesystem/device trả lỗi I/O",
            "Kiểm tra dmesg/SMART/mount state trước khi ghi tiếp.",
        )
    if err == errno.ENOENT:
        return LocalFailureDiagnostic(
            "path-missing",
            "File/thư mục cần thiết không tồn tại",
            "Kiểm tra đường dẫn và race xoá/di chuyển thư mục.",
        )

    return LocalFailureDiagnostic(
        "os-error",
        f"Lỗi hệ điều hành ({type(exc).__name__}, errno={err})",
        "Kiểm tra exception gốc, quyền, filesystem và giới hạn tài nguyên.",
    )
