"""Tests for extensible JSON-driven CategoryRegistry and prompt integration."""

import json
from pathlib import Path
import tempfile
import pytest

from ctf_downloader.categories import CategoryDefinition, CategoryRegistry
from ctf_downloader.services.prompt_builder import CategoryPromptBuilder
from ctf_downloader.services.solver_service import SolverJob, _FILTER_RE


@pytest.fixture(autouse=True)
def reset_category_registry():
    """Ensure registry singleton is cleanly reset between tests."""
    CategoryRegistry.reset()
    yield
    CategoryRegistry.reset()


def test_registry_loads_default_categories():
    registry = CategoryRegistry.get_instance()
    keys = [c.key for c in registry.all_categories()]

    expected_keys = [
        "pwn", "crypto", "reverse", "hardware", "web",
        "forensics", "blockchain", "ai", "mobile", "quantum", "osint", "general"
    ]
    for key in expected_keys:
        assert key in keys, f"Expected category '{key}' missing from registry"

    pwn_def = registry.get("pwn")
    assert pwn_def.safe_label == "Binary Analysis"
    assert "Binary Triage" in pwn_def.title_suffix
    assert len(pwn_def.instructions) >= 3


def test_registry_normalization():
    registry = CategoryRegistry.get_instance()

    # Exact matches
    assert registry.normalize("pwn") == "pwn"
    assert registry.normalize("Crypto") == "crypto"
    assert registry.normalize("REVERSE") == "reverse"

    # Display name matches
    assert registry.normalize("Pwnable") == "pwn"
    assert registry.normalize("AI / ML") == "ai"

    # Keyword matching
    assert registry.normalize("heap exploitation") == "pwn"
    assert registry.normalize("Linux Kernel Buffer") == "pwn"
    assert registry.normalize("RSA and Lattices") == "crypto"
    assert registry.normalize("Crackme Decompilation VM") == "reverse"
    assert registry.normalize("SDR Radio Signal") == "hardware"
    assert registry.normalize("GraphQL and SSRF") == "web"
    assert registry.normalize("PCAP Network DFIR") == "forensics"
    assert registry.normalize("Solidity Smart Contract") == "blockchain"
    assert registry.normalize("Jailbreak LLM Model") == "ai"
    assert registry.normalize("Android APK Package") == "mobile"
    assert registry.normalize("Qiskit Qubit") == "quantum"
    assert registry.normalize("Geoint Geolocation") == "osint"

    # Unknown or empty fallbacks to general
    assert registry.normalize("") == "general"
    assert registry.normalize(None) == "general"
    assert registry.normalize("SomeSuperObscureRandomCategory123") == "general"


def test_registry_safe_label_and_metadata():
    registry = CategoryRegistry.get_instance()

    assert registry.safe_label("pwn") == "Binary Analysis"
    assert registry.safe_label("pwnable") == "Binary Analysis"
    assert registry.safe_label("crypto") == "Cryptographic Modeling"
    assert registry.safe_label("web") == "Web Systems"
    assert registry.safe_label("reverse") == "Algorithm Recovery"
    assert registry.safe_label("hardware") == "Signal Analysis"
    assert registry.safe_label("forensics") == "Data Analysis"
    assert registry.safe_label("blockchain") == "Contract Verification"
    assert registry.safe_label("ai") == "Model Verification"
    assert registry.safe_label("mobile") == "Mobile App Analysis"
    assert registry.safe_label("quantum") == "Quantum Modeling"
    assert registry.safe_label("osint") == "Intelligence Analysis"
    assert registry.safe_label("misc") == "Challenge Analysis"
    assert registry.safe_label("unknown") == "Challenge Analysis"

    assert registry.icon("crypto") == "🔒"
    assert registry.icon("pwn") == "⚔️"
    assert registry.color_token("web") == "web"


