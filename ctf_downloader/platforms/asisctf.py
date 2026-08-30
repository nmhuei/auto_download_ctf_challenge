import re
import time
import urllib.parse
import requests
from typing import List, Dict, Any, Optional, Tuple
from bs4 import BeautifulSoup
from rich.markup import escape
from .base import (
    BasePlatform, Challenge, CTFInfo, EventTimes, Verdict,
    SolveAttribution, epoch_ms, safe_get_json
)
from ..extractors.link_extractor import LinkExtractor
from ..utils.logger import Logger
from .registry import register


def probe_asisctf_challs(origin: str, session, info, done: set) -> bool:
    """Probe /challenges/list endpoint for ASIS CTF / Laravel CTF platform."""
    if "asisctf_challs" in done:
        return False
    done.add("asisctf_challs")
    headers = {
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest"
    }
    try:
        resp = session.get(f"{origin}/challenges/list", headers=headers, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            # Empty challenge lists are valid before publication/after event
            # cleanup. The endpoint itself is ASIS-specific enough to remain a
            # useful probe; non-empty lists get an additional shape check.
            if isinstance(data, list) and (
                not data or (
                    isinstance(data[0], dict)
                    and "id" in data[0]
                    and "name" in data[0]
                )
            ):
                info.capabilities["scoreboard"] = True
                info.add_signal(
                    f"GET /challenges/list -> ASIS CTF challenges list "
                    f"({len(data)} challenges)"
                )
                return True
    except Exception:
        pass
    info.add_signal("GET /challenges/list -> không khớp ASIS CTF")
    return False


@register("asisctf", label="ASIS CTF", throttle=2.0,
          html_markers=("asisctf", "asis ctf", "alpineInstance()", "/challenges/answer", "/challenges/list"),
          cookie_hints=("asis_ctf_quals_2026_session", "asis_ctf_session", "remember_web"),
          probes=(probe_asisctf_challs,),
          supports_scoreboard=True)
class ASISCTFPlatform(BasePlatform):
    """
    Platform adapter for ASIS CTF (Laravel + Alpine.js CTF platform).
    Endpoints:
      - /challenges/list
      - /challenges/answer
      - /scoreboard
      - /dashboard
      - /rules
    """
    SOLVE_ATTR_TTL: float = 300.0
    _last_verdict: Verdict = "unknown"

    @property
    def last_verdict(self) -> Verdict:
        return self._last_verdict

    @last_verdict.setter
    def last_verdict(self, value: Verdict) -> None:
        self._last_verdict = value

    def __init__(self, base_url: str, session: requests.Session):
        super().__init__(base_url, session)
        self.ctf_info.platform_type = "asisctf"

    def _extract_meta(self) -> None:
        try:
            h_resp = self.session.get(self.base_url, timeout=5)
            if h_resp.status_code == 200:
                soup = BeautifulSoup(h_resp.text, "html.parser")
                title_el = soup.find("title")
                if title_el and title_el.text:
                    self.ctf_info.title = title_el.text.strip().replace(" ", "_")
        except Exception:
            pass

        if not self.ctf_info.title or self.ctf_info.title == "CTF Competition":
            self.ctf_info.title = "ASIS_CTF_Quals_2026"

        try:
            d_resp = self.session.get(f"{self.base_url}/dashboard", timeout=5)
            if d_resp.status_code == 200:
                soup = BeautifulSoup(d_resp.text, "html.parser")
                text = d_resp.text

                # Extract Team Name from dashboard
                m_team = re.search(r'Team Profile.*?<div[^>]*>\s*([A-Za-z0-9_\-\.]+)\s*</div>', text, re.DOTALL | re.IGNORECASE)
                if m_team:
                    self.ctf_info.team_name = m_team.group(1).strip()
                else:
                    headings = [h.get_text(strip=True) for h in soup.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'div'])]
                    for idx, h in enumerate(headings):
                        if h == "Team Profile" and idx + 1 < len(headings):
                            cand = headings[idx + 1]
                            if cand and "Invitation Code" not in cand and "User Profile" not in cand:
                                self.ctf_info.team_name = cand
                                break

                # Extract User Name from dashboard
                m_user = re.search(r'User Profile.*?<div[^>]*>\s*([A-Za-z0-9_\-\.]+)\s*</div>', text, re.DOTALL | re.IGNORECASE)
                if m_user:
                    self.ctf_info.user_name = m_user.group(1).strip()
        except Exception:
            pass

    def authenticate(self) -> bool:
        self._extract_meta()
        headers = {
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest"
        }
        try:
            resp = self.session.get(f"{self.base_url}/challenges/list", headers=headers, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list):
                    user_str = self.ctf_info.user_name or "Authenticated"
                    team_str = f" (Team: {self.ctf_info.team_name})" if self.ctf_info.team_name else ""
                    Logger.success(f"Đã xác thực thành công ASIS CTF: [info]{escape(str(user_str))}{escape(str(team_str))}[/info]", markup=True)
                    return True
        except Exception as e:
            Logger.warning(f"Lỗi khi kiểm tra xác thực ASIS CTF: {e}")

        Logger.error("Xác thực thất bại trên ASIS CTF. Hãy kiểm tra lại Cookie/Session.")
        return False

    def fetch_challenges(self) -> List[Challenge]:
        headers = {
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest"
        }
        try:
            resp = self.session.get(f"{self.base_url}/challenges/list", headers=headers, timeout=15)
            if resp.status_code != 200:
                Logger.error(f"Không tải được challenges từ /challenges/list (HTTP {resp.status_code})")
                return []

            raw_challs = resp.json()
            if not isinstance(raw_challs, list):
                Logger.error("Dữ liệu challenges trả về không đúng định dạng list.")
                return []

            Logger.info(f"Tìm thấy {len(raw_challs)} challenges trên ASIS CTF.")
            challenges = []

            for item in raw_challs:
                if not isinstance(item, dict):
                    Logger.warning(
                        "ASIS CTF: bỏ qua challenge entry không phải object."
                    )
                    continue
                chall_id = item.get("id")
                name = str(item.get("name") or f"Challenge_{chall_id}")

                # Category
                cats = item.get("categories", [])
                if not isinstance(cats, list):
                    cats = []
                category = (
                    cats[0].get("name", "Misc")
                    if cats and isinstance(cats[0], dict) else "Misc"
                )
                tags = [
                    c.get("name") for c in cats
                    if isinstance(c, dict) and c.get("name")
                ]

                # Zero is a valid dynamic score; do not fall through merely
                # because it is falsy.
                raw_points = next(
                    (item.get(k) for k in (
                        "dynamic_points", "rewardable_dynamic_points", "points"
                    ) if item.get(k) is not None),
                    0,
                )
                try:
                    points = int(raw_points)
                except (TypeError, ValueError):
                    points = 0
                description = str(item.get("description") or "")
                is_solved = bool(item.get("SolvedByCurrentTeam", False))
                try:
                    solves = int(item.get("solves_count") or 0)
                except (TypeError, ValueError):
                    solves = 0

                # Extract links & files
                extracted_links = LinkExtractor.extract_links_and_files(description, self.base_url)
                files_list = []
                for l in extracted_links:
                    if l.is_downloadable or "/tasks/" in l.url:
                        url_fn = l.url.split("/")[-1].split("?")[0]
                        fn = url_fn if "." in url_fn else (l.filename_hint or url_fn)
                        files_list.append((self.get_full_file_url(l.url), fn))


                # Extract connection info
                conns = LinkExtractor.extract_connection_info(description)
                connection_str = None
                if conns:
                    connection_str = "\n".join(c.raw_command for c in conns if c.raw_command)


                chall_obj = Challenge(
                    id=chall_id,
                    name=name,
                    category=category,
                    points=points,
                    description=description,
                    tags=tags,
                    files=files_list,
                    connection_info=connection_str,
                    solved_by_me=is_solved,
                    solves_count=solves,
                    raw_data=item
                )
                challenges.append(chall_obj)

            self.ctf_info.challenges = challenges
            return challenges

        except Exception as e:
            Logger.error(f"Lỗi khi tải challenges ASIS CTF: {e}")
            return []

    def get_full_file_url(self, file_path: str) -> str:
        if file_path.startswith("http://") or file_path.startswith("https://"):
            return file_path
        return urllib.parse.urljoin(self.base_url, file_path)

    def submit_flag(self, challenge_id: Any, flag: str) -> Tuple[bool, str]:
        self.last_verdict = "unknown"
        try:
            numeric_id = int(challenge_id)
        except (TypeError, ValueError):
            self.last_verdict = "challenge_not_found"
            return False, f"Challenge ID ASIS không hợp lệ: {challenge_id!r}"

        url = f"{self.base_url}/challenges/answer"
        payload = {
            "id": numeric_id,
            "answer": flag.strip()
        }
        headers = {
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/json"
        }

        # Laravel CSRF token handling
        xsrf = self.session.cookies.get("XSRF-TOKEN")
        if xsrf:
            headers["X-XSRF-TOKEN"] = urllib.parse.unquote(xsrf)

        try:
            resp = self.session.post(url, json=payload, headers=headers, timeout=15)
            try:
                data = resp.json()
            except Exception:
                data = {}

            if resp.status_code == 200:
                self.last_verdict = "correct"
                return True, "🎉 Flag chính xác! Challenge đã giải thành công!"
            elif resp.status_code == 422:
                self.last_verdict = "incorrect"
                msg = "Flag không chính xác."
                errors = data.get("errors", {})
                if isinstance(errors, dict) and "answer" in errors:
                    msg = f"❌ {errors['answer'][0] if errors['answer'] else 'Wrong Answer'}"
                elif data.get("message"):
                    msg = f"❌ {data.get('message')}"
                return False, msg
            elif resp.status_code == 429:
                self.last_verdict = "ratelimited"
                return False, "⏳ Rate limit submit flag! Vui lòng thử lại sau."
            elif resp.status_code in (401, 403, 419):
                self.last_verdict = "auth_failed"
                return False, "🚫 Phiên xác thực hết hạn hoặc không có quyền submit."
            else:
                self.last_verdict = "unknown"
                return False, f"Máy chủ trả HTTP {resp.status_code}: {data.get('message') or resp.text[:100]}"

        except Exception as e:
            self.last_verdict = "unknown"
            return False, f"Ngoại lệ khi submit flag: {e}"

    def fetch_rules(self) -> Optional[str]:
        try:
            resp = self.session.get(f"{self.base_url}/rules", timeout=10)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")
                main_el = soup.find("main") or soup.find("body")
                if main_el:
                    return main_el.get_text("\n", strip=True)
        except Exception:
            pass
        return None

    def fetch_scoreboard(self, if_none_match: Optional[str] = None) -> Dict[str, Any]:
        result = {
            "title": self.ctf_info.title or "ASIS CTF Scoreboard",
            "my_team": self.ctf_info.team_name,
            "my_user": self.ctf_info.user_name,
            "my_rank": None,
            "my_score": None,
            "total_teams": 0,
            "standings": [],
            "_http_status": None,
            "_etag": None,
            "_retry_after": None,
            "_not_modified": False,
        }

        try:
            req_headers = {"If-None-Match": if_none_match} if if_none_match else {}
            resp = self.session.get(
                f"{self.base_url}/scoreboard",
                timeout=15,
                headers=req_headers,
            )
            result["_http_status"] = resp.status_code
            resp_headers = getattr(resp, "headers", None) or {}
            result["_etag"] = resp_headers.get("ETag") or if_none_match
            result["_retry_after"] = resp_headers.get("Retry-After")
            if resp.status_code == 304:
                result["_not_modified"] = True
                return result
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")
                table = soup.find("table")
                if table:
                    rows = table.find_all("tr")
                    standings = []
                    for idx, row in enumerate(rows[1:], start=1):
                        tds = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
                        if len(tds) >= 3:
                            pos_str = tds[0] or str(idx)
                            team_name = tds[1]
                            try:
                                pos: Any = int(pos_str)
                            except (TypeError, ValueError):
                                pos = pos_str or idx
                            try:
                                score = int(
                                    str(tds[2]).replace(",", "").replace(" ", "")
                                )
                            except (TypeError, ValueError):
                                score = 0

                            is_my_team = False
                            if "bg-theme-3" in row.get("class", []) or (self.ctf_info.team_name and team_name == self.ctf_info.team_name):
                                is_my_team = True
                                result["my_team"] = team_name
                                result["my_rank"] = pos
                                result["my_score"] = score

                            standings.append({
                                "pos": pos,
                                "name": team_name,
                                "team": team_name,
                                "score": score,
                                "is_me": is_my_team
                            })


                    result["total_teams"] = len(standings)
                    result["standings"] = standings
        except Exception as e:
            result["_error"] = f"{type(e).__name__}: {e}"
            Logger.warning(f"Lỗi khi tải scoreboard ASIS CTF: {e}")

        return result

    def fetch_solve_attribution(self, challenge_ids) -> Dict[Any, SolveAttribution]:
        """Return team-accurate solve attribution with a short session cache.

        ASIS exposes ``SolvedByCurrentTeam`` but not the individual user who
        submitted the solve. Therefore ``by_me`` must remain False; claiming
        the current profile as solver would fabricate information. When the
        current team appears in ``first_n_solves`` we can safely recover its
        solve time and first-blood status.
        """
        wanted = {str(c): c for c in (challenge_ids or [])}
        now = time.monotonic()
        cache = getattr(self, "_solve_attr_cache", None)
        cache_ts = getattr(self, "_solve_attr_ts", None)
        if (isinstance(cache, dict) and cache_ts is not None
                and now - float(cache_ts) < self.SOLVE_ATTR_TTL):
            return {orig: cache[k] for k, orig in wanted.items() if k in cache}

        fresh: Dict[str, SolveAttribution] = {}
        fetched_ok = False
        try:
            headers = {
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            }
            resp = self.session.get(
                f"{self.base_url}/challenges/list",
                headers=headers,
                timeout=15,
            )
            if resp.status_code == 200:
                raw_challs = resp.json()
                if isinstance(raw_challs, list):
                    fetched_ok = True
                    current_team = str(self.ctf_info.team_name or "").strip()
                    for item in raw_challs:
                        if not isinstance(item, dict) or item.get("id") is None:
                            continue
                        cid = str(item.get("id"))
                        if not bool(item.get("SolvedByCurrentTeam", False)):
                            continue

                        first_blood = False
                        solved_at = None
                        first_n = item.get("first_n_solves") or []
                        if isinstance(first_n, list) and current_team:
                            for idx, solve_entry in enumerate(first_n):
                                if not isinstance(solve_entry, dict):
                                    continue
                                if str(solve_entry.get("team_name") or "").strip() \
                                        != current_team:
                                    continue
                                first_blood = idx == 0
                                solved_at = epoch_ms(solve_entry.get("solved_at"))
                                break

                        fresh[cid] = SolveAttribution(
                            by_me=False,
                            by_team=True,
                            solver_names=[current_team] if current_team else [],
                            first_blood=first_blood,
                            solved_at=solved_at,
                        )
        except Exception as exc:
            Logger.warning(
                f"ASIS attribution refresh lỗi: {type(exc).__name__}: {exc}"
            )

        if fetched_ok:
            self._solve_attr_cache = fresh
            self._solve_attr_ts = now
            cache = fresh
        elif isinstance(cache, dict):
            # Solves only accumulate during a contest; stale cache is safer
            # than turning a transient network error into fake unsolved state.
            fresh = cache

        return {orig: fresh[k] for k, orig in wanted.items() if k in fresh}
