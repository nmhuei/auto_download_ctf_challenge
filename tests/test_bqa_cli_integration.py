import pytest

from ctf_downloader.cli import run_with_bqa_recovery


def test_nonzero_command_exit_invokes_recovery_once_and_uses_retry_code():
    incidents = []

    def failing_dispatch():
        raise SystemExit(1)

    def recovery(incident):
        incidents.append(incident)
        return 0

    assert run_with_bqa_recovery(["pull", "--cookie", "session=secret"], failing_dispatch, recovery) == 0
    assert len(incidents) == 1
    assert "secret" not in incidents[0].to_prompt()


def test_successful_command_does_not_invoke_recovery():
    assert run_with_bqa_recovery(["status"], lambda: None, lambda _incident: pytest.fail("called")) == 0


def test_retry_sentinel_skips_recovery(monkeypatch):
    monkeypatch.setenv("CTF_BQA_RETRY", "1")

    assert run_with_bqa_recovery(
        ["pull"], lambda: (_ for _ in ()).throw(SystemExit(1)), lambda _incident: pytest.fail("called")
    ) == 1


def test_help_and_keyboard_interrupt_skip_recovery():
    assert run_with_bqa_recovery(
        ["--help"], lambda: (_ for _ in ()).throw(SystemExit(2)), lambda _incident: pytest.fail("called")
    ) == 2
    assert run_with_bqa_recovery(
        ["pull"], lambda: (_ for _ in ()).throw(KeyboardInterrupt()), lambda _incident: pytest.fail("called")
    ) == 130


def test_pull_failure_preserves_its_specific_bqa_code():
    from ctf_downloader.bqa_recovery import PullCommandFailure

    incidents = []

    def recovery(incident):
        incidents.append(incident)
        return 0

    assert run_with_bqa_recovery(
        ["pull"],
        lambda: (_ for _ in ()).throw(PullCommandFailure("CTF-PULL-D10")),
        recovery,
    ) == 0
    assert incidents[0].error_code == "CTF-PULL-D10"


def test_handle_pull_turns_service_failure_into_specific_bqa_incident(monkeypatch):
    from ctf_downloader.cli import build_unified_parser
    from ctf_downloader.cli_commands import handle_pull
    from ctf_downloader.services.pull_service import PullService
    from ctf_downloader.bqa_recovery import PullCommandFailure

    args = build_unified_parser().parse_args(["pull", "--url", "https://ctf.example"])
    monkeypatch.setattr(PullService, "run", lambda _config: {
        "ok": False,
        "bqa_error_code": "CTF-PULL-D03",
    })

    with pytest.raises(PullCommandFailure) as raised:
        handle_pull(args)
    assert raised.value.bqa_error_code == "CTF-PULL-D03"


def test_download_failure_result_codes_distinguish_integrity_and_provider_failures():
    from ctf_downloader.services.pull_service import PullService

    assert PullService._download_failure_code([
        {"success": False, "source": "platform_attachment", "message": "ETag integrity mismatch"},
    ]) == "CTF-PULL-D08"
    assert PullService._download_failure_code([
        {"success": False, "source": "description_gdrive", "message": "quota exceeded"},
    ]) == "CTF-PULL-D07"
    assert PullService._download_failure_code([
        {"success": False, "source": "platform_attachment", "message": "HTTP 403"},
    ]) == "CTF-PULL-D06"


def test_usage_error_and_non_pull_commands_skip_bqa_recovery():
    # exit_code 2 (argparse usage error)
    assert run_with_bqa_recovery(
        ["pull", "--unknown-flag"],
        lambda: (_ for _ in ()).throw(SystemExit(2)),
        lambda _incident: pytest.fail("called for exit 2"),
    ) == 2

    # non-pull subcommand (e.g. submit)
    assert run_with_bqa_recovery(
        ["submit", "chall_1", "FLAG{xyz}"],
        lambda: (_ for _ in ()).throw(SystemExit(1)),
        lambda _incident: pytest.fail("called for submit"),
    ) == 1

