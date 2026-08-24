import json
import re
import urllib.parse
import requests
from typing import List, Dict, Any, Optional, Tuple
from bs4 import BeautifulSoup
from .base import BasePlatform, Challenge, CTFInfo, SolveAttribution, epoch_ms, safe_get_json
from ..utils.logger import Logger
from ..utils.sanitize import sanitize_filename
from .registry import register


def probe_ctfd_challenges(origin: str, session, info, done: set) -> bool:
    """/api/v1/challenges -> envelope {"success": ...}; phát hiện plugin whale."""
    if "ctfd_challs" in done:
        return False
    done.add("ctfd_challs")
    data, status = safe_get_json(session, f"{origin}/api/v1/challenges",
                                 statuses=(200, 401, 403))
    if isinstance(data, dict) and "success" in data:
        try:
            dumped = json.dumps(data)
        except Exception:
            dumped = ""
        if "ctfd-whale" in dumped:
            info.capabilities["container"] = True
            info.version_hints["whale_fork"] = "frankli0324/ctfd-whale"
            info.add_signal("/api/v1/challenges có template/script /plugins/ctfd-whale/ "
                            "-> hỗ trợ container động (whale fork)")
        info.add_signal('GET /api/v1/challenges -> envelope {"success": ...} của CTFd')
        return True
    info.add_signal(f"GET /api/v1/challenges -> không khớp CTFd (HTTP {status})")
    return False


@register("ctfd", label="CTFd", throttle=6.0,
          html_markers=("csrfNonce'", "window.init", "Powered by CTFd", "/themes/core/"),
          cookie_hints=("session",),
          probes=(probe_ctfd_challenges,),
          supports_scoreboard=True)
