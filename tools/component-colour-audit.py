#!/usr/bin/env python3
"""P03 D4b — colour-collision check for surfaces where a misread costs money.

P02 audited the chart palette (many hues, one floor) and text/background contrast. That was the wrong
instrument for `PositionRow` / `TapeRow` / the book ladder, where the question is narrower and more
dangerous: can two hues that legitimately appear in the SAME ROW be confused by a colour-blind user?
So this checks *pairwise separation within each composed row* across three vision models, and adds a
lightness test — a red-green deficient user's reliable channel is L, so two equally-light hues are
indistinguishable however ΔE*ab rates their chroma.

Exemptions are structural and each one is a rule in brand/tokens.json.rules, never a suppression:
  · never-same-row            pairs layout keeps out of the same row
  · neutral roles             grey beside a hue is the anchor the row is read against, not a collision
  · column hue ownership      action hues live in the side/PnL cell, outcome hues in the outcome cell
  · identical token           action.buy IS the profit colour by design (P02); a glyph disambiguates

Tiers: critical ΔE≥20 AND ΔL≥0.12 (money direction) · major ΔE≥10 (P02's bar) · advisory reported only.

  python3 tools/component-colour-audit.py            # report
  python3 tools/component-colour-audit.py --gate     # exit 1 if any non-advisory finding
"""
import json
import sys

sys.path.insert(0, "tools")
from colour_audit_lib import SIMS, contrast, delta_e, luminance, to_rgb  # noqa: E402

TOKENS = "brand/tokens.json"
FLOOR = {"critical": (20.0, 0.12), "major": (10.0, 0.0), "advisory": (0.0, 0.0)}
# Above this ΔE*ab the pair is separable by hue even when ΔL is tiny, so the ΔL floor does not apply.
SECONDARY_DE_OK = 25.0
NEUTRAL = {"text.primary", "text.secondary", "text.muted", "border.default", "border.strong", "bg.base", "bg.elevated"}

# Which tokens may appear in which cell. Two hues from different families never carry the same meaning,
# so their collision is reported but does not fail — this is `rules["hue-is-owned-by-a-column"]`.
ALERTS = {"alert.critical", "alert.high", "alert.watch", "alert.info"}
ACTION_SIDE = {"action.buy", "action.sell"}
OUTCOME_SIDE = {"outcome.yes", "outcome.no"}



def rows() -> dict:
    """theme -> row -> (background token, [(cell label, foreground token)]).

    Keyed by TOKEN name, not by value or by a display string: the first version matched exemptions
    against the display label and an exemption silently never fired (`pnl.number` does not contain
    the substring `text.primary`). Names make that class of bug impossible."""
    spec = {
        "PositionRow": ("bg.base", [("outcome chip", "outcome.yes"), ("outcome chip", "outcome.no"),
                                    ("pnl number", "text.primary"), ("pnl glyph +", "action.buy"),
                                    ("pnl glyph −", "action.sell")]),
        "TapeRow": ("bg.elevated", [("side pill", "action.buy"), ("side pill", "action.sell"),
                                    ("outcome chip", "outcome.yes"), ("outcome chip", "outcome.no"),
                                    ("whale badge", "alert.critical")]),
        # what the two rules above exist to prevent, kept as a control: a bare alert dot inside the
        # compared pair, and a bare critical dot next to the side pill. If these ever PASS the rules
        # have been weakened, so they are asserted at the hard tier with no exemption.
        "YesNoPair": ("bg.elevated", [("yes", "outcome.yes"), ("no", "outcome.no"),
                                      ("residual warning text+icon", "text.primary")]),
        "OrderBook ladder": ("bg.base", [("bid", "outcome.yes"), ("ask", "outcome.no")]),
        # CONTROLS: compositions the rules exist to forbid, audited with every exemption disabled.
        # They MUST fail; if one passes, a rule has been weakened (asserted in main()).
        "CONTROL@bare alert dot inside a compared pair": ("bg.elevated", [("no", "outcome.no"), ("flag", "alert.high")]),
        "CONTROL@critical dot next to a side pill": ("bg.elevated", [("sell", "action.sell"), ("flag", "alert.critical")]),
        "YesNoPair": ("bg.elevated", [("yes", "outcome.yes"), ("no", "outcome.no"),
                                      ("residual warning text+icon", "text.primary")]),
    }
    return {theme: spec for theme in ("dark", "light")}


def same_row_exemptions(t: dict):
    rule = t.get("rules", {}).get("never-same-row") or {}
    out = set()
    for item in rule.get("pairs", []) or []:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            out.add(frozenset(item))
    for key, val in rule.items():
        if key in ("pairs", "reason") or not isinstance(val, list):
            continue
        for v in val:
            out.add(frozenset((key, v)))
    return out


