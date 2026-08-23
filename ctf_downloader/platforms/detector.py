import re
import urllib.parse
import requests
from typing import Type
from .base import BasePlatform
from .ctfd import CTFdPlatform
from .rctf import RCTFPlatform
from .gzctf import GZCTFPlatform
from .custom_rest import CustomRESTPlatform
from .generic_html import GenericHTMLPlatform
from ..utils.logger import Logger

class PlatformDetector:
    @staticmethod
    def detect_platform(base_url: str, session: requests.Session) -> BasePlatform:
        """
        Auto-detects the CTF platform (CTFd, rCTF, GZCTF, Custom REST, Generic) by checking cookies, API endpoints and headers.
        """
        base_url = base_url.split("#")[0].rstrip("/")
        parsed = urllib.parse.urlparse(base_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        
        path = parsed.path.rstrip("/")
        for suffix in ["/challenges", "/scoreboard", "/login", "/register", "/users", "/teams", "/rules"]:
            if path.endswith(suffix):
                path = path[:-len(suffix)]
                break
        clean_base_url = f"{origin}{path}".rstrip("/") or origin

        # 1. Check GZCTF Indicators (Cookie, path /games/, or /api/account/profile)
        if "GZCTF_Token" in session.cookies or "/games" in parsed.path:
            try:
                resp = session.get(f"{origin}/api/account/profile", timeout=5)
                if resp.status_code in [200, 401]:
                    Logger.info("Detected Platform: [bold green]GZ::CTF[/bold green]")
                    return GZCTFPlatform(clean_base_url, session)
            except Exception:
                pass
            # Even if profile fails, /games/ is typical for GZCTF
            if "/games" in parsed.path:
                Logger.info("Detected Platform: [bold green]GZ::CTF (via URL)[/bold green]")
                return GZCTFPlatform(clean_base_url, session)

        # 2. Check Custom REST / Next.js CTF platform (/api/challenges, /api/auth/me)
        try:
            resp = session.get(f"{origin}/api/challenges", timeout=5)
            if resp.status_code in [200, 401, 403]:
                try:
                    data = resp.json()
                    if data.get("success") and "challenges" in data.get("data", {}):
                        Logger.info("Detected Platform: [bold green]Custom REST / Next.js CTF[/bold green]")
                        return CustomRESTPlatform(clean_base_url, session)
                except Exception:
                    pass
        except Exception:
            pass

        try:
            resp = session.get(f"{origin}/api/auth/me", timeout=5)
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    if data.get("success") and data.get("data", {}).get("user"):
                        Logger.info("Detected Platform: [bold green]Custom REST / Next.js CTF[/bold green]")
                        return CustomRESTPlatform(clean_base_url, session)
                except Exception:
                    pass
        except Exception:
            pass

        # 3. Test CTFd API endpoint
        try:
            resp = session.get(f"{origin}/api/v1/challenges", timeout=5)
            if resp.status_code in [200, 401, 403]:
                try:
                    data = resp.json()
                    if "success" in data:
                        Logger.info("Detected Platform: [bold green]CTFd[/bold green]")
                        return CTFdPlatform(clean_base_url, session)
                except Exception:
                    pass
        except Exception:
            pass

        # 4. Test rCTF API endpoint

        try:
            resp = session.get(f"{origin}/api/v1/challs", timeout=5)
            if resp.status_code in [200, 401, 403]:
                try:
                    data = resp.json()
                    if "kind" in data and ("goodChallenges" in data.get("kind") or "bad" in data.get("kind") or "unauth" in data.get("kind")):
                        Logger.info("Detected Platform: [bold green]rCTF[/bold green]")
                        return RCTFPlatform(clean_base_url, session)
                except Exception:
                    pass
        except Exception:
            pass

        # 4. Check HTML content of root or /challenges
        try:
            resp = session.get(clean_base_url, timeout=5)
            if resp.status_code == 200:
                html = resp.text.lower()
                if "gzctf" in html or "gz::ctf" in html:
                    Logger.info("Detected Platform: [bold green]GZ::CTF (via HTML)[/bold green]")
                    return GZCTFPlatform(clean_base_url, session)
                elif "ctfd" in html or "csrfnonce" in html:
                    Logger.info("Detected Platform: [bold green]CTFd (via HTML)[/bold green]")
                    return CTFdPlatform(clean_base_url, session)
                elif "rctf" in html or "redpwn" in html:
                    Logger.info("Detected Platform: [bold green]rCTF (via HTML)[/bold green]")
                    return RCTFPlatform(clean_base_url, session)
        except Exception:
            pass

        Logger.warning("Could not definitively identify platform. Falling back to CTFd platform.")
        return CTFdPlatform(clean_base_url, session)

