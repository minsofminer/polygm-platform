"""The two primitives P07 needs from outside the standard library, and the boot rule that makes their absence
loud: Argon2id for passwords, AES-256-GCM for the key envelope, Ed25519 for third-party initData checks.

This file is deliberately small and deliberately *not* in `packages/polygm_core`: the core is stdlib-only so a
reviewer, a fuzzer and the 4am recovery script can run the rules without a build environment (`tools/lint-rules.py
core-dep-free`). The consequence is that every decision worth arguing about — parameters, AAD, the update rule —
is in the core, and what is here is glue with one opinion: **a missing crypto backend stops the process.**

That is the whole design. A fallback that "keeps working" without Argon2 (say, PBKDF2-SHA1 because it is in the
stdlib) is a silent downgrade on the one path where the downgrade is the incident. The API and the executor call
`require()` at boot; if it raises, the pod does not serve.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets as _secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))) + "/packages")
from polygm_core.security import keys as _keys                     # noqa: E402
from polygm_core.security import passwords as _pw                  # noqa: E402


class BootError(RuntimeError):
    """Raised for anything that must stop the process rather than degrade: a missing dependency, a missing
    secret, a KEK that is not 32 bytes. The message is written for the person at the keyboard at 3am."""


def _require_cryptography():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.exceptions import InvalidTag
    except Exception as exc:                                       # noqa: BLE001
        raise BootError("cryptography is not importable (%s). Install requirements.txt; this process will not "
                        "fall back to a weaker envelope." % exc) from exc
    return AESGCM, InvalidTag


def _require_argon2():
    try:
        from argon2 import PasswordHasher
        from argon2.exceptions import InvalidHashError, VerifyMismatchError
    except Exception as exc:                                       # noqa: BLE001
        raise BootError("argon2-cffi is not importable (%s). We will not hash a password with anything else."
                        % exc) from exc
    return PasswordHasher, VerifyMismatchError, InvalidHashError


class AesGcmEnvelope:
    """DEKs sealed under a KEK. AAD binds every blob to its row, so a wrapped key cannot be moved between
    users by somebody with write access to one column."""

    def __init__(self, kek: bytes, *, version: int = 1) -> None:
        raw = bytes(kek or b"")
        if len(raw) != _keys.KEY_BYTES:
            raise BootError("KEK v%d must be %d bytes; got %d. A short master key is not 'still fine'."
                            % (version, _keys.KEY_BYTES, len(raw)))
        AESGCM, InvalidTag = _require_cryptography()
        self._impl = AESGCM(raw)
        self._invalid = InvalidTag
        self.version = int(version)
        self.counter = 0

    @staticmethod
    def new_kek() -> bytes:
        return _secrets.token_bytes(_keys.KEY_BYTES)

    def _nonce(self) -> bytes:
        # Random 96-bit nonce, plus a counter mixed in, and a hard stop at the budget: the (key, nonce) pair is
        # the thing GCM cannot survive reusing, so the generator owns the budget instead of trusting callers.
        self.counter += 1
        if self.counter > _keys.NONCE_BUDGET:
            raise BootError("this KEK has sealed %d blobs; rotate it (see keys.rotation_plan)" % self.counter)
        return _secrets.token_bytes(_keys.NONCE_BYTES - 4) + self.counter.to_bytes(4, "big")

    @staticmethod
    def aad_bytes(aad: dict) -> bytes:
        return json.dumps(aad, sort_keys=True, separators=(",", ":")).encode()

    def seal(self, plaintext: bytes, aad: dict) -> dict:
        if not _keys.unwrap_verifies_aad(aad):
            # Refused on the way in, not just on the way out: an unbound blob is a blob that can be moved
            # between rows, and "we could not move it" is not the same control as "we could not build one".
            raise ValueError("KEYSTORE_AAD_MISSING: a wrapped key must name the row it belongs to")
        nonce = self._nonce()
        blob = self._impl.encrypt(nonce, bytes(plaintext), self.aad_bytes(aad))
        # `cryptography` returns ciphertext||tag; we split so the tag is a column of its own. A row where the
        # tag is guessable from the length of the blob is a row where a truncation attack is free.
        ct, tag = blob[:-_keys.TAG_BYTES], blob[-_keys.TAG_BYTES:]
        return {"ciphertext": base64.b64encode(ct).decode(), "nonce": base64.b64encode(nonce).decode(),
                "tag": base64.b64encode(tag).decode(), "kek_version": self.version}

    def open(self, *, ciphertext: str, nonce: str, tag: str, aad: dict) -> bytes:
        if not _keys.unwrap_verifies_aad(aad):
            raise ValueError("KEYSTORE_AAD_MISSING: refusing to unwrap without the binding row identity")
        blob = base64.b64decode(ciphertext) + base64.b64decode(tag)
        try:
            return self._impl.decrypt(base64.b64decode(nonce), blob, self.aad_bytes(aad))
        except self._invalid as exc:
            # One name for the operator, one meaning: the blob or the AAD changed. That is a security event, and
            # the caller must not turn it into a 500 that mentions padding or byte offsets.
            raise TamperError("KEYSTORE_TAMPER: the wrapped key or its row identity does not match") from exc


class TamperError(ValueError):
    pass


class Argon2id:
    """Password hashing with the core's parameters, and `verify` that distinguishes the three outcomes.

    `verify` returns a string rather than raising because the route has to *count* failures for the lockout,
    and a `try/except` around a bare `verify()` is where "unknown hash" and "wrong password" get merged into
    one branch and one of them stops being logged.
    """

    def __init__(self) -> None:
        from argon2 import Type
        PasswordHasher, self._mismatch, self._badhash = _require_argon2()
        self._ph = PasswordHasher(time_cost=_pw.PARAMS["time_cost"], memory_cost=_pw.PARAMS["memory_kib"],
                                  parallelism=_pw.PARAMS["parallelism"], hash_len=_pw.PARAMS["hash_len"],
                                  salt_len=_pw.PARAMS["salt_len"], type=Type.ID)

    def hash(self, password: str) -> str:
        return self._ph.hash(password)

    def verify(self, phc: str, password: str) -> str:
        if not phc:
            return "no_credential"
        try:
            self._ph.verify(phc, password)
        except self._mismatch:
            return "bad_password"
        except self._badhash:
            return "bad_hash_format"
        except Exception:                                          # noqa: BLE001 - a kdf we cannot parse is no kdf
            return "bad_hash_format"
        return "ok" if not _pw.needs_update(phc) else "ok_needs_rehash"

    def rehash_needed(self, phc: str) -> bool:
        return _pw.needs_update(phc)


def ed25519_verify(message: bytes, signature_b64url: str, public_key_b64: str) -> bool:
    """Third-party initData validation (`telegram.third_party_check_string`). A constant True here would be the
    worst possible bug in the file, so it is a *refusal* until the public key is configured."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        pub = Ed25519PublicKey.from_public_bytes(base64.urlsafe_b64decode(public_key_b64.encode() + b"=" * 4))
        pub.verify(base64.urlsafe_b64decode(signature_b64url.encode() + b"=" * 4), message)
    except Exception:                                              # noqa: BLE001 - any failure is "not verified"
        return False
    return True


