"""The pseudonym a public surface may name a counterparty by.

The venue's payloads carry wallet addresses, and every one of them is somebody's. So the product's rule is
that an address never leaves the API: a fill log names traders by a stable pseudonym, and the mapping back
lives on the server, next to the salt.

Three properties, each of which was a decision:

  * **stable** - the same address is the same pseudonym across requests, processes and deploys, because a
    client groups fills by trader and a pseudonym that changes per request cannot be grouped;
  * **one-way** - it is a keyed hash, so somebody who has the pseudonym and the address still cannot confirm
    they match without the salt. The salt is an application secret (`PSEUDONYM_SALT`, with a fixed default for
    dev), which is why the value in this repo's fixtures and the value in production are not the same string;
  * **short** - 10 hex characters, because it appears in list payloads and cache keys. Collision risk at 10 hex
    chars is 1 in 1.1e12 per pair; the failure mode of a collision is two wallets sharing a pseudonym, which
    the API's own resolution step detects (it resolves a pseudonym to the wallets that hash to it, and refuses
    to guess when there is more than one).

`anon()` is the ONLY way a client sees a wallet, and `wallet_pseudonyms` (P10's migration) stores the pairs
we have resolved so a lookup does not have to hash every address in the tape.
"""
from __future__ import annotations

import hashlib
import os

PREFIX = "w_"
SALT_PREFIX = "polygm-anon:"          # the domain separator, so this hash is not reused for anything else
HEX_CHARS = 10


def salt() -> str:
    """The key for the hash. An application secret in every real deployment; a constant in dev, because a dev
    database whose pseudonyms change when an env var is unset is a dev database nobody can write a test for."""
    return os.environ.get("PGM_PSEUDONYM_SALT") or SALT_PREFIX


def anon(wallet: str) -> str:
    """`w_` + 10 hex chars of sha256(salt + address). Uppercase and lowercase spellings of the same address are
    the same wallet, so the address is lowercased first - the venue returns both spellings and two pseudonyms
    for one trader would split their history in half."""
    return PREFIX + hashlib.sha256((salt() + str(wallet).lower()).encode()).hexdigest()[:HEX_CHARS]


def is_anon(text: str) -> bool:
    """Whether a string has the pseudonym's shape. Used by the API to refuse an address where it expected a
    pseudonym (`/v1/tape/fills?wallet=0x...`) instead of returning an empty page, which would read as "this
    trader has no fills"."""
    body = str(text or "")
    return body.startswith(PREFIX) and len(body) == len(PREFIX) + HEX_CHARS and \
        all(c in "0123456789abcdef" for c in body[len(PREFIX):])
