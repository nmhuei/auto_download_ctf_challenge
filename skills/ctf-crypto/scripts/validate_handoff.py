#!/usr/bin/env python3
"""Validate that an Astra handoff contains only a complete math problem."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any


REQUIRED_FIELDS = {
    "schema_version": int,
    "objective": str,
    "domain": str,
    "variables": list,
    "constraints": list,
    "artifacts": list,
    "verification": list,
    "requested_output": list,
}
ALLOWED_ARTIFACT_SUFFIXES = {".csv", ".json", ".npy", ".npz", ".sobj"}
FORBIDDEN_TERMS = {
    "binary",
    "challenge",
    "ciphertext",
    "cookie",
    "credential",
    "ctf",
    "decrypt",
    "encryption",
    "encrypted",
    "exploit",
    "flag",
    "key",
    "network",
    "nonce",
    "oracle",
    "packet",
    "password",
    "payload",
    "plaintext",
    "session",
    "signature",
    "source",
    "source code",
    "token",
    "url",
    "writeup",
}


def strings_in(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for element in value for item in strings_in(element)]
    if isinstance(value, dict):
        return [item for element in value.values() for item in strings_in(element)]
    return []


def forbidden_terms(payload: dict[str, Any]) -> list[str]:
    text = "\n".join(strings_in(payload)).lower()
    return sorted(
        term for term in FORBIDDEN_TERMS
        if re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text)
    )


def validate(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return ["handoff must be a JSON object"]

    errors: list[str] = []
    for field, expected_type in REQUIRED_FIELDS.items():
        value = payload.get(field)
        if not isinstance(value, expected_type) or (expected_type in (str, list) and not value):
            errors.append(f"{field} is required and must be a non-empty {expected_type.__name__}")

    if payload.get("schema_version") != 1:
        errors.append("schema_version must equal 1")

    variables = payload.get("variables", [])
    if isinstance(variables, list):
        for index, variable in enumerate(variables):
            if not isinstance(variable, dict) or not all(
                isinstance(variable.get(key), str) and variable[key]
                for key in ("name", "domain", "bounds")
            ):
                errors.append(f"variables[{index}] must contain non-empty name, domain, and bounds")

    artifacts = payload.get("artifacts", [])
    if isinstance(artifacts, list):
        for artifact in artifacts:
            path = Path(artifact) if isinstance(artifact, str) else None
            if path is None or path.name != artifact or path.suffix not in ALLOWED_ARTIFACT_SUFFIXES:
                errors.append("artifacts must be bare numerical filenames with an allowed suffix")
                break

    terms = forbidden_terms(payload)
    if terms:
        errors.append(f"not mathematics-only: forbidden term(s): {', '.join(terms)}")
    return errors


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: validate_handoff.py HANDOFF.json", file=sys.stderr)
        return 2
    handoff = Path(sys.argv[1])
    try:
        payload = json.loads(handoff.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cannot read handoff: {exc}", file=sys.stderr)
        return 2

    errors = validate(payload)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("mathematics-only handoff is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
