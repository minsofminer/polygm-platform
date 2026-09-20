"""The anti-gaming dashboard's engine: four questions, asked of our own tables, answered with a rule attached.

D7 is an **internal** surface, and that is the whole design constraint. A dashboard that prints "suspicious" and
a wallet id is worse than no dashboard: it invites a human to remove somebody's standing on a feeling. So every
function here returns findings that carry

* the **rule sentence** that produced them, written for a person and served by the API rather than living only in
  this docstring (`RULES`), because the sentence is the thing that keeps the action honest;
* the **evidence**, as numbers from the same rows that produced the finding — a rank and a sample size, an overlap
  and its denominator, a chain's edges — so the reviewer can disagree with the rule instead of guessing at it;
* a **suggestion** (`exclude` or `flag`), never an action. The human decides, and the decision is an append-only
  row (`leaderboard_exclusions`) with an actor and a time. That split is deliberate: this module proposes, the API
  records.

Four questions, one per function, each pure and each callable on its own:

* `climb_findings` — who is moving up the boards faster than the population, and does their record support it.
* `cluster_findings` — which wallets are trading as if they were one wallet (same token, same side, same minute).
* `chain_findings` — which referral trees look manufactured rather than earned.
* `builder_findings` — whose volume reaches our builder code in a shape that is not a person's trading.

**The engine never calls anything fraud.** It reports the *shape* — a fast climb on a thin record, a near-mirror,
a floor-price chain — and names what the shape usually means. "Suspicious" is a conclusion, and a conclusion
belongs to the reviewer who can also see the wallet's side of it.

Every threshold below is a published constant with a comment saying what a false positive costs, and the gate
canaries plant violations against them: a detector that cannot fire on a planted farm is a detector that is
decorating the screen.
"""

from __future__ import annotations

from .rules import INNOCENT, RULES

DAY_MS = 86_400_000
HOUR_MS = 3_600_000

# --------------------------------------------------------------------------------------- climb
CLIMB_WINDOW_MS = 7 * DAY_MS
CLIMB_MIN_PLACES = 25           # below this a climb is ordinary churn; the cost of a low floor is a noisy list
CLIMB_MULTIPLE = 3              # ...and a climb is "fast" against the board's OWN median, not against a constant
CLIMB_PEER_BPS = 9900           # the percentile reading, which matters when the population is large
CLIMB_THIN_SAMPLE_BPS = 5000    # "the record is thinner than the median entrant" — a comparison, not a floor

# --------------------------------------------------------------------------------------- cluster
CORR_FILLS = 8                  # eight co-timed fills is already a coincidence nobody gets by accident
CORR_OVERLAP_BPS = 6000         # of the SMALLER tape, so a whale and a follower still register
CORR_WINDOW_MS = 60_000
CLUSTER_MIN_MEMBERS = 2

# --------------------------------------------------------------------------------------- chain
CHAIN_REFEREES = 3              # one referee is a referral; three is a business, and a business gets asked about
CHAIN_FAST_MS = 48 * HOUR_MS    # a qualifying order inside two days of signup
CHAIN_FLOOR_MICRO = 25 * 1_000_000      # the D5 qualifying notional ($25) — a tree made of floor hits
CHAIN_FLOOR_TOLERANCE_BPS = 200        # within 2% of the floor counts as "exactly the minimum"
CHAIN_SHARED_DIGEST_REFS = 2    # two referees of one referrer sharing a funding/device digest

# --------------------------------------------------------------------------------------- builder
BUILDER_VOLUME_MICRO = 25 * 1_000_000
BUILDER_MARKETS = 5             # volume spread over five markets in one burst is not a trading session
BUILDER_BURST_MS = 10 * 60_000
BUILDER_UNPAID_BPS = 5000       # half of the attributed orders never observed a fee: we were used, not paid


def _int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _bps(part: int, whole: int) -> int:
    """Basis points of `whole`, floored, with the zero-denominator answer being 0 rather than an exception."""
    return 0 if whole <= 0 else (int(part) * 10_000) // int(whole)


def _percentile(values: list[int], v: int) -> int:
    """Where `v` sits in `values`, in basis points (`9900` = at or above 99% of them)."""
    if not values:
        return 0
    below = sum(1 for x in values if x < v)
    return _bps(below, len(values))


