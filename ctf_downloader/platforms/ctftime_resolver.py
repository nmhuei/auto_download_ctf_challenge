"""CTFtimeResolver — nhận diện giải trên CTFtime và lấy event window (spec event-window §3).

- requests thuần, KHÔNG thêm dependency. UA bắt buộc dạng
  ``ctf-downloader/1.0 (+contact)`` — UA mặc định requests/curl bị CTFtime chặn 403.
- ``fetch_window(days_back, days_ahead)``: GET /api/v1/events/?limit=200&start=&finish=
  (1 request, unix giây).
- ``resolve_event_times(title_hint, url_hint)``: fuzzy match ngưỡng 0.60
  (max(SequenceMatcher, 0.5*ratio + 0.7*jaccard_tokens)); URL-domain trùng khớp
  tuyệt đối → score 1.0; top1 - top2 < 0.15 → ambiguous (wizard hỏi user chọn,
  tối đa 5 ứng viên).
- KHÔNG tin tuyệt đối CTFtime (organizer có thể quên update finish sau extension)
  → confidence luôn "medium", platform server ưu tiên khi xung đột.
"""
from __future__ import annotations

import datetime as _dt
import re
import time
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests

from ..services.session_factory import create_session
from .base import EventTimes, normalize_epoch_to_utc

# UA bắt buộc có contact — UA mặc định của requests bị CTFtime chặn 403.
CTFTIME_USER_AGENT = "ctf-downloader/1.0 (+https://github.com/nmhuei/auto_download_ctf_challenge)"

MATCH_THRESHOLD = 0.60      # ngưỡng chấp nhận ứng viên
AMBIGUOUS_GAP = 0.15        # top1 - top2 < gap → multi-candidate cho wizard
MAX_CANDIDATES = 5          # wizard hiển thị tối đa 5 lựa chọn

# Stopword bỏ khi chuẩn hoá title (spec §3)
_TITLE_STOPWORDS = {"ctf", "quals", "finals", "final", "open", "online",
                    "season", "edition", "ctftime"}
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_NONALNUM_RE = re.compile(r"[^a-z0-9]+")


def normalize_title(title: str) -> str:
    """Lowercase, bỏ năm/punctuation/stopword → chuỗi token cách nhau 1 spaces."""
    s = (title or "").lower()
    s = _YEAR_RE.sub(" ", s)
    s = _NONALNUM_RE.sub(" ", s)
    tokens = [t for t in s.split() if t and t not in _TITLE_STOPWORDS]
    return " ".join(tokens)


def jaccard_tokens(a: str, b: str) -> float:
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def title_similarity(a: str, b: str) -> float:
    """similarity = max(SequenceMatcher, 0.5*ratio + 0.7*jaccard_tokens)."""
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return 0.0
    ratio = SequenceMatcher(None, na, nb).ratio()
    return max(ratio, 0.5 * ratio + 0.7 * jaccard_tokens(na, nb))


def url_domain(url: Optional[str]) -> Optional[str]:
    """Domain thường hoá từ URL: lowercase, bỏ 'www.'; None nếu không parse được."""
    if not url:
        return None
    try:
        netloc = urlparse(url if "//" in url else f"https://{url}").netloc.lower()
    except Exception:
        return None
    if not netloc:
        return None
    return netloc[4:] if netloc.startswith("www.") else netloc


