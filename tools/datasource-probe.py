#!/usr/bin/env python3
"""P01 data-source probe — proves every endpoint cited in docs/P01-product-spec.md.

Why this exists: AGENTS.md rule 1 ("do not invent API behaviour; verify against
docs.polymarket.com before writing code") and P01's constraint "no feature without a named
data source." Every row in the spec's feature table traces to a probe here.

Zero third-party dependencies (stdlib only) so it runs in CI, in the sandbox, and on the
ingestion box without a venv. Read-only: GETs only, never a mutating call, never an L2 endpoint
with credentials.

    python3 tools/datasource-probe.py            # human summary + exit code
    python3 tools/datasource-probe.py --json     # machine-readable (for CI assertions)
    python3 tools/datasource-probe.py --save docs/verification/P01-probe.json

Exit 0 = every required expectation held. Exit 1 = a spec-cited fact changed upstream, which is
the signal to re-open P01 rather than to "fix" the test.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, asdict

UA = {"User-Agent": "polygm-p01-verification/1.0 (read-only endpoint audit)"}
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DATA = "https://data-api.polymarket.com"
LB = "https://lb-api.polymarket.com"

# Findings that materially change the build. Keys are the E-numbers used in the spec.
FINDINGS: dict[str, str] = {
    "E1": "Gamma caps page size at 100 rows regardless of limit=",
    "E2": "data-api /trades is served from Cloudflare cache (cf-cache-status: HIT) => not a feed",
    "E3": "5-min up/down markets are absent from top-100 markets by 24h volume",
    "E4": "lb-api has /volume and /profit per window; /pnl 404s; /rank is unusable",
    "E5": "trade rows embed identity fields (name/pseudonym/bio/profileImage) and /profile does not exist",
}


@dataclass
class Check:
    """One assertion about upstream behaviour."""

    id: str
    finding: str
    url: str
    expect: str
    ok: bool
    observed: object
    note: str = ""


@dataclass
class Report:
    started_at: str = ""
    checks: list[Check] = field(default_factory=list)
    measurements: dict[str, object] = field(default_factory=dict)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]


def get(url: str, timeout: int = 30, headers: dict | None = None):
    """Return (status, json_or_bytes, response_headers). Never raises."""
    h = dict(UA)
    h.update(headers or {})
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
            raw = r.read()
            try:
                return r.status, json.loads(raw), dict(r.headers)
            except Exception:
                return r.status, raw[:200], dict(r.headers)
    except urllib.error.HTTPError as e:  # 4xx/5xx still carry headers we assert on
        return e.code, e.read()[:200], dict(e.headers)
    except Exception as e:  # noqa: BLE001 - a probe must degrade, not crash
        return "ERR", repr(e)[:200], {}


def add(r: Report, cid: str, finding: str, url: str, expect: str, ok: bool, observed, note: str = ""):
    r.checks.append(Check(cid, finding, url, expect, bool(ok), observed, note))


def parse_token_ids(market: dict):
    """Gamma returns clobTokenIds as a JSON *string*. The #1 integration mistake [CTX §2]."""
    ct = market.get("clobTokenIds")
    if isinstance(ct, str):
        try:
            return json.loads(ct)
        except Exception:
            return None
    return ct


