"""Facade tương thích — toàn bộ logic ranking nằm ở services.rank_service.RankService.

Các import ``create_session`` / ``PlatformDetector`` được giữ ở đây chỉ để
tương thích ngược với mock.patch("ctf_downloader.ranking....") trong test suite.
"""
from .platforms.detector import PlatformDetector  # noqa: F401
from .services.rank_service import RankService
from .services.session_factory import create_session  # noqa: F401

__all__ = ["RankingManager", "RankService"]


class RankingManager(RankService):
    """Facade mỏng giữ nguyên constructor + method công khai của bản cũ."""