class CTFdPlatform(BasePlatform):
    # Các slug trang public dùng để dò rules / flag format (Pages API là admin-only)
    RULE_PAGE_SLUGS = [
        "rules", "rule", "Rules", "faq", "about", "welcome",
        "guide", "getting-started", "flag-format", "flag_format", "format"
    ]

    def __init__(self, base_url: str, session: requests.Session):
        super().__init__(base_url, session)
        self.nonce: Optional[str] = None
        self.ctf_info.platform_type = "ctfd"
        # Pitfall: khi dùng Authorization token, nếu thiếu Content-Type: application/json
        # thì server CTFd bỏ qua header Authorization.
        try:
            auth_header = self.session.headers.get("Authorization", "") or ""
            current_ct = (self.session.headers.get("Content-Type", "") or "")
            if auth_header and "json" not in current_ct.lower():
                self.session.headers["Content-Type"] = "application/json"
        except Exception:
            pass

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

    def fetch_rules(self) -> Optional[str]:
        """
        Dò rules / flag format trên các trang public của CTFd (Pages API là admin-only).
        Chấp nhận HTTP 200 và nội dung HTML không phải trang 404 của theme.
        Trả về nội dung HTML đầu tiên tìm được, None nếu không có.
        """
        for slug in self.RULE_PAGE_SLUGS:
            url = f"{self.base_url}/{slug}"
            try:
                resp = self.session.get(url, timeout=10)
                if resp.status_code != 200:
                    continue
                try:
                    ctype = (resp.headers or {}).get("content-type", "")
                except Exception:
                    ctype = ""
                if ctype and not any(t in ctype.lower() for t in ("html", "text", "json")):
                    continue
                html = resp.text or ""
            except Exception:
                continue
            if len(html.strip()) < 50 or self._looks_like_404(html):
                continue
            Logger.info(f"Fetched potential rules page: [bold cyan]/{slug}[/bold cyan]")
            return html
        return None

    @staticmethod
    def _looks_like_404(html: str) -> bool:
        """
        Phát hiện trang 404 của theme CTFd (trả HTTP 200 nhưng nội dung là 'not found').
        """
        try:
            soup = BeautifulSoup(html, "html.parser")
            title = soup.find("title")
            title_text = (title.text or "").strip().lower() if title else ""
            if "404" in title_text or "not found" in title_text:
                return True
            h1 = soup.find("h1")
            if h1 and "404" in (h1.text or "").lower():
                return True
            return False
        except Exception:
            return False

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
        Cập nhật self.last_verdict theo kết quả chuẩn:
        correct | incorrect | unknown | ratelimited.
        """
        if not self.nonce:
            self._extract_nonce_and_config()

        self.last_verdict = "unknown"
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
                self.last_verdict = "correct"
                return True, "🎉 Correct flag! Challenge solved!"
            elif status == "already_solved":
                self.last_verdict = "correct"
                return True, "✅ You have already solved this challenge!"
            elif status == "incorrect":
                self.last_verdict = "incorrect"
                return False, "❌ Incorrect flag."
            elif status == "paused":
                self.last_verdict = "unknown"
                return False, "⏸️ CTF is currently paused."
            elif status == "ratelimited":
                self.last_verdict = "ratelimited"
                return False, "⏳ Rate limited! Please wait before submitting again."
            else:
                self.last_verdict = "unknown"
                return False, f"Status: {status} ({message})"

        except Exception as e:
            self.last_verdict = "unknown"
            return False, f"Exception during submission"

    def _clean_user_access(self, val: Optional[str]) -> Optional[str]:
        if not val:
            return val
        val_str = str(val).strip()
        m = re.search(r'href=["\'](https?://[^"\']+)["\']', val_str)
        if m:
            return m.group(1)
        return val_str

    def start_instance(self, challenge_id: Any) -> Tuple[bool, Dict[str, Any]]:
        """
        Spawns a dynamic container instance for the challenge (CTFd Whale / Docker plugin).
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
                    raw_entry = container_data.get("user_access") or container_data.get("domain") or f"{container_data.get('host')}:{container_data.get('port')}"
                    return True, {
                        "entry": self._clean_user_access(raw_entry),
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
                        raw_ent = cdata.get("user_access") or cdata.get("domain") or f"{cdata.get('host')}:{cdata.get('port')}"
                        return {
                            "status": "running",
                            "entry": self._clean_user_access(raw_ent),
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
                    raw_ent = cdata.get("user_access") or cdata.get("domain") or f"{cdata.get('host')}:{cdata.get('port')}"
                    return {
                        "status": "running",
                        "entry": self._clean_user_access(raw_ent),
                        "time_left": cdata.get("remaining_time")
                    }
        except Exception:
            pass
        return {"status": "stopped", "entry": None, "time_left": None}

    # ------------------------------------------------------------------
    # Solve attribution (spec §4) — users mode vs teams mode
    # ------------------------------------------------------------------

    def fetch_solve_attribution(self, challenge_ids) -> Dict[Any, SolveAttribution]:
        """2 requests: /teams/me (detect mode) + solves tương ứng.
        Teams mode: mỗi dòng mang ``user.id/name`` của thành viên submit —
        ``by_me = row.user.id == me_id``. Users mode: by_team ≡ by_me."""
        wanted = {str(c): c for c in (challenge_ids or [])}
        cache = getattr(self, "_solve_attr_cache", None)
        if cache is None:
            cache = self._solve_attr_cache = {}
            try:
                me_id, me_name = None, self.ctf_info.user_name
                r_me = self.session.get(f"{self.base_url}/api/v1/users/me", timeout=10)
                if r_me.status_code == 200:
                    d = (r_me.json() or {}).get("data") or {}
                    me_id, me_name = d.get("id"), d.get("name") or me_name

                teams_mode = False
                rows = []
                try:
                    rt = self.session.get(f"{self.base_url}/api/v1/teams/me", timeout=10)
                    if rt.status_code == 200 and (rt.json() or {}).get("data"):
                        teams_mode = True
                        rs = self.session.get(
                            f"{self.base_url}/api/v1/teams/me/solves", timeout=10)
                        if rs.status_code == 200:
                            rows = (rs.json() or {}).get("data") or []
                except Exception:
                    rows = []

                if not teams_mode:
                    rs = self.session.get(
                        f"{self.base_url}/api/v1/users/me/solves", timeout=10)
                    if rs.status_code == 200:
                        rows = (rs.json() or {}).get("data") or []

                for row in rows:
                    cid = row.get("challenge_id")
                    if cid is None and isinstance(row.get("challenge"), dict):
                        cid = row["challenge"].get("id")
                    if cid is None:
                        continue
                    user = row.get("user") or {}
                    uname = user.get("name")
                    if user:
                        # Teams mode: mỗi dòng mang user.id/name của thành viên submit.
                        # me_id không xác định được -> KHÔNG tự coi là của mình
                        # (fail-safe, tránh kẹt solved_by_me sai).
                        by_me = (user.get("id") == me_id) if me_id is not None else False
                    else:
                        # Users mode: mọi solve của /users/me/solves là của mình
                        by_me = not teams_mode
                    attr = cache.get(str(cid))
                    if attr is None:
                        attr = SolveAttribution(by_team=bool(teams_mode or by_me), by_me=by_me)
                        cache[str(cid)] = attr
                    elif by_me:
                        attr.by_me = True
                        attr.by_team = True
                    if uname and uname not in attr.solver_names:
                        attr.solver_names.append(uname)
                    ts = epoch_ms(row.get("date"))
                    if ts and (attr.solved_at is None or ts < attr.solved_at):
                        attr.solved_at = ts
            except Exception:
                pass
        return {orig: cache[k] for k, orig in wanted.items() if k in cache}

    def fetch_scoreboard(self) -> Dict[str, Any]:
        """
        Fetches full live scoreboard standings and personal/team ranking from CTFd.
        """
        result = {
            "title": self.ctf_info.title or "CTFd Scoreboard",
            "my_team": None,
            "my_user": self.ctf_info.user_name,
            "my_rank": None,
            "my_score": None,
            "total_teams": 0,
            "standings": []
        }

        # 1. Get current team / user rank
        try:
            r_team = self.session.get(f"{self.base_url}/api/v1/teams/me", timeout=10)
            if r_team.status_code == 200:
                tdata = (r_team.json() or {}).get("data", {})
                if tdata:
                    result["my_team"] = tdata.get("name")
                    result["my_rank"] = tdata.get("place") or tdata.get("pos")
                    result["my_score"] = tdata.get("score")
        except Exception:
            pass

        if not result["my_team"] or not result["my_rank"]:
            try:
                r_user = self.session.get(f"{self.base_url}/api/v1/users/me", timeout=10)
                if r_user.status_code == 200:
                    udata = (r_user.json() or {}).get("data", {})
                    if udata:
                        result["my_user"] = udata.get("name")
                        if not result["my_rank"]:
                            result["my_rank"] = udata.get("place") or udata.get("pos")
                        if result["my_score"] is None:
                            result["my_score"] = udata.get("score")
            except Exception:
                pass

        # 2. Get full scoreboard
        try:
            r_sb = self.session.get(f"{self.base_url}/api/v1/scoreboard", timeout=15)
            if r_sb.status_code == 200:
                sb_data = (r_sb.json() or {}).get("data", [])
                result["total_teams"] = len(sb_data)
                standings = []
                for entry in sb_data:
                    pos = entry.get("pos") or entry.get("place")
                    name = entry.get("name") or entry.get("account_name")
                    score = entry.get("score")
                    account_id = entry.get("account_id")
                    
                    # If my_rank wasn't found via /me, check if name matches
                    if not result["my_rank"] and (
                        (result["my_team"] and name == result["my_team"]) or
                        (result["my_user"] and name == result["my_user"])
                    ):
                        result["my_rank"] = f"{pos}th" if pos else "-"
                        result["my_score"] = score

                    standings.append({
                        "pos": pos,
                        "name": name,
                        "score": score,
                        "account_id": account_id
                    })
                result["standings"] = standings
        except Exception as e:
            Logger.warning(f"Failed to fetch scoreboard from CTFd: {e}")

        return result


