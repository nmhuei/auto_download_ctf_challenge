import re
import time
import urllib.parse
import requests
from typing import List, Dict, Any, Optional, Tuple
from .base import BasePlatform, Challenge, CTFInfo, safe_get_json
from ..utils.logger import Logger
from .registry import register

# Bộ field đặc trưng của GZCTF ClientConfig (GET /api/config)
_GZCTF_CONFIG_FIELDS = ("Title", "Slogan", "PortMapping", "DefaultLifetime")


def probe_api_config(origin: str, session, info, done: set) -> bool:
    """/api/config (ClientConfig): xác nhận GZCTF + làm giàu capabilities."""
    if "gzctf_config" in done:
        return False
    done.add("gzctf_config")
    data, status = safe_get_json(session, f"{origin}/api/config")
    if isinstance(data, dict) and all(f in data for f in _GZCTF_CONFIG_FIELDS):
        caps = info.capabilities
        info.version_hints["title"] = data.get("Title")
        caps["rules_via_api"] = bool(data.get("Rules"))
        public_key = data.get("ApiPublicKey")
        caps["api_encryption"] = bool(public_key)
        caps["port_mapping_proxy"] = data.get("PortMapping") == "PlatformProxy"
        info.add_signal(
            f"/api/config khớp ClientConfig GZCTF "
            f"(ApiPublicKey={'có' if public_key else 'null'}, "
            f"PortMapping={data.get('PortMapping')!r})"
        )
        return True
    info.add_signal(f"GET /api/config -> không khớp GZCTF (HTTP {status})")
    return False


def probe_game_recent(origin: str, session, info, done: set) -> bool:
    """/api/game/recent|/api/game (ArrayResponse {data, length, total})."""
    if "gzctf_games" in done:
        return False
    done.add("gzctf_games")
    for endpoint in ("/api/game/recent", "/api/game"):
        data, _status = safe_get_json(session, f"{origin}{endpoint}")
        if isinstance(data, dict) and "data" in data and ("length" in data or "total" in data):
            info.add_signal(f"GET {endpoint} -> ArrayResponse GZCTF {{data, length, total}}")
            return True
    info.add_signal("GET /api/game/recent|/api/game -> không khớp GZCTF")
    return False


@register("gzctf", label="GZ::CTF", throttle=2.0,
          html_markers=("GZCTF", "GZ::CTF"),
          cookie_hints=("GZCTF_Token",),
          probes=(probe_api_config, probe_game_recent),
          supports_container=True, supports_scoreboard=True, rules_via_api=True)