def probe(r: Report, deep: bool = True) -> None:
    # ---------------------------------------------------------------- Gamma
    s, ev, _ = get(f"{GAMMA}/events?limit=2&closed=false&order=volume24hr&ascending=false")
    rows = ev if isinstance(ev, list) else []
    add(r, "gamma-events", "E1/E5", f"{GAMMA}/events", "200 + list",
        s == 200 and len(rows) > 0, {"status": s, "rows": len(rows)})
    if not rows:
        r.measurements["fatal"] = "no active events; cannot continue safely"
        return

    e0 = rows[0]
    m0 = (e0.get("markets") or [{}])[0]
    tids = parse_token_ids(m0)
    add(r, "gamma-tokenids-parseable", "E5", f"{GAMMA}/events?slug={e0.get('slug')}",
        "clobTokenIds is a JSON string that parses to a list",
        isinstance(tids, list) and len(tids) > 0,
        {"type": type(m0.get("clobTokenIds")).__name__, "count": len(tids) if isinstance(tids, list) else 0})
    tid = tids[0] if isinstance(tids, list) and tids else None
    cond = m0.get("conditionId")

    # E1: page-size cap. Ask for more than 100, expect to be capped.
    s, many, _ = get(f"{GAMMA}/events?closed=false&order=volume24hr&ascending=false&limit=200")
    add(r, "E1-gamma-row-cap", "E1", f"{GAMMA}/events?limit=200",
        "100 rows max regardless of limit= (paginate with offset)",
        isinstance(many, list) and len(many) <= 100, {"rows": len(many) if isinstance(many, list) else many})
    r.measurements["gamma_row_cap"] = len(many) if isinstance(many, list) else None
    r.measurements["top100_events_24h_usd"] = round(sum(float(x.get("volume24hr") or 0) for x in many), 2)
    if isinstance(many, list) and many:
        vs = sorted((float(x.get("volume24hr") or 0) for x in many), reverse=True)
        tot = sum(vs) or 1.0
        r.measurements.update(
            top10_share_pct=round(100 * sum(vs[:10]) / tot, 1),
            events_over_1m_24h=sum(1 for v in vs if v > 1e6),
        )
        # E3: up/down share of top volume
        r.measurements["updown_in_top100_events"] = sum(1 for x in many if "updown" in (x.get("slug") or ""))

    # ---------------------------------------------------------------- CLOB
    s, _, _ = get(f"{CLOB}/ok")
    add(r, "clob-ok", "E2", f"{CLOB}/ok", "200", s == 200, s)
    if tid:
        s, bk, _ = get(f"{CLOB}/book?token_id={tid}")
        ok = s == 200 and isinstance(bk, dict) and {"bids", "asks", "tick_size", "min_order_size"} <= set(bk)
        add(r, "clob-book-fields", "E2", f"{CLOB}/book?token_id=…",
            "200 + bids/asks/tick_size/min_order_size present", ok,
            {"status": s, "keys": sorted(bk)[:8]} if isinstance(bk, dict) else bk)
        s, mkt, _ = get(f"{CLOB}/markets/{cond}")
        need = ["minimum_order_size", "minimum_tick_size", "neg_risk", "accepting_orders", "seconds_delay"]
        got = {k: mkt.get(k) for k in need} if isinstance(mkt, dict) else mkt
        add(r, "clob-market-order-gate-fields", "E2", f"{CLOB}/markets/{cond}",
            "all five pre-order fields present", s == 200 and isinstance(mkt, dict) and all(k in mkt for k in need), got)
        s, sp, _ = get(f"{CLOB}/spread?token_id={tid}")
        add(r, "clob-spread", "E2", f"{CLOB}/spread", "200 + spread", s == 200 and isinstance(sp, dict) and "spread" in sp, sp)
        s, ltp, _ = get(f"{CLOB}/last-trade-price?token_id={tid}")
        add(r, "clob-last-trade-price", "E2", f"{CLOB}/last-trade-price", "200 + price",
            s == 200 and isinstance(ltp, dict) and "price" in ltp, ltp)
        s, _, _ = get(f"{CLOB}/data/trades")
        add(r, "clob-l2-requires-auth", "E2", f"{CLOB}/data/trades",
            "401 unauthenticated (auth boundary is real)", s in (401, 403), s)

    # ------------------------------------------------------- E2: cache probe
    if deep:
        u = f"{DATA}/trades?limit=500"
        s1, t1, h1 = get(u)
        time.sleep(8)
        s2, t2, h2 = get(u)
        fresh1 = t1[0]["timestamp"] if isinstance(t1, list) and t1 else None
        fresh2 = t2[0]["timestamp"] if isinstance(t2, list) and t2 else None
        hit = str(h1.get("cf-cache-status", h1.get("CF-Cache-Status", ""))).upper()
        r.measurements["data_trades_cf_cache_status"] = hit or "absent"
        r.measurements["data_trades_newest_ts"] = fresh1
        add(r, "E2-trades-is-cached", "E2", u,
            "cf-cache-status present AND newest ts repeats across 8s => NOT a feed; use WebSocket",
            hit == "HIT" or fresh1 == fresh2,
            {"cache_status": hit, "newest_t0": fresh1, "newest_t8": fresh2,
             "identical": fresh1 == fresh2},
            "If this flips to MISS/STATIC, re-measure before changing the tape design.")
        if isinstance(t1, list) and len(t1) > 10:
            span = max(1, t1[0]["timestamp"] - t1[-1]["timestamp"])
            r.measurements["fills_per_sec_measured"] = round(len(t1) / span, 2)
            notionals = [float(x["price"]) * float(x["size"]) for x in t1]
            r.measurements.update(
                median_fill_usd=round(statistics.median(notionals), 2),
                p90_fill_usd=round(sorted(notionals)[int(len(notionals) * 0.9)], 2),
                max_fill_usd=round(max(notionals), 2),
                pct_fills_ge_1000=round(100 * sum(1 for n in notionals if n >= 1000) / len(notionals), 1),
            )
        # Cache-buster test: does a query param defeat the edge cache? Sampled three times on purpose. A
        # single comparison of two requests a second apart reads "no newer trade arrived yet" as "the buster
        # does not work" whenever the tape is quiet — which is exactly what the first run of
        # `make probe-fresh` reported as venue drift. Three samples with a 1 s gap cannot be answered by one
        # quiet second, and the count is recorded next to the boolean so a reader can see WHY it flipped.
        busts, prev = 0, fresh1
        for _ in range(3):
            time.sleep(1.0)
            s3, t3, h3 = get(u + "&_=%d" % int(time.time() * 1000))
            newest = t3[0]["timestamp"] if (s3 == 200 and isinstance(t3, list) and t3) else None
            if newest is not None:
                if newest != prev:
                    busts += 1
                prev = newest
        r.measurements["cache_buster_works"] = busts > 0
        r.measurements["cache_bust_newer_of_3"] = "%d/3" % busts

    # ---------------------------------------------------------------- Data API
    s, tl, _ = get(f"{DATA}/trades?limit=20")
    if not (isinstance(tl, list) and tl):
        add(r, "data-trades", "E2", f"{DATA}/trades", "200 + list", False, s)
        return
    t0 = tl[0]
    wallet = t0["proxyWallet"]
    add(r, "E5-trades-carry-identity", "E5", f"{DATA}/trades",
        "rows embed name/pseudonym/bio/profileImage so no profile join is needed",
        {"name", "pseudonym", "bio", "profileImage"} <= set(t0), sorted(t0))
    for name, q, expect_param in [("positions", "user", 200), ("activity", "user", 200),
                                  ("value", "user", 200), ("traded", "user", 200)]:
        s2, b2, _ = get(f"{DATA}/{name}?{q}={wallet}&limit=2")
        add(r, f"data-{name}", "E5", f"{DATA}/{name}?{q}=", "200 with required param", s2 == expect_param,
            {"status": s2, "sample": b2[0] if isinstance(b2, list) and b2 else b2})
    s_miss, b_miss, _ = get(f"{DATA}/positions?limit=2")
    add(r, "data-positions-needs-user", "E5", f"{DATA}/positions", "400 + explicit missing-param error when user omitted",
        s_miss == 400 and "user" in json.dumps(b_miss, default=str),
        {"status": s_miss, "body": str(b_miss)[:90]})
    s2, _, _ = get(f"{DATA}/profile?address={wallet}")
    add(r, "E5-no-profile-endpoint", "E5", f"{DATA}/profile", "404 => identity comes from the trade row",
        s2 == 404, s2)
    # activity type enum — needed because REDEEM rows carry price=0 (PnL trap)
    s2, act, _ = get(f"{DATA}/activity?user={wallet}&limit=300")
    kinds: dict[str, int] = {}
    zero_price_redeem = None
    for row in act if isinstance(act, list) else []:
        kinds[row.get("type")] = kinds.get(row.get("type"), 0) + 1
        if row.get("type") == "REDEEM" and zero_price_redeem is None:
            zero_price_redeem = {k: row.get(k) for k in ("price", "usdcSize", "size")}
    add(r, "activity-types", "E5", f"{DATA}/activity?user=", "200 + a type enum including REDEEM",
        s2 == 200 and "TRADE" in kinds, kinds)
    r.measurements["redeem_row_payout_fields"] = zero_price_redeem
    # D4.5: the PnL trap, asserted rather than described: REDEEM rows carry price==0 and the payout
    # in usdcSize, so cost/PnL code MUST read usdcSize, never price*size (price*size reads $0).
    #
    # This must be sampled across MANY wallets. An earlier version asserted on the single top
    # `lb-api/volume` wallet and failed a true rule: that wallet's 250 most recent activity rows
    # contained zero REDEEMs (high-frequency traders redeem rarely relative to trade count), and the
    # check silently disappeared when the list was empty. Verdicts are now emitted unconditionally.
    # Corroborated 2026-09-17 on 125 REDEEM rows across 12 wallets: price==0 on 125/125,
    # usdcSize>0 on 115/125, usdcSize == size (i.e. $1/share) on 116/125.
    wallets: list[str] = []
    for row in tl:
        w = row.get("proxyWallet")
        if w and w not in wallets:
            wallets.append(w)
        if len(wallets) >= 10:
            break
    rd_all: list[dict] = []
    wallets_seen = 0
    for w_ in wallets:
        s3, act2, _ = get(f"{DATA}/activity?user={w_}&limit=250")
        if s3 == 200 and isinstance(act2, list):
            wallets_seen += 1
            rd_all += [a for a in act2 if a.get("type") == "REDEEM"]
    priced = [a for a in rd_all if float(a.get("price") or 0) != 0]
    paid = [a for a in rd_all if float(a.get("usdcSize") or 0) > 0]
    obs = {
        "wallets_sampled": wallets_seen,
        "redeem_rows": len(rd_all),
        "price_nonzero": len(priced),
        "usdcsiz_positive": len(paid),
    }
    if len(rd_all) < 20:
        # too thin to assert either way — say so instead of passing or falsely failing
        add(r, "redeem-payout-in-usdcsiz", "E5", f"{DATA}/activity?user=",
            "INCONCLUSIVE: need >=20 REDEEM rows to assert the payout rule", False, obs,
            "raise the wallet sample; do not assert a rule on an empty or tiny sample")
    else:
        add(r, "redeem-payout-in-usdcsiz", "E5", f"{DATA}/activity?user=",
            f"REDEEM rows ({len(rd_all)} across {wallets_seen} wallets): price==0 on all, payout in usdcSize",
            len(priced) == 0 and len(paid) >= int(0.5 * len(rd_all)), obs)

    # ---------------------------------------------------------------- Leaderboard
    for ep in ("volume", "profit"):
        for w in ("1d", "7d", "30d", "all"):
            s2, b2, _ = get(f"{LB}/{ep}?window={w}&limit=2")
            ok = s2 == 200 and isinstance(b2, list) and b2 and "amount" in b2[0]
            if w == "all":
                add(r, f"lb-{ep}", "E4", f"{LB}/{ep}?window=all", "200 + rows with amount", ok,
                    {"status": s2, "top": {k: b2[0].get(k) for k in ("name", "amount")} if ok else b2})
    s2, _, _ = get(f"{LB}/pnl?window=all&limit=2")
    add(r, "E4-no-pnl-endpoint", "E4", f"{LB}/pnl", "404 => we must compute PnL ourselves", s2 == 404, s2)
    s2, b2, _ = get(f"{LB}/rank?window=all&rank=volume&address={wallet}")
    add(r, "E4-rank-unusable", "E4", f"{LB}/rank?rank=volume&address=",
        "documented as broken: 400 even when rank is supplied", s2 == 400,
        {"status": s2, "body": str(b2)[:90]})

    # -------------------------------------------------- E3: 5-min market reality
    # Claim we can defend: up/down markets carry a negligible share of volume. We do NOT claim
    # "zero of them exist in the top 100" - on 2026-09-16 exactly one did, at rank #91.
    s2, mkts, _ = get(f"{GAMMA}/markets?limit=100&closed=false&order=volume24hr&ascending=false")
    if isinstance(mkts, list) and mkts:
        ud = [m for m in mkts if "updown" in (m.get("slug") or "")]
        mvols = [float(m.get("volume24hr") or 0) for m in mkts]
        udv = sum(float(m.get("volume24hr") or 0) for m in ud)
        r.measurements.update(
            updown_in_top100_markets=len(ud),
            top100_markets_24h_usd=round(sum(mvols), 2),
            updown_share_of_top100_markets_pct=round(100 * udv / (sum(mvols) or 1), 2),
        )
        add(r, "E3-updown-volume-share", "E3", f"{GAMMA}/markets?order=volume24hr",
            "up/down < 2% of top-100 markets' 24h volume (wedge 1 not viable as a standalone product)",
            (100 * udv / (sum(mvols) or 1)) < 2.0,
            {"updown_count": len(ud), "updown_usd": round(udv, 2), "top100_usd": round(sum(mvols), 2),
             "share_pct": round(100 * udv / (sum(mvols) or 1), 2)})
        # Event-level ordering shows them at all, which is itself a bug class for anyone sizing
        # "the 5-minute market" off /events. 3 pages x 100 to beat the row cap (E1).
        ev_all = []
        for off in (0, 100, 200):
            s3, ev3, _ = get(f"{GAMMA}/events?limit=100&offset={off}&closed=false&order=volume24hr&ascending=false")
            if isinstance(ev3, list):
                ev_all += ev3
        ud_e = [e for e in ev_all if "updown" in (e.get("slug") or "")]
        r.measurements.update(
            paged_event_rows=len(ev_all),
            paged_events_24h_usd=round(sum(float(e.get("volume24hr") or 0) for e in ev_all), 2),
            updown_in_paged_events=len(ud_e),
        )
        add(r, "E3-events-page-hides-updown", "E3", f"{GAMMA}/events?order=volume24hr (3 pages)",
            "up/down events absent from volume-ordered /events pages => volume lives on markets, not events",
            len(ud_e) == 0 and len(ev_all) > 200,
            {"rows": len(ev_all), "updown_events": len(ud_e),
             "total_usd": round(sum(float(e.get("volume24hr") or 0) for e in ev_all), 2)},
            "Cross-check: kit says $59.1M/24h for top-500 events; a 300-row page should approach it.")
        fee = [m for m in mkts if m.get("feeType")]
        r.measurements["fee_type_observable_per_market"] = sorted({m.get("feeType") for m in fee})[:6]
        add(r, "feeType-field-exists", "E3", f"{GAMMA}/markets",
            "feeType is readable per market so platform fees need no category guesswork",
            len(fee) > 0, sorted({m.get("feeType") for m in fee})[:6])

    # -------------------------------------------------- WebSocket host (DNS only)
    import socket
    try:
        ip = socket.gethostbyname("ws-subscriptions-clob.polymarket.com")
        r.measurements["ws_dns"] = ip
    except Exception as e:  # noqa: BLE001
        r.measurements["ws_dns"] = f"ERR {e!r}"


