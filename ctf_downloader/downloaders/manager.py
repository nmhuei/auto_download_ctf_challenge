import os
import sys
import requests
from typing import List, Dict, Optional, Tuple

from .http_downloader import HttpDownloader, DownloadFailed, LargeFileSkipped
# Các import handler dưới đây giữ để kích hoạt @register_downloader khi nạp
# module (GDriveDownloader còn được so khớp trực tiếp cho fallback_name).
from .gdrive import GDriveDownloader
from .dropbox import DropboxDownloader  # noqa: F401 — side-effect đăng ký
from .mediafire import MediafireDownloader  # noqa: F401 — side-effect đăng ký
from .mega import MegaDownloader, MEGA_MISSING_TOOL_MESSAGE
from .registry import DOWNLOADERS
from ..extractors.link_extractor import ExtractedLink
from ..utils.logger import console, Logger
from ..utils.sanitize import extract_filename_from_url

# Mặc định 1 GB; 0 = tắt gate (không bao giờ hỏi)
DEFAULT_SIZE_LIMIT_BYTES = 1073741824


def human_size(num_bytes: Optional[float]) -> str:
    """Định dạng dung lượng cho người dùng đọc (VD: 1.5GB)."""
    if num_bytes is None:
        return "không rõ"
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f}{unit}" if unit != "B" else f"{int(size)}B"
        size /= 1024
    return f"{size:.1f}TB"


