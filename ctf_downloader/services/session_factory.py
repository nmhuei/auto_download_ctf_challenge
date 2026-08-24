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
) -> requests.Session:
    """Tạo session đã cấu hình headers/cookies/retry — mọi module phải đi qua đây."""
    return _http_create_session(
        cookie=cookie,
        token=token,
        custom_headers=custom_headers,
        timeout=timeout,
    )


@contextmanager
def thread_local_sessions(master: requests.Session) -> Iterator[Callable[[], requests.Session]]:
    """Context manager cho đa luồng (Phase 5): mỗi worker thread gọi ``get()``
    để nhận một session riêng, copy cookies + headers từ session master."""
    local = threading.local()

    def get() -> requests.Session:
        sess = getattr(local, 'session', None)
        if sess is None:
            sess = create_session()
            sess.headers.update(master.headers)
            sess.cookies.update(master.cookies)
            local.session = sess
        return sess

    yield get
