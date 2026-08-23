import re
import requests
from typing import Optional, Tuple
from ..utils.sanitize import extract_filename_from_headers, sanitize_filename

class GDriveDownloader:
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
    ) -> Tuple[Optional[requests.Response], str]:
        """
        Attempts to obtain a direct download response stream for a Google Drive link,
        handling confirmation tokens for large files.
        Returns (response, filename).
        """
        if session is None:
            session = requests.Session()

        file_id = GDriveDownloader.extract_file_id(url)
        if not file_id:
            return None, ""

        download_url = f"https://drive.usercontent.google.com/download?id={file_id}&export=download&authuser=0&confirm=t"
        
        try:
            resp = session.get(download_url, stream=True, timeout=timeout)
            
            # If Google Drive returns HTML asking for confirmation
            content_type = resp.headers.get("Content-Type", "")
            if "text/html" in content_type:
                # Read response text to search for confirm token or form
                html_text = resp.text
                confirm_token = None
                
                # Check for confirm=xxx in link or form
                token_match = re.search(r'confirm=([0-9A-Za-z_]+)', html_text)
                if token_match:
                    confirm_token = token_match.group(1)
                else:
                    # Look for input name="confirm" value="..."
                    form_match = re.search(r'name="confirm"\s+value="([^"]+)"', html_text)
                    if form_match:
                        confirm_token = form_match.group(1)
                
                if confirm_token:
                    second_url = f"https://drive.usercontent.google.com/download?id={file_id}&export=download&authuser=0&confirm={confirm_token}"
                    resp = session.get(second_url, stream=True, timeout=timeout)

            # Check if response is successful and has content
            if resp.status_code == 200:
                filename = extract_filename_from_headers(resp.headers)
                if not filename or filename == "downloaded_file":
                    filename = f"gdrive_{file_id}.bin"
                return resp, filename
        except Exception:
            pass

        # Fallback to standard uc?id=...
        try:
            std_url = f"https://drive.google.com/uc?export=download&id={file_id}"
            resp = session.get(std_url, stream=True, timeout=timeout)
            if resp.status_code == 200 and "text/html" not in resp.headers.get("Content-Type", ""):
                filename = extract_filename_from_headers(resp.headers)
                if not filename or filename == "downloaded_file":
                    filename = f"gdrive_{file_id}.bin"
                return resp, filename
        except Exception:
            pass

        return None, ""
