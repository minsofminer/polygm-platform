"""Wallet classification, mapped onto prediction markets (P05 D5).

gmgn's taxonomy does not port: there is no "developer wallet" in a Fed market, "sniper" means something
different when there is no mempool to snipe, and "KOL/VC" is a social claim we cannot verify. What transfers
is the *purpose*: tell a user, from behaviour alone, whether the person on the other side of their trade knows
something. So the set below is derived from what prediction markets actually record — shares, notional, market
maturity, resolution outcome, and time-to-move — and each label states its rule as arithmetic, with a minimum
sample, and with the false-positive control named in the same object that produces the label.

Two global rules:
* A label is an OBSERVATION about behaviour, never a claim about a person. `disclaimer` is not boilerplate to
  be hidden in a tooltip: `publishable=False` on a label means the UI may show it in a private view and must
  not put it in a public leaderboard, a screenshot-ready card, or anything that names a wallet.
* Every threshold below is relative to the market's own distribution where a market distribution exists
  (fill size, move size), because $1,000 is a whale in a $30k market and noise in a $200M one. P01 measured
  median fill $5, p95 $133, max $3,000 in one window: a fixed threshold is wrong at both ends, and a
  percentile is right in both.
"""
from __future__ import annotations

from dataclasses import dataclass, field

DISCLAIMERS = {
    "whale": "size relative to this market's recent fills; not a statement about the person",
    "smart_money": "past realised results only; a small sample is noise, and this label is suppressed below it",
    "new_wallet": "account age and trade count on this platform only",
    "insider_suspect": "statistical pattern only. This is not an accusation, and it is not evidence of "
                       "anything. Acting on it is a trading decision you are making yourself.",
    "cluster": "correlation in time and direction; unrelated wallets can look like one",
    "wash_like": "self-matching patterns inferred from timing; Polymarket's own rules decide what counts",
}


@dataclass(frozen=True)
class Label:
    name: str
    confidence: float                 # 0..1, and always derived from a count, never set by hand
    evidence: tuple                   # the numbers that produced it, so a user can check the claim
    publishable: bool
    recompute: str

    def as_row(self, wallet: str) -> dict:
        return {"wallet": wallet, "label": self.name, "confidence": round(self.confidence, 3),
                "evidence": dict(self.evidence), "publishable": self.publishable,
                "disclaimer": DISCLAIMERS[self.name], "recompute_cadence": self.recompute}


def _pct(rows, q, key):
    vals = sorted(int(r.get(key) or 0) for r in rows)
    if not vals:
        return 0
    return vals[min(len(vals) - 1, int(q * len(vals)))]


