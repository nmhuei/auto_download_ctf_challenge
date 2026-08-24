"""Facade tương thích — toàn bộ logic submit nằm ở services.submit_service.SubmitService.

Các import ``create_session`` / ``PlatformDetector`` / ``time`` được giữ ở đây
chỉ để tương thích ngược với các mock.patch("ctf_downloader.submitter....")
trong test suite cũ.
"""
import time  # noqa: F401  (tương thích patch ctf_downloader.submitter.time.sleep)

from .platforms.detector import PlatformDetector  # noqa: F401
from .services.session_factory import create_session  # noqa: F401
from .services.submit_service import (
    BASE_KNOWN_PREFIXES,
    DEFAULT_THROTTLE,
    NO_FORMAT_MESSAGE,
    SubmitService,
)

__all__ = [
    "FlagSubmitter",
    "SubmitService",
    "NO_FORMAT_MESSAGE",
    "DEFAULT_THROTTLE",
    "BASE_KNOWN_PREFIXES",
]


class FlagSubmitter(SubmitService):
    """Facade mỏng giữ nguyên constructor + method công khai của bản cũ."""