class GZCTFPlatform(BasePlatform):
    # Số lần poll tối đa kết quả chấm của một submission
    SUBMISSION_POLL_ATTEMPTS = 6
    SUBMISSION_POLL_INTERVAL = 1.0  # giây

    def __init__(self, base_url: str, session: requests.Session):
        # Extract game_id from base_url if present (e.g., https://.../games/6/challenges)
        parsed = urllib.parse.urlparse(base_url)
        self.origin = f"{parsed.scheme}://{parsed.netloc}"

        self.game_id: Optional[int] = None

        # Ưu tiên 1: query ?gid=<id>
        try:
            qs = urllib.parse.parse_qs(parsed.query or "")
            if "gid" in qs and qs["gid"]:
                self.game_id = int(qs["gid"][0])
        except (ValueError, TypeError):
            self.game_id = None

        # Ưu tiên 2: path /games/<id> hoặc /game/<id>
        if self.game_id is None:
            game_match = re.search(r'/games?/(\d+)', parsed.path)
            if game_match:
                self.game_id = int(game_match.group(1))

        # KHÔNG brute-force probe id nữa — nếu không suy ra được từ URL thì để None.

        super().__init__(self.origin, session)
        self.ctf_info.platform_type = "gzctf"

    def authenticate(self) -> bool:
        """
        Validates authentication on GZCTF via /api/account/profile and /api/game/{id}.
        Không dò (brute-force) game id — game_id phải suy ra được từ URL người dùng.
        """
        # 1. Check Profile
        profile_ok = False
        try:
            resp = self.session.get(f"{self.origin}/api/account/profile", timeout=15)
            if resp.status_code == 200:
                user_data = resp.json()
                self.ctf_info.user_name = user_data.get("userName") or user_data.get("realName")
                Logger.success(f"Authenticated to GZCTF as User: [bold cyan]{self.ctf_info.user_name}[/bold cyan] ({user_data.get('email')})")
                profile_ok = True
        except Exception:
            pass

        # 2. Check Game Info (chỉ khi biết chắc game_id từ URL)
        if self.game_id is not None:
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

        if profile_ok:
            Logger.warning("Không xác định được game_id từ URL (vd: https://host/games/<id>/challenges). Một số tính năng sẽ bị giới hạn.")
            return True

        Logger.error("Failed to authenticate to GZCTF platform. Please verify GZCTF_Token cookie.")
        return False

    def fetch_rules(self) -> Optional[str]:
        """
        Lấy rules / mô tả định dạng flag từ /api/game/{game_id} (field 'content', public).
        Trả về None nếu không biết game_id hoặc request lỗi.
        """
        if not self.game_id:
            return None
        try:
            resp = self.session.get(f"{self.origin}/api/game/{self.game_id}", timeout=15)
            if resp.status_code == 200:
                content = (resp.json() or {}).get("content")
                if content and str(content).strip():
                    return str(content)
        except Exception as e:
            Logger.warning(f"Could not fetch rules from game {self.game_id}: {e}")
        return None

    def fetch_challenges(self) -> List[Challenge]:
        """
        Fetches all challenges and detailed metadata from GZCTF.
        """
        if not self.game_id:
            Logger.error("GZCTF: không xác định được game_id từ URL — bỏ qua fetch challenges.")
            return []
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

            # Fetch solved challenge IDs from scoreboard for current user/team
            solved_chall_ids = set()
            try:
                sb_resp = self.session.get(f"{self.origin}/api/game/{self.game_id}/scoreboard", timeout=10)
                if sb_resp.status_code == 200:
                    sb_json = sb_resp.json()
                    sb_items = sb_json.get("items", []) if isinstance(sb_json, dict) else sb_json
                    for s_item in sb_items:
                        is_my = False
                        if self.ctf_info.team_name and s_item.get("name") == self.ctf_info.team_name:
                            is_my = True
                        elif self.ctf_info.user_name and s_item.get("name") == self.ctf_info.user_name:
                            is_my = True
                        elif self.ctf_info.user_name:
                            for sol in s_item.get("solvedChallenges", []):
                                if sol.get("userName") == self.ctf_info.user_name:
                                    is_my = True
                                    break
                        if is_my:
                            self.ctf_info.team_name = s_item.get("name")
                            for sol in s_item.get("solvedChallenges", []):
                                if sol.get("id"):
                                    solved_chall_ids.add(sol.get("id"))
                            break
            except Exception:
                pass

            detailed_challenges = []

            for category_name, chall_list in raw_categories.items():
                for item in chall_list:
                    chall_id = item.get("id")
                    title = item.get("title", f"Challenge_{chall_id}").strip()
                    score = item.get("score", 0)
                    solved_count = item.get("solved", 0)
                    is_solved = chall_id in solved_chall_ids

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
                        solved_by_me=is_solved,
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

        POST trả về submissionId -> poll GET .../Status/{submissionId}
        đến khi ra 'Accepted' (đúng) hoặc 'WrongAnswer' (sai).
        Cập nhật self.last_verdict: correct | incorrect | unknown | ratelimited.
        """
        self.last_verdict = "unknown"

        if not self.game_id:
            return False, "GZCTF: không xác định được game_id từ URL."

        url = f"{self.origin}/api/game/{self.game_id}/challenges/{challenge_id}"
        payload = {"flag": flag.strip()}

        try:
            resp = self.session.post(url, json=payload, timeout=15)

            if resp.status_code == 200:
                sub_id = str(resp.text or "").strip().strip('"').strip()
                status_url = f"{url}/Status/{sub_id}"

                # Poll kết quả chấm thay vì đoán qua số lượng bloods
                for _attempt in range(self.SUBMISSION_POLL_ATTEMPTS):
                    raw_status = ""
                    try:
                        st_resp = self.session.get(status_url, timeout=10)
                        raw_status = str(st_resp.text or "").strip().strip('"').strip()
                    except Exception:
                        raw_status = ""
                    status_low = raw_status.lower()

                    if "accepted" in status_low:
                        self.last_verdict = "correct"
                        return True, f"🎉 CORRECT FLAG! Challenge solved (Submission ID: {sub_id})!"
                    if "wronganswer" in status_low or "wrong_answer" in status_low or "wrong answer" in status_low:
                        self.last_verdict = "incorrect"
                        return False, f"❌ Incorrect flag (Submission ID: {sub_id})."

                    time.sleep(self.SUBMISSION_POLL_INTERVAL)

                self.last_verdict = "unknown"
                return False, (
                    f"⚠️ Không xác định được kết quả chấm (Submission ID: {sub_id}). "
                    f"Hãy kiểm tra trang submissions của giải."
                )

            elif resp.status_code == 400:
                err_text = resp.text.strip().strip('"') or "Invalid Flag"
                self.last_verdict = "incorrect"
                return False, f"❌ Incorrect flag ({err_text})."
            elif resp.status_code == 403:
                self.last_verdict = "unknown"
                return False, "🚫 Access denied / Competition not active."
            elif resp.status_code == 429:
                self.last_verdict = "ratelimited"
                return False, "⏳ Rate limited. Please wait before submitting again."
            else:
                self.last_verdict = "unknown"
                return False, f"Server returned HTTP {resp.status_code}: {resp.text[:100]}"

        except Exception as e:
            self.last_verdict = "unknown"
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

    def fetch_scoreboard(self) -> Dict[str, Any]:
        """
        Fetches scoreboard and ranking standings from GZCTF.
        """
        result = {
            "title": self.ctf_info.title or "GZCTF Scoreboard",
            "my_team": self.ctf_info.team_name,
            "my_user": self.ctf_info.user_name,
            "my_rank": None,
            "my_score": None,
            "total_teams": 0,
            "standings": []
        }

        if not self.game_id:
            return result

        url = f"{self.origin}/api/game/{self.game_id}/scoreboard"
        try:
            resp = self.session.get(url, timeout=15)
            if resp.status_code == 200:
                data = resp.json() or {}
                items = data.get("items", []) if isinstance(data, dict) else data
                result["total_teams"] = len(items)
                standings = []
                for idx, entry in enumerate(items, 1):
                    name = entry.get("name") or entry.get("teamName") or entry.get("userName")
                    score = entry.get("score") or entry.get("totalScore") or 0
                    rank = entry.get("rank") or idx
                    
                    # Check if my_user is part of this team
                    is_my_team = False
                    if result["my_team"] and name == result["my_team"]:
                        is_my_team = True
                    elif result["my_user"] and name == result["my_user"]:
                        is_my_team = True
                    elif result["my_user"]:
                        for solved in entry.get("solvedChallenges", []):
                            if solved.get("userName") == result["my_user"]:
                                is_my_team = True
                                break

                    if is_my_team:
                        result["my_team"] = name
                        result["my_rank"] = f"{rank}th" if rank else f"{idx}th"
                        result["my_score"] = score

                    standings.append({
                        "pos": rank,
                        "name": name,
                        "score": score,
                        "raw": entry
                    })
                result["standings"] = standings
        except Exception as e:
            Logger.warning(f"Failed to fetch scoreboard from GZCTF: {e}")

        return result



