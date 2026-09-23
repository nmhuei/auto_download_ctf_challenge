"""Dynamic Configurable Platform Adapter.

Instantiates a fully functional BasePlatform adapter from a data-driven PlatformSchema.
Handles authentication, challenge parsing, flag submission, and scoreboard retrieval
based strictly on declarative JSON endpoint specifications and field mappings.
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

from bs4 import BeautifulSoup
from rich.markup import escape

from .base import BasePlatform, Challenge, CTFInfo, safe_get, safe_get_json
from .schema import PlatformSchema
from ..utils.logger import Logger
from ..utils.sanitize import sanitize_filename


def _resolve_json_path(data: Any, path: str) -> Any:
    """Navigate nested dictionary / list by dot-separated path (e.g. 'data.challenges')."""
    if not path or data is None:
        return data
    parts = path.split(".")
    curr = data
    for part in parts:
        if isinstance(curr, dict):
            curr = curr.get(part)
        elif isinstance(curr, list) and part.isdigit():
            idx = int(part)
            curr = curr[idx] if 0 <= idx < len(curr) else None
        else:
            return None
    return curr


class ConfigurablePlatform(BasePlatform):
    """Dynamic platform adapter driven completely by a PlatformSchema instance."""

    def __init__(self, base_url: str, session: Any, schema: PlatformSchema):
        super().__init__(base_url, session)
        self.schema = schema
        self.ctf_info.platform_type = schema.key
        self.ctf_info.title = schema.label

    def _extract_title(self) -> None:
        """Attempt to extract competition title from root HTML."""
        try:
            resp = safe_get(self.session, self.base_url, timeout=5)
            if resp is not None and getattr(resp, "status_code", None) == 200:
                soup = BeautifulSoup(getattr(resp, "text", ""), "html.parser")
                title_el = soup.find("title")
                if title_el and title_el.text:
                    clean_t = title_el.text.strip().split(" - ")[0].split(" | ")[0].strip()
                    if clean_t:
                        self.ctf_info.title = clean_t
        except Exception:
            pass

    def _build_url(self, endpoint_or_path: str) -> str:
        """Construct full target URL from endpoint template and base_url.

        Handles:
          - full URLs with same origin
          - origin-relative paths starting with '/' (e.g. /api/v1/challenges)
          - base-relative paths (e.g. api/v1/challenges)
        """
        if not endpoint_or_path:
            return self.base_url
        ep = str(endpoint_or_path).strip()
        if "://" in ep:
            parsed_ep = urllib.parse.urlparse(ep)
            parsed_base = urllib.parse.urlparse(self.base_url)
            if parsed_ep.netloc == parsed_base.netloc:
                return ep
            ep = parsed_ep.path + (("?" + parsed_ep.query) if parsed_ep.query else "")

        if ep.startswith("/"):
            return urllib.parse.urljoin(self.base_url, ep)
        base = self.base_url if self.base_url.endswith("/") else (self.base_url + "/")
        return urllib.parse.urljoin(base, ep)

    def authenticate(self) -> bool:
        """Validate authentication via configured auth endpoint or challenges list."""
        self._extract_title()
        endpoints = self.schema.endpoints

        # 1. Try dedicated auth endpoint if configured
        if endpoints.auth_check:
            url = self._build_url(endpoints.auth_check)
            data, status = safe_get_json(self.session, url, statuses=(200,))
            if status == 200 and data is not None:
                user_name = None
                if isinstance(data, dict):
                    user_data = data.get("user") or data.get("data", {}).get("user") or data
                    if isinstance(user_data, dict):
                        user_name = user_data.get("username") or user_data.get("name") or user_data.get("email")
                if user_name:
                    self.ctf_info.user_name = str(user_name)
                    Logger.success(f"Đã xác thực User: [info]{escape(str(user_name))}[/info] trên {self.schema.label}", markup=True)
                else:
                    Logger.info(f"Đã xác nhận phiên đăng nhập trên {self.schema.label}")
                return True

        # 2. Try challenges list endpoint as probe
        if endpoints.challenges:
            url = self._build_url(endpoints.challenges)
            data, status = safe_get_json(self.session, url, statuses=(200,))
            if status == 200 and data is not None:
                Logger.info(f"Đã xác nhận truy cập challenges trên {self.schema.label}")
                return True

        Logger.warning(f"Xác thực thất bại trên nền tảng {self.schema.label}.")
        return False

    def fetch_challenges(self) -> List[Challenge]:
        """Fetch and normalize challenges using schema mapping."""
        endpoints = self.schema.endpoints
        if not endpoints.challenges:
            Logger.warning(f"Nền tảng {self.schema.label} không cấu hình endpoint 'challenges'.")
            return []

        url = self._build_url(endpoints.challenges)
        timeout = int(self._timeout(15.0))
        data, status = safe_get_json(self.session, url, statuses=(200,))
        if status != 200 or data is None:
            Logger.error(f"Không tải được challenges từ {endpoints.challenges} (HTTP {status})")
            return []

        mapping = self.schema.schema_mapping
        raw_list = _resolve_json_path(data, mapping.challenges_root)
        if not isinstance(raw_list, list):
            Logger.error(f"Không tìm thấy danh sách challenges tại đường dẫn '{mapping.challenges_root}'")
            return []

        Logger.info(f"Tìm thấy {len(raw_list)} challenges trên {self.schema.label}. Đang chuẩn hoá...")
        challenges: List[Challenge] = []

        for item in raw_list:
            if not isinstance(item, dict):
                continue

            cid = item.get(mapping.id)
            if cid is None:
                continue

            name = str(item.get(mapping.name) or f"Challenge_{cid}")
            category = str(item.get(mapping.category) or "Misc").strip().capitalize()
            points_val = item.get(mapping.points)
            try:
                points = int(points_val) if points_val is not None else 0
            except (ValueError, TypeError):
                points = 0

            desc = str(item.get(mapping.description) or "")
            conn_info = str(item.get(mapping.connection_info) or "")
            solved_val = item.get(mapping.solved)
            solved_by_me = bool(solved_val) if solved_val is not None else False

            # Extract files
            files_raw = item.get(mapping.files) or []
            files_urls: List[str] = []
            if isinstance(files_raw, list):
                for f_item in files_raw:
                    if isinstance(f_item, str):
                        files_urls.append(self._build_url(f_item))
                    elif isinstance(f_item, dict):
                        f_url = f_item.get("url") or f_item.get("path") or f_item.get("link")
                        if f_url:
                            files_urls.append(self._build_url(str(f_url)))

            c = Challenge(
                id=cid,
                name=name,
                category=category,
                points=points,
                description=desc,
                connection_info=conn_info,
                files=files_urls,
                solved_by_me=solved_by_me,
            )
            challenges.append(c)

        return challenges

    def get_full_file_url(self, file_path: str) -> str:
        """Resolve relative attachment/file path to absolute URL."""
        if not file_path:
            return ""
        return urllib.parse.urljoin(self.base_url, file_path)

    def submit_flag(self, challenge_id: Any, flag: str) -> Tuple[bool, str]:
        """Submit a flag according to schema submit specification."""
        endpoints = self.schema.endpoints
        if not endpoints.submit:
            Logger.warning(f"Nền tảng {self.schema.label} không cấu hình endpoint 'submit'.")
            self.last_verdict = "unknown"
            return False, "Submit endpoint not configured"

        submit_spec = self.schema.submit_spec
        # Resolve URL: replace {id} or {challenge_id} placeholder
        sub_url_template = endpoints.submit
        quoted_id = urllib.parse.quote(str(challenge_id), safe="")
        sub_path = sub_url_template.replace("{id}", quoted_id).replace("{challenge_id}", quoted_id)
        target_url = self._build_url(sub_path)

        # Prepare payload
        payload = {submit_spec.flag_param: flag.strip()}
        if submit_spec.id_param and "{id}" not in sub_url_template and "{challenge_id}" not in sub_url_template:
            payload[submit_spec.id_param] = challenge_id

        timeout = int(self._timeout(15.0))
        method = (submit_spec.method or "POST").upper()
        try:
            if method == "POST":
                if submit_spec.content_type == "json":
                    resp = self.session.post(target_url, json=payload, timeout=timeout)
                else:
                    resp = self.session.post(target_url, data=payload, timeout=timeout)
            elif method == "PUT":
                if submit_spec.content_type == "json":
                    resp = self.session.put(target_url, json=payload, timeout=timeout)
                else:
                    resp = self.session.put(target_url, data=payload, timeout=timeout)
            else:
                resp = self.session.post(target_url, json=payload, timeout=timeout)
        except Exception as e:
            Logger.error(f"Lỗi kết nối khi nộp flag lên {self.schema.label}: {e}")
            self.last_verdict = "error"
            return False, f"Connection error: {e}"

        if resp.status_code == 401 or resp.status_code == 403:
            Logger.error(f"Phiên đăng nhập hết hạn hoặc không có quyền nộp flag (HTTP {resp.status_code}).")
            self.last_verdict = "unauthorized"
            return False, "Unauthorized"

        try:
            res_json = resp.json()
        except Exception:
            text_body = resp.text.lower()
            if any(kw in text_body for kw in ("correct", "success", "flag accepted", "solved", "valid")):
                self.last_verdict = "correct"
                Logger.success(f"Flag HỢP LỆ trên {self.schema.label}!")
                return True, "Flag accepted"
            self.last_verdict = "incorrect"
            Logger.warning(f"Flag không đúng ({self.schema.label}) hoặc phản hồi không rõ ràng.")
            return False, "Incorrect flag"

        # Check success indicators
        success = False
        succ_field = submit_spec.success_field
        expected_val = None
        op = "=="
        if "!=" in succ_field:
            succ_path, expected_val = succ_field.split("!=", 1)
            op = "!="
        elif "==" in succ_field:
            succ_path, expected_val = succ_field.split("==", 1)
            op = "=="
        elif ":" in succ_field:
            succ_path, expected_val = succ_field.split(":", 1)
            op = "=="
        elif "=" in succ_field:
            succ_path, expected_val = succ_field.split("=", 1)
            op = "=="
        else:
            succ_path = succ_field

        succ_val = _resolve_json_path(res_json, succ_path.strip())
        if expected_val is not None:
            clean_expected = expected_val.strip().lower()
            val_str = str(succ_val).strip().lower()
            matched = (val_str == clean_expected)
            try:
                matched = matched or (float(succ_val) == float(expected_val.strip()))
            except (ValueError, TypeError):
                pass
            success = matched if op == "==" else not matched
        elif isinstance(succ_val, bool):
            success = succ_val
        elif isinstance(succ_val, (int, float)):
            success = succ_val in (1, 200)
        elif isinstance(succ_val, str):
            success = succ_val.lower() in ("true", "correct", "success", "ok")
        elif resp.status_code in (200, 201):
            msg_str = str(res_json.get(submit_spec.message_field, "")).lower()
            if any(bad in msg_str for bad in ("incorrect", "wrong", "invalid", "failed", "error")):
                success = False
            else:
                success = any(kw in msg_str for kw in ("correct", "success", "flag accepted", "solved"))

        raw_msg = res_json.get(submit_spec.message_field)
        msg = str(raw_msg) if raw_msg is not None else ("Flag hợp lệ!" if success else "Flag không chính xác.")

        if success:
            self.last_verdict = "correct"
            Logger.success(f"Flag HỢP LỆ trên {self.schema.label}!")
            return True, msg
        else:
            self.last_verdict = "incorrect"
            Logger.warning(f"Flag không đúng ({self.schema.label}): {msg}")
            return False, msg

    def fetch_scoreboard(self, if_none_match: Optional[str] = None) -> Dict[str, Any]:
        """Fetch platform scoreboard standings conforming to BasePlatform signature."""
        if not self.schema.endpoints.scoreboard:
            return {"standings": [], "etag": None}
        target_url = self._build_url(self.schema.endpoints.scoreboard)
        headers = {}
        if if_none_match:
            headers["If-None-Match"] = if_none_match
        data, status = safe_get_json(self.session, target_url, headers=headers)
        if status == 304:
            return {"standings": [], "etag": if_none_match, "not_modified": True}
        if status == 200 and isinstance(data, list):
            return {"standings": data, "etag": None}
        elif status == 200 and isinstance(data, dict):
            for k in ("standings", "data", "scoreboard", "scores", "rows"):
                if isinstance(data.get(k), list):
                    res_dict = dict(data)
                    res_dict["standings"] = data[k]
                    res_dict["etag"] = None
                    return res_dict
            return {"standings": [], "etag": None, **data}
        return {"standings": [], "etag": None}
