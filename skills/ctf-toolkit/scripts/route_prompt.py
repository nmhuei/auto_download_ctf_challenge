#!/usr/bin/env python3
"""Route a user prompt into a deterministic CTF command decision."""

import json
import sys
from pathlib import Path

# Add project root to sys.path so we can import the service
project_root = Path(__file__).resolve().parents[3]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from ctf_downloader.services.prompt_router import RouteDecision, route_prompt


def main():
    if len(sys.argv) < 2:
        print("Usage: route_prompt.py <prompt>")
        sys.exit(1)
    prompt = " ".join(sys.argv[1:])
    decision: RouteDecision = route_prompt(prompt)
    output = {
        "action": decision.action.value,
        "target": decision.target,
        "raw_prompt": decision.raw_prompt,
        "normalized_prompt": decision.normalized_prompt,
        "reason": decision.reason,
    }
    print(json.dumps(output, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
