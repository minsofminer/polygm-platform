"""P07 · the security plane: the rules that hold when nobody is watching the deploy.

Everything in here is stdlib-only on purpose (`tools/lint-rules.py: core-dep-free`). A dependency inside an
auth check means the check cannot be run by a reviewer, by a fuzzer, or by the recovery script at 4am on a
machine where `pip install` is the thing that failed. Where a primitive genuinely needs a library — Argon2id,
AES-GCM — the *policy* lives here (parameters, format, what is refused and why) and the primitive is injected
from `services/api/security_backends.py`. The boundary is one object, so "our crypto backend is missing" is a
boot refusal rather than a silent fallback to something worse.
"""
