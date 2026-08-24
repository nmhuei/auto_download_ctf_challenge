from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Tuple

Verdict = Literal["correct", "incorrect", "unknown", "ratelimited"]

@dataclass
class Challenge:
    id: Any
    name: str
    category: str
    points: int = 0
    description: str = ""
    author: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    hints: List[Dict[str, Any]] = field(default_factory=list)
    files: List[Tuple[str, str]] = field(default_factory=list)  # (url, filename)
    connection_info: Optional[str] = None
    state: str = "visible"
    solved_by_me: bool = False
    solves_count: Optional[int] = None
    submit_endpoint: Optional[str] = None
    instance_info: Dict[str, Any] = field(default_factory=dict)
    raw_data: Dict[str, Any] = field(default_factory=dict)

@dataclass
class CTFInfo:
    title: str = "CTF Competition"
    description: str = ""
    platform_type: str = "generic"
    url: str = ""
    user_name: Optional[str] = None
    team_name: Optional[str] = None
    game_id: Optional[Any] = None
    auth_type: Optional[str] = None
    challenges: List[Challenge] = field(default_factory=list)
