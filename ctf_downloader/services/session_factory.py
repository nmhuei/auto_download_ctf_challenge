"""Điểm tạo requests.Session DUY NHẤT của hệ thống — wrap utils.http_client.create_session."""
import threading
from contextlib import contextmanager
from typing import Callable, Dict, Iterator, Optional, Union

import requests

from ..utils.http_client import create_session as _http_create_session


def create_session(
    cookie: Optional[Union[str, Dict[str, str]]] = None,
    token: Optional[str] = None,
    custom_headers: Optional[Dict[str, str]] = None,
    timeout: int = 30,
    base_url: Optional[str] = None,
    impersonate: str = "chrome",
    use_browser_impersonation: bool = False,
    cloudflare_fallback: bool = True,
) -> requests.Session:
    """Tạo session đã cấu hình headers/cookies/retry — mọi module phải đi qua đây."""
    return _http_create_session(
        cookie=cookie,
        token=token,
        custom_headers=custom_headers,
        timeout=timeout,
        base_url=base_url,
        impersonate=impersonate,
        use_browser_impersonation=use_browser_impersonation,
        cloudflare_fallback=cloudflare_fallback,
    )


@contextmanager
def thread_local_sessions(master: requests.Session) -> Iterator[Callable[[], requests.Session]]:
    """Context manager cho đa luồng (Phase 5): mỗi worker thread gọi ``get()``
    để nhận một session riêng, copy cookies + headers từ session master."""
    local = threading.local()

    def get() -> requests.Session:
        sess = getattr(local, 'session', None)
        if sess is None:
            sess = create_session(base_url=getattr(master, '_credential_origin', None))
            sess.headers.update(master.headers)
            sess.cookies.update(master.cookies)
            # If the main-thread platform probe already detected Cloudflare,
            # arm each worker with browser transport before its first request.
            # This keeps cf_clearance + TLS/UA fingerprint coherent and avoids
            # every download worker independently hitting a challenge first.
            if getattr(master, 'cloudflare_active', False) is True:
                activate = getattr(sess, '_activate_browser_transport', None)
                if callable(activate):
                    activate()
            local.session = sess
        return sess

    yield get
