# [CTF-AUTH-A01]

Incident class: authentication failure or session expired.

The credentials (session cookie or token) may be invalid or expired.
Verify whether the authentication handling in the platform adapter is correct.
If the adapter logic has a bug in session handling, repair it and add a regression test.
Do not prompt the user for secrets programmatically.
Never print, persist, request, or commit credentials.