def test_registry_workspace_override():
    with tempfile.TemporaryDirectory() as temp:
        workspace = Path(temp)
        ctf_dir = workspace / ".ctf"
        ctf_dir.mkdir(parents=True)

        override_data = {
            "version": 1,
            "categories": [
                {
                    "key": "satellite",
                    "display_name": "Satellite",
                    "safe_label": "Telemetry Analysis",
                    "title_suffix": "Orbital Telemetry & Modulation Triage",
                    "match_keywords": ["satellite", "cubesat", "orbital", "telemetry"],
                    "instructions": [
                        "Parse ephemeris and downlink framing format.",
                        "Inspect Doppler compensation logic in script/.",
                        "If a candidate flag is found, save it to flag.txt."
                    ],
                    "icon": "🛰️",
                    "color_token": "accent"
                },
                {
                    "key": "crypto",
                    "display_name": "Crypto",
                    "safe_label": "Algebraic Modeling",
                    "title_suffix": "Workspace Custom Crypto Modeling",
                    "match_keywords": ["crypto", "cipher", "rsa"],
                    "instructions": [
                        "Workspace custom crypto step 1.",
                        "Workspace custom crypto step 2."
                    ],
                    "icon": "🔐",
                    "color_token": "crypto"
                }
            ]
        }
        (ctf_dir / "categories.json").write_text(json.dumps(override_data), encoding="utf-8")

        # Initialize registry for this workspace
        registry = CategoryRegistry(workspace=workspace)

        # 1. Custom category discovered
        assert registry.normalize("cubesat downlink") == "satellite"
        sat_def = registry.get("satellite")
        assert sat_def.safe_label == "Telemetry Analysis"
        assert sat_def.title_suffix == "Orbital Telemetry & Modulation Triage"
        assert len(sat_def.instructions) == 3

        # 2. Existing category overridden
        crypto_def = registry.get("crypto")
        assert crypto_def.safe_label == "Algebraic Modeling"
        assert crypto_def.title_suffix == "Workspace Custom Crypto Modeling"
        assert crypto_def.instructions[0] == "Workspace custom crypto step 1."


def test_category_prompt_builder_with_workspace_override():
    with tempfile.TemporaryDirectory() as temp:
        workspace = Path(temp)
        ctf_dir = workspace / ".ctf"
        ctf_dir.mkdir(parents=True)

        override_data = {
            "categories": [
                {
                    "key": "space",
                    "display_name": "Space",
                    "safe_label": "Space Systems",
                    "title_suffix": "Telemetry & Flight Systems Triage",
                    "match_keywords": ["space", "nasa", "flight"],
                    "instructions": [
                        "Extract telemetry packets from mission log.",
                        "Model attitude control state machine in script/."
                    ],
                    "icon": "🚀"
                }
            ]
        }
        (ctf_dir / "categories.json").write_text(json.dumps(override_data), encoding="utf-8")

        chall_dir = workspace / "Space" / "satellite_1"
        chall_dir.mkdir(parents=True)
        (chall_dir / "challenge").mkdir()

        job = SolverJob(
            display_id=1,
            challenge_id="space-1",
            category="Space",
            name="apollo",
            path=chall_dir,
            has_source=True,
            has_instance=False,
        )

        prompt = CategoryPromptBuilder.build(job, workspace=workspace)
        assert "Challenge: apollo (Telemetry & Flight Systems Triage)" in prompt
        assert "Extract telemetry packets from mission log." in prompt
        assert "Model attitude control state machine in script/." in prompt
        assert "Workspace: Current working directory." in prompt

        # Continuation prompt check
        cont = CategoryPromptBuilder.build(job, is_continuation=True, workspace=workspace)
        assert "CONTINUING SPACE SYSTEMS SESSION" in cont


def test_all_categories_filter_cleanliness():
    registry = CategoryRegistry.get_instance()
    forbidden_terms = [
        "exploit", "payload", "attack", "vulnerability",
        "overflow", "hack", "decompile", "pwnable", "weaponize"
    ]

    for cat_def in registry.all_categories():
        # Title suffix must be clean
        assert not _FILTER_RE.search(cat_def.title_suffix)
        for term in forbidden_terms:
            assert term not in cat_def.title_suffix.lower(), f"Forbidden '{term}' in {cat_def.key} title_suffix"

        # Safe label must be clean
        assert not _FILTER_RE.search(cat_def.safe_label)
        for term in forbidden_terms:
            assert term not in cat_def.safe_label.lower(), f"Forbidden '{term}' in {cat_def.key} safe_label"

        # Instructions must be clean
        for inst in cat_def.instructions:
            assert not _FILTER_RE.search(inst)
            for term in forbidden_terms:
                assert term not in inst.lower(), f"Forbidden '{term}' in {cat_def.key} instruction: {inst}"


def test_runtime_registration():
    registry = CategoryRegistry.get_instance()
    custom = CategoryDefinition(
        key="car_hacking",
        display_name="Automotive",
        safe_label="CAN Bus Analysis",
        title_suffix="Automotive Bus & ECU Analysis",
        match_keywords=["can", "bus", "ecu", "automotive", "car"],
        instructions=["Log CAN bus frames.", "Verify checksums."]
    )
    registry.register(custom)

    assert registry.normalize("Automotive CAN") == "car_hacking"
    fetched = registry.get("car_hacking")
    assert fetched.safe_label == "CAN Bus Analysis"
    assert fetched.instructions == ["Log CAN bus frames.", "Verify checksums."]