def kek_from_env(*, version: int | None = None) -> tuple[bytes, int]:
    """`PGM_KEK_v1` etc. base64 or hex, 32 bytes after decoding. No default, no dev key, no example value in
    this file: the moment a fallback exists somebody ships with it."""
    v = int(version if version is not None else os.environ.get("PGM_KEK_VERSION", "1"))
    name = "PGM_KEK_v%d" % v
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        raise BootError("%s is not set. This process signs for user money and will not start without a KEK."
                        % name)
    try:
        key = bytes.fromhex(raw) if all(c in "0123456789abcdefABCDEF" for c in raw) and len(raw) == 64 \
            else base64.b64decode(raw)
    except Exception as exc:                                       # noqa: BLE001
        raise BootError("%s is neither 64 hex characters nor base64: %s" % (name, exc)) from exc
    if len(key) != _keys.KEY_BYTES:
        raise BootError("%s decodes to %d bytes; the envelope needs %d" % (name, len(key), _keys.KEY_BYTES))
    return key, v


def required_env() -> tuple[str, ...]:
    return ("PGM_KEK_v1", "PGM_IP_PEPPER", "PGM_SERVICE_TOKEN", "PGM_IMAGE_PROXY_SECRET")


def boot_check() -> dict:
    """Everything the security plane needs before it will serve. Called from the API's startup and from
    `tools/p07-gate-check.py`, so the same list is the deployment check."""
    missing = [k for k in required_env() if not (os.environ.get(k) or "").strip()]
    notes = []
    tok = (os.environ.get("PGM_SERVICE_TOKEN") or "")
    if tok and len(tok) < 32:
        missing.append("PGM_SERVICE_TOKEN(too short: %d chars, need 32)" % len(tok))
    if len(os.environ.get("PGM_IP_PEPPER", "")) < 16:
        notes.append("PGM_IP_PEPPER is short or unset: ip_hash clustering still works, but it is forgeable")
    try:
        AesGcmEnvelope(_secrets.token_bytes(_keys.KEY_BYTES))
        Argon2id()
        crypto = "available"
    except BootError as exc:
        crypto = "MISSING: %s" % exc
        missing.append("crypto backend")
    return {"ok": not missing, "missing": missing, "notes": notes, "backend": crypto,
            "policy": _keys.policy_hash(_keys.DEFAULT_POLICY)[:16]}


def require() -> None:
    res = boot_check()
    if not res["ok"]:
        raise BootError("security plane is not configured: %s" % ", ".join(res["missing"]))


def _selftest() -> dict:
    kek = _secrets.token_bytes(_keys.KEY_BYTES)
    env = AesGcmEnvelope(kek)
    aad = {"user_id": "u1", "kek_version": 1, "dek_version": 1, "policy_hash": "ph"}
    w = env.seal(b"x" * 32, aad)
    ok = env.open(ciphertext=w["ciphertext"], nonce=w["nonce"], tag=w["tag"], aad=aad) == b"x" * 32
    tampered = False
    try:
        env.open(ciphertext=w["ciphertext"], nonce=w["nonce"], tag=w["tag"],
                 aad=dict(aad, user_id="u2"))
    except TamperError:
        tampered = True
    ph = Argon2id()
    h = ph.hash("correct horse battery staple 42")
    return {"envelope_roundtrip": ok, "aad_mismatch_refused": tampered, "verify_ok": ph.verify(h, "correct horse battery staple 42"),
            "verify_bad": ph.verify(h, "nope"), "parses": _pw.parse_phc(h) is not None}


if __name__ == "__main__":
    print(json.dumps(_selftest(), indent=2, sort_keys=True))
