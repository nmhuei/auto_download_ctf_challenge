from .config import DownloaderConfig
from .core import CTFDownloader
from .platforms.base import Challenge, CTFInfo

__version__ = "1.0.0"
__all__ = ["DownloaderConfig", "CTFDownloader", "Challenge", "CTFInfo"]
