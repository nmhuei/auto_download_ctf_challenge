import os
import shutil
import requests
from typing import Optional, Callable
from ..utils.sanitize import sanitize_filename, extract_filename_from_headers, extract_filename_from_url

class HttpDownloader:
    @staticmethod
    def download_file(
        url: str,
        dest_dir: str,
        session: requests.Session,
        preferred_filename: Optional[str] = None,
        timeout: int = 30,
        force: bool = False,
        progress_callback: Optional[Callable[[int, int], None]] = None
    ) -> Optional[str]:
        """
        Downloads a file from URL to dest_dir.
        Returns the absolute path of the saved file or None on error.
        """
        try:
            os.makedirs(dest_dir, exist_ok=True)
            
            with session.get(url, stream=True, timeout=timeout, allow_redirects=True) as resp:
                if resp.status_code != 200:
                    return None

                # Determine filename
                if preferred_filename:
                    filename = sanitize_filename(preferred_filename)
                else:
                    filename = extract_filename_from_headers(resp.headers, fallback_url=url)
                    
                target_path = os.path.join(dest_dir, filename)
                
                # Check if file already exists with same size
                total_size = int(resp.headers.get('content-length', 0))
                if not force and os.path.exists(target_path) and total_size > 0:
                    if os.path.getsize(target_path) == total_size:
                        return target_path

                temp_path = target_path + ".tmp"
                downloaded_bytes = 0
                
                with open(temp_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=65536):
                        if chunk:
                            f.write(chunk)
                            downloaded_bytes += len(chunk)
                            if progress_callback:
                                progress_callback(len(chunk), total_size)

                # Move temp to final
                shutil.move(temp_path, target_path)
                return target_path
        except Exception:
            return None

    @staticmethod
    def save_response_stream(
        resp: requests.Response,
        dest_dir: str,
        filename: str,
        force: bool = False,
        progress_callback: Optional[Callable[[int, int], None]] = None
    ) -> Optional[str]:
        """
        Saves an existing streaming response object to dest_dir/filename.
        """
        try:
            os.makedirs(dest_dir, exist_ok=True)
            filename = sanitize_filename(filename)
            target_path = os.path.join(dest_dir, filename)
            
            total_size = int(resp.headers.get('content-length', 0))
            if not force and os.path.exists(target_path) and total_size > 0:
                if os.path.getsize(target_path) == total_size:
                    return target_path

            temp_path = target_path + ".tmp"
            with open(temp_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
                        if progress_callback:
                            progress_callback(len(chunk), total_size)

            shutil.move(temp_path, target_path)
            return target_path
        except Exception:
            return None
