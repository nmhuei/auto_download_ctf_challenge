"""
SP3 — Recon facade (tương thích ngược 100%).

Toàn bộ pipeline 4 tầng đã chuyển sang `platforms.detection` (registry-driven).
Class `PlatformDetector` chỉ còn là facade uỷ quyền để giữ nguyên mọi điểm gọi
cũ (`PlatformDetector.detect_platform`, `detect_platform_info`, `_normalize`).
"""

from .detection import detect_platform_info, detect_platform  # noqa: F401
from ..utils.urlnorm import parse_normalized


class PlatformDetector:
    """Facade tĩnh uỷ quyền sang detection.py — KHÔNG chứa logic mới."""

    # Trả (parsed, origin, clean_base_url) — hợp nhất với utils.urlnorm
    _normalize = staticmethod(parse_normalized)
    detect_platform_info = staticmethod(detect_platform_info)
    detect_platform = staticmethod(detect_platform)