# The claims P05's ingest code is written against. Everything else in `measurements` moves with the market and
# is reported informationally, because "fills/sec changed" is a Tuesday, not a broken spec. `redeem_*` is
# deliberately absent: it is a SAMPLE OF ONE ROW, so its values change every run while the RULE behind it
# ("price==0, payout in usdcSize") is asserted by the named check `redeem-payout-in-usdcsiz`, and the
# failing-check-id set below is what compares that. Listing the sample as structural is how a checker starts
# crying wolf, and a checker that cries wolf gets muted.
# `fee_type_observable_per_market` is NOT here, and the reason is the same as `redeem_row_payout_fields`: it is
# a set collected from whichever top-100 sample the run happened to see, so it changed between two runs on the
# same day (`sports_fees_v3` out, `finance_prices_fees` in) while nothing about the *spec* changed. Listing a
# sample as structural turns the checker into a coin flip, and a coin-flip checker gets ignored, which is worse
# than no checker. The structural claim that IS made about fees lives in a named check (the field exists and is
# a non-empty string), and the enum is documented as open-ended in docs/P05-data-ingestion.md.
STABLE_MEASUREMENTS = ("gamma_row_cap", "paged_event_rows", "data_trades_cf_cache_status", "cache_buster_works",
                       "cache_bust_newer_of_3")