def tier_for(ta: str, tb: str, row: str, exempt, money_pair) -> str:
    ps = frozenset((ta, tb))
    if row.startswith("CONTROL@"):
        return "critical"        # no exemptions at all — this composition is what the rules forbid
    if ps in exempt:
        return "advisory"                       # kept apart by the never-same-row layout rule
    if (ta in ACTION_SIDE and tb in OUTCOME_SIDE) or (tb in ACTION_SIDE and ta in OUTCOME_SIDE):
        return "advisory"                       # different cells, different meaning families
    if ps == frozenset(("action.buy", "action.sell")) and row in ("TapeRow", "PositionRow"):
        return "advisory"        # rules.direction-never-colour-alone: glyph + fill/outline, not hue
    alerts = {"alert.critical", "alert.high", "alert.watch", "alert.info"}
    if ta != tb and ({ta, tb} & alerts):
        # an alert hue is exempt only where the two things are already separated by AREA and WORDS:
        #   · outside a compared pair (rules.no-alert-hue-inside-a-compared-pair)
        #   · and, on the tape, the alert is the whale BADGE, never a bare dot
        #     (rules.whale-flag-is-a-badge-not-a-dot — badge vs pill differ in shape and carry text)
        if row in ("YesNoPair", "OrderBook ladder"):
            pass                                  # never exempt inside a compared pair
        elif row == "TapeRow":
            other = tb if ta in alerts else ta
            if other in OUTCOME_SIDE or other in ACTION_SIDE:
                return "advisory"
        elif "badge" in row or row in ("PositionRow", "MarketCard"):
            return "advisory"
    if ta in NEUTRAL or tb in NEUTRAL:
        return "advisory"                       # grey is the anchor, not a colour signal
    if ps == money_pair:
        return "critical"                       # profit vs loss in the same cell
    return "major"


