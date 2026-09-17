# [CTF-INSTANCE-U01]

Incident class: unsupported platform feature (e.g. dynamic instance container management).

Read `ctf_downloader/platforms/base.py`, the target platform adapter in `ctf_downloader/platforms/`, and comparable platform adapters (such as `ctfd.py`, `gzctf.py`, `rctf.py`).
Determine whether the target platform actually exposes dynamic instance lifecycle endpoints.
If the platform exposes the capability, implement the required adapter methods (`start_instance`, `stop_instance`, `extend_instance`, `get_instance_status`), add unit tests, and verify they pass.
If the platform itself does not provide container lifecycle capabilities, do not invent non-existent endpoints; ensure proper diagnostics and fallback handling.
Never print, persist, request, or commit cookies, tokens, flags, or raw credentials.
Run focused regression tests and report changed test paths.
