"""P11 · The leaderboard: six boards, the integrity rules that make them mean something, and the ranking engine.

Three modules, one public surface:

* `boards` — the specification as data: formulas, eligibility gates, tie-breaks, windows, cadence, and the
  integrity rules. Served whole at `/v1/leaderboard/methodology`, so the published page and the engine that ranks
  people are the same object rather than two descriptions of one thing.
* `integrity` — wash/round-trip volume, copy farms, provisional wallets, blown-up accounts, the single-best-trade
  share, and disputed-market exclusions. Everything is checkable with a query; nothing is a model.
* `rank` — the pure engine: evidence in, one board out, every row carrying the components it was ranked on, plus
  `explain()` for the question a leaderboard always gets ("why is this wallet above that one?").
"""
from . import boards, integrity, rank                                             # noqa: F401

__all__ = ["boards", "integrity", "rank"]
