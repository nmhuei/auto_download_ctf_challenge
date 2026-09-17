import json
import os
from io import StringIO
from types import SimpleNamespace

from ctf_downloader.bqa_recovery import (
    BqaRecovery,
    BqaSession,
    BqaSessionStore,
    CookieShape,
    RecoveryIncident,
    redact_argv,
    retry_command,
    verify_bqa_changes,
)


def test_cookie_shape_keeps_names_but_never_values():
    shape = CookieShape.from_input("session=super-secret; cf_clearance=another-secret; broken")

    assert shape.kind == "header"
    assert shape.names == ("cf_clearance", "session")
    assert shape.valid_segments == 2
    assert shape.invalid_segments == 1
    assert "super-secret" not in shape.describe()
    assert "another-secret" not in shape.describe()


def test_cookie_shape_describes_json_and_raw_token_without_secret():
    json_shape = CookieShape.from_input(json.dumps({"GZCTF_Token": "top-secret"}))
    raw_shape = CookieShape.from_input("opaque-session-value")

    assert json_shape.kind == "json"
    assert json_shape.names == ("GZCTF_Token",)
    assert raw_shape.kind == "raw-token"
    assert raw_shape.names == ("session",)
    assert "top-secret" not in json_shape.describe()
    assert "opaque-session-value" not in raw_shape.describe()


def test_redact_argv_hides_all_credential_option_values():
    argv = [
        "pull", "--cookie", "cookie-value", "-t", "token-value",
        "--password=pass-value", "-f", "FLAG{secret}",
        "--cf-clearance", "clearance-value", "--url", "https://ctf.example",
    ]

    assert redact_argv(argv) == [
        "pull", "--cookie", "<redacted>", "-t", "<redacted>",
        "--password=<redacted>", "-f", "<redacted>",
        "--cf-clearance", "<redacted>", "--url", "https://ctf.example",
    ]


def test_incident_prompt_carries_cookie_shape_but_not_secret_values():
    incident = RecoveryIncident.from_failure(
        ["pull", "--cookie", "session=real-secret; malformed"],
        exit_code=1,
        exc=RuntimeError("upstream said token=real-secret"),
    )

    prompt = incident.to_prompt()
    assert "session" in prompt
    assert "malformed" not in prompt
    assert "real-secret" not in prompt
    assert "RuntimeError" in prompt


class _FakeProcess:
    def __init__(self, payload):
        self.stdout = StringIO(json.dumps(payload))
        self.returncode = 0

    def poll(self):
        return self.returncode

    def wait(self):
        return self.returncode


def test_bqa_bootstraps_then_resumes_workspace_session(tmp_path):
    calls = []
    statuses = []

    def fake_popen(command, **kwargs):
        calls.append((command, kwargs))
        return _FakeProcess({
            "conversation_id": "conversation-1",
            "status": "SUCCESS",
            "changed_test_paths": ["tests/test_new_platform.py"],
        })

    store = BqaSessionStore(tmp_path / "sessions.json")
    recovery = BqaRecovery(
        source_root=tmp_path,
        session_store=store,
        popen_factory=fake_popen,
        status_writer=statuses.append,
        revision_provider=lambda _root: "rev-1",
    )
    incident = RecoveryIncident.from_failure(["pull", "--cookie", "session=secret"], 1)

    first = recovery.repair(incident)
    second = recovery.repair(incident)

    assert first.conversation_id == "conversation-1"
    assert first.changed_test_paths == ("tests/test_new_platform.py",)
    assert "--dangerously-skip-permissions" in calls[0][0]
    assert "--conversation" not in calls[0][0]
    assert calls[1][0][calls[1][0].index("--conversation") + 1] == "conversation-1"
    assert any("BQA is collecting diagnostics" in status for status in statuses)
    assert "secret" not in calls[0][0][-1]


