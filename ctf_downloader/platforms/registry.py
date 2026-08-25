"""
Phase 4 — Registry trung tâm của các platform adapter.

Mỗi platform tự đăng ký qua decorator `@register(...)` ngay trong module của mình;
registry là nguồn dữ liệu duy nhất cho:
  - danh sách platform hợp lệ (`capabilities.PLATFORM_TYPES`),
  - throttle giữa 2 lần submit (submitter đọc `spec.throttle`),
  - pipeline nhận diện nền tảng (detection.py đọc markers/cookie_hints/probes),
  - dựng platform instance theo tên (`get_spec(key).cls`).
"""

from dataclasses import dataclass
from typing import Callable, Dict, Tuple, Type


class UnknownPlatformError(ValueError):
    """Nhận diện/tra cứu một platform type không có trong registry."""


@dataclass(frozen=True)
class PlatformSpec:
    key: str                              # "gzctf"
    label: str                            # "GZ::CTF" (hiển thị dashboard/workspace)
    cls: Type                             # class adapter (con của BasePlatform)
    throttle: float = 5.0                 # giây nghỉ tối thiểu giữa 2 lần submit
                                          # (khớp DEFAULT_THROTTLE của submit_service)
    html_markers: Tuple[str, ...] = ()    # chuỗi nhận diện tầng 1 (HTML);
                                          # tiền tố "regex:" = mẫu regex trên HTML gốc
    cookie_hints: Tuple[str, ...] = ()    # tên cookie tầng 2
    probes: Tuple[Callable, ...] = ()     # hàm probe tầng 3 (origin, session, info, done) -> bool
    supports_container: bool = False
    supports_scoreboard: bool = False
    rules_via_api: bool = False


# Registry toàn cục: key -> PlatformSpec
PLATFORMS: Dict[str, PlatformSpec] = {}


def register(key=None, *, label=None, **kw) -> Callable[[Type], Type]:
    """
    Decorator đăng ký một platform class vào PLATFORMS và gán `cls.spec`.
    Ví dụ:
        @register("gzctf", label="GZ::CTF", throttle=2.0,
                  html_markers=("GZCTF",), cookie_hints=("GZCTF_Token",),
                  probes=(probe_api_config,), supports_container=True)
        class GZCTFPlatform(BasePlatform): ...
    """
    def deco(cls: Type) -> Type:
        resolved_key = key or cls.__name__.lower()
        spec = PlatformSpec(
            key=resolved_key,
            label=label or resolved_key,
            cls=cls,
            **kw,
        )
        PLATFORMS[resolved_key] = spec
        cls.spec = spec
        return cls
    return deco


def get_spec(key: str) -> PlatformSpec:
    """Tra PlatformSpec theo key. KeyError được chuyển thành UnknownPlatformError."""
    try:
        return PLATFORMS[key]
    except KeyError:
        raise UnknownPlatformError(
            f"Unknown platform type: {key!r}. Known platforms: {sorted(PLATFORMS)}"
        ) from None


def display_label(key: str, max_len: int = 10) -> str:
    """Nhãn platform an toàn cho UI — không bao giờ lộ key nội bộ ra màn hình
    (synthesis-v6 N2: ``custom_rest`` từng bị slice ``[:10]`` thành ``custom_res``).

    Trả ``spec.label`` đã đăng ký ('CTFd', 'GZ::CTF', 'rCTF', 'Custom REST /
    Next.js CTF'…); key lạ → chính key (literal trung tính). Nhãn dài hơn
    ``max_len`` được cắt tại biên từ ('Custom', không phải 'Custom RES') để
    không gãy giữa token.
    """
    k = str(key)
    if k not in PLATFORMS:
        return k          # key lạ: giữ nguyên để còn tra cứu được
    label = PLATFORMS[k].label
    if len(label) <= max_len:
        return label
    parts = label.split()
    head = parts[0] if parts else label
    return head if 0 < len(head) <= max_len else label[:max_len]


# ---------------------------------------------------------------------------
# Kích hoạt đăng ký: import các module platform để decorator chạy.
# KHÔNG xoá — nếu bỏ thì registry rỗng.
# ---------------------------------------------------------------------------
from . import ctfd, custom_rest, generic_html, gzctf, rctf  # noqa: E402,F401