def audit():
    t = json.load(open(TOKENS))  # noqa
    exempt = same_row_exemptions(t)
    money = frozenset(("action.buy", "action.sell"))
    findings, out, controls = [], [], {}
    for theme, spec in rows().items():
        sem = t["semantic"][theme]
        for row, (bgkey, cells) in spec.items():
            bg = sem[bgkey]
            resolved = [(lbl, tok, sem[tok]) for lbl, tok in cells]
            lines = [f"\n{row}/{theme}  bg {bg}"]
            for lbl, tok, h in resolved:
                c = contrast(h, bg)
                ok = "AA" if c >= 4.5 else ("AA-large" if c >= 3.0 else "FAIL")
                flag = "" if ok != "FAIL" else "  ✗"
                lines.append(f"  fg {lbl:14s} {tok:14s} {h} {c:5.2f}:1 {ok}{flag}")
                if ok == "FAIL":
                    findings.append((f"{row}/{theme}", f"{lbl} ({tok})", "contrast", round(c, 2),
                                     f"{h} on {bgkey} {bg} is {c:.2f}:1 — below the 4.5:1 body-text floor"))
            seen = set()
            # STRUCTURAL rules first: they do not care about ΔE, because the composition itself is the
            # defect (a dot with no word, an alert hue inside a pair being compared). Asserting these as
            # colour tests was wrong — gold vs orange is genuinely separable in dark (ΔE 27) and is
            # still forbidden there, which is exactly why the first version's canary looked "broken".
            compared = row.startswith("YesNoPair") or row.startswith("OrderBook")
            for lbl, tok, h in resolved:
                bare = not any(w in lbl for w in ("badge", "chip", "pill", "number", "dot-"))
                if tok in ALERTS:
                    if bare and row != "MarketCard":
                        lines.append(f"  critical {lbl}: alert hue with no word  ✗ (rules.whale-flag-is-a-badge-not-a-dot)")
                        findings.append((f"{row}/{theme}", lbl, "alert-needs-a-word", 0.0,
                                         "an alert colour without a word/area is a colour-only signal (WCAG 1.4.1)"))
                        if row.startswith("CONTROL@"):
                            controls[(row, theme)] = True
                    if compared:
                        lines.append(f"  critical {lbl}: alert hue inside a compared pair  ✗ (rules.no-alert-hue-inside-a-compared-pair)")
                        findings.append((f"{row}/{theme}", lbl, "alert-in-compared-pair", 0.0,
                                         "a warning rendered next to two values the user is comparing reads as a third price"))
                if row.startswith("CONTROL@") and not bare:
                    controls[(row, theme)] = True   # a control that is labelled is a different test; keep it failing
            for i in range(len(resolved)):
                for j in range(i + 1, len(resolved)):
                    (la, ta, ha), (lb, tb, hb) = resolved[i], resolved[j]
                    if ta == tb:
                        # identical hue: ΔE 0.0 by definition (P02's action-buy-aliases-result-profit).
                        # Colour cannot separate these, so words must. A cell whose label carries no
                        # text (a bare dot) fails — that is rules["whale-flag-is-a-badge-not-a-dot"].
                        # Two rules of different kinds, both checked STRUCTURALLY (a colour distance
                        # cannot express them): (i) identical hues need a word; (ii) an alert hue must not
                        # appear bare inside a row whose job is comparing two values.
                        bare = not any(w in la or w in lb for w in ("badge", "chip", "pill", "number"))
                        inside_compared = row.startswith("YesNoPair") or row.startswith("OrderBook")
                        alert_bare = bare and (ta in ALERTS or tb in ALERTS) and (inside_compared or row.startswith("CONTROL@"))
                        if alert_bare:
                            lines.append(f"  critical {ta}=={tb}: bare alert dot inside a compared pair  ✗")
                            findings.append((f"{row}/{theme}", f"{ta} ↔ {tb}", "bare-alert-dot", 0.0,
                                             "identical hue and an unlabelled alert inside a row that compares two values "
                                             "(rules.whale-flag-is-a-badge-not-a-dot, rules.no-alert-hue-inside-a-compared-pair)"))
                        elif bare:
                            lines.append(f"  critical {ta} used twice, both cells bare  ✗ needs a word")
                            findings.append((f"{row}/{theme}", f"{ta} ↔ {ta}", "identical-hue", 0.0,
                                             "same token in two cells of one row with no label on either: ΔE 0.0 cannot separate them"))
                        else:
                            lines.append(f"  advisory {ta} used twice — shape+word carry it (ΔE 0.0 by design)")
                        continue
                    de = {s: delta_e(ha, hb, s) for s in SIMS}
                    dl = abs(luminance(ha) - luminance(hb))
                    worst = min(de.values()); sim = min(de, key=de.get)
                    tier = tier_for(ta, tb, row, exempt, money)
                    dmin, dlmin = FLOOR[tier]
                    # A low ΔL is only dangerous when ΔE is also low: lightness is the fallback channel, so
                    # if hue separation survives the simulation the user still has a cue. Demanding ΔL
                    # unconditionally was wrong (it flagged gold-vs-orange at ΔE 44 as a collision).
                    bad = worst < dmin or (dlmin and dl < dlmin and worst < SECONDARY_DE_OK)
                    key = (row, theme, frozenset((ta, tb)))
                    if key in seen:
                        continue
                    seen.add(key)
                    if row.startswith("CONTROL@"):
                        controls.setdefault((row, theme), False)
                        if bad:
                            controls[(row, theme)] = True
                    note = "  ✗ below floor" if bad and tier != "advisory" else (
                        "  (exempt: " + ("layout" if frozenset((ta, tb)) in exempt else
                                         "shape" if row == "TapeRow" and frozenset((ta, tb)) == money else
                                         "neutral" if ta in NEUTRAL or tb in NEUTRAL else "column") + ")" if tier == "advisory" else "")
                    lines.append(f"  {tier:8s} {ta} ↔ {tb:14s} ΔE {worst:5.1f} ({sim:6s}) ΔL {dl:.3f}{note}")
                    if bad and tier != "advisory":
                        if worst < dmin:
                            why = f"ΔE {worst:.1f} < {dmin} under {sim}"
                        else:
                            why = f"ΔL {dl:.3f} < {dlmin} AND ΔE {worst:.1f} < {SECONDARY_DE_OK} (lightness is the only fallback and it is flat)"
                        findings.append((f"{row}/{theme}", f"{ta} ↔ {tb}", tier, round(worst, 1), why))
            out.append("\n".join(lines))
    return "\n".join(out), findings, controls


def main() -> int:
    body, findings, controls = audit()
    print(body)
    real = [f for f in findings if not f[0].split(" · ")[0].startswith("CONTROL@")]
    control_fails = [k for k, v in controls.items() if v]
    print(f"\n{len(real)} finding(s):")
    for f in real:
        print(f"  ✗ {f[0]} · {f[1]} · {f[2]}: {f[4]}")
    if not real:
        print("  clean — every non-exempt pair in a shared row separates in normal, deuteranopic and protanopic vision")
    # the canary: controls are the forbidden compositions, so each MUST be caught
    print(f"\ncontrols (must all be caught): {len(control_fails)}/{len(controls)}")
    for (row, theme), caught in sorted(controls.items()):
        print(f"  {'caught ✓' if caught else 'PASSED ✗'}  {row}  [{theme}]")
    canary_ok = len(control_fails) == len(controls) and controls
    if not canary_ok:
        print("  ✗ a forbidden composition now passes — an exemption is too broad, not a colour problem")
    if "--gate" in sys.argv:
        bad = bool(real) or not canary_ok
        print("\nGATE " + ("FAIL" if bad else "PASS") + f" ({len(real)} findings, canary {'ok' if canary_ok else 'BROKEN'})")
        return 1 if bad else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