def test_bqa_discards_session_created_for_different_revision(tmp_path):
    calls = []

    def fake_popen(command, **kwargs):
        calls.append(command)
        return _FakeProcess({"conversation_id": "fresh", "status": "SUCCESS"})

    store = BqaSessionStore(tmp_path / "sessions.json")
    store.save(BqaSession(str(tmp_path), "old-revision", "obsolete"))
    recovery = BqaRecovery(
        source_root=tmp_path,
        session_store=store,
        popen_factory=fake_popen,
        revision_provider=lambda _root: "new-revision",
    )

    result = recovery.repair(RecoveryIncident.from_failure(["pull"], 1))

    assert result.conversation_id == "fresh"
    assert "--conversation" not in calls[0]


def test_bqa_detects_changed_test_files_without_agent_self_report(tmp_path):
    calls = []

    def fake_popen(command, **kwargs):
        calls.append(command)
        test_path = tmp_path / "tests" / "test_repaired_platform.py"
        test_path.parent.mkdir()
        test_path.write_text("def test_repaired_platform():\n    assert True\n", encoding="utf-8")
        return _FakeProcess({"conversation_id": "fresh", "status": "SUCCESS"})

    recovery = BqaRecovery(
        source_root=tmp_path,
        session_store=BqaSessionStore(tmp_path / "sessions.json"),
        popen_factory=fake_popen,
        revision_provider=lambda _root: "rev-1",
    )

    result = recovery.repair(RecoveryIncident.from_failure(["pull"], 1))

    assert result.changed_test_paths == ("tests/test_repaired_platform.py",)


def test_verification_checks_compile_collection_and_changed_tests_before_retry(tmp_path):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    assert verify_bqa_changes(tmp_path, ("tests/test_added_platform.py",), fake_run)
    assert [command[-1] for command, _kwargs in calls] == [
        "ctf_downloader", "-q", "-q",
    ]
    assert calls[-1][0][-2:] == ["tests/test_added_platform.py", "-q"]


def test_failed_verification_prevents_remaining_checks(tmp_path):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=1)

    assert not verify_bqa_changes(tmp_path, ("tests/test_added_platform.py",), fake_run)
    assert len(calls) == 1


def test_retry_uses_fresh_python_process_with_bqa_sentinel(tmp_path):
    received = {}

    def fake_run(command, **kwargs):
        received["command"] = command
        received["environment"] = kwargs["env"]
        return SimpleNamespace(returncode=7)

    assert retry_command(["pull", "--url", "https://ctf.example"], tmp_path, fake_run) == 7
    assert received["command"][1:3] == ["-m", "ctf_downloader.cli"]
    assert received["command"][3:] == ["pull", "--url", "https://ctf.example"]
    assert received["environment"]["CTF_BQA_RETRY"] == "1"
    assert received["environment"].get("PATH") == os.environ.get("PATH")


def test_every_pull_error_code_has_a_packaged_template():
    from ctf_downloader.bqa_recovery import PULL_ERROR_CODES, load_prompt_template

    for code in PULL_ERROR_CODES:
        template = load_prompt_template(code)
        assert f"[{code}]" in template
        assert "Never print, log, persist" in template


def test_incident_uses_error_specific_template_without_credential_values():
    from ctf_downloader.bqa_recovery import PullCommandFailure

    incident = RecoveryIncident.from_failure(
        ["pull", "--cookie", "session=actual-secret", "--url", "https://ctf.example"],
        1,
        PullCommandFailure("CTF-PULL-D02"),
    )

    assert incident.error_code == "CTF-PULL-D02"
    prompt = incident.to_prompt()
    assert "unknown or unsupported CTF platform" in prompt
    assert "actual-secret" not in prompt


class _InteractiveInput(StringIO):
    def isatty(self):
        return True


class _PipeInput(StringIO):
    def isatty(self):
        return False


