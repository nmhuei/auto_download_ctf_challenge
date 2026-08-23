import re
import urllib.parse
import requests
from typing import List, Dict, Any, Optional, Tuple
from bs4 import BeautifulSoup
from .base import BasePlatform, Challenge, CTFInfo
from ..utils.logger import Logger
from ..utils.sanitize import sanitize_filename

class CTFdPlatform(BasePlatform):
    def __init__(self, base_url: str, session: requests.Session):
        super().__init__(base_url, session)
        self.nonce: Optional[str] = None
        self.ctf_info.platform_type = "ctfd"

    def _extract_nonce_and_config(self) -> None:
        """
        Fetches the challenges page or homepage to extract CSRF nonce and CTF title.
        """
        try:
            resp = self.session.get(f"{self.base_url}/challenges", timeout=15)
            if resp.status_code == 200:
                html = resp.text
                
                # Check for window.init = { ... 'csrfNonce': "..." ... }
                nonce_match = re.search(r"['\"]csrfNonce['\"]\s*:\s*['\"]([^'\"]+)['\"]", html)
                if not nonce_match:
                    nonce_match = re.search(r"csrf_nonce\s*=\s*['\"]([^'\"]+)['\"]", html)
                if not nonce_match:
                    # Check meta tag <meta name="csrf-token" content="...">
                    soup = BeautifulSoup(html, "html.parser")
                    meta_csrf = soup.find("meta", {"name": "csrf-token"})
                    if meta_csrf and meta_csrf.get("content"):
                        self.nonce = meta_csrf["content"]
                        self.session.headers["CSRF-Token"] = self.nonce
                
                if nonce_match:
                    self.nonce = nonce_match.group(1)
                    self.session.headers["CSRF-Token"] = self.nonce

                # Extract CTF title
                soup = BeautifulSoup(html, "html.parser")
                title_tag = soup.find("title")
                if title_tag and title_tag.text:
                    self.ctf_info.title = title_tag.text.strip().replace(" - CTFd", "").replace("Challenges", "").strip(" -|:")

        except Exception as e:
            Logger.warning(f"Could not extract CTFd nonce: {e}")

    def authenticate(self) -> bool:
        """
        Validates authentication by checking /api/v1/users/me or /api/v1/challenges.
        """
        self._extract_nonce_and_config()

        # Check /api/v1/users/me
        try:
            resp = self.session.get(f"{self.base_url}/api/v1/users/me", timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("success") and data.get("data"):
                    user_data = data["data"]
                    self.ctf_info.user_name = user_data.get("name")
                    Logger.success(f"Authenticated as User: [bold cyan]{self.ctf_info.user_name}[/bold cyan]")
                    return True
        except Exception:
            pass

        # Try /api/v1/challenges directly (in case user isn't logged in but challenges are public)
        try:
            resp = self.session.get(f"{self.base_url}/api/v1/challenges", timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("success"):
                    Logger.info("Public access to CTFd challenges confirmed.")
                    return True
        except Exception:
            pass

        Logger.error("Failed to authenticate to CTFd platform. Please check your Cookie or Token.")
        return False

    def fetch_challenges(self) -> List[Challenge]:
        """
        Fetches the complete challenge list and detailed information for each challenge.
        """
        challenges_url = f"{self.base_url}/api/v1/challenges"
        try:
            resp = self.session.get(challenges_url, timeout=20)
            if resp.status_code != 200:
                Logger.error(f"Failed to fetch challenges from {challenges_url} (HTTP {resp.status_code})")
                return []

            content_type = resp.headers.get("content-type", "")
            if "json" not in content_type:
                if "/login" in resp.url or "login" in resp.text.lower():
                    Logger.error("CTFd requires login. Please provide a valid session cookie (-c) or API token (-t).")
                else:
                    Logger.error(f"Unexpected response format from CTFd (Content-Type: {content_type}).")
                return []

            json_data = resp.json()
            if not json_data.get("success"):
                Logger.error(f"CTFd API returned unsuccessful response: {json_data.get('errors')}")
                return []

            raw_challs = json_data.get("data", [])
            Logger.info(f"Found {len(raw_challs)} challenges on CTFd. Fetching challenge details...")

            detailed_challenges = []

            # Check team solves if team mode is active
            team_solves = set()
            try:
                ts_resp = self.session.get(f"{self.base_url}/api/v1/teams/me/solves", timeout=10)
                if ts_resp.status_code == 200 and ts_resp.json().get("success"):
                    team_solves = {s.get("challenge_id") for s in ts_resp.json().get("data", [])}
            except Exception:
                pass

            for index, item in enumerate(raw_challs, start=1):
                chall_id = item.get("id")
                name = item.get("name", f"Challenge_{chall_id}")
                category = item.get("category", "Misc").strip() or "Misc"
                value = item.get("value", 0)
                solved_by_me = item.get("solved_by_me", False) or (chall_id in team_solves)
                solves_count = item.get("solves", None)

                # Fetch detailed challenge info (/api/v1/challenges/<id>)
                detail_resp = self.session.get(f"{self.base_url}/api/v1/challenges/{chall_id}", timeout=15)
                
                description = ""
                files_list = []
                tags_list = [t.get("value", t) if isinstance(t, dict) else str(t) for t in item.get("tags", [])]
                hints_list = []
                connection_info = item.get("connection_info")
                detail_data = {}

                if detail_resp.status_code == 200:
                    try:
                        detail_json = detail_resp.json()
                        if detail_json.get("success"):
                            detail_data = detail_json.get("data", {})
                            description = detail_data.get("description", "")
                            connection_info = detail_data.get("connection_info") or connection_info
                            
                            # Parse files
                            for f in detail_data.get("files", []):
                                if isinstance(f, str):
                                    file_url = self.get_full_file_url(f)
                                    # Extract filename from path or query
                                    clean_name = f.split("/")[-1].split("?")[0]
                                    files_list.append((file_url, clean_name))
                                elif isinstance(f, dict):
                                    f_url = f.get("location") or f.get("url")
                                    f_name = f.get("name") or (f_url.split("/")[-1].split("?")[0] if f_url else "attachment")
                                    if f_url:
                                        files_list.append((self.get_full_file_url(f_url), f_name))

                            # Parse tags
                            for tag in detail_data.get("tags", []):
                                tag_val = tag.get("value") if isinstance(tag, dict) else str(tag)
                                if tag_val and tag_val not in tags_list:
                                    tags_list.append(tag_val)

                            # Parse hints
                            for hint in detail_data.get("hints", []):
                                if isinstance(hint, dict):
                                    hints_list.append(hint)
                                elif isinstance(hint, str):
                                    hints_list.append({"content": hint})

                    except Exception as e:
                        Logger.warning(f"Error parsing details for {name}: {e}")

                # Check if it has dynamic container / whale / instancer
                is_container = False
                instance_info = {}
                chall_type = detail_data.get("type") or item.get("type", "standard")
                if chall_type in ["dynamic_docker", "whale", "container", "docker"] or "container" in tags_list:
                    is_container = True
                    instance_info = {
                        "is_container": True,
                        "type": "ctfd_whale" if "whale" in chall_type else "ctfd_container",
                        "start_url": f"{self.base_url}/plugins/ctfd-whale/container",
                        "stop_url": f"{self.base_url}/plugins/ctfd-whale/container",
                        "extend_url": f"{self.base_url}/plugins/ctfd-whale/container",
                        "status_url": f"{self.base_url}/plugins/ctfd-whale/container"
                    }

                submit_endpoint = f"{self.base_url}/api/v1/challenges/attempt"

                chall_obj = Challenge(
                    id=chall_id,
                    name=name,
                    category=category,
                    points=value,
                    description=description,
                    tags=tags_list,
                    hints=hints_list,
                    files=files_list,
                    connection_info=connection_info,
                    solved_by_me=solved_by_me,
                    solves_count=solves_count,
                    submit_endpoint=submit_endpoint,
                    instance_info=instance_info,
                    raw_data=detail_data or item
                )
                detailed_challenges.append(chall_obj)

            self.ctf_info.challenges = detailed_challenges
            return detailed_challenges

        except Exception as e:
            Logger.error(f"Error fetching CTFd challenges: {str(e)}")
            return []

    def get_full_file_url(self, file_path: str) -> str:
        """
        Resolves CTFd file relative paths to full URL.
        """
        if file_path.startswith("http://") or file_path.startswith("https://"):
            return file_path
        return urllib.parse.urljoin(self.base_url, file_path)

    def submit_flag(self, challenge_id: Any, flag: str) -> Tuple[bool, str]:
        """
        Submits a flag to CTFd platform (/api/v1/challenges/attempt).
        """
        if not self.nonce:
            self._extract_nonce_and_config()

        url = f"{self.base_url}/api/v1/challenges/attempt"
        payload = {
            "challenge_id": challenge_id,
            "submission": flag.strip()
        }

        try:
            resp = self.session.post(url, json=payload, timeout=15)
            if resp.status_code != 200:
                return False, f"Server returned HTTP {resp.status_code}"

            data = resp.json()
            if not data.get("success"):
                errors = data.get("errors", {})
                return False, f"Submission error: {errors}"

            sub_data = data.get("data", {})
            status = sub_data.get("status", "")
            message = sub_data.get("message", "")

            if status == "correct":
                return True, "🎉 Correct flag! Challenge solved!"
            elif status == "already_solved":
                return True, "✅ You have already solved this challenge!"
            elif status == "incorrect":
                return False, "❌ Incorrect flag."
            elif status == "paused":
                return False, "⏸️ CTF is currently paused."
            elif status == "ratelimited":
                return False, "⏳ Rate limited! Please wait before submitting again."
            else:
                return False, f"Status: {status} ({message})"

        except Exception as e:
            return False, f"Exception during submission: {str(e)}"

    def start_instance(self, challenge_id: Any) -> Tuple[bool, Dict[str, Any]]:
        """
        Starts container instance on CTFd (supports CTFd-Whale and container plugins).
        """
        if not self.nonce:
            self._extract_nonce_and_config()

        # 1. Try CTFd-Whale API v1 endpoint (/api/v1/plugins/ctfd-whale/container?challenge_id=...)
        whale_v1_url = f"{self.base_url}/api/v1/plugins/ctfd-whale/container?challenge_id={challenge_id}"
        try:
            resp = self.session.post(whale_v1_url, json={}, timeout=15)
            if resp.status_code == 200:
                data = resp.json() or {}
                if data.get("success"):
                    container_data = data.get("data", {})
                    entry_point = container_data.get("user_access") or container_data.get("domain") or f"{container_data.get('host')}:{container_data.get('port')}"
                    return True, {
                        "entry": entry_point,
                        "time_left": container_data.get("remaining_time"),
                        "raw": container_data
                    }
                else:
                    return False, {"message": data.get("message", "Container start failed.")}
            elif resp.status_code == 500:
                return False, {"message": "Server Error (500): Server-side container runner / Docker Swarm is unreachable or not configured by CTF admin."}
        except Exception as e:
            Logger.warning(f"Error calling {whale_v1_url}: {e}")

        # 2. Try legacy /plugins/ctfd-whale/container
        whale_url = f"{self.base_url}/plugins/ctfd-whale/container"
        try:
            resp = self.session.post(whale_url, json={"challenge_id": challenge_id}, timeout=15)
            if resp.status_code == 200:
                data = resp.json() or {}
                if data.get("success"):
                    container_data = data.get("data", {})
                    return True, {
                        "entry": container_data.get("user_access") or container_data.get("domain") or f"{container_data.get('host')}:{container_data.get('port')}",
                        "time_left": container_data.get("remaining_time"),
                        "raw": container_data
                    }
        except Exception:
            pass

        # 3. Try /api/v1/containers
        api_url = f"{self.base_url}/api/v1/containers"
        try:
            resp = self.session.post(api_url, json={"challenge_id": challenge_id}, timeout=15)
            if resp.status_code in [200, 201]:
                data = resp.json() or {}
                return True, data.get("data", data)
        except Exception:
            pass

        return False, {"message": "No active container plugin or instance service found for this challenge."}

    def stop_instance(self, challenge_id: Any) -> Tuple[bool, str]:
        """
        Destroys container instance on CTFd.
        """
        if not self.nonce:
            self._extract_nonce_and_config()

        # Try API v1 first
        whale_v1_url = f"{self.base_url}/api/v1/plugins/ctfd-whale/container?challenge_id={challenge_id}"
        try:
            resp = self.session.delete(whale_v1_url, json={}, timeout=15)
            if resp.status_code == 200:
                return True, "Container stopped."
        except Exception:
            pass

        whale_url = f"{self.base_url}/plugins/ctfd-whale/container"
        try:
            resp = self.session.delete(whale_url, json={"challenge_id": challenge_id}, timeout=15)
            if resp.status_code == 200:
                return True, "Container stopped."
        except Exception:
            pass
        return False, "Failed to stop container on CTFd."

    def extend_instance(self, challenge_id: Any) -> Tuple[bool, str]:
        """
        Extends container lifetime on CTFd.
        """
        if not self.nonce:
            self._extract_nonce_and_config()

        # Try API v1 first
        whale_v1_url = f"{self.base_url}/api/v1/plugins/ctfd-whale/container?challenge_id={challenge_id}"
        try:
            resp = self.session.patch(whale_v1_url, json={}, timeout=15)
            if resp.status_code == 200:
                return True, "Container lifetime extended."
        except Exception:
            pass

        whale_url = f"{self.base_url}/plugins/ctfd-whale/container"
        try:
            resp = self.session.patch(whale_url, json={"challenge_id": challenge_id}, timeout=15)
            if resp.status_code == 200:
                return True, "Container lifetime extended."
        except Exception:
            pass
        return False, "Failed to extend container on CTFd."

    def get_instance_status(self, challenge_id: Any) -> Dict[str, Any]:
        """
        Fetches current container instance status on CTFd.
        """
        # Try API v1 first
        whale_v1_url = f"{self.base_url}/api/v1/plugins/ctfd-whale/container?challenge_id={challenge_id}"
        try:
            resp = self.session.get(whale_v1_url, timeout=10)
            if resp.status_code == 200:
                data = resp.json() or {}
                if data.get("success"):
                    cdata = data.get("data", {})
                    if cdata and cdata.get("remaining_time") is not None:
                        return {
                            "status": "running",
                            "entry": cdata.get("user_access") or cdata.get("domain") or f"{cdata.get('host')}:{cdata.get('port')}",
                            "time_left": cdata.get("remaining_time")
                        }
                    else:
                        return {"status": "stopped", "entry": None, "time_left": None}
        except Exception:
            pass

        whale_url = f"{self.base_url}/plugins/ctfd-whale/container"
        try:
            resp = self.session.get(whale_url, params={"challenge_id": challenge_id}, timeout=10)
            if resp.status_code == 200:
                data = resp.json() or {}
                if data.get("success"):
                    cdata = data.get("data", {})
                    return {
                        "status": "running",
                        "entry": cdata.get("user_access") or cdata.get("domain") or f"{cdata.get('host')}:{cdata.get('port')}",
                        "time_left": cdata.get("remaining_time")
                    }
        except Exception:
            pass
        return {"status": "stopped", "entry": None, "time_left": None}


