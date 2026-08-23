import urllib.parse
from typing import Tuple, Optional
import requests
from ..utils.sanitize import extract_filename_from_headers, extract_filename_from_url

class DropboxDownloader:
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
    def get_download_stream(
        url: str,
        session: Optional[requests.Session] = None,
        timeout: int = 30
    ) -> Tuple[Optional[requests.Response], str]:
        """
        Downloads a Dropbox direct stream.
        """
        if session is None:
            session = requests.Session()
            
        direct_url = DropboxDownloader.get_direct_url(url)
        try:
            resp = session.get(direct_url, stream=True, allow_redirects=True, timeout=timeout)
            if resp.status_code == 200:
                filename = extract_filename_from_headers(resp.headers, fallback_url=url)
                return resp, filename
        except Exception:
            pass
        return None, ""
