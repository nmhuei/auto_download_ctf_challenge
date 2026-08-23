import re
import requests
from bs4 import BeautifulSoup
from typing import Tuple, Optional
from ..utils.sanitize import extract_filename_from_headers, extract_filename_from_url

class MediafireDownloader:
    @staticmethod
    def get_download_stream(
        url: str,
        session: Optional[requests.Session] = None,
        timeout: int = 30
    ) -> Tuple[Optional[requests.Response], str]:
        """
        Scrapes direct download link from a Mediafire file page and returns the stream response.
        """
        if session is None:
            session = requests.Session()

        try:
            # 1. Fetch the mediafire page
            page_resp = session.get(url, timeout=timeout)
            if page_resp.status_code != 200:
                return None, ""
                
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
                    return resp, filename
        except Exception:
            pass

        return None, ""
