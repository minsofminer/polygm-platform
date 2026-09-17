"""Polygm core — the parts of the product that must be runnable anywhere.

Deliberately free of third-party dependencies (measured in P04: the environment that must always
work is `python3` + stdlib, because that is what a recovery script on a jump box has). Services import
this; it imports nothing from them."""

__all__ = ["money", "risk", "ledger", "executor", "config"]