class CTFtimeResolver:
    BASE_URL = "https://ctftime.org/api/v1"
    TIMEOUT = 15

    def __init__(self, session: Optional[requests.Session] = None,
                 user_agent: str = CTFTIME_USER_AGENT):
        # R5: mọi session đi qua create_session (retry/UA chuẩn) — hết
        # session raw; UA CTFtime vẫn ghi đè ngay sau đó.
        self.session = session or create_session(
            custom_headers={"User-Agent": user_agent})
        self.session.headers["User-Agent"] = user_agent

    # ------------------------------------------------------------------
    # HTTP helpers — KHÔNG BAO GIỜ raise
    # ------------------------------------------------------------------
    def _get_json(self, path: str, params: Optional[dict] = None):
        try:
            resp = self.session.get(f"{self.BASE_URL}{path}", params=params,
                                    timeout=self.TIMEOUT)
            if resp.status_code != 200:
                return None
            data = resp.json()
            return data if isinstance(data, list) else (
                data if isinstance(data, dict) else None)
        except Exception:
            return None

    def fetch_window(self, days_back: int = 7, days_ahead: int = 30) -> List[dict]:
        """Event đang/gần diễn ra trong [now-days_back, now+days_ahead].

        GET /api/v1/events/?limit=200&start=<unix>&finish=<unix> (1 request).
        Lỗi mạng/parse → [].
        """
        now = int(time.time())
        params = {
            "limit": 200,
            "start": now - days_back * 86400,
            "finish": now + days_ahead * 86400,
        }
        data = self._get_json("/events/", params=params)
        if isinstance(data, list):
            return [e for e in data if isinstance(e, dict)]
        if isinstance(data, dict) and isinstance(data.get("results"), list):
            return [e for e in data["results"] if isinstance(e, dict)]
        return []

    def get_event(self, ctftime_id: Any) -> Optional[dict]:
        """GET /api/v1/events/{id}/ — cache hit đi thẳng vào đây."""
        data = self._get_json(f"/events/{int(ctftime_id)}/")
        return data if isinstance(data, dict) else None

    # ------------------------------------------------------------------
    # Matching
    # ------------------------------------------------------------------
    @staticmethod
    def score_event(event: dict, title_hint: str,
                    url_hint: Optional[str]) -> float:
        score = title_similarity(title_hint, str(event.get("title") or ""))
        hint_dom = url_domain(url_hint)
        if hint_dom:
            for key in ("url", "website", "live_feed"):
                ev_dom = url_domain(event.get(key))
                if ev_dom and ev_dom == hint_dom:
                    return 1.0   # domain trùng khớp tuyệt đối
        return score

    def rank_candidates(self, title_hint: str, url_hint: Optional[str] = None,
                        events: Optional[List[dict]] = None) -> List[Tuple[float, dict]]:
        """Ứng viên có score >= MATCH_THRESHOLD, sort giảm dần (score, event)."""
        events = self.fetch_window() if events is None else events
        scored = [(self.score_event(e, title_hint, url_hint), e) for e in events]
        matched = [(s, e) for s, e in scored if s >= MATCH_THRESHOLD]
        matched.sort(key=lambda se: (-se[0], str(se[1].get("title") or "")))
        return matched[:MAX_CANDIDATES]

    @staticmethod
    def is_ambiguous(candidates: List[Tuple[float, dict]]) -> bool:
        """top-1 hơn top-2 < AMBIGUOUS_GAP → cần wizard hỏi user chọn."""
        if len(candidates) < 2:
            return False
        return (candidates[0][0] - candidates[1][0]) < AMBIGUOUS_GAP

    def resolve_event_times(
            self, title_hint: str, url_hint: Optional[str] = None,
            events: Optional[List[dict]] = None,
    ) -> Tuple[Optional[EventTimes], List[Tuple[float, dict]]]:
        """Resolve window từ CTFtime.

        Returns ``(event_times|None, candidates)``: khi có MỘT ứng viên rõ ràng
        → EventTimes(confidence="medium"); ambiguous → (None, candidates) để
        wizard hỏi user chọn rồi gọi ``event_times_from``.
        """
        candidates = self.rank_candidates(title_hint, url_hint, events=events)
        if not candidates or self.is_ambiguous(candidates):
            return None, candidates
        return self.event_times_from(candidates[0][1]), candidates

    @staticmethod
    def event_times_from(event: dict) -> Optional[EventTimes]:
        start = normalize_epoch_to_utc(event.get("start"))
        end = normalize_epoch_to_utc(event.get("finish"))
        if start is None and end is None:
            return None
        try:
            ctftime_id = int(event.get("id"))
        except (TypeError, ValueError):
            ctftime_id = None
        return EventTimes(
            start_utc=start, end_utc=end,
            confidence="medium",
            source=f"ctftime:{ctftime_id}" if ctftime_id is not None else "ctftime",
        )

    @staticmethod
    def manual_times(start_utc=None, end_utc=None) -> EventTimes:
        """Nguồn MANUAL — user cung cấp, confidence HIGH (override mọi nguồn)."""
        return EventTimes(start_utc=start_utc, end_utc=end_utc,
                          confidence="high", source="manual")
