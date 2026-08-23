import re
import urllib.parse
import requests
from typing import List, Dict, Any, Optional, Tuple
from bs4 import BeautifulSoup
from .base import BasePlatform, Challenge, CTFInfo
from ..utils.logger import Logger
from ..utils.sanitize import sanitize_filename

class CustomRESTPlatform(BasePlatform):
    """
    Integration for modern Next.js / Node / REST CTF platforms (such as TamilCTF / CTF-Platform).
    Endpoints:
      - /api/auth/me
      - /api/challenges
      - /api/challenges/<id>
      - /api/challenges/<id>/submit
    """
    def __init__(self, base_url: str, session: requests.Session):
        super().__init__(base_url, session)
        self.ctf_info.platform_type = "custom_rest"

    def _extract_title(self) -> None:
        try:
            h_resp = self.session.get(self.base_url, timeout=5)
            if h_resp.status_code == 200:
                soup = BeautifulSoup(h_resp.text, "html.parser")
                title_el = soup.find("title")
                if title_el and title_el.text:
                    self.ctf_info.title = title_el.text.strip().split(" - ")[0].split(" | ")[0].strip()
        except Exception:
            pass

        if not self.ctf_info.title or self.ctf_info.title == "CTF Competition":
            domain = urllib.parse.urlparse(self.base_url).netloc
            clean_dom = domain.replace("ctf.", "").replace("www.", "").replace(".org", "").replace(".com", "").replace(".", "_")
            self.ctf_info.title = f"{clean_dom.capitalize()}_CTF"

    def authenticate(self) -> bool:
        """
        Validates authentication via /api/auth/me or checks challenge list.
        """
        self._extract_title()

        # Check /api/auth/me
        try:
            resp = self.session.get(f"{self.base_url}/api/auth/me", timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("success") and data.get("data", {}).get("user"):
                    user_data = data["data"]["user"]
                    username = user_data.get("username") or user_data.get("name") or user_data.get("email")
                    self.ctf_info.user_name = username
                    Logger.success(f"Authenticated as User: [bold cyan]{username}[/bold cyan]")
                    return True
        except Exception:
            pass

        # Check /api/challenges directly
        try:
            resp = self.session.get(f"{self.base_url}/api/challenges", timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("success") and "challenges" in data.get("data", {}):
                    Logger.info("Public challenge access confirmed on REST platform.")
                    return True
        except Exception:
            pass

        Logger.error("Failed to authenticate to REST CTF platform.")
        return False

    def fetch_challenges(self) -> List[Challenge]:
        """
        Fetches all challenges via /api/challenges and detailed info via /api/challenges/{id}.
        """
        try:
            resp = self.session.get(f"{self.base_url}/api/challenges", timeout=15)
            if resp.status_code != 200:
                Logger.error(f"Failed to fetch challenges from /api/challenges (HTTP {resp.status_code})")
                return []

            json_data = resp.json()
            if not json_data.get("success"):
                Logger.error(f"API error: {json_data.get('error') or json_data.get('message')}")
                return []

            raw_challs = json_data.get("data", {}).get("challenges", [])
            Logger.info(f"Found {len(raw_challs)} challenges on platform. Fetching details...")

            detailed_challenges = []
            for item in raw_challs:
                chall_id = item.get("id")
                name = item.get("title") or item.get("name", f"Challenge_{chall_id}")
                category = item.get("category", "Misc").strip().capitalize()
                points = item.get("points") or item.get("maxPoints", 0)
                author = item.get("author")
                description = item.get("description", "")
                tags = item.get("tags", [])
                is_solved = item.get("isSolved", False)
                solves = item.get("solves", 0)
                
                # Fetch detailed view if available
                detail_data = {}
                try:
                    det_resp = self.session.get(f"{self.base_url}/api/challenges/{chall_id}", timeout=10)
                    if det_resp.status_code == 200:
                        det_json = det_resp.json()
                        if det_json.get("success"):
                            detail_data = det_json.get("data", {}).get("challenge", {})
                            description = detail_data.get("description") or description
                except Exception:
                    pass

                # Parse files/attachments
                files_list = []
                for f in detail_data.get("files", []) or item.get("files", []):
                    if isinstance(f, str):
                        files_list.append((self.get_full_file_url(f), f.split("/")[-1]))
                    elif isinstance(f, dict):
                        f_url = f.get("url") or f.get("location")
                        f_name = f.get("name") or (f_url.split("/")[-1] if f_url else "attachment")
                        if f_url:
                            files_list.append((self.get_full_file_url(f_url), f_name))

                hints_list = []
                for h in detail_data.get("hints", []) or item.get("hints", []):
                    if isinstance(h, str):
                        hints_list.append({"content": h})
                    elif isinstance(h, dict):
                        hints_list.append(h)

                chall_obj = Challenge(
                    id=chall_id,
                    name=name,
                    category=category,
                    points=points,
                    description=description,
                    author=author,
                    tags=tags,
                    hints=hints_list,
                    files=files_list,
                    solved_by_me=is_solved,
                    solves_count=solves,
                    raw_data=detail_data or item
                )
                detailed_challenges.append(chall_obj)

            self.ctf_info.challenges = detailed_challenges
            return detailed_challenges

        except Exception as e:
            Logger.error(f"Error fetching REST CTF challenges: {str(e)}")
            return []

    def get_full_file_url(self, file_path: str) -> str:
        if file_path.startswith("http://") or file_path.startswith("https://"):
            return file_path
        return urllib.parse.urljoin(self.base_url, file_path)

    def submit_flag(self, challenge_id: Any, flag: str) -> Tuple[bool, str]:
        """
        Submits flag via /api/challenges/{challenge_id}/submit.
        """
        url = f"{self.base_url}/api/challenges/{challenge_id}/submit"
        payload = {"flag": flag.strip()}

        try:
            resp = self.session.post(url, json=payload, timeout=15)
            data = resp.json() if "json" in resp.headers.get("content-type", "") else {}

            if resp.status_code == 200 and data.get("success"):
                return True, "🎉 Correct flag! Challenge solved!"
            elif resp.status_code == 400:
                msg = data.get("message") or data.get("error") or "Incorrect flag."
                return False, f"❌ {msg}"
            elif resp.status_code == 403:
                msg = data.get("message") or data.get("error") or "Team membership required or forbidden."
                return False, f"🚫 {msg}"
            else:
                return False, f"Server returned HTTP {resp.status_code}: {data.get('message') or resp.text[:100]}"

        except Exception as e:
            return False, f"Exception during submission: {str(e)}"
