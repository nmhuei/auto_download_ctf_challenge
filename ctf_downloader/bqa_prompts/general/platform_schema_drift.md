# [CTF-PLATFORM-D01]

Incident class: platform schema or API drift.

The platform API response structure or HTML format may have changed.
Read the platform adapter code and trace the parsing error.
Update the parser to be resilient to schema variations while preserving backward compatibility.
Add unit tests reproducing the issue and verifying the fix.
Never print or persist secrets.
Run focused regression tests and report changed test paths.
