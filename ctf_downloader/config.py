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
    # Revalidation file đã có: fast=presence; normal=ETag/Last-Modified/size;
    # strict=normal + SHA-256 local so với baseline persisted.
    verify_downloads: str = "fast"
    # Private/loopback URL ban đầu luôn hợp lệ cho CTF lab; chỉ public→private
    # redirect cần opt-in rõ ràng.
    allow_private_redirects: bool = False
    # Pull tăng dần: --update chỉ xử lý challenge mới + cập nhật metadata bài cũ;
    # --refresh-meta như --update nhưng cho phép tải lại attachment thiếu trên đĩa.
    incremental_update: bool = False
    refresh_meta: bool = False
    # Git lifecycle: CLI pull bật mặc định; library callers giữ opt-in để
    # không tự ý đổi branch khi dùng PullService trực tiếp.
    git_workflow: bool = False
    git_base_branch: str = "main"
    git_remote: str = "origin"
    git_auto_push: bool = True
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

        self.verify_downloads = str(self.verify_downloads or "fast").strip().lower()
        if self.verify_downloads not in ("fast", "normal", "strict"):
            raise ValueError(
                "verify_downloads phải là một trong: fast, normal, strict"
            )
        
        # Expand user path if provided
        if self.output_dir:
            self.output_dir = os.path.abspath(os.path.expanduser(self.output_dir))



