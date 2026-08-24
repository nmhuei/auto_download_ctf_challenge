import re
import requests
from bs4 import BeautifulSoup
from typing import Tuple, Optional
from ..utils.logger import Logger
from ..utils.sanitize import extract_filename_from_headers, extract_filename_from_url

MEDIAFIRE_API_INFO_URL = "https://www.mediafire.com/api/1.4/file/get_info.php"

_UNIT_MULTIPLIERS = {
    "b": 1,
    "kb": 1024,
    "mb": 1024 ** 2,
    "gb": 1024 ** 3,
    "tb": 1024 ** 4,
}


class MediafireDownloader:
    @staticmethod
    def extract_quick_key(url: str) -> Optional[str]:
        """
        Trích quick_key của file từ các dạng link MediaFire:
        - https://www.mediafire.com/file/<key>/<name>/file
        - https://www.mediafire.com/download/<key>/<name>
        - https://www.mediafire.com/?<key> (kiểu cũ)
        """
        match = re.search(r'mediafire\.com/(?:file|download)/([a-zA-Z0-9]+)', url)
        if match:
            return match.group(1)
        match = re.search(r'mediafire\.com/\?([a-zA-Z0-9]+)', url)
        if match:
            return match.group(1)
        return None

    @staticmethod
    def get_expected_size(
        url: str,
        session: requests.Session,
        timeout: int = 30
    ) -> Optional[int]:
        """
        Pre-flight dung lượng file MediaFire (bytes) hoặc None nếu không xác định được.
        Ưu tiên API công khai get_info.php (field `size`, đơn vị bytes), fallback scrape
        dòng "File size:" trên trang download.
        """
        quick_key = MediafireDownloader.extract_quick_key(url)
        if quick_key:
            try:
                resp = session.get(
                    MEDIAFIRE_API_INFO_URL,
                    params={"quick_key": quick_key, "response_format": "json"},
                    timeout=timeout
                )
                if resp.status_code == 200:
                    data = resp.json()
                    size_raw = (data.get("response") or {}).get("file_info", {}).get("size")
                    if size_raw is not None:
                        return int(size_raw)
                else:
                    Logger.warning(f"MediaFire API get_info trả HTTP {resp.status_code}, fallback scrape trang.")
            except (ValueError, TypeError, AttributeError) as e:
                Logger.warning(f"MediaFire API get_info lỗi phản hồi: {type(e).__name__}: {str(e)[:200]}; fallback scrape trang.")
            except requests.RequestException as e:
                Logger.warning(f"MediaFire API get_info lỗi kết nối: {type(e).__name__}: {str(e)[:200]}; fallback scrape trang.")

        # Fallback scrape: tìm "File size:\s*<số>\s*<đơn vị>" trong HTML trang
        try:
            page_resp = session.get(url, timeout=timeout)
            if page_resp.status_code == 200:
                size_match = re.search(
                    r'File\s*size:?.{0,120}?([\d.,]+)\s*(KB|MB|GB|TB|B)\b',
                    page_resp.text,
                    re.IGNORECASE
                )
                if size_match:
                    number = float(size_match.group(1).replace(",", ""))
                    unit = size_match.group(2).lower()
                    return int(number * _UNIT_MULTIPLIERS.get(unit, 1))
        except requests.RequestException as e:
            Logger.warning(f"Lỗi khi scrape trang MediaFire để đo dung lượng: {type(e).__name__}: {str(e)[:200]}")

        return None

    @staticmethod
    def get_download_stream(
        url: str,
        session: Optional[requests.Session] = None,
        timeout: int = 30
    ) -> Tuple[Optional[requests.Response], Optional[int]]:
        """
        Scrapes direct download link from a Mediafire file page and returns the stream response.
        Returns (stream, expected_size) — expected_size lấy từ API/scrape pre-flight.
        """
        if session is None:
            session = requests.Session()

        # Pre-flight size (API + fallback scrape) — dùng cho consent gate
        expected_size = MediafireDownloader.get_expected_size(url, session, timeout)

        try:
            # 1. Fetch the mediafire page
            page_resp = session.get(url, timeout=timeout)
            if page_resp.status_code != 200:
                Logger.warning(f"MediaFire trả HTTP {page_resp.status_code} cho trang {url}.")
                return None, expected_size

            soup = BeautifulSoup(page_resp.text, "html.parser")
            download_btn = soup.find("a", {"id": "downloadButton"}) or soup.find("a", {"aria-label": re.compile(r"Download", re.I)})

            direct_url = None
            if download_btn and download_btn.get("href"):
                direct_url = download_btn["href"]
            else:
                # Regex match for direct link in JS/HTML
                match = re.search(r'https?://download[0-9]+\.mediafire\.com/[^\s"\'<>]+', page_resp.text)
                if match:
                    direct_url = match.group(0)

            if direct_url:
                resp = session.get(direct_url, stream=True, timeout=timeout)
                if resp.status_code == 200:
                    filename = extract_filename_from_headers(resp.headers, fallback_url=direct_url)
                    Logger.info(f"MediaFire: đã lấy direct link cho '{filename}'.")
                    return resp, expected_size
                Logger.warning(f"MediaFire direct link trả HTTP {resp.status_code}.")
            else:
                Logger.warning(f"Không tìm thấy nút download trên trang MediaFire: {url}")
        except requests.RequestException as e:
            Logger.warning(f"Lỗi kết nối tới MediaFire: {type(e).__name__}: {str(e)[:200]}")

        return None, expected_size