def check_cache(path: str, payload: dict) -> int:
    """Compare a fresh probe against the recorded one and report drift in the structural claims.

    Exit codes are the contract, and they are why `make probe-fresh` does not wrap this in `|| echo`:
    0 = every comparable claim matches; 1 = at least one structural claim CONTRADICTS the record, so P01's
    spec is stale and must be re-opened before P05's code is trusted; 2 = INCOMPLETE, something could not be
    compared (a check added after the recording, or a newly failing one whose cause is unclear) which is neither
    a pass nor a venue finding; 3 = the venue was unreachable, which is not a finding about anything.
    """
    fresh = payload
    try:
        cached = json.load(open(path))
    except FileNotFoundError:
        print("no cached probe at %s — run `python3 tools/datasource-probe.py --save %s` first" % (path, path))
        return 3
    except json.JSONDecodeError as e:
        print("cached probe at %s is not valid JSON: %s" % (path, e))
        return 1
    drift, same, absent, skipped = [], [], [], []
    a, b = cached.get("measurements") or {}, fresh.get("measurements") or {}
    # A measurement that the run mode cannot produce is not a venue change. Both sides are consulted because the
    # recorded P01 probe predates this flag and was taken in deep mode.
    shallow = [c for c, side in (("fresh", fresh), ("recorded", cached))
               if side.get("deep") is False]
    for k in STABLE_MEASUREMENTS:
        if k in DEEP_ONLY_MEASUREMENTS and shallow and (k not in a or k not in b):
            skipped.append((k, "+".join(shallow)))
            continue
        if k not in a or k not in b:
            absent.append(k)
            continue
        if k == "cache_bust_newer_of_3":
            # "3/3" vs "1/3" is the tape being quiet, not the cache changing; "0/3" is the claim failing.
            (drift if str(new_ := b[k]).startswith("0") and not str(a[k]).startswith("0") else same).append(
                (k, a[k], b[k]))
            continue
        (same if a[k] == b[k] else drift).append((k, a[k], b[k]))
    print("probe cache check against %s (recorded %s)" % (path, cached.get("started_at")))
    for k, old, new in same:
        print("  same   %-34s %s" % (k, json.dumps(old, default=str)[:60]))
    for k, old, new in drift:
        print("  DRIFT  %-34s recorded %s -> now %s" % (k, json.dumps(old, default=str)[:48],
                                                        json.dumps(new, default=str)[:48]))
    for k, side in skipped:
        print("  not measured: %-24s (a %s run is --fast, which skips the cache block)" % (k, side))
    for k in absent:
        # Saying it out loud matters: an exit code without a line like this is indistinguishable from a real
        # finding, and silence is what let a 502 masquerade as "the venue moved".
        which = "recorded P01 probe" if k not in a else "fresh run"
        print("  not comparable: %-24s missing from the %s (the check postdates that capture: %s)"
              % (k, which, "reported, not diffed" if k in b else "no value either side"))
        if k in b:
            print("      fresh value: %s" % json.dumps(b[k], default=str)[:80])
    old_fails, new_fails = set(cached.get("failures") or []), set(fresh.get("failures") or [])
    if new_fails - old_fails:
        print("  newly FAILING checks:")
        for c in fresh.get("checks") or []:
            if c["id"] in (new_fails - old_fails):
                obs = json.dumps(c.get("observed"), default=str)
                transport = c.get("status") in (0, "ERR") or obs.startswith('"ERR') or "error" in obs.lower()[:40]
                # The distinction that matters: a transport failure says nothing about the spec and will
                # probably vanish on the next run; a payload that came back 200 with different KEYS is a
                # venue change and must reopen P01. Both used to print as a bare id, and the first time that
                # happened I read the id list as "the venue moved" when it was a timeout.
                print("     %-30s %s  expect: %s\n     %-30s observed: %s"
                      % (c["id"], "TRANSIENT?" if transport else "SHAPE", str(c.get("expect"))[:70], "",
                         obs[:180]))
        if all((c.get("status") in (0, "ERR")) for c in (fresh.get("checks") or []) if c["id"] in (new_fails - old_fails)):
            print("     all newly-failing checks look like transport errors — run again before believing it")
    if old_fails - new_fails:
        print("  no longer failing: %s" % ", ".join(sorted(old_fails - new_fails)))
    if drift or absent or (new_fails - old_fails):
        # Do not attach "the venue moved" to every red exit. That phrase sends a reader to the spec when the
        # right move is a re-run, and it is how one 502 became a confident false finding in this phase.
        if drift:
            print("\n  The venue changed under us: at least one structural claim differs. Re-open P01 and "
                  "correct the spec BEFORE writing more ingest code on top of it — do not edit this tool to "
                  "make the diff go away.")
        else:
            print("\n  Nothing is disproven, but not everything was evaluable. A line labelled SHAPE reopens "
                  "P01; a line labelled TRANSIENT? is this script's or the network's problem — run it again "
                  "before reading anything into it, and leave the check red until it is green on its own.")
        # 1 means the venue contradicts the spec. 2 means the comparison was incomplete — a partial pass is
        # not a pass, and conflating the two is how "we'll fix it later" starts.
        return 1 if drift else 2
    print("\n  %d structural claims unchanged; %d measurements moved (expected)"
          % (len(same), len(set(a) - set(STABLE_MEASUREMENTS))))
    return 0


