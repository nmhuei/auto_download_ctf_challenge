"""Platform Reconnaissance and Schema Deduction Engine.

Autonomously probes unknown CTF platforms, analyzes HTTP responses,
discovers API endpoints (auth, challenges, scoreboard, submit), deduces JSON
schema mappings, and generates data-driven PlatformSchema definitions.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

from .base import safe_get, safe_get_json
from .schema import (
    PlatformEndpoints,
    PlatformSchema,
    PlatformSchemaMapping,
    PlatformSubmitSpec,
)
from .schema_store import PlatformSchemaStore
from ..utils.logger import Logger
from ..utils.urlnorm import parse_normalized


@dataclass
class ReconResult:
    """Findings from platform reconnaissance and candidate schema deduction."""
    url: str
    detected_title: str = ""
    confidence: str = "none"           # "high", "medium", "low", "none"
    html_markers: List[str] = field(default_factory=list)
    cookie_hints: List[str] = field(default_factory=list)
    endpoints_found: Dict[str, str] = field(default_factory=dict)
    schema_mapping: Dict[str, str] = field(default_factory=dict)
    candidate_schema: Optional[PlatformSchema] = None
    signals: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "url": self.url,
            "detected_title": self.detected_title,
            "confidence": self.confidence,
            "html_markers": list(self.html_markers),
            "cookie_hints": list(self.cookie_hints),
            "endpoints_found": dict(self.endpoints_found),
            "schema_mapping": dict(self.schema_mapping),
            "candidate_schema": self.candidate_schema.to_dict() if self.candidate_schema else None,
            "signals": list(self.signals),
        }


class PlatformReconEngine:
    """Engine to probe and deduce configuration of an unknown CTF platform."""

    # Candidate paths to probe for challenges
    CHALLENGE_PROBE_PATHS = (
        "/api/v1/challenges",
        "/api/challenges",
        "/api/v2/challenges",
        "/challenges.json",
        "/api/challs",
        "/api/ctf/challenges",
        "/api/game/challenges",
    )

    # Candidate paths to probe for auth/current user
    AUTH_PROBE_PATHS = (
        "/api/auth/me",
        "/api/v1/users/me",
        "/api/v1/user",
        "/api/v1/auth/me",
        "/api/user",
        "/api/me",
        "/api/profile",
        "/api/v1/profile",
    )

    # Candidate paths to probe for scoreboard
    SCOREBOARD_PROBE_PATHS = (
        "/api/v1/scoreboard",
        "/api/scoreboard",
        "/scoreboard.json",
        "/api/scores",
        "/api/v1/scores",
        "/api/leaderboard",
    )

    @classmethod
    def _find_challenges_root(cls, data: Any) -> Tuple[Optional[str], Optional[List[dict]]]:
        """Identify dot-separated path to the challenges list in response JSON."""
        if isinstance(data, list):
            dict_items = [x for x in data if isinstance(x, dict)]
            if dict_items:
                return "", dict_items
            return None, None

        if not isinstance(data, dict):
            return None, None

        # Direct common keys
        candidate_paths = [
            "data.challenges",
            "challenges",
            "data.items",
            "items",
            "data.results",
            "results",
            "data",
            "challs",
        ]

        for path in candidate_paths:
            parts = path.split(".")
            curr = data
            for p in parts:
                if isinstance(curr, dict):
                    curr = curr.get(p)
                else:
                    curr = None
                    break
            if isinstance(curr, list):
                dict_items = [x for x in curr if isinstance(x, dict)]
                if dict_items:
                    return path, dict_items

        # Recursive search for a list of challenge-like dicts
        for k, v in data.items():
            if isinstance(v, list) and v:
                dict_items = [x for x in v if isinstance(x, dict)]
                if dict_items:
                    # Check if items look like challenges (has name, id, or points)
                    sample = dict_items[0]
                    sample_keys = {str(sk).lower() for sk in sample.keys()}
                    if any(c in sample_keys for c in ("id", "name", "title", "points", "category")):
                        return k, dict_items
            elif isinstance(v, dict):
                sub_path, sub_items = cls._find_challenges_root(v)
                if sub_path is not None and sub_items:
                    return f"{k}.{sub_path}" if sub_path else k, sub_items

        return None, None

    @classmethod
    def _deduce_field_mapping(cls, sample: Dict[str, Any]) -> PlatformSchemaMapping:
        """Deduce schema mapping fields from a representative challenge JSON object."""
        mapping = PlatformSchemaMapping()
        keys_lower = {str(k).lower(): str(k) for k in sample.keys()}

        def pick_key(*candidates: str) -> Optional[str]:
            for c in candidates:
                if c.lower() in keys_lower:
                    return keys_lower[c.lower()]
            return None

        # ID
        cid = pick_key("id", "_id", "challenge_id", "chall_id", "uid")
        if cid:
            mapping.id = cid

        # Name
        name = pick_key("name", "title", "chall_name", "challenge_name", "label")
        if name:
            mapping.name = name

        # Category
        cat = pick_key("category", "type", "tag", "cat", "tags", "genre")
        if cat:
            mapping.category = cat

        # Points
        pts = pick_key("points", "value", "score", "weight", "initial", "reward")
        if pts:
            mapping.points = pts

        # Description
        desc = pick_key("description", "desc", "content", "body", "details", "info")
        if desc:
            mapping.description = desc

        # Files
        files = pick_key("files", "attachments", "downloads", "file", "links")
        if files:
            mapping.files = files

        # Solved
        solved = pick_key("solved", "is_solved", "solved_by_me", "completed", "has_solved")
        if solved:
            mapping.solved = solved

        # Connection Info
        conn = pick_key("connection_info", "connection", "host", "service", "nc", "port", "target")
        if conn:
            mapping.connection_info = conn

        # Solves count
        solves = pick_key("solves", "solve_count", "solves_count", "solvers", "solve_num")
        if solves:
            mapping.solves_count = solves

        return mapping

    @classmethod
    def probe_url(
        cls,
        base_url: str,
        session: Optional[Any] = None,
        custom_key: Optional[str] = None,
        custom_label: Optional[str] = None,
    ) -> ReconResult:
        parsed, origin, clean_base_url = parse_normalized(base_url)
        from ..services.session_factory import create_session
        sess = session or create_session(base_url=clean_base_url)

        result = ReconResult(url=clean_base_url)
        result.signals.append(f"Khởi động Reconnaissance cho target: {clean_base_url}")

        # 1. Probe root HTML
        root_resp = safe_get(sess, clean_base_url, timeout=6)
        if root_resp is not None and getattr(root_resp, "status_code", None) == 200:
            html = getattr(root_resp, "text", "") or ""
            soup = BeautifulSoup(html, "html.parser")

            # Title
            title_el = soup.find("title")
            if title_el and title_el.text:
                raw_title = title_el.text.strip().split(" - ")[0].split(" | ")[0].strip()
                result.detected_title = raw_title
                result.signals.append(f"Phát hiện tiêu đề HTML: '{raw_title}'")
                if raw_title:
                    result.html_markers.append(raw_title)

            # Meta tags
            for meta in soup.find_all("meta"):
                name = (meta.get("name") or meta.get("property") or "").lower()
                content = meta.get("content") or ""
                if name in ("generator", "author", "ctf-framework", "application-name") and content:
                    result.signals.append(f"Meta tag '{name}': {content}")
                    result.html_markers.append(content)

            # Script hints
            for script in soup.find_all("script", src=True):
                src = script["src"]
                for kw in ("app", "main", "bundle", "ctf", "next", "nuxt", "vite", "webpack"):
                    if kw in src.lower():
                        result.signals.append(f"Script bundle: {src}")
                        break

            # Cookies in jar
            try:
                jar_cookies = list(sess.cookies.keys())
                if jar_cookies:
                    result.cookie_hints.extend(jar_cookies)
                    result.signals.append(f"Cookies phát hiện: {jar_cookies}")
            except Exception:
                pass

        # 2. Probe API Endpoints
        # Check both the specific base URL (e.g. https://host/contest) and origin
        probe_roots = [clean_base_url.rstrip("/")]
        if origin != clean_base_url.rstrip("/"):
            probe_roots.append(origin)

        def _resolve_candidate_path(path: str, root: str) -> str:
            full = f"{root.rstrip('/')}{path if path.startswith('/') else ('/' + path)}"
            return urllib.parse.urlparse(full).path or "/"

        # 2a. Challenges
        sample_challenge: Optional[Dict[str, Any]] = None
        challenges_root_path = "data.challenges"

        for ch_path in cls.CHALLENGE_PROBE_PATHS:
            for root_url in probe_roots:
                test_url = f"{root_url}{ch_path}"
                data, status = safe_get_json(sess, test_url, statuses=(200, 401, 403))
                if status in (200, 401, 403):
                    result.signals.append(f"Probe {ch_path} -> HTTP {status}")
                if status == 200 and data is not None:
                    root_p, items = cls._find_challenges_root(data)
                    if items:
                        resolved_path = _resolve_candidate_path(ch_path, root_url)
                        result.endpoints_found["challenges"] = resolved_path
                        challenges_root_path = root_p if root_p is not None else ""
                        sample_challenge = items[0]
                        result.signals.append(
                            f"Xác nhận endpoint challenges: {resolved_path} (tìm thấy {len(items)} challenges, root='{challenges_root_path}')"
                        )
                        break
                elif status in (401, 403) and not result.endpoints_found.get("challenges"):
                    resolved_path = _resolve_candidate_path(ch_path, root_url)
                    result.endpoints_found["challenges"] = resolved_path
                    result.signals.append(f"Ứng viên endpoint challenges (yêu cầu auth): {resolved_path}")
            if sample_challenge:
                break

        # 2b. Auth / Current User
        for au_path in cls.AUTH_PROBE_PATHS:
            for root_url in probe_roots:
                test_url = f"{root_url}{au_path}"
                data, status = safe_get_json(sess, test_url, statuses=(200, 401, 403))
                if status in (200, 401, 403):
                    result.signals.append(f"Probe {au_path} -> HTTP {status}")
                if status == 200 and data is not None:
                    resolved_path = _resolve_candidate_path(au_path, root_url)
                    result.endpoints_found["auth_check"] = resolved_path
                    result.signals.append(f"Xác nhận endpoint auth: {resolved_path}")
                    break
                elif status in (401, 403) and not result.endpoints_found.get("auth_check"):
                    resolved_path = _resolve_candidate_path(au_path, root_url)
                    result.endpoints_found["auth_check"] = resolved_path
            if result.endpoints_found.get("auth_check") and status == 200:
                break

        # 2c. Scoreboard
        for sc_path in cls.SCOREBOARD_PROBE_PATHS:
            for root_url in probe_roots:
                test_url = f"{root_url}{sc_path}"
                data, status = safe_get_json(sess, test_url, statuses=(200, 401, 403))
                if status == 200 and data is not None:
                    resolved_path = _resolve_candidate_path(sc_path, root_url)
                    result.endpoints_found["scoreboard"] = resolved_path
                    result.signals.append(f"Xác nhận endpoint scoreboard: {resolved_path}")
                    break
            if result.endpoints_found.get("scoreboard"):
                break

        # 3. Deduce Mappings & Submit Spec
        schema_mapping = PlatformSchemaMapping(challenges_root=challenges_root_path)
        if sample_challenge:
            schema_mapping = cls._deduce_field_mapping(sample_challenge)
            schema_mapping.challenges_root = challenges_root_path
            result.schema_mapping = schema_mapping.to_dict()
            result.confidence = "high"
        elif result.endpoints_found.get("challenges"):
            result.confidence = "medium"
        elif result.detected_title or result.endpoints_found.get("auth_check"):
            result.confidence = "low"
        else:
            result.confidence = "none"

        # Submit endpoint guess
        submit_endpoint: Optional[str] = None
        if result.endpoints_found.get("challenges"):
            base_ch = result.endpoints_found["challenges"]
            if base_ch.endswith("/challenges"):
                submit_endpoint = f"{base_ch}/{{id}}/submit"
            elif "/v1/" in base_ch:
                submit_endpoint = "/api/v1/submit"
            else:
                submit_endpoint = "/api/submit"
            result.endpoints_found["submit"] = submit_endpoint

        # 4. Generate Candidate Schema
        domain_name = parsed.netloc.split(":")[0].replace(".", "_")
        try:
            p_val = parsed.port
            port_suffix = f"_{p_val}" if p_val and p_val not in (80, 443) else ""
        except (ValueError, TypeError):
            port_suffix = ""
        raw_key = (custom_key or f"recon_{domain_name}{port_suffix}").strip().lower()
        key_cand = re.sub(r"[^a-z0-9_-]", "_", raw_key).strip("_") or "custom_ctf"
        if not key_cand[0].isalnum():
            key_cand = f"ctf_{key_cand}"
        key = PlatformSchemaStore.validate_key(key_cand)

        label = (custom_label or result.detected_title or domain_name.capitalize()).strip()

        candidate = PlatformSchema(
            key=key,
            label=label,
            throttle=3.0,
            html_markers=sorted(set(result.html_markers)),
            cookie_hints=sorted(set(result.cookie_hints)),
            url_patterns=[parsed.netloc],
            endpoints=PlatformEndpoints(
                auth_check=result.endpoints_found.get("auth_check"),
                challenges=result.endpoints_found.get("challenges"),
                challenge_detail=f"{result.endpoints_found.get('challenges')}/{{id}}" if result.endpoints_found.get("challenges") else None,
                submit=submit_endpoint,
                scoreboard=result.endpoints_found.get("scoreboard"),
            ),
            schema_mapping=schema_mapping,
            submit_spec=PlatformSubmitSpec(
                method="POST",
                flag_param="flag",
                id_param="challenge_id" if submit_endpoint and "{id}" not in submit_endpoint else None,
                content_type="json",
                success_field="success",
                message_field="message",
            ),
            supports_scoreboard=bool(result.endpoints_found.get("scoreboard")),
            source="custom",
            description=f"Auto-recon schema deduced for {clean_base_url}",
        )

        result.candidate_schema = candidate
        return result

    @classmethod
    def save_recon_schema(
        cls,
        schema: PlatformSchema,
        scope: str = "global",
        workspace_path: Optional[str | Path] = None,
    ) -> Path:
        """Persist candidate schema and synchronize with runtime PLATFORMS registry."""
        saved_path = PlatformSchemaStore.save(schema, scope=scope, workspace_path=workspace_path)
        PlatformSchemaStore.sync_to_registry(workspace_path=workspace_path)
        Logger.success(f"Đã lưu platform schema '[info]{schema.key}[/info]' vào {saved_path}")
        return saved_path
