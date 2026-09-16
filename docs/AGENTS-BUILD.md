# AGENTS-BUILD.md — protocol in force for this repo

Superset of the kit's `docs/AGENTS.md`. Read `00-SHARED-CONTEXT.md` first; it is factually binding.
When this file and the kit disagree, this file governs *this repo* only (it adds the founder's
session instructions); the kit remains the source of the spec.

## Working order

`P01 → P02 → … → P16`, one phase at a time, **no skipping ahead**. If a later phase needs an earlier
deliverable that does not exist, produce the earlier one first.

Exception, recorded so it is not treated as drift: the kit's `README.md` suggests
P4→P5→P8→P9 before P7→P6. The founder's instruction is strict numerical order. **Strict order wins**
because P13's money-path matrix and P14's key-compromise drills are exactly what make P6 safe to
build — but read the consequence: after P06 ships, no real funds until P13/P14 are green (kit rule 7).

## Per-phase definition of done

A phase is done when all five are true:

1. The deliverable file exists in `docs/` (or code lands in `server/` / `web/`) with its
   **Quality Gate** section written out, answering the gate questions the prompt poses.
2. `python3 tools/datasource-probe.py` exits 0 — no cited API fact has rotted.
3. The phase's own gate check exits 0 (P01: `tools/p01-gate-check.py`; later phases add theirs).
4. It is committed, message prefixed `P0N:`.
5. `docs/BUILD-LOG.md` gains a 3-bullet entry: **built / verified / `[UNVERIFIED]`**.

No phase is reported as done on assertion. "It works" without a command that ran is not done.

## Honesty mechanics

- Anything unverifiable is written `[UNVERIFIED: what, why, cheapest way to close it]`. A bare
  `[UNVERIFIED]` is allowed only when the reason is in the adjacent sentence.
- Numbers get a source: **measured** (probe/script output), **[CTX]** (shared context, cited not
  re-verified), or **assumption** (stated as such, with its sensitivity).
- Sampling variance is quoted as a range. A 500-row tape sample is not a statistic.
- When a check disagrees with the document, the **document** changes — never weaken the check. If a
  check is genuinely wrong, fix its logic and re-run the mutation suite that proves it bites.

## Code rules (apply from P04 on)

- Money path: integers or `Decimal`, never `float`. Scale documented per field.
- Every price/order/position surface carries a freshness timestamp in the payload, not just the UI.
- New endpoints are read-only until explicitly promoted to write.
- Secrets only via env/secret store. No key, mnemonic, L2 credential or DB password in code, logs,
  errors, telemetry, fixtures, or commit messages.
- Fail closed at every boundary: upstream 5xx, stale book, unauthenticated signer, unreconciled
  position ⇒ disable the action and say why on-screen.
- Tests ship with the feature in the same commit.

## Brand rules

- `brand/BRAND-KIT.md` is the Brand Lock; `fixed` fields are immutable without explicit human approval.
- No Higgsfield CLI in this workspace ⇒ `docs/SKILLS.md`'s built-in-generator adapter applies.
  The image generator may never redraw `brand/svg/mark.svg`; composite the SVG.
- Every generated asset lands in `brand/` **and** gains a row in the Brand Kit's asset inventory table.

## Git

- Branch: work on `main` unless a phase is risky, then `pNN-<slug>` and merge.
- Commit per phase; the phase commit must contain the gate script's passing output reference
  (a line in the commit body like `gate: p01-gate-check 33/33`).
- Push using the pre-configured credential helper (`gh auth git-credential`). **Never embed a token
  in a remote URL, a file, or a commit message.** The workspace keeps credentials in
  `/home/user/.secrets/`, which is gitignored and outside this repo.
- `.gitignore` must keep ignoring `.secrets/`, `.env`, `*.pem`; verify with
  `git check-ignore -v .secrets/tokens.env` after touching it.
