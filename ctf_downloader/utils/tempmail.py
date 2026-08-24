"""Client mail.tm (public, free) cho auto-register: tạo mailbox tạm, lấy JWT,
poll thư xác nhận. requests thuần, timeout ngắn, KHÔNG dependency ngoài.

Fallback mọi lỗi -> TempMailError; caller phải báo user tự cung cấp --email.

API docs: https://docs.mail.tm
  GET  /domains           -> {"hydra:member": [{domain,...}]}
  POST /accounts          {address, password} -> account
  POST /token             {address, password} -> {token (JWT)}
  GET  /messages          (Bearer) -> {"hydra:member": [message]}
  GET  /messages/{id}     (Bearer) -> message đầy đủ (text, html)
"""
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from .logger import Logger

# Password mailbox tạm — mail.tm yêu cầu tối thiểu ~6 ký tự.
_MAILBOX_PASSWORD_ALPHABET = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class TempMailError(Exception):
    """Mọi lỗi của mail.tm (mạng, API, hết domain, parse...)."""


def _random_string(rng, length: int) -> str:
    return "".join(rng.choice(_MAILBOX_PASSWORD_ALPHABET) for _ in range(length))


class TempMailClient:
    """Client tối giản mail.tm. Mọi lỗi HTTP/parse đều raise TempMailError."""

    DEFAULT_BASE = "https://api.mail.tm"

    def __init__(self, session: Optional[requests.Session] = None,
                 base_url: str = DEFAULT_BASE, timeout: float = 10.0,
                 rng=None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._rng = rng  # None -> dùng random module mặc định
        if session is None:
            session = requests.Session()
        self.session = session
        # Mailbox đang quản lý (address, password, jwt)
        self.address: Optional[str] = None
        self.mail_password: Optional[str] = None
        self.token: Optional[str] = None

    # ------------------------------------------------------------------ #
    # Low-level
    # ------------------------------------------------------------------ #
    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"{self.base_url}{path}"
        try:
            resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
        except Exception as exc:
            raise TempMailError(f"mail.tm request lỗi ({method} {path}): {exc}") from exc
        if resp.status_code >= 400:
            raise TempMailError(
                f"mail.tm {method} {path} -> HTTP {resp.status_code}: "
                f"{resp.text[:120]}")
        return resp

    @staticmethod
    def _hydra_list(data: Any) -> List[Dict[str, Any]]:
        """mail.tm trả list trong 'hydra:member' (hoặc trực tiếp là list)."""
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            member = data.get("hydra:member")
            if isinstance(member, list):
                return member
        raise TempMailError("mail.tm: response không chứa hydra:member list")

    # ------------------------------------------------------------------ #
    # High-level flow
    # ------------------------------------------------------------------ #
    def get_domains(self) -> List[str]:
        """Danh sách domain công khai hiện có."""
        data = self._request("GET", "/domains").json()
        domains = []
        for item in self._hydra_list(data):
            dom = item.get("domain") or item.get("name")
            if dom and item.get("isActive", True):
                domains.append(str(dom))
        if not domains:
            raise TempMailError("mail.tm: không còn domain khả dụng")
        return domains

    def create_mailbox(self, local_hint: str = "ctf") -> Tuple[str, str, str]:
        """Tạo mailbox ngẫu nhiên. Trả về (address, password, jwt_token)."""
        import secrets as _secrets
        import random as _random

        rng = self._rng if self._rng is not None else _random.SystemRandom()
        last_err: Optional[Exception] = None
        for _attempt in range(3):  # trùng address hiếm gặp -> thử lại
            domain = self.get_domains()[0]
            local = f"{local_hint}{_random_string(_secrets, 10)}".lower()
            address = f"{local}@{domain}"
            password = _random_string(rng, 16)
            try:
                self._request("POST", "/accounts",
                              json={"address": address, "password": password})
                token = self._request(
                    "POST", "/token",
                    json={"address": address, "password": password}).json().get("token")
                if not token:
                    raise TempMailError("mail.tm /token không trả JWT")
                self.address, self.mail_password, self.token = address, password, token
                return address, password, token
            except TempMailError as exc:
                last_err = exc
        raise TempMailError(f"Không tạo được mailbox tạm sau 3 lần thử: {last_err}")

    def list_messages(self, token: Optional[str] = None) -> List[Dict[str, Any]]:
        tok = token or self.token
        if not tok:
            raise TempMailError("Chưa có JWT mailbox — gọi create_mailbox() trước.")
        data = self._request(
            "GET", "/messages?page=1",
            headers={"Authorization": f"Bearer {tok}"}).json()
        return self._hydra_list(data)

    def wait_for_message(self, predicate: Optional[Callable[[Dict], bool]] = None,
                         timeout_s: float = 120.0, interval: float = 5.0,
                         sleep_fn: Callable[[float], None] = time.sleep,
                         token: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Poll hộp thư đến khi có message khớp ``predicate`` hoặc hết giờ.

        Trả message dict hoặc None nếu timeout (KHÔNG raise khi timeout).
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                for msg in self.list_messages(token=token):
                    if predicate is None or predicate(msg):
                        return msg
            except TempMailError as exc:
                Logger.warning(f"tempmail poll lỗi (sẽ thử lại): {exc}")
            sleep_fn(interval)
        return None

    def get_message_content(self, msg_id: str,
                            token: Optional[str] = None) -> Dict[str, Any]:
        tok = token or self.token
        data = self._request(
            "GET", f"/messages/{msg_id}",
            headers={"Authorization": f"Bearer {tok}"}).json()
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------------ #
    # Tiện ích tìm link xác nhận email (CTFd /confirm/<token>)
    # ------------------------------------------------------------------ #
    CONFIRM_LINK_RE = re.compile(
        r"https?://[^\s\"'<>\\]+?/confirm/[A-Za-z0-9]+")

    @classmethod
    def find_confirm_link(cls, content: str) -> Optional[str]:
        match = cls.CONFIRM_LINK_RE.search(content or "")
        return match.group(0) if match else None

    def fetch_message_text(self, msg_id: str) -> str:
        """Text + html ghép lại để scan link xác nhận."""
        data = self.get_message_content(msg_id)
        parts = [str(data.get("text") or "")]
        for chunk in (data.get("html") or []):
            parts.append(str(chunk))
        return "\n".join(parts)
