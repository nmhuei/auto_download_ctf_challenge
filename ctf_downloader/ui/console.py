"""Console instances and the output-discipline convention.

Convention (borrowed from uv / cargo):

- Every progress, status, diagnostic or human-facing message goes to
  ``err_console`` (stderr).
- ``out_console`` (stdout) carries ONLY machine-readable data: JSON blobs,
  bare paths, tab-separated records — anything a caller may safely pipe.
  Never print decorated text to stdout.

Keeping this rule means `tool ... | jq` and `tool ... 2>/dev/null` always
behave predictably.
"""

from __future__ import annotations

from rich.console import Console

#: Human-facing output: progress bars, status lines, diagnostics. -> stderr
err_console = Console(stderr=True)

#: Machine-readable output only (JSON, paths, piped data). -> stdout
out_console = Console(soft_wrap=False)

__all__ = ["err_console", "out_console"]
