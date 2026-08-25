import re
import requests
from typing import Optional, Tuple
from ..utils.logger import Logger
from ..utils.sanitize import extract_filename_from_headers
from .http_downloader import DownloadFailed
from .registry import register_downloader

# Các dấu hiệu HTML của Google Drive khi hết hạn mức tải / thiếu quyền
_QUOTA_MARKERS = (
    "quota exceeded",
    "downloadquotaexceeded",
    "too many users have viewed or downloaded",
    "can't download this file",
    "couldn't download this file",
    "permission denied",
)


def _looks_like_quota_html(html_text: str) -> bool:
    lowered = html_text.lower()
    return any(marker in lowered for marker in _QUOTA_MARKERS)


# Các biến thể form confirm token của trang interstitial Google Drive:
# 1) link dạng ?confirm=xxx
# 2) <input name="confirm" value="..."> (thứ tự thuộc tính chuẩn)
# 3) <input value="..." name="confirm"> (thứ tự thuộc tính ĐẢO)
# 4/5) như (2)/(3) nhưng dùng nháy đơn
_CONFIRM_TOKEN_PATTERNS = (
    r'confirm=([0-9A-Za-z_]+)',
    r'name=["\']confirm["\'][^>]*value=["\']([^"\']+)["\']',
    r'value=["\']([^"\']+)["\'][^>]*name=["\']confirm["\']',
)


def _extract_confirm_token(html_text: str) -> Optional[str]:
    """Trích confirm token từ HTML interstitial; trả None nếu không có."""
    for pattern in _CONFIRM_TOKEN_PATTERNS:
        match = re.search(pattern, html_text)
        if match:
            return match.group(1)
    return None


@register_downloader("gdrive", domains=("drive.google.com", "docs.google.com"))
class GDriveDownloader:
    # Flag dispatch tường minh: handler trả stream qua get_download_stream()
    streams = True

    @staticmethod
    def extract_file_id(url: str) -> Optional[str]:
        """
        Extracts Google Drive file ID from various Google Drive URL patterns.
        """
        patterns = [
            r'drive\.google\.com/file/d/([a-zA-Z0-9_-]+)',
            r'drive\.google\.com/open\?id=([a-zA-Z0-9_-]+)',
            r'drive\.google\.com/uc\?(?:[^&]+&)*id=([a-zA-Z0-9_-]+)',
            r'docs\.google\.com/(?:document|spreadsheets|presentation)/d/([a-zA-Z0-9_-]+)',
        ]
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)
        return None

    @staticmethod
    def get_download_stream(
        url: str,
        session: Optional[requests.Session] = None,
        timeout: int = 30
    ) -> Tuple[Optional[requests.Response], Optional[int]]:
        """
        Attempts to obtain a direct download response stream for a Google Drive link,
        handling confirmation tokens for large files.
        Returns (stream, expected_size). Google Drive luôn đi qua trang interstitial
        nên KHÔNG biết trước dung lượng -> expected_size luôn None và việc gate
        kích thước được xử lý kiểu unknown-size ở tầng stream.
        Raises DownloadFailed nếu phát hiện HTML hết quota.
        """
        if session is None:
            session = requests.Session()

        file_id = GDriveDownloader.extract_file_id(url)
        if not file_id:
            Logger.warning(f"Không đọc được file ID từ link Google Drive: {url}")
            return None, None

        def _fail_quota() -> DownloadFailed:
            msg = "Google Drive quota exceeded / cần quyền truy cập: file đã vượt hạn mức tải hoặc không chia sẻ công khai."
            Logger.error(msg)
            return DownloadFailed(msg)

        download_url = f"https://drive.usercontent.google.com/download?id={file_id}&export=download&authuser=0&confirm=t"

        try:
            resp = session.get(download_url, stream=True, timeout=timeout)

            # If Google Drive returns HTML asking for confirmation
            content_type = resp.headers.get("Content-Type", "")
            if "text/html" in content_type:
                # Read response text to search for confirm token or form
                html_text = resp.text

                # QUAN TRỌNG: tìm confirm token TRƯỚC, chỉ khi KHÔNG có token
                # mới xét marker quota. Trang interstitial hợp lệ vẫn có thể
                # chứa cụm "permission denied" trong help-text — nếu check quota
                # trước sẽ chặn nhầm file hoàn toàn tải được (false-positive).
                confirm_token = _extract_confirm_token(html_text)

                if confirm_token:
                    second_url = f"https://drive.usercontent.google.com/download?id={file_id}&export=download&authuser=0&confirm={confirm_token}"
                    resp = session.get(second_url, stream=True, timeout=timeout)
                elif _looks_like_quota_html(html_text):
                    raise _fail_quota()
                else:
                    # HTML interstitial nhưng không tìm thấy confirm token -> thử fallback
                    Logger.warning(f"Google Drive: HTML interstitial không có confirm token cho file {file_id}, thử fallback...")

            # Check if response is successful and has content
            if resp.status_code == 200:
                if "text/html" in (resp.headers.get("Content-Type", "") or "").lower():
                    raise _fail_quota()
                filename = extract_filename_from_headers(resp.headers)
                if not filename or filename == "downloaded_file":
                    filename = f"gdrive_{file_id}.bin"
                Logger.info(f"Google Drive: chuẩn bị tải '{filename}' (dung lượng unknown do interstitial).")
                return resp, None

            Logger.warning(f"Google Drive trả HTTP {resp.status_code} cho file {file_id}.")
        except DownloadFailed:
            raise
        except requests.RequestException as e:
            Logger.warning(f"Lỗi kết nối tới Google Drive: {type(e).__name__}: {str(e)[:200]}")

        # Fallback to standard uc?id=...
        try:
            std_url = f"https://drive.google.com/uc?export=download&id={file_id}"
            resp = session.get(std_url, stream=True, timeout=timeout)
            if resp.status_code == 200 and "text/html" not in resp.headers.get("Content-Type", ""):
                filename = extract_filename_from_headers(resp.headers)
                if not filename or filename == "downloaded_file":
                    filename = f"gdrive_{file_id}.bin"
                return resp, None
            if resp.status_code == 200 and _looks_like_quota_html(resp.text):
                raise _fail_quota()
            Logger.warning(f"Google Drive fallback trả HTTP {resp.status_code} cho file {file_id}.")
        except DownloadFailed:
            raise
        except requests.RequestException as e:
            Logger.warning(f"Lỗi kết nối tới Google Drive (fallback): {type(e).__name__}: {str(e)[:200]}")

        Logger.warning(f"Không lấy được stream tải trực tiếp từ Google Drive cho file {file_id}.")
        return None, None
