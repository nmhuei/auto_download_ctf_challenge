"""Xác thực tập trung: ưu tiên tham số CLI > auth map trong global config."""
import os
from typing import Optional, Tuple
from urllib.parse import urlparse

from ..storage.global_config import load_global_config


def auth_key(workspace: Optional[str], url: Optional[str] = None) -> Optional[str]:
    """Key CHUẨN của entry auth trong global config — quy ước duy nhất dùng
    chung bởi register (ghi, qua ``RegisterService._auth_key``) và read-side
    của module này. Open-code batch-3 (DEFERRED_TRIAGE #10): trước đây ghi
    và đọc mỗi bên một impl rời rạc → drift quy ước key giữa các lần sửa.

      - workspace là thư mục thật → đường dẫn tuyệt đối của nó;
      - ngược lại (workspace ảo/không truyền) → URL platform, bỏ dấu ``/``
        cuối để key ổn định giữa các kiểu ghi URL.

    Trả None khi không đủ dữ liệu suy ra key. Read-side KHÔNG được chỉ dựa
    vào helper này: phải qua :meth:`AuthService.lookup_auth_entry` để giữ
    compat cả hai quy ước key cũ trong dữ liệu user (không migration).
    """
    if workspace:
        abs_ws = os.path.abspath(str(workspace))
        if os.path.isdir(abs_ws):
            return abs_ws
    if url:
        return str(url).rstrip('/')
    return None


class AuthService:
    @staticmethod
    def resolve_cookie_arg(cookie_arg: Optional[str]) -> Optional[str]:
        """Resolve a literal cookie or a path to a cookie file.

        An existing file that cannot be read is an input error, not a cookie
        literal: raise a clear RuntimeError so callers never send the pathname
        itself as an HTTP Cookie header after an I/O failure.
        """
        if not cookie_arg:
            return cookie_arg
        if os.path.isfile(cookie_arg):
            try:
                with open(cookie_arg, 'r', encoding='utf-8') as f:
                    return f.read().strip()
            except OSError as exc:
                raise RuntimeError(
                    f"Không đọc được cookie file '{cookie_arg}': {exc}"
                ) from exc
        return cookie_arg

    @staticmethod
    def resolve(
        workspace: str,
        cookie_arg: Optional[str] = None,
        token_arg: Optional[str] = None,
    ) -> Tuple[Optional[str], Optional[str]]:
        """Trả về (cookie, token) cho workspace.

        Ưu tiên: tham số CLI > global config auth map[key=os.path.abspath(workspace)]
        > tra key URL chính xác của workspace (entry do register lưu khi
        --workspace không phải dir thật — R1/R3).
        Nếu cookie_arg trỏ tới file thật thì đọc nội dung file làm cookie.
        """
        if cookie_arg:
            return AuthService.resolve_cookie_arg(cookie_arg), token_arg

        cfg = load_global_config()
        saved = AuthService.lookup_auth_entry(workspace, cfg.get('auth', {}))
        if saved is not None:
            return saved.get('cookie'), token_arg or saved.get('token')
        return None, token_arg

    @classmethod
    def lookup_auth_entry(cls, workspace: str,
                          auth_map: dict) -> Optional[dict]:
        """Read-side helper chung (open-code batch-3): tra entry auth cho
        workspace theo CẢ HAI quy ước key hiện có — compat đầy đủ với dữ
        liệu user cũ, không migration:

          1. key workspace tuyệt đối — probe VÔ ĐIỀU KIỆN (không gate qua
             ``auth_key``): entry từng được ghi khi workspace VẪN là dir
             thật, dir có thể đã bị xoá sau đó (test_arch_phase3 đọc entry
             key abs cho workspace chưa tồn tại);
          2. URL-keyed qua :meth:`_url_keyed_entry` — exact + unique-host
             (R3), cho entry do register lưu khi --workspace không phải dir.
        """
        if not isinstance(auth_map, dict) or not auth_map:
            return None
        saved = auth_map.get(os.path.abspath(str(workspace)))
        if isinstance(saved, dict):
            return saved
        return cls._url_keyed_entry(workspace, auth_map)

    @staticmethod
    def _url_host(value) -> Optional[str]:
        """Host (netloc lowercase) của một URL; None nếu không parse được."""
        try:
            netloc = urlparse(str(value)).netloc.lower()
        except Exception:
            return None
        return netloc or None

    @classmethod
    def _url_keyed_entry(cls, workspace: str,
                         auth_map: dict) -> Optional[dict]:
        """Tra entry auth lưu dưới key URL (register với --workspace ảo).

        Thứ tự (R3 — exact TRƯỚC mọi heuristic, chống leak cookie chéo
        platform):

        1. EXACT: workspace là dir thật → platform URL của workspace;
           workspace là URL (vd ``ctf pull https://ctfB.com``) → chính nó.
           So khớp key nguyên văn + bản không dấu ``/`` cuối.
        2. Fallback cuối cùng: entry URL-keyed DUY NHẤT có HOST khớp host
           platform của workspace (vd khác scheme/dấu ``/``). Host khác →
           KHÔNG bao giờ mượn cookie — trả None thay vì gửi cookie sang
           host lạ.
        """
        if not isinstance(auth_map, dict) or not auth_map:
            return None

        def _exact(url: str) -> Optional[dict]:
            for key in (url, str(url).rstrip('/')):
                entry = auth_map.get(key)
                if isinstance(entry, dict):
                    return entry
            return None

        resolved_url = None
        if os.path.isdir(workspace):
            try:
                from ..storage.workspace_repo import WorkspaceRepo
                resolved_url = WorkspaceRepo(workspace).resolve_platform_url()
            except Exception:
                return None
            if not resolved_url:
                return None
            hit = _exact(resolved_url)
            if hit is not None:
                return hit
        elif str(workspace).startswith(('http://', 'https://')):
            hit = _exact(str(workspace))
            if hit is not None:
                return hit
            resolved_url = str(workspace)

        # Fallback: DUY NHẤT entry cùng host với platform của workspace.
        host = cls._url_host(resolved_url) if resolved_url else None
        if not host:
            return None     # không xác định được platform → không đoán mò
        matches = [v for k, v in auth_map.items()
                   if isinstance(k, str)
                   and k.startswith(('http://', 'https://'))
                   and isinstance(v, dict)
                   and cls._url_host(k) == host]
        return matches[0] if len(matches) == 1 else None
