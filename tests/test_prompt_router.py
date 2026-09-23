"""Tests for deterministic prompt routing engine."""

import pytest
from ctf_downloader.services.prompt_router import RouteAction, normalize_prompt, route_prompt


def test_normalize_prompt_telex():
    assert "tiến độ" in normalize_prompt("tieens ddooj")
    assert "có thể" in normalize_prompt("cos theer ddos laf 1 hint")
    assert "cái này" in normalize_prompt("cai nafy laf flag decoy")


@pytest.mark.parametrize(
    "raw,expected_action,expected_target",
    [
        ("pwn5", RouteAction.SELECT, "pwn5"),
        ("pwn10", RouteAction.SELECT, "pwn10"),
        ("rev12", RouteAction.SELECT, "rev12"),
        ("crypto_3", RouteAction.SELECT, "crypto3"),
        ("/ctf-toolkit pwn7", RouteAction.SELECT, "pwn7"),
        ("/ctf Lottery", RouteAction.SELECT, "Lottery"),
        ("next", RouteAction.CONTINUE, None),
        ("nexxt", RouteAction.CONTINUE, None),
        ("continue", RouteAction.CONTINUE, None),
        ("/ctf-toolkit next", RouteAction.CONTINUE, None),
        ("tieens ddooj", RouteAction.STATUS, None),
        ("tiến độ", RouteAction.STATUS, None),
        ("status", RouteAction.STATUS, None),
        ("decoy", RouteAction.REJECT_CANDIDATE, None),
        ("cai nafy laf flag decoy, timf ddoof thaajt", RouteAction.REJECT_CANDIDATE, None),
        ("decoy flag{tcache_local_placeholder}", RouteAction.REJECT_CANDIDATE, None),
        ("cos theer ddos laf 1 hint hay j ddos", RouteAction.HYPOTHESIS, None),
        ("server chỉ có thể connect qua lan", RouteAction.CONSTRAINT, None),
    ],
)
def test_route_prompt_cases(raw, expected_action, expected_target):
    decision = route_prompt(raw)
    assert decision.action == expected_action
    if expected_target is not None:
        assert decision.target == expected_target


def test_precedence_decoy_over_flag():
    # If a prompt mentions both decoy and a candidate token, reject candidate must win
    decision = route_prompt("decoy flag{test_token}")
    assert decision.action == RouteAction.REJECT_CANDIDATE


def test_command_prompt_builder():
    from unittest.mock import MagicMock
    from ctf_downloader.services.prompt_builder import CategoryPromptBuilder

    job = MagicMock()
    job.name = "🎟_The_Lottery_Race"
    cmd_prompt = CategoryPromptBuilder.build_command_prompt(job)
    assert cmd_prompt == "/ctf-toolkit 🎟_The_Lottery_Race"

    minimal_prompt = CategoryPromptBuilder.build(job, minimal=True)
    assert minimal_prompt == "/ctf-toolkit 🎟_The_Lottery_Race"