class DownloadManager:
    def __init__(
        self,
        session: requests.Session,
        timeout: int = 30,
        force: bool = False,
        size_limit_bytes: int = DEFAULT_SIZE_LIMIT_BYTES
    ):
        self.session = session
        self.timeout = timeout
        self.force = force
        # Ngưỡng consent file lớn; 0 = vô hiệu hoá gate
        self.size_limit_bytes = size_limit_bytes or 0

    # ------------------------------------------------------------------ #
    # Consent gate cho file lớn
    # ------------------------------------------------------------------ #
    def _confirm_large_download(self, url: str, expected_size: Optional[int]) -> bool:
        """
        Trả True nếu được phép tải tiếp.
        - Gate tắt (size_limit_bytes == 0) hoặc kích thước unknown/nhỏ hơn ngưỡng -> True.
        - Vượt ngưỡng: hỏi user qua input() nếu stdin là tty; nếu không phải tty
          thì tự skip kèm log cảnh báo.
        """
        limit = self.size_limit_bytes
        if not limit or expected_size is None or expected_size <= limit:
            return True

        pretty_size = human_size(expected_size)
        pretty_limit = human_size(limit)

        stdin = sys.stdin
        is_tty = False
        try:
            is_tty = bool(stdin and stdin.isatty())
        except Exception:
            is_tty = False

        if not is_tty:
            Logger.warning(
                f"Bỏ qua {url}: dung lượng {pretty_size} vượt quá giới hạn {pretty_limit} "
                f"(chạy non-interactive nên không thể hỏi consent)."
            )
            return False

        try:
            answer = input(
                f"File '{os.path.basename(url)}' ({pretty_size}) vượt quá giới hạn {pretty_limit}, tải? [y/N] "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        return answer in ("y", "yes")

    @staticmethod
    def _skip_large_message(url: str, size: Optional[int], limit: int) -> str:
        size_str = human_size(size) + " " if size is not None else ""
        return (
            f"skipped_large_file: {url} — dung lượng {size_str}vượt quá giới hạn "
            f"{human_size(limit)}, người dùng không đồng ý tải."
        )

    def _post_consent_gate(self, expected_size: Optional[int]) -> int:
        """Ngưỡng ``max_size`` truyền xuống downloader SAU khi consent pass.

        C9-05: consent chỉ thực sự được hỏi khi kích thước đã biết và vượt
        ngưỡng — trường hợp đó phải NỚI hard gate (0), không thì gate cứng
        trong downloader raise LargeFileSkipped ngay sau khi user đã đồng ý,
        làm consent vô nghĩa. Các trường hợp còn lại (unknown size, nhỏ hơn
        ngưỡng, gate tắt) giữ nguyên ngưỡng để vẫn cắt stream vượt giới hạn
        giữa chừng (probe HEAD có thể đã thất bại)."""
        limit = self.size_limit_bytes
        if expected_size is not None and limit and expected_size > limit:
            return 0
        return limit

    @staticmethod
    def _close_quietly(stream) -> None:
        try:
            if stream is not None:
                stream.close()
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Single URL download
    # ------------------------------------------------------------------ #
    def download_url(
        self,
        url: str,
        dest_dir: str,
        link_type: str = "direct_file",
        preferred_name: Optional[str] = None
    ) -> Tuple[bool, Optional[str], str]:
        """
        Downloads a single URL based on its link_type.
        Returns (success, saved_file_path_or_None, message).

        Message 'skipped_large_file: ...' đánh dấu file bị skip bởi consent gate.
        """
        try:
            handler = DOWNLOADERS.get(link_type)

            # 0. Mega: shell-out sang megatools — manager tự kiểm tra tool,
            #    không có thì bỏ qua với message hướng dẫn cài đặt
            if handler is MegaDownloader:
                if MegaDownloader.available_tool() is None:
                    Logger.warning(MEGA_MISSING_TOOL_MESSAGE)
                    return False, None, MEGA_MISSING_TOOL_MESSAGE
                saved_path, msg = MegaDownloader.download(url, dest_dir, timeout=max(self.timeout * 10, 600))
                return (saved_path is not None), saved_path, msg

            stream = None
            expected_size: Optional[int] = None
            fallback_name: Optional[str] = None

            # 1-3. Handler trả stream (Google Drive / Dropbox / Mediafire) —
            #      mỗi handler tự pre-flight dung lượng của mình.
            #      Dispatch qua flag `streams = True` khai báo tường minh trên
            #      class handler (không duck-typing hasattr(method)).
            if handler is not None and getattr(handler, "streams", False):
                stream, expected_size = handler.get_download_stream(url, session=self.session, timeout=self.timeout)
                if handler is GDriveDownloader:
                    file_id = GDriveDownloader.extract_file_id(url)
                    if file_id:
                        fallback_name = f"gdrive_{file_id}.bin"

                # Các nhánh trả stream: gate consent rồi lưu qua save_response_stream
                if stream is None:
                    return False, None, f"Tải thất bại qua handler {link_type} (không lấy được stream tải trực tiếp)."

                if not self._confirm_large_download(url, expected_size):
                    self._close_quietly(stream)
                    return False, None, self._skip_large_message(url, expected_size, self.size_limit_bytes)

                # C9-05: user đã đồng ý -> nới hard gate (nếu consent được hỏi)
                hard_gate = self._post_consent_gate(expected_size)

                filename = preferred_name or fallback_name or extract_filename_from_url(url)
                saved_path = HttpDownloader.save_response_stream(
                    stream, dest_dir, filename,
                    force=self.force,
                    max_size=hard_gate
                )
                if saved_path:
                    return True, saved_path, f"Đã tải qua handler {link_type}"
                return False, None, f"Tải thất bại qua handler {link_type} (lỗi khi ghi dữ liệu)."

            # 4. Default: Direct / GitHub / GitLab / Discord / catbox / 0x0 / HTTP thuần
            expected_size = HttpDownloader.probe_content_length(url, session=self.session, timeout=self.timeout)
            if not self._confirm_large_download(url, expected_size):
                return False, None, self._skip_large_message(url, expected_size, self.size_limit_bytes)

            saved_path = HttpDownloader.download_file(
                url, dest_dir, self.session,
                preferred_filename=preferred_name,
                timeout=self.timeout,
                force=self.force,
                max_size=self._post_consent_gate(expected_size)
            )
            if saved_path:
                return True, saved_path, "Đã tải trực tiếp thành công"
            return False, None, "Tải file thất bại (HTTP status, lỗi kết nối hoặc nội dung HTML)"

        except LargeFileSkipped as e:
            # Unknown-size: vượt ngưỡng phát hiện trong lúc stream -> đã ngắt sớm & dọn tmp
            return False, None, self._skip_large_message(url, e.size, self.size_limit_bytes)
        except DownloadFailed as e:
            return False, None, str(e)
        except Exception as e:
            Logger.warning(f"Ngoại lệ khi tải {url}: {type(e).__name__}: {str(e)[:200]}")
            return False, None, f"Ngoại lệ khi tải: {type(e).__name__}: {str(e)[:200]}"

    # ------------------------------------------------------------------ #
    # Per-challenge orchestration
    # ------------------------------------------------------------------ #
    def download_challenge_files(
        self,
        files: List[Tuple[str, str]], # (url, preferred_name)
        extracted_links: List[ExtractedLink],
        dest_dir: str,
        download_third_party: bool = True
    ) -> List[Dict[str, any]]:
        """
        Downloads all platform files and 3rd party files for a single challenge.
        """
        results = []
        download_targets = []
        seen_urls = set()

        # Add explicit platform files
        for url, name in files:
            if url not in seen_urls:
                seen_urls.add(url)
                download_targets.append({
                    "url": url,
                    "name": name,
                    "type": "direct_file",
                    "source": "platform_attachment"
                })

        # Add 3rd party downloadable links if enabled
        if download_third_party:
            for link in extracted_links:
                if link.is_downloadable and link.url not in seen_urls:
                    seen_urls.add(link.url)
                    download_targets.append({
                        "url": link.url,
                        "name": link.filename_hint,
                        "type": link.link_type,
                        "source": f"description_{link.link_type}"
                    })

        # Execute downloads
        for item in download_targets:
            success, path, msg = self.download_url(
                url=item["url"],
                dest_dir=dest_dir,
                link_type=item["type"],
                preferred_name=item["name"]
            )
            results.append({
                "url": item["url"],
                "name": os.path.basename(path) if path else item["name"],
                "saved_path": path,
                "success": success,
                "source": item["source"],
                "message": msg
            })

        return results
