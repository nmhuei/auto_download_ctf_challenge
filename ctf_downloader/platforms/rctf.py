import re
import urllib.parse
import requests
from typing import List, Dict, Any, Optional, Tuple
from bs4 import BeautifulSoup
from .base import (BasePlatform, Challenge, CTFInfo, EventTimes,
                   SolveAttribution, epoch_ms, normalize_epoch_to_utc, safe_get_json)
from ..utils.logger import Logger
from .registry import register

# Envelope JSON đặc trưng của rCTF: {"kind": "...", "message": ..., "data": ...}
_RCTF_KIND_RE = re.compile(r"^(good|bad|unauth)")


def probe_rctf_challs(origin: str, session, info, done: set) -> bool:
    """/api/v1/challs -> envelope {kind, message, data}; badEndpoint cũng là dấu hiệu rCTF."""
    if "rctf_challs" in done:
        return False
    done.add("rctf_challs")
    data, status = safe_get_json(session, f"{origin}/api/v1/challs",
                                 statuses=(200, 401, 403))
    kind = data.get("kind") if isinstance(data, dict) else None
    if isinstance(kind, str) and _RCTF_KIND_RE.match(kind):
        info.capabilities["scoreboard"] = True
        info.add_signal(f"GET /api/v1/challs -> envelope rCTF kind={kind}")
        return True
    info.add_signal(f"GET /api/v1/challs -> không khớp rCTF (HTTP {status})")
    return False


