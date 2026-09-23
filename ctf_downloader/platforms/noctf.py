import re
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

import requests

from ..utils.logger import Logger
from ..utils.sanitize import sanitize_filename
from .base import (
    BasePlatform,
    Challenge,
    EventTimes,
    SolveAttribution,
    Verdict,
    normalize_epoch_to_utc,
    safe_get,
)
from .registry import register


def probe_noctf_api(origin: str, session: Any, info: Any, done: set) -> bool:
    """Probe noCTF platform by detecting domain, HTML markers, or API endpoints."""
    if "noctf_api" in done:
        return False
    done.add("noctf_api")

    # Domain match
    if "k17ctf" in origin or "noctf" in origin:
        info.capabilities["scoreboard"] = True
        info.add_signal(f"URL khớp domain noCTF/k17ctf: {origin}")
        return True

    # Check HTML / title
    try:
        resp = safe_get(session, origin, timeout=5)
        if resp is not None and resp.status_code == 200:
            if "<title>noCTF</title>" in resp.text or "_app/immutable" in resp.text:
                info.capabilities["scoreboard"] = True
                info.add_signal(f"HTML marker noCTF (<title>noCTF</title>) tại {origin}")
                return True
    except Exception:
        pass

    return False


@register(
    "noctf",
    label="noCTF",
    throttle=2.0,
    html_markers=(
        "<title>noCTF</title>",
        "noctf",
    ),
    probes=(probe_noctf_api,),
    supports_container=False,
    supports_scoreboard=True,
)
class NoCTFPlatform(BasePlatform):
    """
    Adapter cho nền tảng noCTF (SvelteKit frontend + REST API backend).
    Tự động suy luận API URL theo domain convention (scoreboard.<domain> -> api-<domain>).
    """

    def __init__(self, base_url: str, session: requests.Session):
        super().__init__(base_url, session)
        self.ctf_info.platform_type = "noctf"
        self._last_verdict: Verdict = "unknown"

        parsed = urllib.parse.urlparse(self.base_url)
        netloc = parsed.netloc.lower()

        # Determine default API base URL
        if netloc.startswith("scoreboard."):
            domain_part = netloc[len("scoreboard."):]
            self.api_url = f"https://api-{domain_part}"
            self.ctf_info.title = domain_part.split(".")[0].replace("-", "_").upper()
        elif netloc.startswith("api-") or netloc.startswith("api."):
            self.api_url = self.base_url
            clean_host = netloc.split(".", 1)[-1]
            self.ctf_info.title = clean_host.split(".")[0].replace("-", "_").upper()
        else:
            self.api_url = self.base_url
            self.ctf_info.title = netloc.split(".")[0].replace("-", "_").upper()

        # Try dynamic discovery if base_url frontend contains api url in JS chunks
        self._discover_api_url()

        # Token extraction
        auth_hdr = self.session.headers.get("Authorization", "")
        self.token = ""
        if auth_hdr:
            if auth_hdr.lower().startswith("bearer "):
                self.token = auth_hdr[7:].strip()
            elif auth_hdr.lower().startswith("token "):
                self.token = auth_hdr[6:].strip()
            else:
                self.token = auth_hdr.strip()

    def _discover_api_url(self) -> None:
        """Scan frontend HTML/JS to discover backend api URL if available."""
        try:
            resp = self.session.get(self.base_url, timeout=5)
            if resp.status_code == 200:
                app_m = re.search(r'/_app/immutable/entry/app\.[^"\'\s>]+\.js', resp.text)
                if app_m:
                    app_url = urllib.parse.urljoin(self.base_url, app_m.group(0))
                    app_resp = self.session.get(app_url, timeout=5)
                    if app_resp.status_code == 200:
                        chunk_m = re.findall(r'\"(\.\./chunks/index\.svelte\.[^\"\'\s>]+\.js)\"', app_resp.text)
                        for c_rel in chunk_m:
                            chunk_url = urllib.parse.urljoin(app_url, c_rel)
                            chunk_resp = self.session.get(chunk_url, timeout=5)
                            if chunk_resp.status_code == 200:
                                api_m = re.search(r'(https://api[^\",\s\']+)', chunk_resp.text)
                                if api_m:
                                    discovered = api_m.group(1).rstrip("/")
                                    base_netloc = urllib.parse.urlparse(self.base_url).netloc.split(":")[0].lower()
                                    disc_netloc = urllib.parse.urlparse(discovered).netloc.split(":")[0].lower()
                                    base_parts = base_netloc.split(".")
                                    base_parent = ".".join(base_parts[-2:]) if len(base_parts) >= 2 else base_netloc
                                    if disc_netloc == base_netloc or disc_netloc.endswith("." + base_parent):
                                        Logger.info(f"Discovered verified noCTF API URL: {discovered}")
                                        self.api_url = discovered
                                        return
                                    else:
                                        Logger.warning(f"Ignored untrusted discovered noCTF API URL: {discovered}")
        except Exception:
            pass

    @property
    def auth_headers(self) -> Dict[str, str]:
        if not self.token:
            auth_hdr = self.session.headers.get("Authorization", "")
            if auth_hdr:
                if auth_hdr.lower().startswith("bearer "):
                    self.token = auth_hdr[7:].strip()
                else:
                    self.token = auth_hdr.strip()
        hdrs: Dict[str, str] = {}
        if self.token:
            hdrs["Authorization"] = f"Bearer {self.token}"
        parsed = urllib.parse.urlparse(self.base_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        hdrs["Origin"] = origin
        hdrs["Referer"] = f"{origin}/"
        return hdrs

    @property
    def last_verdict(self) -> Verdict:
        return self._last_verdict

    @last_verdict.setter
    def last_verdict(self, value: Verdict) -> None:
        self._last_verdict = value

    def authenticate(self) -> bool:
        """Kiểm tra xác thực bằng token Authorization: Bearer <token> qua /user/me và /team."""
        if not self.token:
            Logger.warning("Chưa cung cấp token xác thực cho noCTF.")
            return False

        headers = self.auth_headers
        try:
            resp = self.session.get(f"{self.api_url}/user/me", headers=headers, timeout=10)
            if resp.status_code == 200:
                data = resp.json().get("data", {})
                self.ctf_info.user_name = data.get("name")
                self.ctf_info.team_name = data.get("team_name")
                Logger.success(f"Xác thực thành công noCTF! User: {self.ctf_info.user_name} | Team: {self.ctf_info.team_name}")
                return True
            elif resp.status_code == 401:
                Logger.error("❌ Token xác thực noCTF không hợp lệ hoặc đã hết hạn (HTTP 401).")
                return False
        except Exception as exc:
            Logger.warning(f"Lỗi khi kiểm tra /user/me: {exc}")

        # Fallback to /challenges
        try:
            resp = self.session.get(f"{self.api_url}/challenges", headers=headers, timeout=10)
            if resp.status_code == 200:
                Logger.success("Xác thực thành công noCTF qua /challenges!")
                return True
        except Exception as exc:
            Logger.error(f"Lỗi khi kiểm tra xác thực noCTF: {exc}")

        return False


    def fetch_rules(self) -> str:
        """Lấy nội dung rules và flag format của giải."""
        try:
            resp = self.session.get(f"{self.api_url}/info", headers=self.auth_headers, timeout=5)
            if resp.status_code == 200:
                data = resp.json().get("data", {})
                if isinstance(data, dict) and data.get("rules"):
                    return str(data["rules"])
        except Exception:
            pass
        return ""

    def fetch_event_times(self) -> Optional[EventTimes]:
        try:
            resp = self.session.get(f"{self.api_url}/info", headers=self.auth_headers, timeout=5)
            if resp.status_code == 200:
                data = resp.json().get("data", {})
                if isinstance(data, dict):
                    start = data.get("start_time") or data.get("start")
                    end = data.get("end_time") or data.get("end")
                    if start or end:
                        return EventTimes(
                            start_utc=normalize_epoch_to_utc(start) if start else None,
                            end_utc=normalize_epoch_to_utc(end) if end else None,
                            confidence="high",
                            source=f"noctf:{self.ctf_info.title.lower() if self.ctf_info.title else 'api'}",
                        )
        except Exception:
            pass
        return None

    def fetch_challenges(self) -> List[Challenge]:
        """Tải toàn bộ challenges, category, difficulty và file attachments."""
        try:
            resp = self.session.get(f"{self.api_url}/challenges", headers=self.auth_headers, timeout=15)
            if resp.status_code != 200:
                Logger.error(f"Không tải được challenges từ {self.api_url}/challenges (HTTP {resp.status_code})")
                return []

            try:
                payload = resp.json()
            except Exception:
                payload = None

            if isinstance(payload, list):
                raw_challs = payload
            elif isinstance(payload, dict):
                body = payload.get("data")
                if isinstance(body, list):
                    raw_challs = body
                elif isinstance(body, dict):
                    raw_challs = body.get("challenges", [])
                else:
                    raw_challs = payload.get("challenges", [])
            else:
                raw_challs = []

            if not isinstance(raw_challs, list):
                raw_challs = []

            # Canonical categories mapping
            CATEGORY_MAP = {
                "crypto": "Crypto",
                "cryptography": "Crypto",
                "web": "Web",
                "pwn": "Pwn",
                "rev": "Rev",
                "reverse": "Rev",
                "forensics": "Forensics",
                "misc": "Misc",
                "osint": "Osint",
                "blockchain": "Blockchain",
                "hardware": "Hardware",
                "ai": "AI",
                "meta": "Misc",
                "sanity": "Misc",
            }

            challenges: List[Challenge] = []
            headers = self.auth_headers

            for item in raw_challs:
                if not isinstance(item, dict):
                    continue
                cid = item.get("id")
                name = item.get("title") or item.get("slug") or f"Challenge_{cid}"
                points = item.get("value", 0)
                solves = item.get("solve_count", 0)
                is_solved = bool(item.get("solved_by_me", False))
                tags_dict = item.get("tags") or {}

                # Determine category & tags
                categories_str = tags_dict.get("categories", "") if isinstance(tags_dict, dict) else ""
                cat_tokens = [c.strip().lower() for c in categories_str.split(",") if c.strip()]
                
                difficulty = tags_dict.get("difficulty", "") if isinstance(tags_dict, dict) else ""
                author = tags_dict.get("author") if isinstance(tags_dict, dict) else None

                tags = list(cat_tokens)
                if difficulty:
                    tags.append(difficulty)

                primary_cat = "Misc"
                # Find matching primary category from tokens
                matched = False
                for token in cat_tokens:
                    if token in CATEGORY_MAP:
                        primary_cat = CATEGORY_MAP[token]
                        matched = True
                        break
                if not matched and cat_tokens:
                    primary_cat = cat_tokens[0].capitalize()

                # Fetch detailed challenge info (/challenges/{id})
                description = ""
                hints: List[Dict[str, Any]] = []
                files_list: List[Tuple[str, str]] = []
                conn_info = None

                try:
                    detail_resp = self.session.get(f"{self.api_url}/challenges/{cid}", headers=headers, timeout=10)
                    if detail_resp.status_code == 200:
                        detail_data = detail_resp.json().get("data", {})
                        description = detail_data.get("description", "")
                        meta = detail_data.get("metadata", {}) or {}
                        
                        raw_files = meta.get("files", [])
                        for f in raw_files:
                            f_url = f.get("url")
                            f_name = f.get("filename") or "attachment"
                            if f_url:
                                full_url = self.get_full_file_url(f_url)
                                files_list.append((full_url, sanitize_filename(f_name)))

                        raw_hints = meta.get("hints", [])
                        for h in raw_hints:
                            if isinstance(h, dict):
                                hints.append(h)
                            elif isinstance(h, str):
                                hints.append({"content": h})
                except Exception as exc:
                    Logger.warning(f"Không lấy được chi tiết challenge {cid}: {exc}")

                # Extract connection info from description (e.g. `nc chal.secso.cc 2000`)
                if description:
                    m_conn = re.search(r"nc\s+([a-zA-Z0-9.-]+)\s+(\d+)", description)
                    if m_conn:
                        conn_info = f"{m_conn.group(1)}:{m_conn.group(2)}"
                    else:
                        m_code = re.search(r"Connection command:\s*`([^`]+)`", description)
                        if m_code:
                            conn_info = m_code.group(1).strip()

                chall_obj = Challenge(
                    id=cid,
                    name=name,
                    category=primary_cat,
                    points=points,
                    description=description,
                    author=author,
                    tags=tags,
                    hints=hints,
                    files=files_list,
                    connection_info=conn_info,
                    solved_by_me=is_solved,
                    solves_count=solves,
                    raw_data=item,
                )
                challenges.append(chall_obj)

            self.ctf_info.challenges = challenges
            Logger.info(f"Đã tải {len(challenges)} challenges từ noCTF.")
            return challenges

        except Exception as exc:
            Logger.error(f"Lỗi khi tải challenges noCTF: {exc}")
            return []

    def get_full_file_url(self, file_path: str) -> str:
        if not file_path:
            return ""
        if file_path.startswith("http://") or file_path.startswith("https://"):
            return file_path
        return urllib.parse.urljoin(f"{self.api_url}/", file_path)

    def fetch_solve_attribution(self, challenge_ids: List[Any]) -> Dict[Any, SolveAttribution]:
        """Lấy thông tin challenge đã được giải từ noCTF."""
        wanted = {str(c): c for c in (challenge_ids or [])}
        result: Dict[Any, SolveAttribution] = {}
        try:
            resp = self.session.get(f"{self.api_url}/challenges", headers=self.auth_headers, timeout=10)
            if resp.status_code == 200:
                data = resp.json().get("data", {})
                raw_challs = data.get("challenges", [])
                for item in raw_challs:
                    cid = str(item.get("id"))
                    if cid in wanted:
                        is_solved = bool(item.get("solved_by_me", False))
                        if is_solved:
                            orig_id = wanted[cid]
                            result[orig_id] = SolveAttribution(by_me=True, by_team=True)
        except Exception as exc:
            Logger.warning(f"Lỗi khi lấy solve attribution từ noCTF: {exc}")
        return result

    def submit_flag(self, challenge_id: Any, flag: str) -> Tuple[bool, str]:
        """Nộp flag lên /challenges/{id}/solves."""
        url = f"{self.api_url}/challenges/{challenge_id}/solves"
        payload = {"data": flag.strip()}

        try:
            resp = self.session.post(url, json=payload, headers=self.auth_headers, timeout=15)
            data = resp.json() if "json" in resp.headers.get("content-type", "") else {}

            if resp.status_code == 200:
                sol_data = data.get("data", {})
                status = sol_data.get("status")
                if status == "correct":
                    self.last_verdict = "correct"
                    return True, "🎉 Flag chính xác! Chúc mừng bạn đã giải quyết challenge!"
                elif status == "incorrect":
                    self.last_verdict = "incorrect"
                    return False, "❌ Flag không đúng."
                elif status == "already_solved":
                    self.last_verdict = "already_solved"
                    return False, "ℹ️ Challenge này đã được giải trước đó."
                else:
                    self.last_verdict = "unknown"
                    return False, f"Trạng thái không rõ: {status}"
            elif resp.status_code == 429:
                self.last_verdict = "ratelimited"
                return False, "⏳ Rate limit: Bạn đang submit quá nhanh, vui lòng thử lại sau."
            elif resp.status_code == 401:
                self.last_verdict = "auth_failed"
                return False, "❌ Token xác thực không hợp lệ hoặc đã hết hạn."
            else:
                self.last_verdict = "unknown"
                err_msg = data.get("error", {}).get("message") if isinstance(data.get("error"), dict) else (data.get("message") or resp.text[:100])
                return False, f"Máy chủ phản hồi HTTP {resp.status_code}: {err_msg}"
        except Exception as exc:
            self.last_verdict = "unknown"
            return False, f"Lỗi kết nối khi nộp flag: {exc}"

    def fetch_scoreboard(self, if_none_match: Optional[str] = None) -> Dict[str, Any]:
        """Tải bảng xếp hạng từ /scoreboard/divisions/{div_id}."""
        result: Dict[str, Any] = {
            "title": f"{self.ctf_info.title} Scoreboard",
            "my_team": self.ctf_info.team_name,
            "my_user": self.ctf_info.user_name,
            "my_rank": None,
            "my_score": None,
            "total_teams": 0,
            "standings": [],
        }
        try:
            div_id = 1
            user_resp = self.session.get(f"{self.api_url}/user/me", headers=self.auth_headers, timeout=5)
            if user_resp.status_code == 200:
                user_data = user_resp.json().get("data", {})
                if user_data.get("division_id"):
                    div_id = user_data["division_id"]

            resp = self.session.get(f"{self.api_url}/scoreboard/divisions/{div_id}", headers=self.auth_headers, timeout=15)
            if resp.status_code == 200:
                data = resp.json().get("data", {})
                entries = data.get("entries", [])
                standings = []
                for entry in entries:
                    t_id = entry.get("team_id")
                    rank = entry.get("rank")
                    score = entry.get("score", 0)
                    t_name = f"Team_{t_id}"
                    standings.append({
                        "pos": rank,
                        "team": t_name,
                        "score": score,
                    })
                    if self.ctf_info.team_name and t_name == self.ctf_info.team_name:
                        result["my_rank"] = rank
                        result["my_score"] = score
                result["standings"] = standings
                result["total_teams"] = len(entries)
        except Exception as exc:
            Logger.warning(f"Lỗi khi lấy scoreboard noCTF: {exc}")

        return result
