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

        Ưu tiên: tham số CLI > global config auth map[key=os.path.abspath(workspace)].
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
        if abs_ws in auth_map:
            saved = auth_map[abs_ws]
            return saved.get('cookie'), token_arg or saved.get('token')
        return None, token_arg
