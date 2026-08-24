from dataclasses import dataclass, field
from typing import Optional, List, Dict
import os
import urllib.parse

from .utils.urlnorm import normalize_base_url

@dataclass
class DownloaderConfig:
    url: str
    cookie: Optional[str] = None
    token: Optional[str] = None
    output_dir: Optional[str] = None
    download_third_party: bool = True
    threads: int = 4
    timeout: int = 30
    create_solve_template: bool = True
    force_redownload: bool = False
    # Pull tăng dần: --update chỉ xử lý challenge mới + cập nhật metadata bài cũ;
    # --refresh-meta như --update nhưng cho phép tải lại attachment thiếu trên đĩa.
    incremental_update: bool = False
    refresh_meta: bool = False
    # Ngưỡng dung lượng file tối đa (bytes) trước khi hỏi consent người dùng.
    # 0 = tắt gate (không bao giờ hỏi, tải mọi kích thước).
    size_limit_bytes: int = 1073741824  # 1 GB
    categories: Optional[List[str]] = None
    exclude_categories: Optional[List[str]] = None
    custom_headers: Dict[str, str] = field(default_factory=dict)
    
    def validate(self):
        if not self.url:
            raise ValueError("CTF platform URL is required.")
        
        # Remove hash fragment
        self.url = self.url.split("#")[0].strip()
        
        # Ensure url starts with http:// or https://
        if not self.url.startswith("http://") and not self.url.startswith("https://"):
            self.url = "https://" + self.url
        self.url = self.url.rstrip("/")

        # Extract token from query params if present in URL
        parsed = urllib.parse.urlparse(self.url)
        if not self.token and parsed.query:
            qs = urllib.parse.parse_qs(parsed.query)
            if "token" in qs and qs["token"]:
                self.token = qs["token"][0]

        # Strip common trailing page suffixes (like /challenges, /scoreboard, /login)
        self.url = normalize_base_url(self.url)
        
        # Expand user path if provided
        if self.output_dir:
            self.output_dir = os.path.abspath(os.path.expanduser(self.output_dir))



