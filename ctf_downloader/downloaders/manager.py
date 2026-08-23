import os
import requests
from typing import List, Dict, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, DownloadColumn, TransferSpeedColumn, TimeRemainingColumn

from .http_downloader import HttpDownloader
from .gdrive import GDriveDownloader
from .dropbox import DropboxDownloader
from .mediafire import MediafireDownloader
from ..extractors.link_extractor import ExtractedLink
from ..utils.logger import console, Logger

class DownloadManager:
    def __init__(self, session: requests.Session, timeout: int = 30, force: bool = False):
        self.session = session
        self.timeout = timeout
        self.force = force

    def download_url(
        self,
        url: str,
        dest_dir: str,
        link_type: str = "direct_file",
        preferred_name: Optional[str] = None
    ) -> Tuple[bool, Optional[str], str]:
        """
        Downloads a single URL based on its link_type.
        Returns (success, saved_file_path, message)
        """
        try:
            # 1. Google Drive
            if link_type == "gdrive":
                resp, fname = GDriveDownloader.get_download_stream(url, session=self.session, timeout=self.timeout)
                if resp and fname:
                    saved_path = HttpDownloader.save_response_stream(
                        resp, dest_dir, preferred_name or fname, force=self.force
                    )
                    if saved_path:
                        return True, saved_path, "Downloaded via Google Drive handler"
                return False, None, "Google Drive download failed or requires manual access permission"

            # 2. Dropbox
            elif link_type == "dropbox":
                resp, fname = DropboxDownloader.get_download_stream(url, session=self.session, timeout=self.timeout)
                if resp and fname:
                    saved_path = HttpDownloader.save_response_stream(
                        resp, dest_dir, preferred_name or fname, force=self.force
                    )
                    if saved_path:
                        return True, saved_path, "Downloaded via Dropbox handler"
                return False, None, "Dropbox direct download failed"

            # 3. Mediafire
            elif link_type == "mediafire":
                resp, fname = MediafireDownloader.get_download_stream(url, session=self.session, timeout=self.timeout)
                if resp and fname:
                    saved_path = HttpDownloader.save_response_stream(
                        resp, dest_dir, preferred_name or fname, force=self.force
                    )
                    if saved_path:
                        return True, saved_path, "Downloaded via Mediafire handler"
                return False, None, "Mediafire direct download failed"

            # 4. Direct / GitHub / Discord / Standard HTTP
            else:
                saved_path = HttpDownloader.download_file(
                    url, dest_dir, self.session,
                    preferred_filename=preferred_name,
                    timeout=self.timeout,
                    force=self.force
                )
                if saved_path:
                    return True, saved_path, "Direct download successful"
                return False, None, "Failed to download file (HTTP status or connection error)"

        except Exception as e:
            return False, None, f"Exception during download: {str(e)}"

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
