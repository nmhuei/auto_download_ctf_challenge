"""ctf_downloader.services — service layer (session factory + auth).

Import LAZY qua PEP 562 (__getattr__ cấp module): chỉ nạp submodule khi
symbol thật sự được dùng — tránh kéo toàn bộ phụ thuộc của từng service vào
mọi entrypoint import ``ctf_downloader.services``. Tương thích ngược với
cả hai kiểu import cũ::

    from ctf_downloader.services import create_session, AuthService
    from ctf_downloader.services.auth_service import AuthService
"""

from importlib import import_module

_LAZY_EXPORTS = {
    "create_session": ".session_factory",
    "thread_local_sessions": ".session_factory",
    "AuthService": ".auth_service",
}

__all__ = [
    "create_session",
    "thread_local_sessions",
    "AuthService",
]


def __getattr__(name):
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}"
        )
    module = import_module(module_name, __name__)
    value = getattr(module, name)
    # Cache vào namespace module: các lần truy cập sau bỏ qua __getattr__
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(_LAZY_EXPORTS))
