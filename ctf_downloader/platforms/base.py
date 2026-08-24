from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional, Tuple

from ..models import Challenge, CTFInfo, Verdict  # noqa: F401

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

