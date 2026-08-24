"""Registry các downloader theo link_type.

Thêm service tải mới: viết class handler (có `get_download_stream` kiểu
(stream, expected_size) hoặc `download` kiểu mega) rồi trang trí
`@register_downloader(...)`. DownloadManager tự tra bảng DOWNLOADERS lúc tải —
không cần sửa if/elif trong manager.
"""

from typing import Callable, Dict, Optional, Tuple, Type

from ..utils.logger import Logger

# Bảng dispatch: link_type -> class handler
DOWNLOADERS: Dict[str, Type] = {}


def register_downloader(
    link_type: str,
    *,
    domains: Tuple[str, ...] = (),
    extensions: Tuple[str, ...] = ()
) -> Callable[[Type], Type]:
    """
    Decorator đăng ký class downloader cho `link_type`.
    - domains/extensions là metadata định tuyến (tra cứu/tài liệu); việc phân loại
      URL thành link_type hiện vẫn thuộc LinkExtractor.
    """
    def decorator(cls: Type) -> Type:
        if link_type in DOWNLOADERS:
            # Ghi đè im lặng dễ giấu bug (hai module tranh cùng link_type):
            # cảnh báo để người thêm downloader mới nhận ra ngay.
            Logger.warning(
                f"register_downloader: link_type '{link_type}' đã được đăng ký "
                f"bởi {DOWNLOADERS[link_type].__name__} — bị ghi đè bởi "
                f"{cls.__name__}."
            )
        DOWNLOADERS[link_type] = cls
        cls.link_type = link_type
        cls.domains = domains
        cls.extensions = extensions
        return cls
    return decorator


def get_downloader(link_type: str) -> Optional[Type]:
    """Tra handler đã đăng ký; None nếu không có (manager dùng default HTTP)."""
    return DOWNLOADERS.get(link_type)
