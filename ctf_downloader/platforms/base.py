from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime  # noqa: F401 — dùng trong type hint EventTimes
from typing import List, Dict, Any, Optional, Tuple

from ..models import Challenge, CTFInfo, Verdict  # noqa: F401


@dataclass
class SolveAttribution:
    """Ai đã giải một challenge (spec challenge-status-model §4)."""
    by_me: bool = False
    by_team: bool = False
    solver_names: list = field(default_factory=list)
    first_blood: bool = False
    solved_at: Optional[int] = None  # epoch-ms


@dataclass
class EventTimes:
    """Thời gian bắt đầu/kết thúc giải (spec event-window §3).

    Mọi datetime đều aware UTC; ``confidence`` ∈ high|medium|low;
    ``source`` vd "gzctf:/api/game/{id}" | "ctftime:{id}" | "manual".
    """
    start_utc: Optional["datetime"] = None
    end_utc: Optional["datetime"] = None
    confidence: str = "high"
    source: str = ""


def normalize_epoch_to_utc(value: Any) -> Optional["datetime"]:
    """Chuẩn hoá timestamp về ``datetime`` aware UTC (spec event-window §2).

    Phân biệt ms/giây bằng ĐỘ DÀI CHỮ SỐ (bẫy đơn vị GZCTF/rCTF=ms,
    CTFd=giây): >= 13 chữ số → ms, <= 11 → giây. Nhận cả ISO string.
    Giá trị ≤ 0 hoặc năm < 2000 → None (= "chưa đặt lịch").
    Không bao giờ raise.
    """
    import datetime as _dt

    if value is None:
        return None
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            num = float(value)
            if num <= 0:
                return None
            dt = (_dt.datetime.fromtimestamp(num / 1000.0, tz=_dt.timezone.utc)
                  if num >= 1e11
                  else _dt.datetime.fromtimestamp(num, tz=_dt.timezone.utc))
        else:
            s = str(value).strip()
            if not s or s.lower() == "null":
                return None
            if s.isdigit():
                return normalize_epoch_to_utc(int(s))
            iso = s.replace("Z", "+00:00")
            dt = _dt.datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_dt.timezone.utc)
    except Exception:
        return None
    if dt.year < 2000:
        return None
    return dt


def epoch_ms(value: Any) -> Optional[int]:
    """Chuyển timestamp (epoch giây/ms, ISO string) sang epoch-ms; None nếu lỗi."""
    import datetime as _dt

    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            num = float(value)
            return int(num if num > 1e11 else num * 1000)
        s = str(value).strip()
        if s.isdigit():
            num = float(s)
            return int(num if num > 1e11 else num * 1000)
        iso = s.replace("Z", "+00:00")
        dt = _dt.datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None



# --------------------------------------------------------------------------- #
# Tiện ích HTTP an toàn dùng chung (detector/probe đều gọi qua đây)
# --------------------------------------------------------------------------- #
def safe_get(session: Any, url: str, timeout: int = 5):
    """GET an toàn: trả response hoặc None (mọi exception bị nuốt)."""
    try:
        return session.get(url, timeout=timeout)
    except Exception:
        return None


def safe_get_json(session: Any, url: str, statuses=(200,)):
    """GET và parse JSON. Trả (data|None, status_code|None)."""
    resp = safe_get(session, url)
    status = getattr(resp, "status_code", None)
    if resp is None or status not in statuses:
        return None, status
    try:
        return resp.json(), status
    except Exception:
        return None, status

class BaseCTFPlatform(ABC):
    def __init__(self, base_url: str, session: Any):
        self.base_url = base_url.rstrip("/")
        self.session = session
        self.ctf_info = CTFInfo(url=self.base_url)
        # Verdict chuẩn hoá của lần submit gần nhất: correct | incorrect | unknown | ratelimited
        self.last_verdict: str = "unknown"

    @abstractmethod
    def authenticate(self) -> bool:
        """
        Validates authentication and tests connection.
        Returns True if successful, False otherwise.
        """
        pass

    @abstractmethod
    def fetch_challenges(self) -> List[Challenge]:
        """
        Fetches all available challenges, categories, descriptions, files, and hints.
        """
        pass

    @abstractmethod
    def get_full_file_url(self, file_path: str) -> str:
        """
        Converts a relative file path or attachment URL to a full URL.
        """
        pass

    @abstractmethod
    def submit_flag(self, challenge_id: Any, flag: str) -> Tuple[bool, str]:
        """
        Submits a flag for a given challenge ID.
        Returns (is_correct, message).
        """
        pass

    def fetch_rules(self) -> Optional[str]:
        """
        Fetches competition rules / flag-format description (raw text, HTML or markdown).
        Returns None if unavailable. Must never raise.
        """
        return None

    def fetch_event_times(self) -> Optional["EventTimes"]:
        """Thời gian bắt đầu/kết thúc giải (spec event-window §2-§3).

        Trả EventTimes hoặc None nếu platform không khai báo. KHÔNG BAO GIỜ
        raise — mọi lỗi HTTP/parse phải được nuốt bên trong.
        """
        return None

    def fetch_solve_attribution(self, challenge_ids) -> Dict[Any, "SolveAttribution"]:
        """
        Sync ai đã giải challenge nào từ server (spec §4).
        Trả về dict[cid, SolveAttribution]; default: rỗng (platform không hỗ trợ).
        Mọi exception phải được nuốt bên trong — KHÔNG BAO GIỜ raise.
        """
        return {}

    def start_instance(self, challenge_id: Any) -> Tuple[bool, Dict[str, Any]]:
        """
        Spawns/starts a dynamic container instance for the challenge.
        Returns (success, info_dict e.g. {'entry': 'host:port', 'time_left': 1800, 'message': '...'}).
        """
        return False, {"message": f"Instance management is not supported for {self.ctf_info.platform_type}"}

    def stop_instance(self, challenge_id: Any) -> Tuple[bool, str]:
        """
        Destroys/stops an active container instance.
        """
        return False, f"Instance management is not supported for {self.ctf_info.platform_type}"

    def extend_instance(self, challenge_id: Any) -> Tuple[bool, str]:
        """
        Extends the lifetime of an active container instance.
        """
        return False, f"Instance management is not supported for {self.ctf_info.platform_type}"

    def get_instance_status(self, challenge_id: Any) -> Dict[str, Any]:
        """
        Gets current status of container instance for challenge.
        """
        return {"status": "unsupported", "entry": None, "time_left": None}

    def fetch_scoreboard(self) -> Dict[str, Any]:
        """
        Fetches scoreboard and ranking standings from platform.
        Returns dict containing standings, my_rank, my_score, etc.
        """
        return {
            "title": "Scoreboard",
            "my_team": self.ctf_info.team_name,
            "my_user": self.ctf_info.user_name,
            "my_rank": None,
            "my_score": None,
            "total_teams": 0,
            "standings": []
        }

BasePlatform = BaseCTFPlatform