@register("rctf", label="rCTF", throttle=5.0,
          html_markers=('name="rctf-config"', r'regex:"kind"\s*:\s*"'),
          cookie_hints=(),
          probes=(probe_rctf_challs,),
          supports_scoreboard=True)
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

    def fetch_rules(self) -> Optional[str]:
        """
        rCTF render rules client-side và không có endpoint public cho rules
        -> không thể fetch, trả về None.
        """
        return None

    def submit_flag(self, challenge_id: Any, flag: str) -> Tuple[bool, str]:
        """
        Submits a flag to rCTF platform (/api/v1/challs/{challenge_id}/submit).
        Cập nhật self.last_verdict: correct | incorrect | unknown | ratelimited.
        """
        url = f"{self.base_url}/api/v1/challs/{challenge_id}/submit"
        payload = {"flag": flag.strip()}

        self.last_verdict = "unknown"

        try:
            resp = self.session.post(url, json=payload, timeout=15)
            try:
                data = resp.json()
            except Exception:
                data = {}

            kind = data.get("kind", "")
            message = data.get("message", "")

            if kind == "goodFlag" or (resp.status_code == 200 and kind == "goodFlag"):
                self.last_verdict = "correct"
                return True, "🎉 Correct flag! Challenge solved!"
            elif kind == "alreadySolved":
                self.last_verdict = "correct"
                return True, "✅ You have already solved this challenge!"
            elif kind == "badFlag":
                self.last_verdict = "incorrect"
                return False, f"❌ Incorrect flag ({message or 'Bad Flag'})."
            elif kind == "badRateLimit" or resp.status_code == 429:
                self.last_verdict = "ratelimited"
                return False, f"⏳ Rate limited! {message or 'Please wait before submitting again.'}"
            elif kind == "badChallenge":
                self.last_verdict = "unknown"
                return False, f"⚠️ Challenge not found or unavailable ({message})."
            elif kind == "badToken":
                self.last_verdict = "unknown"
                return False, "🚫 Authentication expired or invalid token."
            else:
                if resp.status_code == 200:
                    self.last_verdict = "correct"
                    return True, f"✅ Submission received: {message or kind}"
                self.last_verdict = "unknown"
                return False, f"Server returned HTTP {resp.status_code}: {message or kind or resp.text[:100]}"

        except Exception as e:
            self.last_verdict = "unknown"
            return False, f"Exception during submission: {str(e)}"

    # ------------------------------------------------------------------
    # Solve attribution (spec §4) — 1 account = 1 team: by_team ≡ by_me
    # ------------------------------------------------------------------

    def fetch_solve_attribution(self, challenge_ids) -> Dict[Any, SolveAttribution]:
        """``users/me.solves[]`` có sẵn; ``challs/{id}/solves`` public bổ sung
        solver_names / first-blood (0–1+N requests)."""
        wanted = {str(c): c for c in (challenge_ids or [])}
        cache = getattr(self, "_solve_attr_cache", None)
        if cache is None:
            cache = self._solve_attr_cache = {}
            try:
                r_me = self.session.get(f"{self.base_url}/api/v1/users/me", timeout=15)
                if r_me.status_code == 200:
                    data = (r_me.json() or {}).get("data") or {}
                    me_name = data.get("name")
                    for s in data.get("solves") or []:
                        cid = (s.get("chalId") if s.get("chalId") is not None
                               else s.get("challengeId",
                                            s.get("chaId", s.get("id"))))
                        if cid is None:
                            continue
                        ts = epoch_ms(s.get("createdAt") or s.get("ts") or s.get("time"))
                        names = [me_name] if me_name else []
                        prev = cache.get(str(cid))
                        if prev is not None:
                            # Giữ mốc SỚM NHẤT giữa các lần solve ghi nhận được
                            if prev.solved_at is not None and ts is not None:
                                ts = min(prev.solved_at, ts)
                            elif ts is None:
                                ts = prev.solved_at
                        cache[str(cid)] = SolveAttribution(
                            by_me=True, by_team=True,
                            solver_names=names, solved_at=ts)

                # Public solves: solver_names đầy đủ + first-blood
                for key, attr in list(cache.items()):
                    if wanted and key not in wanted:
                        continue
                    try:
                        rc = self.session.get(
                            f"{self.base_url}/api/v1/challs/{key}/solves", timeout=10)
                        if rc.status_code != 200:
                            continue
                        rows = (rc.json() or {}).get("data") or []
                    except Exception:
                        continue
                    names, all_ts = [], []
                    for row in rows:
                        nm = None
                        if isinstance(row.get("user"), dict):
                            nm = row["user"].get("name")
                        nm = nm or row.get("userName") or row.get("name")
                        if nm:
                            names.append(nm)
                        t = epoch_ms(row.get("ts") or row.get("time") or row.get("createdAt"))
                        if t:
                            all_ts.append(t)
                    if names:
                        attr.solver_names = names
                    if all_ts:
                        earliest = min(all_ts)
                        attr.solved_at = attr.solved_at or earliest
                        if attr.by_me and attr.solved_at == earliest:
                            attr.first_blood = True
            except Exception:
                pass
        return {orig: cache[k] for k, orig in wanted.items() if k in cache}

    def fetch_scoreboard(self) -> Dict[str, Any]:
        """
        Fetches leaderboard standings from rCTF (/api/v1/leaderboard/now).
        """
        result = {
            "title": self.ctf_info.title or "rCTF Leaderboard",
            "my_team": self.ctf_info.team_name,
            "my_user": self.ctf_info.user_name,
            "my_rank": None,
            "my_score": None,
            "total_teams": 0,
            "standings": []
        }

        url = f"{self.base_url}/api/v1/leaderboard/now"
        limit, max_pages = 100, 10
        try:
            # rCTF schema bắt buộc query params limit & offset trên
            # /api/v1/leaderboard/now — thiếu → lỗi validation → standings rỗng.
            # limit=100 an toàn dưới maxLimit mặc định của rCTF.
            all_entries: List[dict] = []
            total: Optional[int] = None
            offset = 0
            # Phân trang: data.total > số dòng đã nhận → loop GET với
            # offset += limit; chặn tối đa 10 trang để tránh loop vô hạn.
            for _page in range(max_pages):
                resp = self.session.get(
                    url, params={"limit": limit, "offset": offset}, timeout=15)
                if resp.status_code != 200:
                    break
                data = resp.json() or {}
                data_field = data.get("data")
                if isinstance(data_field, dict):
                    page_rows = data_field.get("leaderboard", []) or []
                    total = data_field.get("total")
                else:
                    page_rows = data_field if isinstance(data_field, list) else []
                    total = None
                if not page_rows:
                    break
                all_entries.extend(page_rows)
                offset += limit
                # Đủ total rồi, hoặc server trả trang ngắn hơn limit (hết dữ liệu)
                if ((isinstance(total, int) and total <= len(all_entries))
                        or len(page_rows) < limit):
                    break

            result["total_teams"] = len(all_entries)
            standings = []
            for idx, entry in enumerate(all_entries, 1):
                name = entry.get("name")
                score = entry.get("score")
                pos = idx
                if (result["my_team"] and name == result["my_team"]) or (result["my_user"] and name == result["my_user"]):
                    result["my_rank"] = f"{pos}th"
                    result["my_score"] = score

                standings.append({
                    "pos": pos,
                    "name": name,
                    "score": score,
                    "raw": entry
                })
            result["standings"] = standings
        except Exception as e:
            Logger.warning(f"Failed to fetch leaderboard from rCTF: {e}")

        return result


    # ------------------------------------------------------------------
    # Event window (spec event-window §2): GET /api/v1/integrations/client/
    # config → startTime/endTime EPOCH MS (có thể vắng mặt); fallback
    # <meta name="rctf-config"> trong HTML trang chủ.
    # ------------------------------------------------------------------
    def fetch_event_times(self) -> Optional[EventTimes]:
        start = end = None
        confidence = "high"
        source = "rctf:/api/v1/integrations/client/config"

        # 1. Client config API
        try:
            resp = self.session.get(
                f"{self.base_url}/api/v1/integrations/client/config", timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                payload = data.get("data") if isinstance(data, dict) else None
                payload = payload if isinstance(payload, dict) else {}
                start = normalize_epoch_to_utc(payload.get("startTime"))
                end = normalize_epoch_to_utc(payload.get("endTime"))
        except Exception:
            pass

        # 2. Fallback meta tag <meta name="rctf-config" content='{"startTime":...}'>
        if start is None and end is None:
            try:
                resp = self.session.get(self.base_url, timeout=10)
                if resp.status_code == 200:
                    m = re.search(
                        r'<meta\s+name="rctf-config"\s+content="([^"]*)"',
                        resp.text, re.I)
                    if m:
                        import html as _html
                        import json as _json
                        cfg = _json.loads(_html.unescape(m.group(1)))
                        start = normalize_epoch_to_utc(cfg.get("startTime"))
                        end = normalize_epoch_to_utc(cfg.get("endTime"))
                        if start is not None or end is not None:
                            confidence = "medium"
                            source = "rctf:meta[rctf-config]"
            except Exception:
                pass

        if start is None and end is None:
            return None
        return EventTimes(start_utc=start, end_utc=end,
                          confidence=confidence, source=source)
