import urllib.parse
from typing import Tuple, Optional
import requests
from ..utils.logger import Logger
from ..utils.sanitize import extract_filename_from_headers, extract_filename_from_url
from .http_downloader import HttpDownloader, DownloadFailed
from .registry import register_downloader


@register_downloader("dropbox", domains=("dropbox.com",))
class DropboxDownloader:
    # Flag dispatch tường minh: handler trả stream qua get_download_stream()
    streams = True

    @staticmethod
    def get_direct_url(url: str) -> str:
        """
        Converts a Dropbox preview URL to a direct download URL.
        """
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        query["dl"] = ["1"]
        new_query = urllib.parse.urlencode(query, doseq=True)
        return urllib.parse.urlunparse((
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            new_query,
            parsed.fragment
        ))

    @staticmethod
    def get_expected_size(
        url: str,
        session: requests.Session,
        timeout: int = 30
    ) -> Optional[int]:
        """
        Pre-flight dung lượng file Dropbox (bytes) bằng HEAD trên URL ?dl=1.
        Trả về None nếu không xác định được.
        """
        return HttpDownloader.probe_content_length(DropboxDownloader.get_direct_url(url), session, timeout)

    @staticmethod
    def get_download_stream(
        url: str,
        session: Optional[requests.Session] = None,
        timeout: int = 30
    ) -> Tuple[Optional[requests.Response], Optional[int]]:
        """
        Downloads a Dropbox direct stream.
        Returns (stream, expected_size) — expected_size đọc từ Content-Length của
        response cuối (sau redirect), None nếu server không cho biết.
        """
        if session is None:
            session = requests.Session()

        direct_url = DropboxDownloader.get_direct_url(url)
        try:
            resp = session.get(direct_url, stream=True, allow_redirects=True, timeout=timeout)

            if resp.status_code == 429:
                msg = "Dropbox bandwidth cap (HTTP 429): tài khoản/link đã vượt hạn chế băng thông tạm thời, thử lại sau."
                Logger.error(msg)
                raise DownloadFailed(msg)

            if resp.status_code != 200:
                Logger.warning(f"Dropbox trả HTTP {resp.status_code} cho {url}.")
                return None, None

            content_type = (resp.headers.get("Content-Type") or "").lower()
            if "text/html" in content_type:
                Logger.warning(f"Dropbox trả về trang HTML thay vì file (interstitial/preview) cho {url}.")
                return None, None

            filename = extract_filename_from_headers(resp.headers, fallback_url=url)
            expected_size = None
            raw_cl = resp.headers.get("Content-Length")
            if raw_cl and str(raw_cl).strip().isdigit():
                expected_size = int(raw_cl)
            Logger.info(f"Dropbox: đã lấy stream cho '{filename}'.")
            return resp, expected_size
        except DownloadFailed:
            raise
        except requests.RequestException as e:
            Logger.warning(f"Lỗi kết nối tới Dropbox: {type(e).__name__}: {str(e)[:200]}")
        return None, None
