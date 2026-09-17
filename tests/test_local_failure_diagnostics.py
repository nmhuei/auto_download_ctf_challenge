import errno

from ctf_downloader.utils.failure_diagnostics import diagnose_os_error


def test_disk_full_is_distinct_and_not_retryable():
    diag = diagnose_os_error(OSError(errno.ENOSPC, "No space left on device"))
    assert diag.code == "disk-full"
    assert "df -h" in diag.hint
    assert diag.retryable is False


def test_permission_and_read_only_are_distinct():
    denied = diagnose_os_error(PermissionError(errno.EACCES, "denied"))
    ro = diagnose_os_error(OSError(errno.EROFS, "Read-only file system"))
    assert denied.code == "permission-denied"
    assert ro.code == "read-only-filesystem"


def test_fd_exhaustion_suggests_concurrency_or_ulimit():
    process = diagnose_os_error(OSError(errno.EMFILE, "Too many open files"))
    system = diagnose_os_error(OSError(errno.ENFILE, "File table overflow"))
    assert process.code == "process-fd-limit"
    assert "ulimit" in process.hint
    assert process.retryable is True
    assert system.code == "system-fd-limit"
    assert "concurrency" in system.hint


def test_missing_path_and_long_name_have_specific_codes():
    missing = diagnose_os_error(FileNotFoundError(errno.ENOENT, "missing"))
    long_name = diagnose_os_error(OSError(errno.ENAMETOOLONG, "too long"))
    assert missing.code == "path-missing"
    assert long_name.code == "path-too-long"


def test_cross_device_and_io_error_fail_closed():
    cross = diagnose_os_error(OSError(errno.EXDEV, "cross-device link"))
    io = diagnose_os_error(OSError(errno.EIO, "I/O error"))
    assert cross.code == "cross-device-rename"
    assert cross.retryable is False
    assert io.code == "io-error"