# ------------------------------------------------------------------------------------------- climbers
def climb_findings(*, history: list[dict], at_ms: int, window_ms: int = CLIMB_WINDOW_MS) -> list[dict]:
    """Wallets whose rank improved faster than their record explains.

    `history` is `leaderboard_snapshots` rows over one board: `{wallet, board, windowKey, rank, settled, scoreBps,
    drawdownMicro, computedMs}`. The finding is a **comparison inside the population** rather than an absolute
    speed, because a board's churn is its own fact: 25 places in a 10,000-wallet board and 25 places in a
    64-wallet board are different events, and a fixed "climbed 20 places" rule would say the same thing about both.

    A climb is **fast** when it is at least `CLIMB_MIN_PLACES` places and meets one of two population readings: it
    is `CLIMB_MULTIPLE` times the board's own median climb, or it sits at or above the 99th percentile of the
    board's climbs. The percentile reading alone is not enough, and the first version of this function proved it:
    in a population of forty-one climbers the single largest climb sits at 9756 bps, so a 9900-bps bar can never
    be reached by *anybody* — a threshold no specimen can satisfy is not a strict rule, it is a dead one. The
    multiple is what makes a small population work, and the percentile is what keeps a large one honest.

    The **record** decides severity rather than whether to list: a fast climb whose settled count is below the
    median of the wallets it climbed past is the loudest thing on the screen (a lucky streak and a manufactured
    record look identical from here, which is exactly why a human sees it), and the same climb on a deep record
    is still worth a look but is not the top of the queue.
    """
    horizon = _int(at_ms) - max(_int(window_ms), 1)
    per: dict[str, dict] = {}
    for row in history or []:
        wallet = str(row.get("wallet") or "")
        ts = _int(row.get("computedMs"))
        if not wallet or ts < horizon or ts > _int(at_ms):
            continue
        bucket = per.setdefault(wallet, {"wallet": wallet, "board": str(row.get("board") or ""),
                                         "first": None, "last": None})
        if bucket["first"] is None or ts < _int(bucket["first"].get("computedMs")):
            bucket["first"] = row
        if bucket["last"] is None or ts >= _int(bucket["last"].get("computedMs")):
            bucket["last"] = row

    scored = []
    for wallet, b in per.items():
        if b["first"] is None or b["last"] is None or b["first"] is b["last"]:
            continue
        climb = _int(b["first"].get("rank")) - _int(b["last"].get("rank"))
        if climb <= 0:
            continue
        scored.append({"wallet": wallet, "board": b["board"], "climb": climb,
                       "fromRank": _int(b["first"].get("rank")), "toRank": _int(b["last"].get("rank")),
                       "settled": _int(b["last"].get("settled")), "scoreBps": _int(b["last"].get("scoreBps")),
                       "drawdownMicro": _int(b["last"].get("drawdownMicro")),
                       "firstMs": _int(b["first"].get("computedMs")), "lastMs": _int(b["last"].get("computedMs"))})
    if not scored:
        return []

    climbs = [s["climb"] for s in scored]
    settled_values = sorted(s["settled"] for s in scored)
    median_settled = settled_values[len(settled_values) // 2] if settled_values else 0
    median_climb = climbs[len(climbs) // 2] if climbs else 0

    out = []
    for s in scored:
        pct = _percentile(climbs, s["climb"])
        if s["climb"] < CLIMB_MIN_PLACES:
            continue
        multiple = s["climb"] >= CLIMB_MULTIPLE * max(median_climb, 1)
        if not (multiple or pct >= CLIMB_PEER_BPS):
            continue
        thin = s["settled"] < median_settled
        reaches = ["climb is %d place(s), %s the board's median of %d"
                   % (s["climb"], "%dx or more" % CLIMB_MULTIPLE if multiple else "under %dx" % CLIMB_MULTIPLE,
                      median_climb),
                   "peer percentile %d bps of %d climbing wallet(s)" % (pct, len(scored))]
        if thin:
            reaches.append("%d settled market(s) against a median of %d among the climbers"
                           % (s["settled"], median_settled))
        out.append({**s, "kind": "fast_climb", "severity": (3 if thin else 2),
                    "percentileBps": pct, "medianClimb": median_climb,
                    "rule": RULES["fast_climb"], "suggested": "flag",
                    "evidence": reaches + ["rank %d → %d over %d day(s)" % (s["fromRank"], s["toRank"],
                                                                          max(1, (s["lastMs"] - s["firstMs"]) // DAY_MS))]})
    out.sort(key=lambda f: (-f["severity"], -f["climb"], f["wallet"]))
    return out


# -------------------------------------------------------------------------------------------- clusters
def _co_timed(fills: list[dict], *, window_ms: int) -> dict[str, set[int]]:
    """For each fill, WHICH OTHER WALLETS have a fill co-timed with it (same token, same side, same window).

    Bucketing by (token, side) and walking each bucket in time order makes this a near-linear pass instead of the
    pairwise comparison it started as: the pairwise version is O(n²) over a tape that a busy day makes large, and
    it was the first thing this function did before it was measured.

    The value is a set of *wallets*, not a set of fill indices, and that is a correction rather than a detail. The
    first version returned "indices of other wallets' fills co-timed with this one" and the overlap was computed
    as `|hit[a] ∩ fills(b)|` — which counts **b's** fills and can therefore exceed the size of a's tape whenever
    one a-fill sits near several b-fills. The gate's own tape produced an overlap of 10034 basis points, i.e. an
    overlap of 100.34%, which is the kind of number that gets a threshold quietly raised instead of fixed. Asking
    "how many of the smaller wallet's own fills have a counterpart" cannot exceed 100% by construction.
    """
    buckets: dict[tuple, list[tuple[int, dict]]] = {}
    for i, f in enumerate(fills):
        key = (str(f.get("tokenId") or ""), str(f.get("side") or "").lower())
        buckets.setdefault(key, []).append((i, f))
    partners: dict[int, set[str]] = {}
    for _key, rows in buckets.items():
        rows = sorted(rows, key=lambda r: _int(r[1].get("tsMs")))
        for a in range(len(rows)):
            i, fa = rows[a]
            for b in range(a + 1, len(rows)):
                j, fb = rows[b]
                if _int(fb.get("tsMs")) - _int(fa.get("tsMs")) > window_ms:
                    break
                wa, wb = str(fa.get("wallet") or ""), str(fb.get("wallet") or "")
                if not wa or not wb or wa == wb:
                    continue
                partners.setdefault(i, set()).add(wb)
                partners.setdefault(j, set()).add(wa)
    return partners


def cluster_findings(*, fills: list[dict], at_ms: int, window_ms: int = CORR_WINDOW_MS,
                     min_fills: int = CORR_FILLS, min_overlap_bps: int = CORR_OVERLAP_BPS) -> list[dict]:
    """Groups of wallets whose trading is co-timed on the same token and side, then joined into components.

    The overlap is measured against the **smaller** tape (`coTimed / min(nA, nB)`), because a wallet with thirty
    fills and a wallet with three that all match the thirty is a follower, and measuring against the larger tape
    would hide it. Components are unioned transitively: a farm of six wallets whose members each mirror two others
    arrives as one cluster, not as fifteen pairs for a reviewer to reassemble by hand.
    """
    rows = [f for f in (fills or []) if str(f.get("wallet") or "")]
    partners = _co_timed(rows, window_ms=max(_int(window_ms), 1))
    per_wallet: dict[str, list[int]] = {}
    for i, f in enumerate(rows):
        per_wallet.setdefault(str(f["wallet"]), []).append(i)

    pairs: list[dict] = []
    wallets = sorted(per_wallet)
    for x in range(len(wallets)):
        for y in range(x + 1, len(wallets)):
            a, b = wallets[x], wallets[y]
            ia, ib = per_wallet[a], per_wallet[b]
            if min(len(ia), len(ib)) < min_fills:
                continue
            # Counted on the SMALLER tape, from the smaller wallet's own fills outwards: "293 of 293 fills have a
            # counterpart on that wallet" is a sentence that cannot be wrong the way a ratio of two different
            # denominators can. The ratio is therefore ≤ 100% by construction.
            small, large = (a, b) if len(ia) <= len(ib) else (b, a)
            denom = min(len(ia), len(ib))
            shared = sum(1 for i in per_wallet[small] if large in (partners.get(i) or ()))
            overlap = min(10_000, _bps(shared, denom))
            if overlap < min_overlap_bps:
                continue
            first = min(_int(rows[i].get("tsMs")) for i in ia[:1] + ib[:1])
            pairs.append({"a": a, "b": b, "overlapBps": overlap, "coTimed": shared, "smaller": denom,
                          "fillsA": len(ia), "fillsB": len(ib), "firstMs": first})

    if not pairs:
        return []

    parent: dict[str, str] = {}

    def find(w: str) -> str:
        parent.setdefault(w, w)
        while parent[w] != w:
            parent[w] = parent[parent[w]]
            w = parent[w]
        return w

    for p in pairs:
        ra, rb = find(p["a"]), find(p["b"])
        if ra != rb:
            parent[ra] = rb

    groups: dict[str, list[str]] = {}
    for w in parent:
        groups.setdefault(find(w), []).append(w)

    out = []
    for _root, members in groups.items():
        if len(members) < CLUSTER_MIN_MEMBERS:
            continue
        members = sorted(members)
        inside = [p for p in pairs if p["a"] in members and p["b"] in members]
        worst = max(inside, key=lambda p: (p["overlapBps"], p["coTimed"]))
        # `walletCount`, not `size`: "size" is the name of a money-shaped field all over this codebase, and the
        # contract's own rule - prices and sizes are strings, never JSON numbers - fired on it the first time this
        # payload met the checker. Renaming the field is cheaper than an exemption, and the exemption is how the
        # next one gets missed.
        out.append({"kind": "correlated_cluster", "rule": RULES["correlated_cluster"],
                    "wallets": members, "walletCount": len(members), "pairs": len(inside),
                    "worstOverlapBps": worst["overlapBps"], "worstPair": [worst["a"], worst["b"]],
                    "coTimed": worst["coTimed"], "windowMs": _int(window_ms),
                    "severity": 3 if worst["overlapBps"] >= 8000 else (2 if worst["overlapBps"] >= 7000 else 1),
                    "suggested": "flag",
                    "evidence": ["%d wallet(s) joined by %d co-timed pair(s)" % (len(members), len(inside)),
                                 "worst pair shares %d of %d fill(s) within %d s"
                                 % (worst["coTimed"], worst["smaller"], _int(window_ms) // 1000),
                                 "overlap %d bps of the smaller tape" % worst["overlapBps"]]})
    out.sort(key=lambda f: (-f["severity"], -f["walletCount"], f["wallets"][0]))
    return out


# ---------------------------------------------------------------------------------------------- chains
def chain_findings(*, referrals: list[dict], at_ms: int, min_referees: int = CHAIN_REFEREES,
                   fast_ms: int = CHAIN_FAST_MS) -> list[dict]:
    """Referral trees whose shape says manufactured: floor-price orders signed fast, under one funder.

    `referrals` rows come from the attribution table joined to the D5 signal digests: `{referrer, referee,
    signedUpMs, qualifyMs, notionalMicro, fundingDigest, deviceDigest, ipDigest, accrualMicro}`. A digest is a
    one-way value (`f_…`/`d_…`), so this function can compare two referrals without ever seeing a funding address
    — which is what lets the finding be printed on a screen.

    Three shapes:
    * a referrer with `min_referees` or more referees — a referral **business**, which is a question about the
      referrer rather than an accusation about any one referee;
    * two or more of one referrer's referees sharing a funding or device digest — the D5 rule refuses a
      *self*-referral at apply time; a referrer whose referees collide with **each other** is the same fact seen
      from the other end, and it is invisible to a pairwise check;
    * a qualifying order at the floor notional inside `fast_ms` of signup — the signature of a tree built to hit
      the $25 minimum as cheaply as possible. The floor carries a tolerance because the qualifying order is
      often a few cents over it.
    """
    by_referrer: dict[str, list[dict]] = {}
    for row in referrals or []:
        ref = str(row.get("referrer") or "")
        if ref:
            by_referrer.setdefault(ref, []).append(row)

    out = []
    for referrer, refs in by_referrer.items():
        if len(refs) < min_referees:
            continue
        funded: dict[str, list[str]] = {}
        devices: dict[str, list[str]] = {}
        fast: list[str] = []
        floor: list[str] = []
        for r in refs:
            referee = str(r.get("referee") or "")
            for digest, bucket in ((str(r.get("fundingDigest") or ""), funded),
                                   (str(r.get("deviceDigest") or ""), devices)):
                if digest:
                    bucket.setdefault(digest, []).append(referee)
            signed, qual = _int(r.get("signedUpMs")), _int(r.get("qualifyMs"))
            if signed and qual and 0 <= qual - signed <= fast_ms:
                fast.append(referee)
            notional = _int(r.get("notionalMicro"))
            if notional and notional <= CHAIN_FLOOR_MICRO + (CHAIN_FLOOR_MICRO * CHAIN_FLOOR_TOLERANCE_BPS) // 10_000:
                floor.append(referee)
        shared_funding = {d: rs for d, rs in funded.items() if len(rs) >= CHAIN_SHARED_DIGEST_REFS}
        shared_device = {d: rs for d, rs in devices.items() if len(rs) >= CHAIN_SHARED_DIGEST_REFS}
        if not (shared_funding or shared_device or len(fast) >= 2 or len(floor) >= 2):
            continue
        edges = [{"referrer": referrer, "referee": str(r.get("referee") or "")} for r in refs]
        evidence = ["%d referee(s) attributed to one referrer" % len(refs)]
        if shared_funding:
            evidence.append("%d funding digest(s) shared by %d referee(s)"
                            % (len(shared_funding), max(len(v) for v in shared_funding.values())))
        if shared_device:
            evidence.append("%d device digest(s) shared by %d referee(s)"
                            % (len(shared_device), max(len(v) for v in shared_device.values())))
        if fast:
            evidence.append("%d qualifying order(s) inside %d h of signup" % (len(fast), fast_ms // HOUR_MS))
        if floor:
            evidence.append("%d referees qualified at the $%d floor"
                            % (len(floor), CHAIN_FLOOR_MICRO // 1_000_000))
        total = sum(_int(r.get("accrualMicro")) for r in refs)
        severity = 3 if (shared_funding or shared_device) else (2 if (len(fast) >= 2 and len(floor) >= 2) else 1)
        out.append({"kind": "synthetic_chain", "referrer": referrer, "referees": [e["referee"] for e in edges],
                    "refereeCount": len(refs), "edges": edges, "fastReferees": fast, "floorReferees": floor,
                    "sharedFunding": len(shared_funding), "sharedDevice": len(shared_device),
                    "accrualMicro": total, "severity": severity, "suggested": "flag",
                    "rule": RULES["synthetic_chain"], "evidence": evidence})
    out.sort(key=lambda f: (-f["severity"], -f["refereeCount"], f["referrer"]))
    return out


# --------------------------------------------------------------------------------------------- builder
def builder_findings(*, attributions: list[dict], at_ms: int,
                     volume_floor_micro: int = BUILDER_VOLUME_MICRO) -> list[dict]:
    """Whose volume reaches our builder code in a shape that is not a person's trading.

    `attributions` rows: `{wallet, userId, orderId, marketId, notionalMicro, feeMicroExpected, feeMicroObserved,
    placedMs, builderCode}`. Two shapes, both about *attribution* rather than about trading:

    * a burst — five or more distinct markets inside ten minutes — which is a script hitting our code, or a
      market maker we should be glad about; either way the reviewer should know before the volume board does;
    * an unpaid pattern — at least half the attributed orders for a wallet never observed a fee, which is the
      shape of volume that used us without paying us. Expected-vs-observed is the only pair of numbers that can
      tell "the venue charged less" (D5's lesson) apart from "the venue charged nothing".
    """
    per: dict[str, list[dict]] = {}
    for row in attributions or []:
        w = str(row.get("wallet") or "")
        if w:
            per.setdefault(w, []).append(row)

    out = []
    for wallet, rows in per.items():
        rows = sorted(rows, key=lambda r: _int(r.get("placedMs")))
        volume = sum(_int(r.get("notionalMicro")) for r in rows)
        if volume < volume_floor_micro:
            continue
        markets = {str(r.get("marketId") or "") for r in rows}
        burst = 0
        start = 0
        for i, r in enumerate(rows):
            while _int(r.get("placedMs")) - _int(rows[start].get("placedMs")) > BUILDER_BURST_MS:
                start += 1
            burst = max(burst, len({str(x.get("marketId") or "") for x in rows[start:i + 1]}))
        paid = sum(1 for r in rows if _int(r.get("feeMicroObserved")) > 0)
        unpaid_bps = _bps(len(rows) - paid, len(rows))
        if burst < BUILDER_MARKETS and unpaid_bps < BUILDER_UNPAID_BPS:
            continue
        reasons = []
        severity = 1
        if burst >= BUILDER_MARKETS:
            reasons.append("%d markets inside %d minute(s)" % (burst, BUILDER_BURST_MS // 60_000))
            severity = max(severity, 2)
        if unpaid_bps >= BUILDER_UNPAID_BPS:
            reasons.append("%d of %d attributed order(s) never observed a fee (%d bps)"
                           % (len(rows) - paid, len(rows), unpaid_bps))
            severity = 3 if unpaid_bps >= 8000 else max(severity, 2)
        out.append({"kind": "builder_anomaly", "wallet": wallet,
                    "userId": str(rows[0].get("userId") or ""), "orders": len(rows),
                    "markets": len(markets), "burstMarkets": burst, "volumeMicro": volume,
                    "unpaidBps": unpaid_bps, "rule": RULES["builder_anomaly"], "severity": severity,
                    "suggested": "flag", "evidence": reasons + ["volume $%d over %d order(s)"
                                                                % (volume // 1_000_000, len(rows))]})
    out.sort(key=lambda f: (-f["severity"], -f["volumeMicro"], f["wallet"]))
    return out


# ------------------------------------------------------------------------------------------- dashboard
def dashboard(*, history: list[dict], fills: list[dict], referrals: list[dict], attributions: list[dict],
              at_ms: int, limit: int = 25, flags: dict | None = None) -> dict:
    """The four lists, the rules they were produced by, and the counts — one payload for one screen.

    `flags` is the newest human decision per wallet (`{wallet: {"action": "flag"|"exclude", "atMs": …}}`), carried
    into each finding so the screen can show *already reviewed* rather than re-offering the same decision. A
    finding is a question; a second identical question is noise, and noise is how a dashboard stops being read.
    """
    lim = max(1, _int(limit, 25))
    flags = flags or {}
    climbers = climb_findings(history=history, at_ms=at_ms)[:lim]
    clusters = cluster_findings(fills=fills, at_ms=at_ms)[:lim]
    chains = chain_findings(referrals=referrals, at_ms=at_ms)[:lim]
    builder = builder_findings(attributions=attributions, at_ms=at_ms)[:lim]
    for group, key in ((climbers, "wallet"), (builder, "wallet")):
        for f in group:
            decision = flags.get(str(f.get(key) or ""))
            if decision:
                f["decided"] = decision
    for f in chains:
        decision = flags.get(str(f.get("referrer") or ""))
        if decision:
            f["decided"] = decision
    for f in clusters:
        decided = [w for w in f["wallets"] if str(w) in flags]
        if decided:
            f["decided"] = {"action": str(flags[decided[0]].get("action") or ""), "wallets": decided,
                            "atMs": _int(flags[decided[0]].get("atMs"))}
    # The rule and its other reading travel together, as a pair, so no consumer can render one without having
    # the other in hand — the product's "rule plus disclaimer" constraint made structural instead of editorial.
    return {"atMs": _int(at_ms), "limit": lim,
            "rules": {k: {"rule": RULES[k], "innocent": INNOCENT[k]} for k in RULES},
            "climbers": climbers, "clusters": clusters, "chains": chains, "builder": builder,
            "counts": {"climbers": len(climbers), "clusters": len(clusters), "chains": len(chains),
                       "builder": len(builder), "reviewed": len(flags)}}
