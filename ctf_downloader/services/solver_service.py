"""Local Agy worker pool for downloaded CTF challenges.

The scheduler deliberately owns no platform actions: it classifies files,
starts local workers and records progress.  Challenge-specific operational
files always live below ``script/``; the final reusable program belongs in
``solver/solve.py``.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Sequence

from ..storage.fileio import atomic_write_json, locked_update_json
from ..storage.workspace_repo import WorkspaceRepo, is_superseded
from ..utils.agy_resolver import resolve_agy_binary
from .prompt_builder import CategoryPromptBuilder


class SolverSelectionError(ValueError):
    """The user selected an invalid display ID."""


class SolverAlreadyRunning(RuntimeError):
    """Another scheduler currently owns this workspace."""


@dataclass(frozen=True)
class SolverEligibility:
    """Whether a challenge is safe to add to the automatic solver queue."""

    ready: bool
    reason: str
    local_flag: str | None = None


@dataclass(frozen=True)
class SolverJob:
    display_id: int
    challenge_id: object
    category: str
    name: str
    path: Path
    has_source: bool
    has_instance: bool
    is_solved: bool = False

    @property
    def script_dir(self) -> Path:
        return self.path / "script"

    @property
    def state_path(self) -> Path:
        return self.script_dir / "worker-state.json"

    @property
    def log_path(self) -> Path:
        return self.script_dir / "agy.log"

    @property
    def progress_path(self) -> Path:
        return self.script_dir / "progress.json"


_SOURCE_SUFFIXES = {
    ".c", ".cc", ".cpp", ".cs", ".go", ".h", ".hpp", ".java", ".js",
    ".lua", ".php", ".pl", ".py", ".rb", ".rs", ".sh", ".sol", ".ts",
    ".vue", ".yaml", ".yml",
    ".sage", ".asm", ".s", ".wasm", ".sql", ".html", ".htm",
    ".enc", ".bin", ".hex",
    ".txz", ".xz", ".tar", ".gz", ".tgz", ".tar.gz", ".tar.xz", ".zip",
    ".7z", ".rar", ".bz2", ".tbz2", ".zst",
    ".elf", ".exe", ".dll", ".so", ".dylib", ".out",
    ".pcap", ".pcapng", ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".bmp",
    ".wav", ".mp3", ".raw", ".img", ".iso", ".vmdk", ".apk", ".jar",
}
_SOURCE_NAMES = {
    "dockerfile", "makefile", "compose.yml", "docker-compose.yml",
    "ciphertext.txt", "output.txt", "cipher.txt", "flag.enc", "vuln", "challenge",
}
_PROGRESS_PREFIX = "@@CTF_PROGRESS@@"
_FILTER_RE = re.compile(
    r"(?:cannot|can't|unable to)\s+(?:help|assist|decompile|disassemble|reverse|exploit|generate functional exploit|create an exploit|weaponize)"
    r"|cyber.?safety|safety\s+polic(?:y|ies)|policy\s+restrict|"
    r"(?:request\s+was\s+)?blocked\s+by\s+.*?filters?|"
    r"cannot\s+(?:generate|provide)\s+(?:functional\s+)?(?:exploit|weaponized)",
    re.IGNORECASE,
)
_AGY_PRINT_TIMEOUT_RE = re.compile(r"\[agy\]\s+print timeout after", re.IGNORECASE)
_QUOTA_RE = re.compile(
    r"quota\s+(?:reached|exceeded)|rate\s+limit|too\s+many\s+requests|"
    r"individual\s+quota\s+reached",
    re.IGNORECASE,
)
_FLAG_RE = re.compile(r'\b[A-Za-z0-9_\-]{3,24}\{[^\s{}\'\"]{4,200}\}')


def _is_dummy_flag(candidate: str) -> bool:
    try:
        content = candidate.split("{", 1)[1].rstrip("}").strip().lower()
    except IndexError:
        return True
    if len(content) < 4:
        return True
    dummy_patterns = {
        "test", "dummy", "fake", "placeholder", "example", "sample",
        "flag", "flag_here", "your_flag_here", "xxx", "todo",
    }
    if content in dummy_patterns or content.startswith("test_"):
        return True
    return False


def _valid_local_flag(candidate: object) -> str | None:
    """Return a complete, non-placeholder flag token from durable state."""
    if not isinstance(candidate, str):
        return None
    match = _FLAG_RE.search(candidate.strip())
    if match and not _is_dummy_flag(match.group(0)):
        return match.group(0)
    return None


def _extract_flag_from_job(job: SolverJob, report: dict, log_content: str) -> str | None:
    # 1. Report explicit fields
    if isinstance(report, dict):
        raw = report.get("candidate_flag") or report.get("flag")
        if isinstance(raw, str) and raw.strip():
            m = _FLAG_RE.search(raw.strip())
            if m and not _is_dummy_flag(m.group(0)):
                return m.group(0)

    # 2. Local flag.txt files (top priority for solver outputs)
    flag_candidates = [
        job.path / "flag.txt",
        job.path / "solver" / "flag.txt",
        job.script_dir / "flag.txt",
    ]
    for p in flag_candidates:
        if p.is_file():
            try:
                txt = p.read_text(encoding="utf-8", errors="replace").strip()
                m = _FLAG_RE.search(txt)
                if m and not _is_dummy_flag(m.group(0)):
                    return m.group(0)
            except OSError:
                pass

    # 3. Report summary / notes
    if isinstance(report, dict):
        for field in ("summary", "notes", "description"):
            val = str(report.get(field) or "")
            m = _FLAG_RE.search(val)
            if m and not _is_dummy_flag(m.group(0)):
                return m.group(0)

    # 4. Reverse search in log content (filtering dummies, prefer latest match)
    if log_content:
        matches = _FLAG_RE.findall(log_content)
        for m in reversed(matches):
            if not _is_dummy_flag(m):
                return m

    return None


def get_pid_start_ticks(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        rparen = content.rfind(")")
        if rparen == -1:
            return None
        fields = content[rparen + 2:].split()
        return int(fields[19])
    except (OSError, ValueError, IndexError):
        return None


def get_system_boot_id() -> str:
    try:
        p = Path("/proc/sys/kernel/random/boot_id")
        if p.exists():
            return p.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    return ""


class SolverService:
    """A single-process scheduler with per-challenge durable state."""

    def __init__(self, workspace: str | Path, *, timeout_seconds: int = 3600,
                 stale_seconds: float = 300):
        self.workspace = Path(workspace).resolve()
        self.repo = WorkspaceRepo(self.workspace)
        self.timeout_seconds = max(1, int(timeout_seconds))
        self.stale_seconds = max(0.01, float(stale_seconds))
        self.max_active_workers = 0
        self._activity_lock = threading.Lock()
        self._last_activity: dict[Path, float] = {}

    def _touch_activity(self, job: SolverJob) -> None:
        with self._activity_lock:
            self._last_activity[job.path] = time.monotonic()

    def _activity_at(self, job: SolverJob, fallback: float) -> float:
        with self._activity_lock:
            return self._last_activity.get(job.path, fallback)

    def scan(self, *, force_refresh: bool = False) -> list[SolverJob]:
        now = time.monotonic()
        cached = getattr(self, "_cached_jobs", None)
        cached_time = getattr(self, "_cached_jobs_time", 0.0)
        if not force_refresh and cached is not None and (now - cached_time < 1.5):
            return list(cached)
        rows: list[tuple[dict, Path]] = []
        for meta_path in self.repo.iter_challenges():
            meta = self.repo.read_metadata(meta_path)
            if not meta or is_superseded(meta):
                continue
            rows.append((meta, meta_path.parent))
        rows.sort(key=lambda row: (
            str(row[0].get("category") or "Other").casefold(),
            str(row[0].get("name") or row[0].get("id") or "").casefold(),
            str(row[0].get("id") or ""),
        ))
        jobs: list[SolverJob] = []
        for display_id, (meta, path) in enumerate(rows, start=1):
            solve_state = self.repo.read_status(path / "metadata.json", meta=meta).get("solve")
            is_solved = bool(meta.get("solved_by_me")) or solve_state in {
                "solved_by_me", "solved_by_team", "solved_other",
            }
            jobs.append(SolverJob(
                display_id=display_id,
                challenge_id=meta.get("id"),
                category=str(meta.get("category") or "Other"),
                name=str(meta.get("name") or meta.get("id") or path.name),
                path=path,
                has_source=self._has_source(path),
                has_instance=self._has_instance(meta),
                is_solved=is_solved,
            ))
        self._cached_jobs = list(jobs)
        self._cached_jobs_time = now
        return jobs

    @staticmethod
    def _has_source(path: Path) -> bool:
        challenge_dir = path / "challenge"
        if not challenge_dir.is_dir():
            return False
        for candidate in challenge_dir.rglob("*"):
            if not candidate.is_file():
                continue
            if candidate.name.endswith(".ctfmeta.json") or candidate.name == "NOTE.md":
                continue
            if candidate.name == "README.md":
                try:
                    text = candidate.read_text(encoding="utf-8", errors="replace")
                    if len(text.splitlines()) > 5 and len(text) > 100:
                        if "## 📝 Description" in text and len(text) > 200:
                            return True
                except OSError:
                    pass
                continue
            lowered = candidate.name.casefold()
            if lowered in _SOURCE_NAMES or candidate.suffix.casefold() in _SOURCE_SUFFIXES:
                return True
            if not candidate.suffix and os.access(candidate, os.X_OK):
                return True
            # Any non-placeholder payload file in challenge/ counts as source
            return True
        return False

    def _has_instance(self, meta: dict) -> bool:
        if self.repo.is_container(meta):
            return True
        inst = meta.get("instance_info")
        if isinstance(inst, dict) and any(inst.get(key) for key in ("entry", "url", "host", "port")):
            return True
        connection = meta.get("connection_info")
        if isinstance(connection, str):
            return bool(connection.strip())
        return bool(connection)

    def get_local_flag(self, job: SolverJob, state: dict | None = None) -> str | None:
        """Return a non-placeholder flag already hoarded in this challenge.

        This deliberately reads only durable local artifacts.  It does not infer
        a solve from an Agy completion without a flag or platform confirmation.
        """
        state = state if isinstance(state, dict) else self.read_job(job)
        candidate = _valid_local_flag(state.get("candidate_flag") or state.get("flag"))
        if candidate:
            return candidate

        report: dict = {}
        report_path = job.script_dir / "worker-report.json"
        if report_path.is_file():
            try:
                raw_report = json.loads(report_path.read_text(encoding="utf-8"))
                if isinstance(raw_report, dict):
                    report = raw_report
            except (OSError, ValueError):
                pass
        candidate = _valid_local_flag(report.get("candidate_flag") or report.get("flag"))
        if candidate:
            return candidate
        candidate = _extract_flag_from_job(job, report, "")
        if candidate:
            return candidate

        try:
            status = self.repo.read_status(job.path / "metadata.json")
            saved = _valid_local_flag((status.get("flag") or {}).get("value"))
            if saved:
                return saved
        except Exception:
            pass
        return None

    def queue_eligibility(self, job: SolverJob, state: dict | None = None) -> SolverEligibility:
        """Classify one job for BQA EATING and render the matching radar phase."""
        state = state if isinstance(state, dict) else self.read_job(job)
        local_flag = self.get_local_flag(job, state)
        if job.is_solved:
            return SolverEligibility(False, "platform_solved", local_flag)
        if local_flag:
            return SolverEligibility(False, "local_flag", local_flag)
        if str(state.get("state") or "") in {"queued", "starting", "running"}:
            return SolverEligibility(False, "active")
        if not (job.has_source or job.has_instance):
            return SolverEligibility(False, "no_input")
        return SolverEligibility(True, "ready")

    def select_ids(self, raw_ids: str) -> list[SolverJob]:
        tokens = [part.strip() for part in str(raw_ids).split(",") if part.strip()]
        if not tokens:
            raise SolverSelectionError("Hãy nhập ít nhất một challenge ID.")
        if any(not token.isdecimal() for token in tokens):
            raise SolverSelectionError("Challenge IDs không hợp lệ; dùng dạng 1,2,3.")
        try:
            selected_ids = [int(token) for token in tokens]
        except ValueError:
            raise SolverSelectionError("Challenge IDs không hợp lệ; dùng dạng 1,2,3.")
        if len(set(selected_ids)) != len(selected_ids):
            raise SolverSelectionError("Challenge IDs không hợp lệ: ID bị lặp.")
        known = {job.display_id: job for job in self.scan()}
        missing = [str(item) for item in selected_ids if item not in known]
        if missing:
            raise SolverSelectionError("Challenge ID không tồn tại: " + ", ".join(missing))
        return [known[item] for item in selected_ids]

    @staticmethod
    def prompt_ids() -> str:
        """Ask for display IDs outside the thin CLI command layer."""
        from rich.prompt import Prompt

        return Prompt.ask("Nhập challenge IDs (ví dụ: 1,2,3)")

    @staticmethod
    def _now() -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def read_job(self, job: SolverJob) -> dict:
        try:
            raw = json.loads(job.state_path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_job(self, job: SolverJob, **updates: object) -> dict:
        job.script_dir.mkdir(parents=True, exist_ok=True)

        def mutate(state: dict) -> dict:
            state.update(updates)
            state["display_id"] = job.display_id
            state["challenge_id"] = job.challenge_id
            state["updated_at"] = self._now()
            return state

        self._cached_jobs = None
        state = locked_update_json(job.state_path, mutate)
        if state is not None:
            return state
        fallback = dict(updates)
        fallback.update({
            "display_id": job.display_id,
            "challenge_id": job.challenge_id,
            "updated_at": self._now(),
        })
        return fallback

    def _write_progress(self, job: SolverJob, event: dict) -> None:
        job.script_dir.mkdir(parents=True, exist_ok=True)
        event = dict(event)
        event["updated_at"] = self._now()
        atomic_write_json(job.progress_path, event)

    def build_prompt(self, job: SolverJob, *, fallback_mode: bool = False, is_continuation: bool = False, is_resume: bool = False) -> str:
        return CategoryPromptBuilder.build(job, fallback_mode=fallback_mode, is_continuation=is_continuation, is_resume=is_resume)

    @property
    def category_sessions_path(self) -> Path:
        return self.workspace / ".ctf-solver" / "category-sessions.json"

    def get_category_sessions(self) -> dict[str, dict]:
        path = self.category_sessions_path
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def get_category_session(self, category: str) -> str | None:
        if not category:
            return None
        sessions = self.get_category_sessions()
        if category in sessions:
            val = sessions[category]
            return val.get("conversation_id") if isinstance(val, dict) else val
        cat_lower = category.strip().lower()
        for k, val in sessions.items():
            if k.strip().lower() == cat_lower:
                return val.get("conversation_id") if isinstance(val, dict) else val
        return None

    def save_category_session(self, category: str, conversation_id: str, job: SolverJob | None = None) -> None:
        if not category or not conversation_id:
            return
        cat_key = category.strip().lower()
        self.category_sessions_path.parent.mkdir(parents=True, exist_ok=True)

        def mutate(cur: dict) -> dict:
            entry = None
            matched_k = None
            for k, v in cur.items():
                if k.strip().lower() == cat_key:
                    entry = v if isinstance(v, dict) else {}
                    matched_k = k
                    break
            if entry is None:
                entry = {}
            if matched_k and matched_k != cat_key:
                cur.pop(matched_k, None)

            entry["conversation_id"] = conversation_id
            entry["category"] = category.strip()
            entry["updated_at"] = self._now()
            if job:
                entry["last_display_id"] = job.display_id
                entry["last_challenge_id"] = job.challenge_id
                entry["last_challenge_name"] = job.name
            cur[cat_key] = entry
            return cur

        locked_update_json(self.category_sessions_path, mutate)

    def clear_category_sessions(self, category: str | None = None) -> None:
        if not self.category_sessions_path.is_file():
            return
        if category:
            cat_key = category.strip().lower()
            locked_update_json(self.category_sessions_path, lambda cur: {k: v for k, v in cur.items() if k.strip().lower() != cat_key})
        else:
            with contextlib.suppress(OSError):
                self.category_sessions_path.unlink()

    def recover_category_sessions_from_history(self) -> dict[str, dict]:
        sessions = self.get_category_sessions()
        for job in self.scan():
            cat_key = job.category.strip()
            if self.get_category_session(cat_key):
                continue
            st = self.read_job(job)
            conv_id = st.get("conversation_id")
            if not conv_id and job.log_path.is_file():
                try:
                    content = job.log_path.read_text(encoding="utf-8", errors="replace")
                    m = re.search(r'"conversation_id"\s*:\s*"([a-f0-9-]+)"', content)
                    if m:
                        conv_id = m.group(1)
                except OSError:
                    pass
            if conv_id:
                self.save_category_session(cat_key, conv_id, job)
                sessions[cat_key] = {"conversation_id": conv_id}
        return sessions

    def distill_playbook(self, category: str, job: SolverJob | None = None, *, timeout: int = 120) -> dict:
        from .playbook_distiller import PlaybookDistiller
        distiller = PlaybookDistiller(self.workspace)
        jobs = [job] if job else self.scan()
        main_conv_id = self.get_category_session(category)
        return distiller.distill(
            category,
            jobs,
            main_conversation_id=main_conv_id,
            timeout=timeout,
        )

    @contextlib.contextmanager
    def _manager_lock(self) -> Iterator[None]:
        import fcntl

        control = self.workspace / ".ctf-solver"
        control.mkdir(parents=True, exist_ok=True)
        path = control / "manager.lock"
        handle = open(path, "a+", encoding="utf-8")
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise SolverAlreadyRunning("Solver manager is already running in this workspace.") from exc
            handle.seek(0)
            handle.truncate()
            handle.write(str(os.getpid()))
            handle.flush()
            yield
        finally:
            with contextlib.suppress(OSError):
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    @classmethod
    def _pid_is_alive(
        cls,
        pid: object,
        *,
        start_ticks: int | None = None,
        boot_id: str | None = None,
    ) -> bool:
        if not isinstance(pid, int) or pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

        if boot_id:
            curr_boot = get_system_boot_id()
            if curr_boot and curr_boot != boot_id:
                return False

        if start_ticks is not None:
            curr_ticks = get_pid_start_ticks(pid)
            if curr_ticks is not None and curr_ticks != start_ticks:
                return False

        cmdline_path = Path(f"/proc/{pid}/cmdline")
        if cmdline_path.exists():
            try:
                cmdline = cmdline_path.read_text(encoding="utf-8", errors="ignore")
                if cmdline and not any(term in cmdline for term in ("agy", "python", "solve", "sh", "bash", "pytest")):
                    return False
            except OSError:
                pass
        return True

    def recover_stale_jobs(self) -> int:
        recovered = 0
        now_dt = datetime.now(timezone.utc)
        for job in self.scan():
            state = self.read_job(job)
            cur_state = state.get("state")
            if cur_state == "running":
                pid = state.get("pid")
                ticks = state.get("pid_start_ticks")
                boot = state.get("boot_id")
                is_dead = pid is not None and not self._pid_is_alive(pid, start_ticks=ticks, boot_id=boot)

                is_stale_hb = False
                heartbeat_at_str = str(state.get("heartbeat_at") or state.get("started_at") or "")
                if heartbeat_at_str:
                    try:
                        hb_dt = datetime.fromisoformat(heartbeat_at_str.replace("Z", "+00:00"))
                        if (now_dt - hb_dt).total_seconds() > self.stale_seconds:
                            is_stale_hb = True
                    except Exception:
                        pass

                if is_dead:
                    self._write_job(job, state="failed", phase="stopped", error_code="E_WORKER_CRASH",
                                    message="Worker process is no longer alive.", ended_at=self._now())
                    recovered += 1
                elif is_stale_hb:
                    if pid is not None:
                        with contextlib.suppress(ProcessLookupError, OSError):
                            os.kill(pid, signal.SIGTERM)
                    self._write_job(job, state="failed", phase="stalled", error_code="E_STALLED",
                                    message="Worker exceeded the stale-heartbeat deadline.", ended_at=self._now())
                    recovered += 1
                elif pid is None:
                    started_at_str = str(state.get("started_at") or "")
                    try:
                        started_dt = datetime.fromisoformat(started_at_str.replace("Z", "+00:00"))
                        if (now_dt - started_dt).total_seconds() > 15.0:
                            self._write_job(job, state="failed", phase="stopped", error_code="E_WORKER_CRASH",
                                            message="Worker failed to report PID.", ended_at=self._now())
                            recovered += 1
                    except Exception:
                        pass
            elif cur_state == "starting":
                started_at_str = str(state.get("started_at") or "")
                try:
                    started_dt = datetime.fromisoformat(started_at_str.replace("Z", "+00:00"))
                    if (now_dt - started_dt).total_seconds() > 15.0:
                        self._write_job(job, state="failed", phase="stopped", error_code="E_WORKER_CRASH",
                                        message="Worker failed to start within grace period.", ended_at=self._now())
                        recovered += 1
                except Exception:
                    pass
        return recovered

    def _append_output(self, job: SolverJob, line: str) -> None:
        job.script_dir.mkdir(parents=True, exist_ok=True)
        with job.log_path.open("a", encoding="utf-8") as log:
            log.write(line)
        stripped = line.strip()
        if not stripped:
            return
        self._touch_activity(job)
        updates: dict[str, object] = {"heartbeat_at": self._now(), "last_output": stripped[-500:]}
        if _FILTER_RE.search(stripped):
            updates["filter_detected"] = True
        if _AGY_PRINT_TIMEOUT_RE.search(stripped):
            updates["agy_timeout_detected"] = True

        # Extract real-time telemetry from stream-json events
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                evt_obj = json.loads(stripped)
                if isinstance(evt_obj, dict):
                    conv_id = (
                        evt_obj.get("conversation_id")
                        or evt_obj.get("step_update", {}).get("conversation_id")
                        or evt_obj.get("init", {}).get("conversation_id")
                    )
                    if conv_id:
                        updates["conversation_id"] = conv_id
                        if not self.get_category_session(job.category):
                            self.save_category_session(job.category, conv_id, job)
                if isinstance(evt_obj, dict) and evt_obj.get("event") == "step_update":
                    su = evt_obj.get("step_update", {})
                    stype = su.get("step_type")
                    if stype == "tool":
                        tname = su.get("tool_name", "tool")
                        tinfo = su.get("tool_info") or {}
                        params = tinfo.get("parameters") or {}
                        target = (
                            params.get("AbsolutePath")
                            or params.get("CommandLine")
                            or params.get("DirectoryPath")
                            or params.get("Pattern")
                            or params.get("Url")
                            or ""
                        )
                        target_str = Path(str(target)).name if target else ""
                        summary = f"[{tname}] {target_str}".strip() if target_str else f"[{tname}]"
                        updates["telemetry"] = summary
                        updates["message"] = summary[:120]
                    elif stype == "agent_response" and su.get("state") == "ACTIVE":
                        updates["telemetry"] = "reasoning"
                        updates["message"] = "Reasoning & analyzing..."
            except Exception:
                pass

        progress_raw = None
        if stripped.startswith(_PROGRESS_PREFIX):
            progress_raw = stripped[len(_PROGRESS_PREFIX):].strip()
        elif _PROGRESS_PREFIX in stripped:
            try:
                event_obj = json.loads(stripped)
                if isinstance(event_obj, dict):
                    candidate = (
                        event_obj.get("step_update", {}).get("text_delta")
                        or event_obj.get("result", {}).get("response")
                    )
                    if isinstance(candidate, str) and _PROGRESS_PREFIX in candidate:
                        progress_raw = candidate.split(_PROGRESS_PREFIX, 1)[1].strip()
            except Exception:
                pass
            if progress_raw is None:
                progress_raw = stripped.split(_PROGRESS_PREFIX, 1)[1].strip()

        if progress_raw:
            try:
                event = json.loads(progress_raw)
            except ValueError:
                start = progress_raw.find("{")
                end = progress_raw.rfind("}")
                if start != -1 and end != -1 and end > start:
                    try:
                        event = json.loads(progress_raw[start:end + 1])
                    except ValueError:
                        event = None
                else:
                    event = None
            if isinstance(event, dict) and isinstance(event.get("phase"), str):
                self._write_progress(job, event)
                updates["phase"] = event["phase"]
                if event.get("message"):
                    updates["message"] = str(event["message"])[-500:]
        self._write_job(job, **updates)

    def _start_worker(self, job: SolverJob, agy_command: Sequence[str], *, reuse_session: bool = True, fork_session: bool = False, force_resume: bool = False) -> subprocess.Popen:
        prior_state = self.read_job(job)
        prior_conv_id = prior_state.get("conversation_id")
        is_filtered = (
            prior_state.get("state") == "filtered"
            or prior_state.get("filter_detected") is True
            or prior_state.get("was_filtered") is True
        )
        is_resume = force_resume or (bool(prior_conv_id) and is_filtered and reuse_session)

        category_conv_id = self.get_category_session(job.category) if reuse_session else None
        if is_resume and prior_conv_id:
            worker_conv_id = prior_conv_id
        elif category_conv_id and fork_session:
            from .session_forker import fork_agy_session
            forked = fork_agy_session(category_conv_id, title=f"{job.category} · {job.name}")
            worker_conv_id = forked if forked else None
        else:
            worker_conv_id = category_conv_id

        is_continuation = bool(worker_conv_id) and not is_resume

        base_cmd = list(agy_command)
        if worker_conv_id and "--conversation" not in base_cmd:
            base_cmd.extend(["--conversation", worker_conv_id])

        command = [
            *base_cmd,
            "--output-format", "stream-json",
            "--print-timeout", f"{self.timeout_seconds + 60}s",
            "--print", self.build_prompt(job, is_continuation=is_continuation, is_resume=is_resume),
        ]
        solver = job.path / "solver" / "solve.py"
        try:
            prior_solver_mtime = solver.stat().st_mtime_ns
        except OSError:
            prior_solver_mtime = None
        job.script_dir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            job.log_path.write_text("", encoding="utf-8")
        if is_resume:
            msg = f"Resuming {job.name} after filter"
        elif is_continuation:
            msg = f"Resuming {job.category} session"
        else:
            msg = "Agy worker starting"
        self._write_job(job, state="starting", phase="starting", error_code=None, message=msg,
                        started_at=self._now(), heartbeat_at=self._now(), command=command[:-1],
                        conversation_id=worker_conv_id,
                        reused_session=bool(worker_conv_id and worker_conv_id == category_conv_id),
                        forked_session=bool(worker_conv_id and worker_conv_id != category_conv_id),
                        filter_detected=False, agy_timeout_detected=False,
                        solver_mtime_ns_before=prior_solver_mtime)
        self._touch_activity(job)
        try:
            process = subprocess.Popen(
                command, cwd=str(job.path), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, start_new_session=True, errors="replace",
            )
        except OSError as exc:
            self._write_job(job, state="failed", phase="crashed", error_code="E_WORKER_CRASH",
                            message=f"Agy worker failed to start: {exc}", ended_at=self._now())
            raise
        start_ticks = get_pid_start_ticks(process.pid)
        boot_id = get_system_boot_id()
        self._write_job(
            job,
            state="running",
            pid=process.pid,
            pgid=process.pid,
            pid_start_ticks=start_ticks,
            boot_id=boot_id,
            phase="running",
            heartbeat_at=self._now(),
            message="Agy worker started",
        )

        def _consume() -> None:
            stdout = getattr(process, "stdout", None)
            if stdout is None:
                return
            for line in stdout:
                self._append_output(job, line)
            with contextlib.suppress(Exception):
                stdout.close()

        reader = threading.Thread(target=_consume, daemon=True, name=f"agy-log-{job.display_id}")
        reader.start()
        process._reader_thread = reader  # type: ignore[attr-defined]
        return process

    @staticmethod
    def _terminate_group(process: subprocess.Popen | int, timeout: float = 2.0, force_group: bool = False,
                         start_ticks: int | None = None, boot_id: str | None = None) -> None:
        pid = process if isinstance(process, int) else getattr(process, "pid", None)
        if not isinstance(pid, int) or pid <= 0:
            return
        if start_ticks is not None or boot_id is not None:
            if not SolverService._pid_is_alive(pid, start_ticks=start_ticks, boot_id=boot_id):
                return
        has_poll = hasattr(process, "poll")
        is_proc_alive = (process.poll() is None) if has_poll else True
        if is_proc_alive or force_group:
            with contextlib.suppress(ProcessLookupError, OSError):
                os.killpg(pid, signal.SIGTERM)
            if hasattr(process, "wait"):
                try:
                    process.wait(timeout=timeout)
                except (subprocess.TimeoutExpired, TypeError, AttributeError):
                    pass
            with contextlib.suppress(ProcessLookupError, OSError):
                os.killpg(pid, signal.SIGKILL)
            if hasattr(process, "wait"):
                with contextlib.suppress(Exception):
                    process.wait(timeout=0.5)

    def _finish_worker(self, job: SolverJob, process: subprocess.Popen, *, timed_out: bool,
                       stalled: bool = False) -> dict:
        exit_code = process.poll()
        state = self.read_job(job)
        output = str(state.get("last_output") or "")
        log_content = ""
        if job.log_path.is_file():
            with contextlib.suppress(OSError):
                log_content = job.log_path.read_text(encoding="utf-8", errors="replace")

        solver = job.path / "solver" / "solve.py"
        try:
            new_solver_mtime = solver.stat().st_mtime_ns
        except OSError:
            new_solver_mtime = None

        prior_mtime = state.get("solver_mtime_ns_before")
        solver_touched = (new_solver_mtime is not None and prior_mtime is not None and new_solver_mtime != prior_mtime) or (new_solver_mtime is not None and prior_mtime is None)

        if state.get("state") == "cancelled":
            return self._write_job(job, state="cancelled", phase="cancelled", exit_code=exit_code, ended_at=self._now())
        if stalled:
            return self._write_job(job, state="failed", phase="stalled", error_code="E_STALLED",
                                   message="Worker stopped producing output before the stale-heartbeat deadline.",
                                   exit_code=exit_code, ended_at=self._now())
        if timed_out:
            return self._write_job(job, state="failed", phase="stopped", error_code="E_TIMEOUT",
                                   message="Worker exceeded its time limit.", exit_code=exit_code, ended_at=self._now())
        if (state.get("agy_timeout_detected") is True
            or _AGY_PRINT_TIMEOUT_RE.search(output)
            or _AGY_PRINT_TIMEOUT_RE.search(log_content)):
            return self._write_job(job, state="failed", phase="stopped", error_code="E_TIMEOUT",
                                   message="Agy print mode timed out before the turn completed.",
                                   exit_code=exit_code, ended_at=self._now())
        if (state.get("filter_detected") is True
            or _FILTER_RE.search(output)
            or _FILTER_RE.search(log_content)):
            return self._write_job(job, state="filtered", phase="filtered", error_code="E_FILTER",
                                   message="Worker was stopped by a safety filter.", exit_code=exit_code, ended_at=self._now())
        try:
            report = json.loads((job.script_dir / "worker-report.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            report = {}

        candidate_flag = _extract_flag_from_job(job, report if isinstance(report, dict) else {}, log_content)
        if candidate_flag:
            meta_path = job.path / "metadata.json"
            if meta_path.is_file():
                def _mut_flag(st: dict) -> dict:
                    st["flag"]["value"] = candidate_flag
                    st["flag"]["state"] = "hoarded"
                    if st.get("solve") in ("none", "unsolved"):
                        st["solve"] = "working"
                    return st
                with contextlib.suppress(Exception):
                    self.repo.update_status(meta_path, _mut_flag)

        has_analysis = (job.script_dir / "analysis.md").is_file() or (job.script_dir / "findings.json").is_file()

        if exit_code != 0:
            error_code = "E_WORKER_CRASH"
            msg = f"Worker exited with code {exit_code}."
            if _QUOTA_RE.search(output) or _QUOTA_RE.search(log_content):
                error_code = "E_QUOTA"
                msg = "Worker stopped: model quota limit reached."
                if has_analysis:
                    msg += " Analysis preserved in script/analysis.md."

            updates: dict[str, object] = {
                "state": "failed",
                "phase": "stopped",
                "error_code": error_code,
                "message": msg,
                "exit_code": exit_code,
                "ended_at": self._now(),
            }
            if candidate_flag:
                updates["candidate_flag"] = candidate_flag
            if has_analysis:
                updates["outcome"] = "analyzed"
            return self._write_job(job, **updates)

        if candidate_flag and not (isinstance(report, dict) and report.get("local_verification") == "passed"):
            return self._write_job(job, state="completed", phase="completed", error_code=None,
                                   candidate_flag=candidate_flag, outcome="candidate_found",
                                   message="Worker recovered candidate flag.",
                                   exit_code=exit_code, ended_at=self._now())

        if not (job.path / "solver" / "solve.py").is_file():
            updates = {
                "state": "failed",
                "phase": "verify_local",
                "error_code": "E_VERIFY_LOCAL",
                "message": "Worker exited without solver/solve.py.",
                "exit_code": exit_code,
                "ended_at": self._now(),
            }
            if has_analysis:
                updates["outcome"] = "analyzed"
                updates["message"] = "Analysis preserved in script/analysis.md; solver/solve.py missing."
            return self._write_job(job, **updates)
        try:
            new_solver_mtime = (job.path / "solver" / "solve.py").stat().st_mtime_ns
        except OSError:
            new_solver_mtime = None
        prior_solver_mtime = state.get("solver_mtime_ns_before")
        if prior_solver_mtime is not None and new_solver_mtime == prior_solver_mtime:
            updates = {
                "state": "failed",
                "phase": "verify_local",
                "error_code": "E_VERIFY_LOCAL",
                "message": "Worker did not update the generated solver template.",
                "exit_code": exit_code,
                "ended_at": self._now(),
            }
            if has_analysis:
                updates["outcome"] = "analyzed"
            return self._write_job(job, **updates)

        if not isinstance(report, dict) or report.get("local_verification") != "passed":
            updates = {
                "state": "failed",
                "phase": "verify_local",
                "error_code": "E_VERIFY_LOCAL",
                "message": "Missing successful script/worker-report.json local verification.",
                "exit_code": exit_code,
                "ended_at": self._now(),
            }
            if has_analysis:
                updates["outcome"] = "analyzed"
            return self._write_job(job, **updates)

        updates: dict[str, object] = {
            "state": "completed",
            "phase": "completed",
            "error_code": None,
            "outcome": "solved_local",
            "message": "Worker finished and reported local verification.",
            "exit_code": exit_code,
            "ended_at": self._now(),
        }
        if candidate_flag:
            updates["candidate_flag"] = candidate_flag
        return self._write_job(job, **updates)

    def run(self, raw_ids: str, *, workers: int = 3, agy_command: Sequence[str] | None = None,
            on_refresh: Callable[[], None] | None = None, acquire_lock: bool = True,
            reuse_session: bool = True, per_category: bool = False) -> list[dict]:
        if workers < 1 or (workers > 3 and not per_category):
            raise ValueError("workers must be between 1 and 3 unless per_category mode is enabled.")
        jobs = self.select_ids(raw_ids)
        raw_cmd = list(agy_command or ["agy", "--mode", "accept-edits", "--dangerously-skip-permissions"])
        if raw_cmd:
            raw_cmd[0] = resolve_agy_binary(raw_cmd[0])
        command = raw_cmd
        active: dict[SolverJob, tuple[subprocess.Popen, float]] = {}
        completed: dict[SolverJob, dict] = {}
        lock_ctx = self._manager_lock() if acquire_lock else contextlib.nullcontext()
        with lock_ctx:
            self.recover_stale_jobs()
            if reuse_session:
                self.recover_category_sessions_from_history()
            queue: list[SolverJob] = []
            for job in jobs:
                prior = self.read_job(job)
                eligibility = self.queue_eligibility(job, prior)
                if not eligibility.ready:
                    skip_reasons = {
                        "platform_solved": "Platform already solved; worker was not started.",
                        "local_flag": f"Local flag already hoarded ({eligibility.local_flag}); worker was not started.",
                        "active": "Worker already active; skipped duplicate launch.",
                        "no_input": "No local source or remote instance detected; worker was not started.",
                    }
                    completed[job] = self._write_job(
                        job, state=f"skipped_{eligibility.reason}", phase="skipped", error_code=None,
                        message=skip_reasons.get(eligibility.reason, f"Skipped: {eligibility.reason}"),
                        ended_at=self._now(),
                    )
                    continue
                was_filtered = (prior.get("state") == "filtered") or bool(prior.get("filter_detected")) or bool(prior.get("was_filtered"))
                prior_conv_id = prior.get("conversation_id")
                self._write_job(
                    job, state="queued", phase="queued", error_code=None,
                    message="Waiting for a worker slot.",
                    pid=None, exit_code=None, ended_at=None,
                    filter_detected=False, agy_timeout_detected=False,
                    was_filtered=was_filtered,
                    conversation_id=prior_conv_id,
                    last_output=None,
                )
                queue.append(job)
            last_refresh = 0.0
            last_active_count = -1
            try:
                while queue or active:
                    active_categories = (
                        {j.category.strip().casefold() for j in active.keys()}
                        if (reuse_session or per_category) else set()
                    )
                    while len(active) < workers:
                        if not queue:
                            break
                        if per_category:
                            candidate_idx = next(
                                (
                                    idx for idx, queued_job in enumerate(queue)
                                    if queued_job.category.strip().casefold() not in active_categories
                                ),
                                None,
                            )
                            if candidate_idx is None:
                                break
                        else:
                            candidate_idx = 0
                        job = queue.pop(candidate_idx)
                        job_cat = job.category.strip().casefold()
                        # Normal BQA may run same-category jobs concurrently;
                        # per-category mode deliberately selects a different
                        # category while one worker for this category is active.
                        # When reuse is enabled, a concurrent same-category
                        # worker forks the session if one is available.
                        fork_session = bool(reuse_session and job_cat in active_categories)
                        if reuse_session or per_category:
                            active_categories.add(job_cat)
                        started = time.monotonic()
                        try:
                            try:
                                proc = self._start_worker(
                                    job,
                                    command,
                                    reuse_session=reuse_session,
                                    fork_session=fork_session,
                                    )
                            except TypeError:
                                proc = self._start_worker(job, command)
                        except OSError:
                            completed[job] = self.read_job(job)
                            if reuse_session or per_category:
                                active_categories.discard(job.category.strip().casefold())
                            continue
                        active[job] = (proc, started)
                    self.max_active_workers = max(self.max_active_workers, len(active))
                    now = time.monotonic()
                    if on_refresh and (now - last_refresh >= 0.2 or len(active) != last_active_count):
                        last_refresh = now
                        last_active_count = len(active)
                        with contextlib.suppress(Exception):
                            on_refresh()
                    for job, (process, started) in list(active.items()):
                        now = time.monotonic()
                        exited = process.poll() is not None
                        timed_out = False
                        stalled = False
                        if not exited:
                            timed_out = now - started > self.timeout_seconds
                            stalled = now - self._activity_at(job, started) > self.stale_seconds
                            if stalled:
                                self._write_job(job, state="failed", phase="stalled", error_code="E_STALLED",
                                                message="Worker exceeded the stale-heartbeat deadline.")
                            if timed_out or stalled:
                                self._terminate_group(process)
                        if exited or timed_out or stalled:
                            if timed_out or stalled:
                                self._terminate_group(process)
                            reader = getattr(process, "_reader_thread", None)
                            if reader is not None and isinstance(reader, threading.Thread):
                                reader.join(timeout=1.5)
                                if reader.is_alive():
                                    self._terminate_group(process, force_group=True)
                                    reader.join(timeout=0.5)
                            completed[job] = self._finish_worker(
                                job, process, timed_out=timed_out, stalled=stalled,
                            )
                            del active[job]
                    if active:
                        time.sleep(0.05)
            except BaseException as exc:
                reason = "user interrupt" if isinstance(exc, KeyboardInterrupt) else f"unexpected error: {exc}"
                for active_job, (active_proc, _) in list(active.items()):
                    self._terminate_group(active_proc, force_group=True)
                    reader = getattr(active_proc, "_reader_thread", None)
                    if reader is not None and isinstance(reader, threading.Thread):
                        reader.join(timeout=0.5)
                    completed[active_job] = self._write_job(
                        active_job, state="cancelled", phase="cancelled", error_code=None,
                        message=f"Cancelled while running due to {reason}.", ended_at=self._now(),
                    )
                for cancelled_job in queue:
                    completed[cancelled_job] = self._write_job(
                        cancelled_job, state="cancelled", phase="cancelled", error_code=None,
                        message=f"Cancelled before starting due to {reason}.", ended_at=self._now(),
                    )
                raise
        if on_refresh:
            on_refresh()
        return [completed[job] for job in jobs]

    def get_daemon_status(self) -> dict:
        state_path = self.workspace / ".ctf-solver" / "manager-state.json"
        if not state_path.is_file():
            return {"is_running": False, "status": "idle"}
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            return {"is_running": False, "status": "unknown"}
        if not isinstance(data, dict):
            return {"is_running": False, "status": "unknown"}
        pid = data.get("daemon_pid")
        ticks = data.get("pid_start_ticks")
        boot_id = data.get("boot_id")
        alive = self._pid_is_alive(pid, start_ticks=ticks, boot_id=boot_id)
        if alive and data.get("status") == "running":
            data["is_running"] = True
        else:
            data["is_running"] = False
            if data.get("status") == "running":
                data["status"] = "stopped"
        return data

    def spawn_background(
        self,
        raw_ids: str,
        *,
        workers: int = 3,
        timeout_seconds: int = 3600,
        stale_seconds: int = 300,
        reuse_session: bool = True,
        per_category: bool = False,
    ) -> dict:
        jobs = self.select_ids(raw_ids)
        if not jobs:
            return {"success": False, "error": "NO_JOBS", "message": "No valid challenges found."}

        self.recover_stale_jobs()
        daemon_info = self.get_daemon_status()
        if daemon_info.get("is_running"):
            return {
                "success": False,
                "error": "ALREADY_RUNNING",
                "message": f"SuperBQA daemon is already running (PID: {daemon_info.get('daemon_pid')}).",
                "daemon_info": daemon_info,
            }

        control_dir = self.workspace / ".ctf-solver"
        control_dir.mkdir(parents=True, exist_ok=True)
        log_path = control_dir / "supervisor.log"

        cmd = [
            sys.executable,
            "-m",
            "ctf_downloader.services.solver_daemon",
            "--workspace", str(self.workspace.resolve()),
            "--ids", raw_ids,
            "--workers", str(workers),
            "--timeout", str(timeout_seconds),
            "--stale-timeout", str(stale_seconds),
        ]
        if not reuse_session:
            cmd.append("--new-session")
        if per_category:
            cmd.append("--per-category")

        env = os.environ.copy()
        pkg_root = str(Path(__file__).resolve().parent.parent.parent)
        existing_pp = env.get("PYTHONPATH")
        env["PYTHONPATH"] = f"{pkg_root}:{existing_pp}" if existing_pp else pkg_root

        try:
            with open(log_path, "a", encoding="utf-8") as log_file:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=log_file,
                    cwd=str(self.workspace),
                    start_new_session=True,
                    env=env,
                )
        except OSError as exc:
            return {"success": False, "error": "SPAWN_FAILED", "message": f"Failed to spawn daemon: {exc}"}

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            time.sleep(0.1)
            info = self.get_daemon_status()
            if info.get("is_running"):
                return {
                    "success": True,
                    "daemon_pid": proc.pid,
                    "target_ids": raw_ids,
                    "message": f"SuperBQA daemon started successfully (PID {proc.pid}).",
                }
            if proc.poll() is not None:
                return {
                    "success": False,
                    "error": "DAEMON_EXITED_EARLY",
                    "message": f"Daemon exited prematurely with code {proc.returncode}. Check log at {log_path}.",
                }

        return {
            "success": True,
            "daemon_pid": proc.pid,
            "target_ids": raw_ids,
            "message": f"SuperBQA daemon detached in background (PID {proc.pid}).",
        }

    def stop_background(self, raw_id: str | None = None) -> dict:
        stopped_workers = 0
        if raw_id:
            try:
                jobs = self.select_ids(raw_id)
            except Exception:
                jobs = [j for j in self.scan() if str(j.challenge_id) == str(raw_id) or str(j.name).lower() == str(raw_id).lower()]
                if not jobs:
                    return {"success": False, "message": f"Challenge with ID '{raw_id}' not found."}
            for j in jobs:
                st = self.read_job(j)
                pid = st.get("pid")
                ticks = st.get("pid_start_ticks")
                boot_id = st.get("boot_id")
                if pid and self._pid_is_alive(pid, start_ticks=ticks, boot_id=boot_id):
                    self._terminate_group(pid, force_group=True, start_ticks=ticks, boot_id=boot_id)
                    self._write_job(j, state="cancelled", phase="cancelled", message="Cancelled by user.", ended_at=self._now())
                    stopped_workers += 1
            return {"success": True, "stopped_workers": stopped_workers, "message": f"Stopped {stopped_workers} worker(s) for challenge {raw_id}."}

        daemon_info = self.get_daemon_status()
        d_pid = daemon_info.get("daemon_pid")
        d_ticks = daemon_info.get("pid_start_ticks")
        d_boot = daemon_info.get("boot_id")
        if d_pid and self._pid_is_alive(d_pid, start_ticks=d_ticks, boot_id=d_boot):
            with contextlib.suppress(ProcessLookupError, OSError):
                os.kill(d_pid, signal.SIGTERM)

        for j in self.scan():
            st = self.read_job(j)
            if st.get("state") in ("starting", "running"):
                pid = st.get("pid")
                ticks = st.get("pid_start_ticks")
                boot_id = st.get("boot_id")
                if pid and self._pid_is_alive(pid, start_ticks=ticks, boot_id=boot_id):
                    self._terminate_group(pid, force_group=True, start_ticks=ticks, boot_id=boot_id)
                self._write_job(j, state="cancelled", phase="cancelled", message="Cancelled by user stop_all.", ended_at=self._now())
                stopped_workers += 1

        state_path = self.workspace / ".ctf-solver" / "manager-state.json"
        if state_path.is_file():
            locked_update_json(state_path, lambda cur: {**cur, "status": "idle", "active_ids": [], "ended_at": self._now()})

        return {"success": True, "stopped_workers": stopped_workers, "message": f"Stopped daemon and {stopped_workers} worker(s)."}
