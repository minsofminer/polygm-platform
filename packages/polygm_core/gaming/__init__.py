"""P11 · The anti-gaming dashboard: four shapes that are worth a human's attention, and the rules that name them.

D7 is the internal half of the leaderboard's integrity story. The public boards already carry the rules that
protect a reader (`integrity.py`: wash volume, copy farms, thin records, disputed markets); this module is what we
look at ourselves, on our own tables, before a wallet's standing becomes somebody else's number to trust.

The kit asks for four surfaces — fast climbers, correlated clusters, synthetic referral chains, and wallets whose
volume reaches our builder code unusually — and, next to each, a one-click exclude and a one-click flag. Two
decisions shape everything here:

**A finding is a question, and the screen has to read like one.** `RULES` below is not documentation; it is served
by the API alongside the findings and rendered by the panel. Every entry states what was measured, at what
threshold, and — in the same sentence — what the shape *also* looks like when it is innocent. A climb of forty
places is a lucky streak as easily as it is a farm; ninety percent co-timed fills is a copy-trader following
somebody they admire. The reviewer sees both readings, because a label that only has a damning reading will always
be read damningly.

**Nothing here acts.** The detectors return findings with a `suggested` action, the API turns a human's click into
an append-only `leaderboard_exclusions` row (`exclude`/`flag`/`include`) with an actor and a reason, and the boards
apply the newest such row at read time. So the blast radius of a wrong click is one reversible row with a name
attached, not a deletion. `flag` deliberately does **not** remove anybody from a board: a flag is a question for a
person, and a ranking that quietly dropped flagged wallets would be hiding them instead of reviewing them.

The detectors are pure (`detect.py`) and take plain rows, so the gate can plant a farm in a fixture and prove each
one fires — and, just as important, that each one stays quiet on the innocent near-miss next to it. A detector that
only ever fires on the specimen it was written for is the machine-learning version of a stopped clock.
"""
from __future__ import annotations

from . import detect, rules
from .rules import INNOCENT, RULES

__all__ = ["INNOCENT", "RULES", "detect"]