def test_bqa_consent_requires_explicit_tty_yes_and_renders_code():
    from ctf_downloader.bqa_recovery import request_bqa_help

    incident = RecoveryIncident.from_failure(["pull"], 1)
    output = StringIO()

    assert request_bqa_help(incident, input_stream=_InteractiveInput("y\n"), output=output)
    rendered = output.getvalue()
    assert "[CTF-PULL-D99]" in rendered
    assert "SuperBQA? [Y/n]" in rendered

    output = StringIO()
    assert request_bqa_help(incident, input_stream=_InteractiveInput("\n"), output=output)

    output = StringIO()
    assert not request_bqa_help(incident, input_stream=_InteractiveInput("n\n"), output=output)
    assert "không chạy" in output.getvalue().lower()


def test_bqa_consent_never_runs_in_non_interactive_mode():
    from ctf_downloader.bqa_recovery import request_bqa_help

    output = StringIO()
    incident = RecoveryIncident.from_failure(["pull"], 1)

    assert not request_bqa_help(incident, input_stream=_PipeInput("y\n"), output=output)
    assert "terminal tương tác" in output.getvalue()


def test_bqa_receives_cookie_only_through_child_environment(tmp_path):
    captured = {}

    def fake_popen(command, **kwargs):
        captured["prompt"] = command[-1]
        captured["environment"] = kwargs["env"]
        return _FakeProcess({"conversation_id": "conversation-1", "status": "SUCCESS"})

    recovery = BqaRecovery(
        source_root=tmp_path,
        session_store=BqaSessionStore(tmp_path / "sessions.json"),
        popen_factory=fake_popen,
        revision_provider=lambda _root: "rev-1",
    )
    incident = RecoveryIncident.from_failure(
        ["pull", "--url", "https://ctf.example", "--cookie", "session=secret-value"],
        1,
        __import__("ctf_downloader.bqa_recovery", fromlist=["PullCommandFailure"])
        .PullCommandFailure("CTF-PULL-D02"),
    )

    recovery.repair(incident)
    assert captured["environment"]["BQA_CTF_COOKIE"] == "session=secret-value"
    assert captured["environment"]["BQA_CTF_URL"] == "https://ctf.example"
    assert "secret-value" not in captured["prompt"]


def test_redact_argv_hides_sensitive_url_query_values():
    command = redact_argv([
        "pull", "--url", "https://ctf.example/challenges?token=secret-token&view=all",
    ])

    assert "secret-token" not in command[-1]
    assert "token=%3Credacted%3E" in command[-1]
    assert "view=all" in command[-1]


def test_contaminated_cookie_failure_is_routed_to_parser_template():
    from ctf_downloader.bqa_recovery import PullCommandFailure

    incident = RecoveryIncident.from_failure(
        ["pull", "--cookie", "session=old-valid; pasted-junk"],
        1,
        PullCommandFailure("CTF-PULL-D03"),
    )

    assert incident.error_code == "CTF-PULL-D01"
    assert "old-valid" not in incident.to_prompt()
    assert "pasted-junk" not in incident.to_prompt()


def test_fresh_cookie_prompt_replaces_only_runtime_retry_argument():
    from ctf_downloader.bqa_recovery import request_fresh_cookie

    incident = RecoveryIncident.from_failure(
        ["pull", "--url", "https://ctf.example", "--cookie", "session=expired"],
        1,
        __import__("ctf_downloader.bqa_recovery", fromlist=["PullCommandFailure"])
        .PullCommandFailure("CTF-PULL-D03"),
    )
    output = StringIO()

    retry_argv = request_fresh_cookie(
        incident,
        input_stream=_InteractiveInput("session=fresh-secret\n"),
        output=output,
    )

    assert retry_argv[-1] == "session=fresh-secret"
    assert "fresh-secret" not in output.getvalue()
    assert "cookie mới" in output.getvalue()


def test_platform_recon_evidence_is_in_prompt_without_response_values():
    from ctf_downloader.bqa_recovery import PullCommandFailure

    incident = RecoveryIncident.from_failure(
        ["pull", "--url", "https://ctf.example"],
        1,
        PullCommandFailure("CTF-PULL-D02", evidence={
            "detector_signals": ["no known marker"],
            "http_status": 200,
            "api_paths": ["/api/challenges"],
            "response_schema": {"type": "object", "keys": ["challenges"]},
        }),
    )

    prompt = incident.to_prompt()
    assert "no known marker" in prompt
    assert "/api/challenges" in prompt
    assert "response_schema" in prompt


