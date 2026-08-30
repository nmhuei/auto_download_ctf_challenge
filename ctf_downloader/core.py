from typing import Any, Dict

from .config import DownloaderConfig
from .services.pull_service import PullService
from .services.session_factory import create_session
from .downloaders.manager import DownloadManager


class CTFDownloader:
    """Facade mỏng — giữ constructor + method công khai cũ, delegate vào
    ``services.pull_service.PullService``."""

    def __init__(self, config: DownloaderConfig):
        self.config = config
        self.config.validate()
        # Session master (main thread): chỉ dùng cho detect/authenticate.
        # Các worker thread nhận bản sao riêng qua thread_local_sessions.
        self.session = create_session(
            cookie=config.cookie,
            token=config.token,
            custom_headers=config.custom_headers,
            timeout=config.timeout,
            base_url=config.url,
        )
        self.download_manager = DownloadManager(
            session=self.session,
            timeout=config.timeout,
            force=config.force_redownload,
            size_limit_bytes=config.size_limit_bytes,
            verify_mode=config.verify_downloads,
            allow_private_redirects=config.allow_private_redirects,
        )

    @property
    def output_dir(self):
        """Output dir hiện hành của config (được PullService cập nhật)."""
        return self.config.output_dir

    def run(self) -> bool:
        result = PullService.run(self.config, session=self.session)
        return bool(result.get("ok"))
