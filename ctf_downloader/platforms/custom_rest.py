import re
import urllib.parse
import requests
from typing import List, Dict, Any, Optional, Tuple
from bs4 import BeautifulSoup
from rich.markup import escape
from .base import BasePlatform, Challenge, CTFInfo
from ..utils.logger import Logger
from ..utils.sanitize import sanitize_filename
from .registry import register


@register("custom_rest", label="Custom REST / Next.js CTF", throttle=5.0)
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
        from ..utils.urlnorm import normalize_base_url
        super().__init__(normalize_base_url(base_url), session)
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
        Validates authentication via /api/auth/me, /api/me, or checks challenge list.
        """
        self._extract_title()

        # Check /api/auth/me
        try:
            resp = self.session.get(f"{self.base_url}/api/auth/me", timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                user_data = (
                    data.get("data", {}).get("user")
                    if isinstance(data, dict) and isinstance(data.get("data"), dict)
                    else (data.get("user") if isinstance(data, dict) else None)
                )
                if user_data:
                    username = user_data.get("username") or user_data.get("name") or user_data.get("email")
                    self.ctf_info.user_name = username
                    Logger.success(f"Đã xác thực User: [info]{escape(str(username))}[/info]", markup=True)
                    return True
        except Exception:
            pass

        # Check /api/me
        try:
            resp = self.session.get(f"{self.base_url}/api/me", timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict) and (data.get("logged_in") or data.get("user")):
                    user_data = data.get("user") or {}
                    username = (
                        user_data.get("username")
                        or user_data.get("name")
                        or user_data.get("email")
                        or user_data.get("bracket")
                    )
                    if username:
                        self.ctf_info.user_name = username
                    Logger.success(f"Đã xác thực User: [info]{escape(str(username or 'logged_in'))}[/info]", markup=True)
                    return True
        except Exception:
            pass

        # Check /api/challenges directly
        try:
            resp = self.session.get(f"{self.base_url}/api/challenges", timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                has_challs = False
                if isinstance(data, dict):
                    if data.get("success") and "challenges" in data.get("data", {}):
                        has_challs = True
                    elif "challenges" in data and isinstance(data["challenges"], list):
                        has_challs = True
                    elif isinstance(data.get("data"), list):
                        has_challs = True
                elif isinstance(data, list):
                    has_challs = True
                if has_challs:
                    Logger.info("Đã xác nhận truy cập public vào challenges trên nền tảng REST.")
                    return True
        except Exception:
            pass

        Logger.error("Xác thực thất bại trên nền tảng REST CTF.")
        return False

    def fetch_challenges(self) -> List[Challenge]:
        """
        Fetches all challenges via /api/challenges and detailed info via /api/challenges/{id}.
        """
        try:
            resp = self.session.get(f"{self.base_url}/api/challenges", timeout=15)
            if resp.status_code != 200:
                Logger.error(f"Không tải được challenges từ /api/challenges (HTTP {resp.status_code})")
                return []

            json_data = resp.json()
            raw_challs = []
            if isinstance(json_data, dict):
                if "challenges" in json_data and isinstance(json_data["challenges"], list):
                    raw_challs = json_data["challenges"]
                elif "data" in json_data:
                    data_field = json_data["data"]
                    if isinstance(data_field, dict) and "challenges" in data_field:
                        raw_challs = data_field["challenges"]
                    elif isinstance(data_field, list):
                        raw_challs = data_field
                elif json_data.get("success") is False:
                    Logger.error(f"Lỗi API: {json_data.get('error') or json_data.get('message')}")
                    return []
            elif isinstance(json_data, list):
                raw_challs = json_data

            if not isinstance(raw_challs, list):
                Logger.error("Không tìm thấy challenges trong response từ /api/challenges")
                return []

            if not raw_challs:
                Logger.info("Nền tảng REST CTF chưa có challenge nào (danh sách trống).")
                self.ctf_info.challenges = []
                return []

            Logger.info(f"Tìm thấy {len(raw_challs)} challenges trên nền tảng. Đang tải chi tiết...")

            detailed_challenges = []
            for item in raw_challs:
                if not isinstance(item, dict):
                    continue
                chall_id = item.get("id") or item.get("_id")
                name = item.get("title") or item.get("name", f"Challenge_{chall_id}")
                category = item.get("category", "Misc").strip().capitalize()
                points_raw = item.get("points") or item.get("maxPoints") or item.get("value") or 0
                try:
                    points = int(points_raw)
                except (ValueError, TypeError):
                    points = 0
                author = item.get("author")
                description = item.get("description") or item.get("prompt_text") or item.get("prompt_html") or ""
                tags = item.get("tags", [])
                is_solved = bool(item.get("isSolved") or item.get("solved") or item.get("solved_by_me"))
                solves = item.get("solves") or item.get("solves_count") or 0
                conn_info = item.get("target_url") or item.get("connection_info") or ""

                # Fetch detailed view if available
                detail_data = {}
                try:
                    det_resp = self.session.get(f"{self.base_url}/api/challenges/{chall_id}", timeout=10)
                    if det_resp.status_code == 200:
                        det_json = det_resp.json()
                        if isinstance(det_json, dict):
                            payload_data = det_json.get("data") if det_json.get("success") else None
                            if isinstance(payload_data, dict):
                                detail_data = (
                                    payload_data.get("challenge")
                                    if isinstance(payload_data.get("challenge"), dict)
                                    else payload_data
                                )
                            elif "challenge" in det_json and isinstance(det_json["challenge"], dict):
                                detail_data = det_json["challenge"]
                            elif "data" in det_json and isinstance(det_json["data"], dict):
                                detail_data = det_json["data"]
                            else:
                                detail_data = det_json
                            description = (
                                detail_data.get("description")
                                or detail_data.get("prompt_text")
                                or detail_data.get("prompt_html")
                                or description
                            )
                            if not conn_info and detail_data.get("target_url"):
                                conn_info = detail_data["target_url"]
                            elif not conn_info and detail_data.get("connection_info"):
                                conn_info = detail_data["connection_info"]
                except Exception:
                    pass

                # Parse files/attachments
                files_list = []
                raw_files = (
                    detail_data.get("files")
                    or detail_data.get("attachments")
                    or item.get("files")
                    or item.get("attachments")
                    or []
                )
                if isinstance(raw_files, list):
                    for f in raw_files:
                        if isinstance(f, str):
                            files_list.append((self.get_full_file_url(f), f.split("/")[-1]))
                        elif isinstance(f, dict):
                            f_url = f.get("url") or f.get("location") or f.get("path")
                            f_name = f.get("name") or (f_url.split("/")[-1] if f_url else "attachment")
                            if f_url:
                                files_list.append((self.get_full_file_url(f_url), f_name))

                hints_list = []
                raw_hints = detail_data.get("hints") or item.get("hints") or []
                if isinstance(raw_hints, list):
                    for h in raw_hints:
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
                    connection_info=conn_info or None,
                    solved_by_me=is_solved,
                    solves_count=solves,
                    raw_data=detail_data or item
                )
                detailed_challenges.append(chall_obj)

            self.ctf_info.challenges = detailed_challenges
            return detailed_challenges

        except Exception as e:
            Logger.error(f"Lỗi khi tải challenges REST CTF: {str(e)}")
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
                return True, "🎉 Flag chính xác! Đã giải xong challenge!"
            elif resp.status_code == 400:
                msg = data.get("message") or data.get("error") or "Flag không đúng."
                return False, f"❌ {msg}"
            elif resp.status_code == 403:
                msg = data.get("message") or data.get("error") or "Yêu cầu là thành viên team hoặc bị từ chối quyền."
                return False, f"🚫 {msg}"
            else:
                return False, f"Máy chủ trả HTTP {resp.status_code}: {data.get('message') or resp.text[:100]}"

        except Exception as e:
            return False, f"Ngoại lệ khi submit flag: {str(e)}"
