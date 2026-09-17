"""BQA-backed, one-shot recovery for failed ``ctf`` commands.

BQA is the user-facing name of the local ``agy`` agent session.  This module
keeps incident construction separate from CLI dispatch so all data crossing
the agent boundary can be redacted in one place.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from importlib import resources
from hashlib import sha256
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .utils.agy_resolver import extract_json_payload, resolve_agy_binary


_SECRET_OPTIONS = frozenset({
    "-c", "--cookie", "-t", "--token", "--password", "-f", "--flag",
    "--cf-clearance",
})


PULL_ERROR_CODES = (
    "CTF-PULL-D01", "CTF-PULL-D02", "CTF-PULL-D03", "CTF-PULL-D04",
    "CTF-PULL-D05", "CTF-PULL-D06", "CTF-PULL-D07", "CTF-PULL-D08",
    "CTF-PULL-D09", "CTF-PULL-D10", "CTF-PULL-D11", "CTF-PULL-D12",
    "CTF-PULL-D13", "CTF-PULL-D14", "CTF-PULL-D15", "CTF-PULL-D99",
)

_PROMPT_FILES = {
    "CTF-PULL-D01": "input_or_cookie",
    "CTF-PULL-D02": "unknown_platform",
    "CTF-PULL-D03": "auth_or_empty_private_contest",
    "CTF-PULL-D04": "api_schema_or_pagination",
    "CTF-PULL-D05": "filter_selection",
    "CTF-PULL-D06": "platform_attachment",
    "CTF-PULL-D07": "third_party_download",
    "CTF-PULL-D08": "resume_or_integrity",
    "CTF-PULL-D09": "parallel_download",
    "CTF-PULL-D10": "partial_or_total_failure",
    "CTF-PULL-D11": "workspace_write",
    "CTF-PULL-D12": "summary_generation",
    "CTF-PULL-D13": "git_postprocess",
    "CTF-PULL-D14": "network_or_cloudflare",
    "CTF-PULL-D15": "bqa_recovery",
    "CTF-PULL-D99": "unhandled_crash",
}

_ERROR_TITLES = {
    "CTF-PULL-D01": "Input hoặc cookie không hợp lệ",
    "CTF-PULL-D02": "Không nhận diện được platform",
    "CTF-PULL-D03": "Xác thực thất bại hoặc contest private không có challenge",
    "CTF-PULL-D04": "API challenge thay đổi schema hoặc pagination",
    "CTF-PULL-D05": "Filter đã loại toàn bộ challenge",
    "CTF-PULL-D06": "Không tải được attachment của platform",
    "CTF-PULL-D07": "Không tải được file từ provider bên thứ ba",
    "CTF-PULL-D08": "Resume hoặc kiểm tra integrity thất bại",
    "CTF-PULL-D09": "Worker tải song song thất bại",
    "CTF-PULL-D10": "Có challenge hoặc attachment tải thất bại",
    "CTF-PULL-D11": "Không ghi được workspace hoặc metadata",
    "CTF-PULL-D12": "Không tạo được summary sau khi tải",
    "CTF-PULL-D13": "Git hậu xử lý thất bại",
    "CTF-PULL-D14": "Lỗi mạng, rate limit hoặc Cloudflare",
    "CTF-PULL-D15": "BQA recovery hoặc verification thất bại",
    "CTF-PULL-D99": "Pull bị crash chưa phân loại",
}


class PullCommandFailure(RuntimeError):
    """A pull failure with a stable, user-visible BQA error code."""

    def __init__(self, error_code: str, evidence: Optional[dict] = None):
        self.bqa_error_code = (
            error_code if error_code in PULL_ERROR_CODES else "CTF-PULL-D99"
        )
        self.bqa_evidence = _safe_bqa_evidence(evidence)
        super().__init__(self.bqa_error_code)


def _safe_bqa_evidence(value: object) -> dict:
    """Keep only structural platform recon evidence across the BQA boundary."""
    if not isinstance(value, dict):
        return {}
    safe: dict = {}
    signals = value.get("detector_signals")
    if isinstance(signals, (list, tuple)):
        safe["detector_signals"] = [str(item)[:240] for item in signals[:20]]
    status = value.get("http_status")
    if isinstance(status, int):
        safe["http_status"] = status
    paths = value.get("api_paths")
    if isinstance(paths, (list, tuple)):
        safe["api_paths"] = [str(item)[:160] for item in paths[:20] if str(item).startswith("/")]
    schema = value.get("response_schema")
    if isinstance(schema, (dict, list, str, int, float, bool)) or schema is None:
        safe["response_schema"] = schema
    return safe


def _classify_exception(exc: Optional[BaseException]) -> str:
    explicit = getattr(exc, "bqa_error_code", None)
    if explicit in PULL_ERROR_CODES:
        return explicit
    if exc is None:
        return "CTF-PULL-D99"
    module = type(exc).__module__
    name = type(exc).__name__
    trace = traceback.extract_tb(exc.__traceback__)
    trace_names = {frame.name for frame in trace}
    trace_files = {os.path.basename(frame.filename) for frame in trace}
    if "summary_generator" in module or "summary_generator.py" in trace_files or "generate_summary" in trace_names:
        return "CTF-PULL-D12"
    if "git_workflow" in module or name == "GitWorkflowError":
        return "CTF-PULL-D13"
    if "UnknownPlatform" in name or "detection" in module:
        return "CTF-PULL-D02"
    if name in {"JSONDecodeError", "KeyError"} and any(
        filename in trace_files for filename in {"ctfd.py", "gzctf.py", "rctf.py", "asisctf.py", "noctf.py", "tfcctf.py"}
    ):
        return "CTF-PULL-D04"
    if name in {"DownloadFailed", "LargeFileSkipped"}:
        return "CTF-PULL-D06"
    if name in {"CloudflareChallengeError", "ConnectionError", "Timeout", "SSLError"}:
        return "CTF-PULL-D14"
    if isinstance(exc, OSError):
        return "CTF-PULL-D11"
    return "CTF-PULL-D99"


def load_prompt_template(error_code: str) -> str:
    """Load the base policy plus the incident-specific bundled prompt."""
    code = error_code if error_code in _PROMPT_FILES else "CTF-PULL-D99"
    root = resources.files("ctf_downloader").joinpath("bqa_prompts")
    base = root.joinpath("base.md").read_text(encoding="utf-8")
    detail = root.joinpath("pull", f"{_PROMPT_FILES[code]}.md").read_text(encoding="utf-8")
    return f"{base.rstrip()}\n\n{detail.rstrip()}\n"


def request_bqa_help(
    incident: "RecoveryIncident",
    input_stream=None,
    output=None,
) -> bool:
    """Render a coded error and accept BQA only after a TTY user says yes."""
    input_stream = input_stream or sys.stdin
    output = output or sys.stdout
    code = incident.error_code
    print(f"[{code}] {_ERROR_TITLES[code]}", file=output, flush=True)
    print("BQA có thể chẩn đoán, sửa source, chạy test và retry lệnh gốc.",
          file=output, flush=True)
    if not getattr(input_stream, "isatty", lambda: False)():
        print("BQA không tự chạy khi không có terminal tương tác.", file=output, flush=True)
        return False
    print("SuperBQA? [Y/n]: ", end="", file=output, flush=True)
    answer = input_stream.readline().strip().lower()
    if answer in {"", "y", "yes"}:
        return True
    print("BQA không chạy. Bạn có thể sửa input rồi chạy lại lệnh gốc.", file=output, flush=True)
    return False


def _replace_cookie_argument(argv: Sequence[str], cookie: str) -> Tuple[str, ...]:
    values = [str(value) for value in argv]
    # Priority 1: Replace existing session cookie (-c, --cookie, --cookie=)
    for index, value in enumerate(values):
        if value in {"-c", "--cookie"} and index + 1 < len(values):
            values[index + 1] = cookie
            return tuple(values)
        option, separator, _argument = value.partition("=")
        if separator and option in {"-c", "--cookie"}:
            values[index] = f"{option}={cookie}"
            return tuple(values)
    # Priority 2: Replace --cf-clearance if cookie options are absent
    for index, value in enumerate(values):
        if value == "--cf-clearance" and index + 1 < len(values):
            values[index + 1] = cookie
            return tuple(values)
        option, separator, _argument = value.partition("=")
        if separator and option == "--cf-clearance":
            values[index] = f"{option}={cookie}"
            return tuple(values)
    # Priority 3: Append new --cookie option
    values.extend(("--cookie", cookie))
    return tuple(values)


def request_fresh_cookie(
    incident: "RecoveryIncident",
    input_stream=None,
    output=None,
) -> Optional[Tuple[str, ...]]:
    """Ask the terminal user for a replacement cookie without echoing it."""
    input_stream = input_stream or sys.stdin
    output = output or sys.stdout
    if not getattr(input_stream, "isatty", lambda: False)():
        print("Cookie có thể đã hết hạn; hãy chạy trong terminal và truyền --cookie mới.",
              file=output, flush=True)
        return None
    print("Cookie có thể đã hết hạn. Dán cookie mới, hoặc Enter để bỏ qua: ",
          end="", file=output, flush=True)
    value = input_stream.readline().strip()
    from .utils.sanitize import sanitize_cookie_input
    value = sanitize_cookie_input(value) or ""
    return _replace_cookie_argument(incident.retry_argv, value) if value else None


@dataclass(frozen=True)
class CookieShape:
    """Non-secret structural information from a cookie argument."""

    kind: str
    names: Tuple[str, ...]
    valid_segments: int
    invalid_segments: int

    @classmethod
    def from_input(cls, value: Optional[str]) -> "CookieShape":
        from .utils.sanitize import sanitize_cookie_input
        cleaned = sanitize_cookie_input(value) or ""
        text = cleaned.strip()
        if not text:
            return cls("empty", (), 0, 0)

        if text.startswith("{"):
            try:
                parsed = json.loads(text)
            except (TypeError, ValueError, json.JSONDecodeError):
                return cls("json-malformed", (), 0, 1)
            if isinstance(parsed, dict):
                names = tuple(sorted(str(key) for key in parsed))
                return cls("json", names, len(names), 0)
            return cls("json-malformed", (), 0, 1)

        segments = [part.strip() for part in text.split(";") if part.strip()]
        pairs = [part for part in segments if "=" in part]
        if not pairs:
            return cls("raw-token", ("session",), 1, 0)

        raw_names = set()
        for part in pairs:
            name = part.split("=", 1)[0].strip()
            if re.match(r"^[Cc][Oo][Oo][Kk][Ii][Ee]\s*:", name):
                name = re.sub(r"^[Cc][Oo][Oo][Kk][Ii][Ee]\s*:\s*", "", name).strip()
            if name:
                raw_names.add(name)
        names = tuple(sorted(raw_names))
        return cls("header", names, len(pairs), len(segments) - len(pairs))

    def describe(self) -> str:
        names = ", ".join(self.names) if self.names else "(none)"
        return (
            f"kind={self.kind}; names=[{names}]; "
            f"valid_segments={self.valid_segments}; invalid_segments={self.invalid_segments}"
        )


_SECRET_QUERY_KEYS = frozenset({
    "token", "access_token", "auth", "authorization", "cookie", "session",
    "key", "api_key", "password", "signature", "sig",
})


def _redact_url_query(value: str) -> str:
    """Redact secret-bearing query values while retaining URL routing context."""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    if not parsed.scheme or not parsed.netloc or not parsed.query:
        return value
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    if not pairs:
        return value
    redacted = [
        (key, "<redacted>" if key.lower() in _SECRET_QUERY_KEYS else item)
        for key, item in pairs
    ]
    return urlunsplit(parsed._replace(query=urlencode(redacted)))


_FLAG_RE = re.compile(r"^[a-zA-Z0-9_\-]+{[^}\s]+}$")
_SENSITIVE_ENV_PREFIXES = ("AWS_", "AZURE_", "GCP_", "GITHUB_", "GITLAB_", "SLACK_")
_SENSITIVE_ENV_SUBSTRINGS = ("SECRET", "TOKEN", "PASSWORD", "PASSWD", "CREDENTIAL", "AUTH_TOKEN", "API_KEY")


def redact_argv(argv: Sequence[str]) -> list[str]:
    """Return a display-safe command, replacing credential values."""
    redacted: list[str] = []
    hide_next = False
    is_flag_command = bool(argv and str(argv[0]) in ("submit", "hoard"))
    for index, raw in enumerate(argv):
        value = str(raw)
        if hide_next:
            redacted.append("<redacted>")
            hide_next = False
            continue
        option, separator, _argument = value.partition("=")
        if option in _SECRET_OPTIONS and separator:
            redacted.append(f"{option}=<redacted>")
        elif is_flag_command and index > 0 and (index == len(argv) - 1 or _FLAG_RE.match(value)):
            redacted.append("<redacted>")
        elif _FLAG_RE.match(value):
            redacted.append("<redacted>")
        else:
            redacted.append(_redact_url_query(value))
            hide_next = value in _SECRET_OPTIONS
    return redacted


def _cookie_shapes(argv: Sequence[str]) -> Tuple[CookieShape, ...]:
    shapes: list[CookieShape] = []
    for index, raw in enumerate(argv):
        value = str(raw)
        if value in ("-c", "--cookie", "--cf-clearance") and index + 1 < len(argv):
            shapes.append(CookieShape.from_input(str(argv[index + 1])))
        elif value.startswith("--cookie=") or value.startswith("--cf-clearance="):
            shapes.append(CookieShape.from_input(value.split("=", 1)[1]))
    return tuple(shapes)


def _option_value(argv: Sequence[str], names: Sequence[str]) -> Optional[str]:
    """Read a CLI option without exposing it in output or persisted state."""
    accepted = set(names)
    for index, raw in enumerate(argv):
        value = str(raw)
        if value in accepted and index + 1 < len(argv):
            return str(argv[index + 1])
        option, separator, argument = value.partition("=")
        if separator and option in accepted:
            return argument
    return None


def _bqa_environment(incident: "RecoveryIncident") -> dict[str, str]:
    """Pass sanitized environment and allowed credentials to the BQA child environment."""
    environment: dict[str, str] = {}
    for key, value in os.environ.items():
        key_upper = key.upper()
        if any(key_upper.startswith(prefix) for prefix in _SENSITIVE_ENV_PREFIXES):
            continue
        if any(substring in key_upper for substring in _SENSITIVE_ENV_SUBSTRINGS):
            continue
        environment[key] = value

    url = _option_value(incident.retry_argv, ("-u", "--url"))
    cookie = _option_value(incident.retry_argv, ("-c", "--cookie", "--cf-clearance"))
    if url:
        environment["BQA_CTF_URL"] = url
    if cookie and incident.error_code in {
        "CTF-PULL-D01", "CTF-PULL-D02", "CTF-PULL-D03", "CTF-PULL-D04",
        "CTF-PULL-D06", "CTF-PULL-D07",
    }:
        environment["BQA_CTF_COOKIE"] = cookie
    return environment


def _safe_trace(exc: BaseException) -> Tuple[str, ...]:
    if exc.__traceback__ is None:
        return ()
    return tuple(
        f"{os.path.basename(frame.filename)}:{frame.lineno} in {frame.name}"
        for frame in traceback.extract_tb(exc.__traceback__)
    )


@dataclass(frozen=True)
class RecoveryIncident:
    argv: Tuple[str, ...]
    retry_argv: Tuple[str, ...] = field(repr=False)
    exit_code: int
    exception_type: Optional[str]
    trace: Tuple[str, ...]
    cookie_shapes: Tuple[CookieShape, ...]
    error_code: str = "CTF-PULL-D99"
    evidence: dict = field(default_factory=dict)

    @classmethod
    def from_failure(
        cls,
        argv: Sequence[str],
        exit_code: int,
        exc: Optional[BaseException] = None,
    ) -> "RecoveryIncident":
        cookie_shapes = _cookie_shapes(argv)
        error_code = _classify_exception(exc)
        if any(shape.invalid_segments for shape in cookie_shapes) and error_code in {
            "CTF-PULL-D01", "CTF-PULL-D03", "CTF-PULL-D99",
        }:
            error_code = "CTF-PULL-D01"
        return cls(
            argv=tuple(redact_argv(argv)),
            retry_argv=tuple(str(value) for value in argv),
            exit_code=int(exit_code),
            exception_type=type(exc).__name__ if exc is not None else None,
            trace=_safe_trace(exc) if exc is not None else (),
            cookie_shapes=cookie_shapes,
            error_code=error_code,
            evidence=_safe_bqa_evidence(getattr(exc, "bqa_evidence", None)),
        )

    def to_prompt(self) -> str:
        lines = [
            f"Incident code: {self.error_code}",
            f"Command: {' '.join(self.argv)}",
            f"Exit code: {self.exit_code}",
        ]
        if self.exception_type:
            lines.append(f"Exception type: {self.exception_type}")
        if self.trace:
            lines.append("Safe traceback:\n" + "\n".join(self.trace))
        if self.cookie_shapes:
            lines.append("Cookie shapes:\n" + "\n".join(shape.describe() for shape in self.cookie_shapes))
        if self.evidence:
            lines.append("Platform recon evidence (sanitized):\n" + json.dumps(
                self.evidence, indent=2, sort_keys=True, ensure_ascii=False))
        return load_prompt_template(self.error_code).replace("{incident}", "\n".join(lines))


@dataclass(frozen=True)
class BqaSession:
    source_root: str
    source_revision: str
    conversation_id: str


class BqaSessionStore:
    """Small atomic store keyed by canonical source checkout path."""

    def __init__(self, path: os.PathLike[str] | str):
        self.path = Path(path)

    def load(self, source_root: os.PathLike[str] | str) -> Optional[BqaSession]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            item = payload.get(str(Path(source_root).resolve()))
            if not isinstance(item, dict):
                return None
            revision = item.get("source_revision")
            conversation_id = item.get("conversation_id")
            if not isinstance(revision, str) or not isinstance(conversation_id, str):
                return None
            return BqaSession(str(Path(source_root).resolve()), revision, conversation_id)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def save(self, session: BqaSession) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(".lock")
        with open(lock_path, "a+") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                payload = {}
                try:
                    payload = json.loads(self.path.read_text(encoding="utf-8"))
                except (OSError, TypeError, ValueError, json.JSONDecodeError):
                    pass
                if not isinstance(payload, dict):
                    payload = {}
                payload[session.source_root] = {
                    "source_revision": session.source_revision,
                    "conversation_id": session.conversation_id,
                }
                fd, temporary_path = tempfile.mkstemp(prefix=".bqa-sessions-", dir=str(self.path.parent))
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as stream:
                        json.dump(payload, stream, indent=2, sort_keys=True)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary_path, self.path)
                    os.chmod(self.path, 0o600)
                finally:
                    if os.path.exists(temporary_path):
                        os.unlink(temporary_path)
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@dataclass(frozen=True)
class BqaRepairResult:
    conversation_id: Optional[str]
    returncode: int
    changed_test_paths: Tuple[str, ...]
    reason: str


def _default_session_store() -> BqaSessionStore:
    return BqaSessionStore(Path.home() / ".config" / "ctf_toolkit" / "bqa_sessions.json")


def _source_revision(source_root: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(source_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _default_status(message: str) -> None:
    print(message, flush=True)


def _test_fingerprints(source_root: Path) -> dict[str, str]:
    """Hash test files so an agent response cannot omit its own changed tests."""
    tests_root = source_root / "tests"
    if not tests_root.is_dir():
        return {}
    fingerprints: dict[str, str] = {}
    for path in tests_root.rglob("test_*.py"):
        try:
            relative = path.relative_to(source_root).as_posix()
            fingerprints[relative] = sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
    return fingerprints


class BqaRecovery:
    """Resume BQA for one sanitized incident in the current checkout."""

    def __init__(
        self,
        source_root: os.PathLike[str] | str,
        session_store: Optional[BqaSessionStore] = None,
        popen_factory: Callable[..., object] = subprocess.Popen,
        status_writer: Callable[[str], None] = _default_status,
        revision_provider: Callable[[Path], str] = _source_revision,
    ):
        self.source_root = Path(source_root).resolve()
        self.session_store = session_store or _default_session_store()
        self.popen_factory = popen_factory
        self.status_writer = status_writer
        self.revision_provider = revision_provider

    def _bootstrap_prompt(self, incident: RecoveryIncident) -> str:
        return "\n".join((
            "Bootstrap a persistent BQA session for this CTF CLI source checkout.",
            f"Source root: {self.source_root}",
            "Read the existing code before editing. Only repair CLI reliability; never solve a CTF challenge.",
            "Never print, persist, commit, or include credential values in prompts, source, tests, or output. Add a regression test and run it.",
            incident.to_prompt(),
        ))

    @staticmethod
    def _safe_changed_tests(value: object) -> Tuple[str, ...]:
        if not isinstance(value, list):
            return ()
        paths = []
        for raw in value:
            candidate = str(raw).replace("\\", "/")
            if candidate.startswith("tests/") and ".." not in candidate.split("/"):
                paths.append(candidate)
        return tuple(paths)

    def repair(self, incident: RecoveryIncident) -> BqaRepairResult:
        revision = self.revision_provider(self.source_root)
        existing = self.session_store.load(self.source_root)
        resume = existing is not None and existing.source_revision == revision
        before_tests = _test_fingerprints(self.source_root)
        agy_bin = resolve_agy_binary("agy")
        command = [
            agy_bin, "--mode", "accept-edits", "--dangerously-skip-permissions",
            "--output-format", "json",
        ]
        if resume:
            command.extend(("--conversation", existing.conversation_id))
            prompt = incident.to_prompt()
        else:
            prompt = self._bootstrap_prompt(incident)
        command.extend(("--print", prompt))

        self.status_writer("Need help? BQA is investigating this failure…")
        self.status_writer("[BQA] BQA is collecting diagnostics")
        try:
            try:
                process = self.popen_factory(
                    command,
                    cwd=str(self.source_root),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    env=_bqa_environment(incident),
                    start_new_session=True,
                )
            except TypeError:
                process = self.popen_factory(
                    command,
                    cwd=str(self.source_root),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    env=_bqa_environment(incident),
                )
        except OSError as exc:
            return BqaRepairResult(None, 127, (), f"BQA could not start: {exc}")

        output_chunks: list[str] = []

        def _drain_stdout() -> None:
            stdout = getattr(process, "stdout", None)
            if stdout is not None:
                try:
                    for line in iter(stdout.readline, ""):
                        output_chunks.append(line)
                except Exception:
                    pass

        drain_thread = threading.Thread(target=_drain_stdout, daemon=True)
        drain_thread.start()

        statuses = ("[BQA] BQA is updating platform support", "[BQA] BQA is running regression tests")
        status_index = 0
        try:
            while process.poll() is None:
                self.status_writer(statuses[status_index % len(statuses)])
                status_index += 1
                time.sleep(0.25)
            returncode = int(process.wait())
            drain_thread.join(timeout=2.0)
            if drain_thread.is_alive():
                pid = getattr(process, "pid", None)
                if isinstance(pid, int) and pid > 0:
                    with contextlib.suppress(ProcessLookupError, OSError):
                        os.killpg(pid, signal.SIGKILL)
                drain_thread.join(timeout=0.5)
        except KeyboardInterrupt:
            pid = getattr(process, "pid", None)
            if isinstance(pid, int) and pid > 0:
                with contextlib.suppress(ProcessLookupError, OSError):
                    os.killpg(pid, signal.SIGTERM)
                try:
                    process.wait(timeout=1.0)
                except (subprocess.TimeoutExpired, TypeError, AttributeError):
                    with contextlib.suppress(ProcessLookupError, OSError):
                        os.killpg(pid, signal.SIGKILL)
            terminate = getattr(process, "terminate", None)
            if callable(terminate):
                with contextlib.suppress(Exception):
                    terminate()
            drain_thread.join(timeout=0.5)
            raise

        output = "".join(output_chunks)
        if not output and not drain_thread.is_alive():
            stdout = getattr(process, "stdout", None)
            if stdout is not None:
                with contextlib.suppress(Exception):
                    output = stdout.read() or ""
        payload = extract_json_payload(output)
        if not isinstance(payload, dict):
            snippet = output.strip()[-300:] if output.strip() else ""
            msg = f"BQA returned invalid JSON: {snippet}" if snippet else "BQA returned invalid JSON"
            return BqaRepairResult(None, returncode, (), msg)
        conversation_id = payload.get("conversation_id") if isinstance(payload, dict) else None
        if returncode != 0 or payload.get("status") != "SUCCESS":
            error_detail = payload.get("error") or payload.get("response") or "BQA repair did not succeed"
            return BqaRepairResult(conversation_id or None, returncode, (), f"BQA repair did not succeed: {error_detail}")
        if not isinstance(conversation_id, str) or not conversation_id:
            return BqaRepairResult(None, returncode, (), "BQA did not return a conversation ID")

        self.session_store.save(BqaSession(str(self.source_root), revision, conversation_id))
        after_tests = _test_fingerprints(self.source_root)
        detected_tests = {
            path for path in set(before_tests) | set(after_tests)
            if before_tests.get(path) != after_tests.get(path)
        }
        reported_tests = set(self._safe_changed_tests(payload.get("changed_test_paths")))
        return BqaRepairResult(
            conversation_id,
            returncode,
            tuple(sorted(detected_tests | reported_tests)),
            "BQA repair completed",
        )


def verify_bqa_changes(
    source_root: os.PathLike[str] | str,
    changed_test_paths: Sequence[str],
    run: Callable[..., object] = subprocess.run,
) -> bool:
    """Run deterministic local gates before allowing a repaired command retry."""
    root = Path(source_root).resolve()
    commands = [
        [sys.executable, "-m", "compileall", "-q", "ctf_downloader"],
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
    ]
    for path in changed_test_paths:
        normalized = str(path).replace("\\", "/")
        if not normalized.startswith("tests/") or ".." in normalized.split("/"):
            return False
        commands.append([sys.executable, "-m", "pytest", normalized, "-q"])
    for command in commands:
        try:
            result = run(command, cwd=str(root), check=False)
        except OSError:
            return False
        if int(getattr(result, "returncode", 1)) != 0:
            return False
    return True


def retry_command(
    argv: Sequence[str],
    source_root: os.PathLike[str] | str,
    run: Callable[..., object] = subprocess.run,
) -> int:
    """Run the original command in a fresh process with recovery disabled."""
    environment = os.environ.copy()
    environment["CTF_BQA_RETRY"] = "1"
    command = [sys.executable, "-m", "ctf_downloader.cli", *map(str, argv)]
    try:
        result = run(command, cwd=str(Path(source_root).resolve()), env=environment, check=False)
    except OSError:
        return 127
    return int(getattr(result, "returncode", 1))