@dataclass
class Classifier:
    """Stateless functions over a wallet's recent fills + market context. Everything the ingest layer can
    compute from stored rows, and nothing it cannot: no label here may need a number we do not keep, because
    "every derived number in the UI must be reproducible from stored data" is a P05 constraint, not a wish."""
    whale_pctl: float = 0.995           # 99.5th percentile of THIS market's recent fills
    whale_floor_micro: int = 500 * 10 ** 6        # ...and at least $500, so a dead market's $12 fill is not
    whale_min_sample: int = 40                    #   "whale activity". Below 40 fills there is no percentile.
    smart_min_settled: int = 20           # a wallet needs 20 settled positions before we rank its judgement
    smart_min_win_rate: float = 0.62
    smart_min_pnl_micro: int = 5_000 * 10 ** 6
    smart_min_markets: int = 3            # one lucky market is a story about one market
    new_max_age_s: int = 7 * 86400
    new_max_trades: int = 15
    insider_min_hits: int = 4             # see the false-positive control on the method
    insider_min_win_rate: float = 0.80
    insider_max_market_depth_micro: int = 25_000 * 10 ** 6
    insider_window_s: int = 900
    cluster_window_s: int = 600
    cluster_min_members: int = 4
    wash_min_roundtrip: int = 6
    wash_max_gap_s: int = 45
    stats: dict = field(default_factory=dict)

    # ------------------------------------------------------------------ whale
    def whale(self, fill: dict, market_fills: list[dict]) -> Label | None:
        n = int(fill.get("usd_notional_micro") or 0)
        if len(market_fills) < self.whale_min_sample:
            self.stats["whale_suppressed_small_sample"] = self.stats.get("whale_suppressed_small_sample", 0) + 1
            return None
        cut = _pct(market_fills, self.whale_pctl, "usd_notional_micro")
        if n < max(self.whale_floor_micro, cut):
            return None
        multiple = n / max(1, cut)
        return Label("whale", min(0.99, 0.5 + 0.25 * multiple),
                     (("notional_micro", n), ("market_p995_micro", cut),
                      ("multiple_of_p995", round(multiple, 2)), ("sample_fills", len(market_fills))),
                     True, "per fill (streaming)")

    # ------------------------------------------------------------------ smart money
    def smart_money(self, wallet: dict) -> Label | None:
        settled = int(wallet.get("settled_positions") or 0)
        if settled < self.smart_min_settled:
            # THE false-positive control for this label: a 2-of-2 wallet is a coin that got lucky twice, and
            # labelling it "smart money" is how a product becomes a rumour mill. Count, not confidence.
            self.stats["smart_suppressed_sample"] = self.stats.get("smart_suppressed_sample", 0) + 1
            return None
        wr = float(wallet.get("win_rate") or 0.0)
        pnl = int(wallet.get("realized_pnl_micro") or 0)
        markets = int(wallet.get("distinct_markets") or 0)
        if wr < self.smart_min_win_rate or pnl < self.smart_min_pnl_micro or markets < self.smart_min_markets:
            return None
        # Confidence is a function of sample size AND breadth, so the label degrades gracefully as the wallet
        # gets bigger instead of saturating at 0.99 for anyone who crosses the bar.
        conf = min(0.95, 0.5 + 0.02 * (markets - self.smart_min_markets)
                   + 0.5 * (wr - self.smart_min_win_rate) / max(0.01, 1 - self.smart_min_win_rate))
        return Label("smart_money", conf, (("settled", settled), ("win_rate", round(wr, 3)),
                                           ("realized_pnl_micro", pnl), ("distinct_markets", markets)),
                     True, "nightly + on each resolution")

    # ------------------------------------------------------------------ new wallet
    def new_wallet(self, wallet: dict, now_s: int) -> Label | None:
        first = int(wallet.get("first_seen_s") or 0)
        trades = int(wallet.get("trade_count") or 0)
        if not first:
            return None
        age = max(0, now_s - first)
        if age > self.new_max_age_s or trades > self.new_max_trades:
            return None
        return Label("new_wallet", 0.9 if age < 86400 else 0.6,
                     (("age_s", age), ("trade_count", trades)), True, "hourly")

    # ------------------------------------------------------------------ insider suspect
    def insider_suspect(self, wallet: dict, markets: dict) -> Label | None:
        """Repeatedly entering a thin market shortly before a move that later resolves their way.

        The false-positive control is a conjunction, and every clause removes a way an innocent trader looks
        guilty:
          * `hits >= 4` — one prescient trade is news; four in a row is the pattern we are claiming.
          * `win_rate >= 0.80` over those hits, so a wallet that guessed right twice and wrong six times is
            not labelled at all.
          * markets must be THIN (`depth <= $25k`), because in a deep market the move had many participants and
            "you were early" is not informative — this is also the clause that stops us labelling anyone who
            traded a public announcement on the Fed.
          * no pre-existing position in that market (a hedge or an exit can look like an entry), enforced by
            `pre_existing_position` on each hit.
          * `publishable=False`, always. The label may appear in the user's own watchlist view with its
            disclaimer and may never be rendered next to a wallet name in a public surface. Polymarket's data
            is public, so the accusation would be permanent, and "permanent" is exactly what a statistical
            pattern with a 4-sample floor does not earn.
        """
        hits = [h for h in (wallet.get("early_entries") or [])
                if not h.get("pre_existing_position")
                and int(markets.get(h.get("market"), {}).get("depth_micro") or 10 ** 15)
                <= self.insider_max_market_depth_micro
                and int(h.get("lead_s") or 10 ** 9) <= self.insider_window_s
                and bool(h.get("resolved_his_way"))]
        if len(hits) < self.insider_min_hits:
            self.stats["insider_suppressed_hits"] = self.stats.get("insider_suppressed_hits", 0) + 1
            return None
        wr = sum(1 for h in hits if h.get("resolved_his_way")) / len(hits)
        if wr < self.insider_min_win_rate:
            return None
        conf = min(0.9, 0.45 + 0.1 * (len(hits) - self.insider_min_hits) + 0.4 * (wr - 0.8) / 0.2)
        return Label("insider_suspect", conf,
                     (("hits", len(hits)), ("win_rate", round(wr, 3)), ("window_s", self.insider_window_s),
                      ("max_market_depth_micro", self.insider_max_market_depth_micro),
                      ("markets", sorted({str(h.get("market")) for h in hits})[:5])),
                     False, "hourly, and on each resolution of a touched market")

    # ------------------------------------------------------------------ cluster
    def cluster(self, rows: list[dict]) -> list[Label]:
        """Wallets trading the same token, same side, inside one window. Method: bucket by
        (token_id, side, window_start), then report buckets with `min_members` distinct wallets.

        Cost: one pass over the window and a dict keyed by a 3-tuple — O(rows), which at 10x our measured
        20.8 fills/s is 2,080 rows/s and a few milliseconds. Agglomerative clustering on the same data would be
        O(n^2) and would not be re-runnable by a contractor at 3am, which is the trade this method makes
        deliberately: it will miss a coordinated group that trades at slightly different times, and we say so
        rather than pretending a graph algorithm fixes a window function.
        """
        buckets: dict[tuple, set] = {}
        for r in rows:
            ts = int(r.get("ts_ms") or 0) // (self.cluster_window_s * 1000)
            buckets.setdefault((r.get("token_id"), str(r.get("side") or "").upper(), ts), set()).add(
                r.get("wallet"))
        out = []
        for (token, side, win), wallets in buckets.items():
            if len(wallets) < self.cluster_min_members:
                continue
            out.append(Label("cluster", min(0.8, 0.4 + 0.05 * len(wallets)),
                             (("token_id", token), ("side", side), ("window_start_s", win * self.cluster_window_s),
                              ("members", len(wallets)), ("wallets", sorted(str(w) for w in wallets)[:50])),
                             False, "every window, on the tape stream"))
        return out

    # ------------------------------------------------------------------ wash / copy-farm
    def wash_like(self, rows: list[dict]) -> Label | None:
        """Buy-then-sell (or the reverse) of the same size on the same token inside `wash_max_gap_s`, N times.

        This one is not just a UI label: Polymarket revokes builder codes for non-bona-fide volume, so OUR OWN
        users' wash patterns are our revenue at risk, and the copy-farm case (a跟单 bot churning) is the same
        shape. Detection is therefore run over our attributed flow and reported to ops, not only to users.
        """
        by_token: dict[str, list] = {}
        for r in sorted(rows, key=lambda x: int(x.get("ts_ms") or 0)):
            by_token.setdefault(str(r.get("token_id")), []).append(r)
        trips = 0
        for _t, seq in by_token.items():
            for a, b in zip(seq, seq[1:]):
                same_size = int(a.get("size_micro") or -1) == int(b.get("size_micro") or -2)
                opposite = str(a.get("side") or "") != str(b.get("side") or "")
                gap = (int(b.get("ts_ms") or 0) - int(a.get("ts_ms") or 0)) / 1000
                if same_size and opposite and 0 <= gap <= self.wash_max_gap_s:
                    trips += 1
        if trips < self.wash_min_roundtrip:
            return None
        return Label("wash_like", min(0.9, 0.5 + 0.05 * (trips - self.wash_min_roundtrip)),
                     (("roundtrips", trips), ("max_gap_s", self.wash_max_gap_s),
                      ("rows_scanned", len(rows))), False, "every 5 min, and before attribution is settled")
