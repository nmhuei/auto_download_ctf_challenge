"""Xác thực tập trung: ưu tiên tham số CLI > auth map trong global config."""
import os
from typing import Optional, Tuple

from ..storage.global_config import load_global_config


class AuthService:
    @staticmethod
    def resolve(
        workspace: str,
        cookie_arg: Optional[str] = None,
        token_arg: Optional[str] = None,
    ) -> Tuple[Optional[str], Optional[str]]:
        """Trả về (cookie, token) cho workspace.

        Ưu tiên: tham số CLI > global config auth map[key=os.path.abspath(workspace)]
        > fallback tra theo platform URL của workspace (entry do register lưu
        khi --workspace không phải dir thật — R1).
        Nếu cookie_arg trỏ tới file thật thì đọc nội dung file làm cookie.
        """
        if cookie_arg:
            if os.path.isfile(cookie_arg):
                with open(cookie_arg, 'r', encoding='utf-8') as f:
                    return f.read().strip(), token_arg
            return cookie_arg, token_arg

        abs_ws = os.path.abspath(workspace)
        cfg = load_global_config()
        auth_map = cfg.get('auth', {})
        saved = auth_map.get(abs_ws)
        if saved is None:
            saved = AuthService._url_keyed_entry(workspace, auth_map)
        if saved is not None:
            return saved.get('cookie'), token_arg or saved.get('token')
        return None, token_arg

    @staticmethod
    def _url_keyed_entry(workspace: str,
                         auth_map: dict) -> Optional[dict]:
        """R1 fallback: entry register lưu dưới key URL phải đọc lại được.

        - Workspace là dir thật -> tra đúng platform URL của workspace qua
          WorkspaceRepo.resolve_platform_url() (import muộn tránh vòng phụ thuộc).
        - Workspace không tồn tại trên đĩa (path ảo) -> không có cách tra URL
          chính xác; chấp nhận entry URL-keyed DUY NHẤT trong auth map.
        """
        if not isinstance(auth_map, dict) or not auth_map:
            return None
        url = None
        if os.path.isdir(workspace):
            try:
                from ..storage.workspace_repo import WorkspaceRepo
                url = WorkspaceRepo(workspace).resolve_platform_url()
            except Exception:
                return None
            if not url:
                return None
            for key in (url, str(url).rstrip('/')):
                entry = auth_map.get(key)
                if isinstance(entry, dict):
                    return entry
            return None
        url_entries = [v for k, v in auth_map.items()
                       if isinstance(k, str)
                       and k.startswith(('http://', 'https://'))
                       and isinstance(v, dict)]
        return url_entries[0] if len(url_entries) == 1 else None
