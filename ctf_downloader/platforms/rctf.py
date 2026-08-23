import urllib.parse
import requests
from typing import List, Dict, Any, Optional, Tuple
from bs4 import BeautifulSoup
from .base import BasePlatform, Challenge, CTFInfo
from ..utils.logger import Logger

class RCTFPlatform(BasePlatform):
    def __init__(self, base_url: str, session: requests.Session):
        super().__init__(base_url, session)
        self.ctf_info.platform_type = "rctf"

    def _extract_title(self) -> None:
        try:
            h_resp = self.session.get(self.base_url, timeout=5)
            if h_resp.status_code == 200:
                soup = BeautifulSoup(h_resp.text, "html.parser")
                title_el = soup.find("title")
                if title_el and title_el.text and "rCTF" not in title_el.text:
                    self.ctf_info.title = title_el.text.strip()
        except Exception:
            pass

        if not self.ctf_info.title or self.ctf_info.title == "CTF Competition":
            domain = urllib.parse.urlparse(self.base_url).netloc
            clean_dom = domain.replace("ctf.", "").replace("www.", "").replace(".org", "").replace(".mn", "").replace(".com", "").replace(".", "_")
            self.ctf_info.title = f"{clean_dom.capitalize()}_CTF"

    def authenticate(self) -> bool:
        """
        Validates authentication on rCTF via /api/v1/auth/login, /api/v1/users/me, or /api/v1/challs.
        """
        self._extract_title()

        # 1. Try exchanging token if provided in session or URL
        auth_header = self.session.headers.get("Authorization", "")
        extracted_token = None
        
        if auth_header.startswith("Bearer "):
            extracted_token = auth_header.split("Bearer ")[1].strip()
        elif auth_header:
            extracted_token = auth_header.strip()

        # If we have a token, attempt teamToken login first
        if extracted_token:
            try:
                login_resp = self.session.post(
                    f"{self.base_url}/api/v1/auth/login",
                    json={"teamToken": extracted_token},
                    timeout=10
                )
                if login_resp.status_code == 200:
                    l_data = login_resp.json()
                    if l_data.get("kind") == "goodLogin" and l_data.get("data", {}).get("authToken"):
                        new_auth = l_data["data"]["authToken"]
                        self.session.headers["Authorization"] = f"Bearer {new_auth}"
            except Exception:
                pass

        # 2. Check /api/v1/users/me
        try:
            resp = self.session.get(f"{self.base_url}/api/v1/users/me", timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("kind") in ["goodUserData", "goodUserSelfData"] and data.get("data"):
                    user_name = data["data"].get("name")
                    self.ctf_info.user_name = user_name
                    self.ctf_info.team_name = user_name
                    Logger.success(f"Authenticated to rCTF as Team: [bold cyan]{user_name}[/bold cyan]")
                    return True
        except Exception:
            pass

        # 3. Check public challenge access
        try:
            resp = self.session.get(f"{self.base_url}/api/v1/challs", timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("kind") == "goodChallenges":
                    Logger.info("Public access to rCTF challenges confirmed.")
                    return True
        except Exception:
            pass

        Logger.error("Failed to authenticate to rCTF platform.")
        return False



    def fetch_challenges(self) -> List[Challenge]:
        """
        Fetches all challenges from rCTF.
        """
        try:
            resp = self.session.get(f"{self.base_url}/api/v1/challs", timeout=20)
            if resp.status_code != 200:
                Logger.error(f"Failed to fetch challenges from rCTF (HTTP {resp.status_code})")
                return []

            json_data = resp.json()
            if json_data.get("kind") != "goodChallenges":
                Logger.error(f"rCTF API error: {json_data.get('message')}")
                return []

            raw_challs = json_data.get("data", [])
            Logger.info(f"Found {len(raw_challs)} challenges on rCTF.")

            challenges = []
            for item in raw_challs:
                chall_id = item.get("id")
                name = item.get("name", f"Challenge_{chall_id}")
                category = item.get("category", "Misc").strip() or "Misc"
                points = item.get("points", 0)
                author = item.get("author")
                description = item.get("description", "")
                solves = item.get("solves", 0)
                
                # Parse files: [{"name": "file.zip", "url": "/uploads/..."}]
                files_list = []
                for f in item.get("files", []):
                    f_name = f.get("name", "attachment")
                    f_url = f.get("url", "")
                    if f_url:
                        files_list.append((self.get_full_file_url(f_url), f_name))

                chall_obj = Challenge(
                    id=chall_id,
                    name=name,
                    category=category,
                    points=points,
                    description=description,
                    author=author,
                    files=files_list,
                    solves_count=solves,
                    raw_data=item
                )
                challenges.append(chall_obj)

            self.ctf_info.challenges = challenges
            return challenges

        except Exception as e:
            Logger.error(f"Error fetching rCTF challenges: {str(e)}")
            return []

    def get_full_file_url(self, file_path: str) -> str:
        if file_path.startswith("http://") or file_path.startswith("https://"):
            return file_path
        return urllib.parse.urljoin(self.base_url, file_path)

    def submit_flag(self, challenge_id: Any, flag: str) -> Tuple[bool, str]:
        """
        Submits a flag to rCTF platform (/api/v1/challs/{challenge_id}/submit).
        """
        url = f"{self.base_url}/api/v1/challs/{challenge_id}/submit"
        payload = {"flag": flag.strip()}

        try:
            resp = self.session.post(url, json=payload, timeout=15)
            try:
                data = resp.json()
            except Exception:
                data = {}

            kind = data.get("kind", "")
            message = data.get("message", "")

            if kind == "goodFlag" or (resp.status_code == 200 and kind == "goodFlag"):
                return True, "🎉 Correct flag! Challenge solved!"
            elif kind == "alreadySolved":
                return True, "✅ You have already solved this challenge!"
            elif kind == "badFlag":
                return False, f"❌ Incorrect flag ({message or 'Bad Flag'})."
            elif kind == "badRateLimit" or resp.status_code == 429:
                return False, f"⏳ Rate limited! {message or 'Please wait before submitting again.'}"
            elif kind == "badChallenge":
                return False, f"⚠️ Challenge not found or unavailable ({message})."
            elif kind == "badToken":
                return False, "🚫 Authentication expired or invalid token."
            else:
                if resp.status_code == 200:
                    return True, f"✅ Submission received: {message or kind}"
                return False, f"Server returned HTTP {resp.status_code}: {message or kind or resp.text[:100]}"

        except Exception as e:
            return False, f"Exception during submission: {str(e)}"