def test_fresh_cookie_prioritizes_cookie_option_over_cf_clearance():
    from ctf_downloader.bqa_recovery import request_fresh_cookie

    incident = RecoveryIncident.from_failure(
        ["pull", "--cf-clearance", "cf-token", "--cookie", "session=old-session"],
        1,
    )
    retry_argv = request_fresh_cookie(
        incident,
        input_stream=_InteractiveInput("session=new-session\n"),
        output=StringIO(),
    )

    assert retry_argv is not None
    assert "--cookie" in retry_argv
    cookie_idx = retry_argv.index("--cookie")
    assert retry_argv[cookie_idx + 1] == "session=new-session"
    cf_idx = retry_argv.index("--cf-clearance")
    assert retry_argv[cf_idx + 1] == "cf-token"


def test_bqa_concurrent_drain_handles_large_output_payload(tmp_path):
    class _LargeOutputProcess:
        def __init__(self, data):
            self.stdout = StringIO(data)
            self.returncode = 0

        def poll(self):
            return self.returncode

        def wait(self):
            return self.returncode

    large_payload = json.dumps({
        "conversation_id": "conv-large",
        "status": "SUCCESS",
        "diagnostic_dump": "X" * 128000,
        "changed_test_paths": [],
    })

    recovery = BqaRecovery(
        source_root=tmp_path,
        session_store=BqaSessionStore(tmp_path / "sessions.json"),
        popen_factory=lambda *args, **kwargs: _LargeOutputProcess(large_payload),
        status_writer=lambda _msg: None,
        revision_provider=lambda _root: "rev-large",
    )
    result = recovery.repair(RecoveryIncident.from_failure(["pull"], 1))

    assert result.conversation_id == "conv-large"
    assert result.returncode == 0


def test_redact_argv_hides_positional_flags_for_submit_and_hoard():
    # Positional flag in submit
    assert redact_argv(["submit", "FLAG{pos_secret}"]) == ["submit", "<redacted>"]
    assert redact_argv(["submit", "web/chall_1", "FLAG{pos_secret}"]) == [
        "submit", "web/chall_1", "<redacted>",
    ]
    # Positional flag in hoard
    assert redact_argv(["hoard", "FLAG{hoard_secret}"]) == ["hoard", "<redacted>"]
    # Standalone flag in any command
    assert redact_argv(["pull", "pwn/chall_2", "CTF{flag_format}"]) == [
        "pull", "pwn/chall_2", "<redacted>",
    ]


def test_bqa_environment_strips_sensitive_host_secrets(monkeypatch):
    from ctf_downloader.bqa_recovery import _bqa_environment

    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "sensitive-aws-key")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secrettoken123")
    monkeypatch.setenv("DATABASE_PASSWORD", "dbpass456")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("USER", "testuser")

    incident = RecoveryIncident.from_failure(
        ["pull", "--url", "https://ctf.test", "--cookie", "session=123"],
        exit_code=1,
    )
    env = _bqa_environment(incident)

    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "GITHUB_TOKEN" not in env
    assert "DATABASE_PASSWORD" not in env
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["USER"] == "testuser"
    assert env.get("BQA_CTF_URL") == "https://ctf.test"


def test_bqa_session_store_flock_concurrency(tmp_path):
    import concurrent.futures

    store = BqaSessionStore(tmp_path / "sessions.json")

    def _worker(idx):
        store.save(BqaSession(f"/path/to/root_{idx}", f"rev_{idx}", f"conv_{idx}"))

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(_worker, range(20)))

    # Verify all 20 sessions were saved without lost updates
    payload = json.loads((tmp_path / "sessions.json").read_text(encoding="utf-8"))
    assert len(payload) == 20
    for idx in range(20):
        assert f"/path/to/root_{idx}" in payload