DEEP_ONLY_MEASUREMENTS = ("data_trades_nocache", "cache_bust_newer_of_3")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--fast", action="store_true", help="skip the 8s cache-staleness wait")
    ap.add_argument("--save", metavar="PATH")
    ap.add_argument("--check-cache", metavar="PATH", nargs="?", const="docs/verification/P01-probe.json",
                    help="re-probe and diff the structural claims against a recorded run (exit 1 on drift)")
    a = ap.parse_args()

    r = Report(started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    deep_run = not a.fast
    if a.check_cache and not deep_run:
        # A drift verdict has to include the cache measurement, and only the deep block makes it. Fast mode and
        # a verdict together used to produce a red "MISSING cache_bust_newer_of_3" that meant nothing but the
        # flag combination.
        print("--check-cache needs the deep block for the cache measurement, so --fast is being ignored")
        deep_run = True
    probe(r, deep=deep_run)
    payload = {
        "started_at": r.started_at,
        # `--fast` skips the deep block, and the deep block is the ONLY thing that measures the cache. Without
        # this in the record, a fast check compares a half-run against a full one and calls the missing half
        # "drift" — which is exactly the false alarm I got from it.
        "deep": bool(deep_run),
        "findings": FINDINGS,
        "measurements": r.measurements,
        "checks": [asdict(c) for c in r.checks],
        "failures": [c.id for c in r.failures],
    }
    if a.save:
        with open(a.save, "w") as fh:
            json.dump(payload, fh, indent=2, default=str)
    if a.check_cache:
        if not a.json:
            print("re-probing, then diffing against", a.check_cache)
        unreachable = [c for c in r.checks if not c.ok and "unreachable" in str(c.observed).lower()]
        if unreachable or r.measurements.get("gamma_row_cap") in (None, 0):
            print("venue unreachable, so this says nothing about the spec: %s"
                  % ", ".join(c.id for c in unreachable) or "gamma returned nothing")
            return 3
        return check_cache(a.check_cache, payload)
    if a.json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(f"P01 data-source probe — {r.started_at}\n")
        for c in r.checks:
            print(f"  [{'ok' if c.ok else 'FAIL'}] {c.id:32s} expect: {c.expect}")
            if not c.ok:
                print(f"         observed: {json.dumps(c.observed, default=str)[:200]}")
        print("\n  measurements:")
        for k, v in r.measurements.items():
            print(f"    {k:34s} {json.dumps(v, default=str)[:120]}")
        print(f"\n  {len(r.checks) - len(r.failures)}/{len(r.checks)} checks passed"
              + (f"  → FAILURES: {', '.join(c.id for c in r.failures)}" if r.failures else ""))
        print("  Note: an upstream change here means the spec is stale — re-open P01, do not edit this test.")
    return 1 if r.failures else 0


if __name__ == "__main__":
    sys.exit(main())
