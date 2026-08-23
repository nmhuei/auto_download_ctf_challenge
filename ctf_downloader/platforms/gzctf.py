import re
import urllib.parse
import requests
from typing import List, Dict, Any, Optional, Tuple
from .base import BasePlatform, Challenge, CTFInfo
from ..utils.logger import Logger

class GZCTFPlatform(BasePlatform):
    def __init__(self, base_url: str, session: requests.Session):
        # Extract game_id from base_url if present (e.g., https://.../games/6/challenges)
        parsed = urllib.parse.urlparse(base_url)
        self.origin = f"{parsed.scheme}://{parsed.netloc}"
        
        game_match = re.search(r'/games?/(\d+)', parsed.path)
        self.game_id = int(game_match.group(1)) if game_match else 1
        
        super().__init__(self.origin, session)
        self.ctf_info.platform_type = "gzctf"

    def authenticate(self) -> bool:
        """
        Validates authentication on GZCTF via /api/account/profile and /api/game/{id}.
        """
        # 1. Check Profile
        try:
            resp = self.session.get(f"{self.origin}/api/account/profile", timeout=15)
            if resp.status_code == 200:
                user_data = resp.json()
                self.ctf_info.user_name = user_data.get("userName") or user_data.get("realName")
                Logger.success(f"Authenticated to GZCTF as User: [bold cyan]{self.ctf_info.user_name}[/bold cyan] ({user_data.get('email')})")
        except Exception:
            pass

        # 2. Check Game Info
        try:
            resp = self.session.get(f"{self.origin}/api/game/{self.game_id}", timeout=15)
            if resp.status_code == 200:
                game_data = resp.json()
                self.ctf_info.title = game_data.get("title", f"Game {self.game_id}")
                self.ctf_info.team_name = game_data.get("teamName")
                if self.ctf_info.team_name:
                    Logger.info(f"Team: [bold magenta]{self.ctf_info.team_name}[/bold magenta] | Competition: [bold yellow]{self.ctf_info.title}[/bold yellow]")
                return True
        except Exception as e:
            Logger.warning(f"Could not fetch game {self.game_id} info: {e}")

        # If game_id not found, probe other game IDs
        for gid in range(1, 10):
            if gid == self.game_id:
                continue
            try:
                resp = self.session.get(f"{self.origin}/api/game/{gid}", timeout=5)
                if resp.status_code == 200:
                    self.game_id = gid
                    game_data = resp.json()
                    self.ctf_info.title = game_data.get("title", f"Game {self.game_id}")
                    Logger.info(f"Auto-selected active Game ID: {gid} ({self.ctf_info.title})")
                    return True
            except Exception:
                pass

        if self.ctf_info.user_name:
            return True

        Logger.error("Failed to authenticate to GZCTF platform. Please verify GZCTF_Token cookie.")
        return False

    def fetch_challenges(self) -> List[Challenge]:
        """
        Fetches all challenges and detailed metadata from GZCTF.
        """
        details_url = f"{self.origin}/api/game/{self.game_id}/details"
        try:
            resp = self.session.get(details_url, timeout=20)
            if resp.status_code != 200:
                Logger.error(f"Failed to fetch challenges from {details_url} (HTTP {resp.status_code})")
                return []

            data = resp.json()
            raw_categories = data.get("challenges", {})
            if not raw_categories:
                Logger.warning("No challenges returned in game details.")
                return []

            total_count = sum(len(challs) for challs in raw_categories.values())
            Logger.info(f"Found {total_count} challenges across {len(raw_categories)} categories on GZCTF. Fetching details...")

            detailed_challenges = []

            for category_name, chall_list in raw_categories.items():
                for item in chall_list:
                    chall_id = item.get("id")
                    title = item.get("title", f"Challenge_{chall_id}").strip()
                    score = item.get("score", 0)
                    solved_count = item.get("solved", 0)

                    # Fetch individual challenge details: /api/game/{game_id}/challenges/{challenge_id}
                    single_url = f"{self.origin}/api/game/{self.game_id}/challenges/{chall_id}"
                    chall_resp = self.session.get(single_url, timeout=15)
                    
                    description = ""
                    hints_list = []
                    files_list = []
                    chall_type = item.get("type", "Standard")
                    single_data = {}

                    if chall_resp.status_code == 200:
                        try:
                            single_data = chall_resp.json() or {}
                            description = single_data.get("content") or ""
                            chall_type = single_data.get("type") or chall_type
                            
                            # Hints
                            for h in (single_data.get("hints") or []):
                                if isinstance(h, str):
                                    hints_list.append({"content": h})
                                elif isinstance(h, dict):
                                    hints_list.append(h)

                            # Attachments in context
                            ctx = single_data.get("context") or {}
                            if isinstance(ctx, dict):
                                asset_url = ctx.get("url")
                                if asset_url:
                                    full_asset_url = self.get_full_file_url(asset_url)
                                    filename = asset_url.rstrip("/").split("/")[-1]
                                    files_list.append((full_asset_url, filename))

                        except Exception as e:
                            Logger.warning(f"Error parsing detail for {title}: {e}")

                    # Determine if it's a dynamic container
                    is_container = chall_type == "DynamicContainer" or (single_data.get("type") == "DynamicContainer")
                    instance_info = {}
                    if is_container:
                        ctx = single_data.get("context") or {}
                        instance_info = {
                            "is_container": True,
                            "type": "gzctf",
                            "start_url": f"{self.origin}/api/game/{self.game_id}/container/{chall_id}",
                            "stop_url": f"{self.origin}/api/game/{self.game_id}/container/{chall_id}",
                            "extend_url": f"{self.origin}/api/game/{self.game_id}/container/{chall_id}/extend",
                            "status_url": f"{self.origin}/api/game/{self.game_id}/challenges/{chall_id}",
                            "entry": ctx.get("instanceEntry"),
                            "close_time": ctx.get("closeTime")
                        }

                    submit_endpoint = f"{self.origin}/api/game/{self.game_id}/challenges/{chall_id}"

                    chall_obj = Challenge(
                        id=chall_id,
                        name=title,
                        category=category_name,
                        points=score,
                        description=description,
                        tags=[chall_type] if chall_type else [],
                        hints=hints_list,
                        files=files_list,
                        solves_count=solved_count,
                        submit_endpoint=submit_endpoint,
                        instance_info=instance_info,
                        raw_data=single_data or item
                    )
                    detailed_challenges.append(chall_obj)

            self.ctf_info.challenges = detailed_challenges
            self.ctf_info.game_id = self.game_id
            return detailed_challenges

        except Exception as e:
            Logger.error(f"Error fetching GZCTF challenges: {str(e)}")
            return []

    def get_full_file_url(self, file_path: str) -> str:
        if file_path.startswith("http://") or file_path.startswith("https://"):
            return file_path
        return urllib.parse.urljoin(self.origin, file_path)

    def submit_flag(self, challenge_id: Any, flag: str) -> Tuple[bool, str]:
        """
        Submits a flag to GZCTF platform (/api/game/{game_id}/challenges/{challenge_id}).
        """
        url = f"{self.origin}/api/game/{self.game_id}/challenges/{challenge_id}"
        payload = {"flag": flag.strip()}

        try:
            # 1. Check pre-state
            pre_bloods_count = 0
            try:
                pre_det = self.session.get(f"{self.origin}/api/game/{self.game_id}/details", timeout=5).json()
                for cat, challs in pre_det.get("challenges", {}).items():
                    for c in challs:
                        if str(c.get("id")) == str(challenge_id):
                            pre_bloods_count = len(c.get("bloods", []))
            except Exception:
                pass

            resp = self.session.post(url, json=payload, timeout=15)
            
            if resp.status_code == 200:
                sub_id = resp.text.strip().strip('"')
                
                # 2. Check post-state
                is_correct = False
                try:
                    post_det = self.session.get(f"{self.origin}/api/game/{self.game_id}/details", timeout=5).json()
                    for cat, challs in post_det.get("challenges", {}).items():
                        for c in challs:
                            if str(c.get("id")) == str(challenge_id):
                                post_bloods = c.get("bloods", [])
                                # If bloods increased or my team is in bloods
                                if len(post_bloods) > pre_bloods_count:
                                    is_correct = True
                                elif self.ctf_info.team_name and any(b.get("name") == self.ctf_info.team_name for b in post_bloods):
                                    is_correct = True
                except Exception:
                    pass

                if is_correct:
                    return True, f"🎉 CORRECT FLAG! Challenge solved (Submission ID: {sub_id})!"
                else:
                    return False, f"❌ Incorrect flag (Submission ID: {sub_id})."

            elif resp.status_code == 400:
                err_text = resp.text.strip().strip('"') or "Invalid Flag"
                return False, f"❌ Incorrect flag ({err_text})."
            elif resp.status_code == 403:
                return False, "🚫 Access denied / Competition not active."
            elif resp.status_code == 429:
                return False, "⏳ Rate limited. Please wait before submitting again."
            else:
                return False, f"Server returned HTTP {resp.status_code}: {resp.text[:100]}"

        except Exception as e:
            return False, f"Exception during submission: {str(e)}"

    def start_instance(self, challenge_id: Any) -> Tuple[bool, Dict[str, Any]]:
        """
        Starts or retrieves container instance for challenge on GZCTF.
        """
        url = f"{self.origin}/api/game/{self.game_id}/container/{challenge_id}"
        try:
            resp = self.session.post(url, timeout=15)
            if resp.status_code == 200:
                data = resp.json() or {}
                entry = data.get("entry")
                # If entry is None, fetch from challenges details
                if not entry:
                    status_info = self.get_instance_status(challenge_id)
                    entry = status_info.get("entry")
                    data["entry"] = entry
                return True, data
            else:
                # Check if it is already running
                status_info = self.get_instance_status(challenge_id)
                if status_info.get("entry"):
                    return True, status_info
                return False, {"message": f"HTTP {resp.status_code}: {resp.text}"}
        except Exception as e:
            return False, {"message": str(e)}

    def stop_instance(self, challenge_id: Any) -> Tuple[bool, str]:
        """
        Stops/destroys container instance for challenge on GZCTF.
        """
        url = f"{self.origin}/api/game/{self.game_id}/container/{challenge_id}"
        try:
            resp = self.session.delete(url, timeout=15)
            if resp.status_code in [200, 204]:
                return True, "Container stopped successfully."
            return False, f"Failed to stop container (HTTP {resp.status_code}): {resp.text}"
        except Exception as e:
            return False, str(e)

    def extend_instance(self, challenge_id: Any) -> Tuple[bool, str]:
        """
        Extends active container instance lifetime.
        """
        url = f"{self.origin}/api/game/{self.game_id}/container/{challenge_id}/extend"
        try:
            resp = self.session.post(url, timeout=15)
            if resp.status_code == 200:
                return True, "Container lifetime extended successfully."
            return False, f"Failed to extend container (HTTP {resp.status_code}): {resp.text}"
        except Exception as e:
            return False, str(e)

    def get_instance_status(self, challenge_id: Any) -> Dict[str, Any]:
        """
        Fetches current container instance status and entry info.
        """
        url = f"{self.origin}/api/game/{self.game_id}/challenges/{challenge_id}"
        try:
            resp = self.session.get(url, timeout=10)
            if resp.status_code == 200:
                data = resp.json() or {}
                ctx = data.get("context") or {}
                entry = ctx.get("instanceEntry")
                close_time = ctx.get("closeTime")
                return {
                    "status": "running" if entry else "stopped",
                    "entry": entry,
                    "close_time": close_time,
                    "type": data.get("type")
                }
        except Exception:
            pass
        return {"status": "unknown", "entry": None, "close_time": None}



