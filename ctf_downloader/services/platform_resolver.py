"""
PlatformResolver — dựng platform adapter cho một workspace đã download.

Hợp nhất logic cũ của InstanceManager._init_platform (if/elif + hardcode
'infosecptit' + gọi nhầm PlatformDetector.detect_and_init không tồn tại):

  1. Lấy URL nền tảng từ repo (`resolve_platform_url()`).
  2. Nếu challenges.json khai báo `ctf_info.platform` trùng key trong registry
     -> dựng thẳng adapter từ registry (KHÔNG gọi mạng), gán game_id đã lưu.
  3. Ngược lại -> chạy pipeline recon 4 tầng (`detection.detect_platform_info`)
     với session đã gắn auth.

Trả về `(session, platform, info)` để caller tái dùng session/info.
"""

from typing import Optional, Tuple

from ..platforms.base import BasePlatform
from ..platforms.capabilities import PlatformInfo
from ..platforms.detection import detect_platform_info
from ..platforms.registry import UnknownPlatformError, get_spec
from ..utils.logger import Logger
from .session_factory import create_session

# Giá trị khai báo "chưa biết" -> luôn đi qua pipeline recon
_UNDECLARED = ("", "generic", "unknown", "generic_html")


class PlatformResolver:
    @staticmethod
    def for_workspace(repo, cookie: Optional[str] = None,
                      token: Optional[str] = None) -> Tuple[object, BasePlatform, PlatformInfo]:
        """
        Args:
            repo: object WorkspaceRepo exposing `read_challenges()` và
                  `resolve_platform_url()`.
            cookie / token: auth tùy chọn, gắn vào session tạo mới.
        Returns:
            (session, platform_instance, PlatformInfo)
        Raises:
            ValueError: không suy ra được URL nền tảng từ workspace.
        """
        read_challenges = getattr(repo, "read_challenges", None)
        data = read_challenges() if callable(read_challenges) else {}
        ctf_info = (data or {}).get("ctf_info") or {}

        resolve_url = getattr(repo, "resolve_platform_url", None)
        url = resolve_url() if callable(resolve_url) else None
        if not url:
            raise ValueError(
                "Could not determine CTF platform URL from workspace.")

        session = create_session(cookie=cookie, token=token, base_url=url)

        declared = str(ctf_info.get("platform", "") or "").strip().lower()
        spec = None
        if declared and declared not in _UNDECLARED:
            try:
                spec = get_spec(declared)
            except UnknownPlatformError:
                Logger.warning(
                    f"Workspace khai báo platform không rõ: {declared!r} "
                    f"-> chuyển sang auto-detect.")

        if spec is not None:
            # Khai báo rõ ràng -> dựng trực tiếp, KHÔNG dò mạng
            platform = spec.cls(url, session)
            info = PlatformInfo(
                platform_type=declared,
                base_url=getattr(platform, "base_url", url),
                confidence="high",
                signals=[f"Workspace khai báo platform={declared} -> dựng {spec.label}"],
            )
            game_id = ctf_info.get("game_id")
            if game_id:
                try:
                    platform.game_id = int(game_id)
                    info.game_id = int(game_id)
                except (TypeError, ValueError):
                    pass
            return session, platform, info

        # Không khai báo (hoặc khai báo lạ) -> pipeline recon 4 tầng
        platform, info = detect_platform_info(url, session, cookie_hint=cookie)
        return session, platform, info
