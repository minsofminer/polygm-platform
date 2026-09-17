#!/usr/bin/env python3
"""P03 D1 — extend brand/tokens.json with the foundations layer.

Written as a generator rather than a hand edit for one reason: P02 established that every value the
UI consumes must be traceable to brand/tokens.json, and a hand-typed scale is where a design system
starts lying. Re-runnable and idempotent.

  python3 tools/build-foundations.py          # writes tokens.json + prints the diff summary
  python3 tools/build-foundations.py --check  # exit 1 if tokens.json drifts from this file
"""
import hashlib
import json
import sys

TOKENS = "brand/tokens.json"

# --- D1.1 spacing: 4px base -------------------------------------------------------------
# module-level constants are evaluated top-down: this is used by DENSITY below, so it must precede it
DENSITY_NOTE = (
    "min_touch_target 44 is NOT scaled down by dense — dense shrinks text rows, never hit areas; at "
    "dense the row's padded box stays ≥44px tall on touch breakpoints. shell-max-width 1680 is the "
    "ceiling: past it the user gets bigger gutters, not a 5th column.")

SPACING = {
    "base": 4,
    "unit": "px",
    "rule": "every gap/pad/margin in the app is a multiple of 4; 2px and 6px exist only for optical work inside the mark and 1px hairlines",
    "scale": [
        {"name": "0", "px": 0}, {"name": "1", "px": 4}, {"name": "2", "px": 8},
        {"name": "3", "px": 12}, {"name": "4", "px": 16}, {"name": "5", "px": 20},
        {"name": "6", "px": 24}, {"name": "8", "px": 32}, {"name": "10", "px": 40},
        {"name": "12", "px": 48}, {"name": "16", "px": 64}, {"name": "20", "px": 80},
    ],
    "allowed_offgrid": [
        {"px": 1, "use": "hairline borders and the OrderBook depth bar's 1px track"},
        {"px": 2, "use": "icon-to-label gap inside 11px text, where 4px reads disconnected"},
        {"px": 3, "use": "stacked yes/no chip gutter at dense density only"},
    ],
}

# --- D1.2 borders ------------------------------------------------------------------------
BORDER = {
    "widths": [
        {"name": "hair", "px": 1, "use": "default row/column separators, card edges"},
        {"name": "strong", "px": 2, "use": "focus rings, active tab underline, selected row left rule"},
        {"name": "heavy", "px": 3, "use": "drop targets (drag an alert onto a market), resize handles"},
    ],
    "colors": {
        "default": "var(--pgm-border-default)",
        "strong": "var(--pgm-border-strong)",
        "note": "border tokens are theme-scoped in P02's semantic block; never hardcode a grey",
    },
}

# --- D1.3 elevation: deliberately weak ---------------------------------------------------
ELEVATION = {
    "policy": "a trading terminal is read all day; elevation is used almost never. Depth comes from background steps + hairlines, not shadow.",
    "levels": [
        {"name": "0", "use": "page surface", "shadow": "none"},
        {"name": "1", "use": "cards, panels, sticky table headers", "shadow": "0 1px 0 var(--pgm-border-default)"},
        {"name": "2", "use": "dropdown, popover, command palette", "shadow": "0 4px 12px -6px rgba(0,0,0,.55), 0 0 0 1px var(--pgm-border-default)"},
        {"name": "3", "use": "modal, toast, TradeTicket sheet (the ONLY surfaces above 2)", "shadow": "0 12px 32px -12px rgba(0,0,0,.65), 0 0 0 1px var(--pgm-border-strong)"},
    ],
    "forbidden": ["glassmorphism/blur on data surfaces", "elevation on rows or cells", "inner shadow on inputs (use bg.inset instead)", "more than 20px animated blur (perf rule)"],
    "light_theme_variant": "shadows drop the black alpha to .12 on light; the 1px ring keeps the surface legible instead",
}

# --- D1.4 density ------------------------------------------------------------------------
DENSITY = {
    "setting": {"levels": ["comfortable", "compact", "dense"], "default": "dense", "storage": "user setting, per device, no account needed",
                "why_dense_default": "median fill is $5–6 and a 24h book page is 18.4M USD across ~100 markets; a terminal that shows 14 rows is a terminal you scroll"},
    "row_heights_px": {
        "table_row": {"dense": 22, "compact": 28, "comfortable": 36},
        "tape_row": {"dense": 20, "compact": 26, "comfortable": 34},
        "book_level": {"dense": 16, "compact": 20, "comfortable": 26},
        "list_card": {"dense": 44, "compact": 56, "comfortable": 72},
    },
    "padding_px": {
        "cell_x": {"dense": 6, "compact": 8, "comfortable": 12},
        "panel": {"dense": 8, "compact": 12, "comfortable": 16},
        "screen": {"dense": 8, "compact": 12, "comfortable": 20},
    },
    "font_size_px": {"dense": 11, "compact": 12, "comfortable": 13},
    "constants": {
        "topbar": 40, "tab_bar": 32, "trade_ticket_row": 28, "min_touch_target": 44,
        # must live in the GENERATOR's dict: the builder replaces the whole constants block, so a key added
        # only to brand/tokens.json silently vanished on the next run and took --pgm-shell-max-width with it
        # (build-tokens.mjs emits that var only when this key exists). 2026-09-17: this cost a green gate.
        "shell-max-width": 1680,
        "note": DENSITY_NOTE,
    },
}

# --- D1.5 breakpoints ---------------------------------------------------------------------
BREAKPOINTS = {
    "unit": "px",
    "values": [
        {"name": "xs", "max": 639, "layout": "single column, one panel at a time, bottom sheet for the ticket, tab bar for Book/Tape/Positions", "who": "the actual majority — mid-range Android"},
        {"name": "sm", "min": 640, "layout": "single column + persistent topbar; ticket becomes an inline panel"},
        {"name": "md", "min": 768, "layout": "two columns: chart+book left, tape right"},
        {"name": "lg", "min": 1024, "layout": "two columns with a rail for watchlist"},
        {"name": "xl", "min": 1280, "layout": "full terminal: 3 columns (watchlist | market+chart | book+tape), ticket docks right"},
        {"name": "2xl", "min": 1536, "layout": "same as xl, wider tape and 12 book levels"},
        {"name": "3xl", "min": 1680, "layout": "wide: 4 columns — watchlist rail can collapse to icons, DepthChart gets a second pane (time + price axis), tape shows 24 rows"},
    ],
    "rules": [
        "layout is fluid between breakpoints; only column COUNT changes at them",
        "never introduce a breakpoint above 1680 — a 4k user gets bigger gutters, not a 5th column",
        "the ≥1280 terminal layout and the <640 mobile layout are the only two designs; everything between is a reflow of the terminal",
    ],
}

# --- D1.6 motion: values taken verbatim from the vendored skill ---------------------------
MOTION = {
    "source": "skills/emilkowalski-skills/skills/review-animations/STANDARDS.md (Easing + Duration + Physicality sections) — not invented here",
    "easing": {
        "ease-out": "cubic-bezier(0.23, 1, 0.32, 1)",
        "ease-in-out": "cubic-bezier(0.77, 0, 0.175, 1)",
        "ease-drawer": "cubic-bezier(0.32, 0.72, 0, 1)",
        "decision_order": "entering/exiting → ease-out; moving or morphing on screen → ease-in-out; hover/colour → ease; constant motion (marquee, progress) → linear; default → ease-out",
        "forbidden": ["ease-in on UI", "transition: all", "hand-rolled curves when easing.dev/easings.co exist"],
    },
    "duration_ms": {
        "press": {"min": 100, "max": 160, "use": "button/row :active scale(0.97–0.98)"},
        "micro": {"min": 80, "max": 120, "use": "flash-on-change in, tooltip appear after first"},
        "small": {"min": 125, "max": 200, "use": "popovers, small overlays, chip toggles"},
        "medium": {"min": 150, "max": 250, "use": "dropdowns, selects, disclosure, tab underline slide"},
        "large": {"min": 200, "max": 300, "use": "modals, sheet settle"},
        "drawer": {"min": 300, "max": 450, "use": "drag-to-dismiss with velocity, ease-drawer — the ONLY surface allowed past 300ms, and only because it is gesture-driven and interruptible (springs keep velocity on reversal)"},
        "forbidden": "anything animated on a data value: see number_policy",
    },
    "ui_ceiling_ms": 300,
    "flash_on_change": {
        "what": "background only — the glyph and the number never move, animate, or crossfade",
        # 90ms spike + 200ms decay = 290ms total. The first retoken used 260ms, which summed to 350ms and
        # broke the ≤300ms ceiling the same commit claimed to enforce — P03's G4.2 caught it. Kept in the
        # GENERATOR, not just the output: patching tokens.json alone left the source of truth contradicting
        # the tool that owns it, and the phase gate stayed green because nothing compared the two (now G1.7).
        "in_ms": 90, "hold_ms": 0, "out_ms": 200, "easing_in": "ease", "easing_out": "ease-out",
        "up": "var(--pgm-action-buy-bg)", "down": "var(--pgm-action-sell-bg)",
        "why_background": "a terminal user tracks a column's vertical position; anything that shifts glyphs or reflows a digit makes the whole column unreadable at 20–30 updates/sec",
        "total_ms": 290,
        "ceiling_note": "the 300ms ceiling is per-transition; a flash is two sequential ones (90ms spike, 200ms decay) = 290ms total, still under the ceiling both ways. The prompt asked for a 600ms decay, which is over the ceiling per-transition AND in total.",
    },
    "number_policy": {
        "rule": "nothing animates a number",
        "includes": ["no counting/tweening to a new price", "no per-character flip/roll", "no crossfade between old and new value", "no width animation on the cell (tabular-nums + fixed slot keeps layout still)"],
        "allowed": ["instant text swap", "background flash (above)", "a ▲/▼ glyph that changes with the value, not instead of it"],
        "reduced_motion": "flash becomes a 1-frame 1px left border in the same colour, or nothing at all; all transforms become none",
    },
    "physicality": {
        "never": "scale(0) — start at 0.9–0.97 with opacity 0",
        "popovers": "transform-origin from the trigger (origin-aware animation), never center",
        "modals": "exempt — stay transform-origin: center",
        "springs": "reserve for drag-to-dismiss; bounce 0.1–0.3, e.g. {type:'spring', duration:0.5, bounce:0.2}",
    },
    "vocabulary": {
        "note": "terms quoted from skills/animation-vocabulary/SKILL.md — use these names in code comments and specs, do not invent synonyms",
        "used": ["Stagger", "Origin-aware animation", "Crossfade", "Clip-path", "Rubber-banding"],
        "banned_as_design": ["Morph", "Shared element transition", "Layout animation", "Pop in", "Bounce"],
        "why_banned": "each is a data-integrity risk here: morphing or sharing an element between a market row and a detail view means the same pixels represent two prices at once",
    },
    "where_motion_is_allowed": {
        "allowed": ["sheet/drawer enter+exit", "popover open", "toast", "command palette", "tab indicator slide", "copy-confirmation icon"],
        "rejected_after_review": [
            {"candidate": "stagger-in market cards on the Explore grid", "verdict": "reject — seen on every navigation, adds 200ms to the slowest thing (perception), and the grid is data not marketing"},
            {"candidate": "animated count-up on portfolio value", "verdict": "reject — violates number_policy and hides the fact that a number changed"},
            {"candidate": "tape row slide-in from the right", "verdict": "reject — 14.7–33.3 fills/sec means 20+ concurrent animations; it also destroys the vertical scan. New rows appear instantly; only background flashes"},
            {"candidate": "depth chart path morph on tick", "verdict": "reject — see performance rule; re-rendering a path each tick at 20Hz is the exact way to drop frames on a mid-range Android. New data appends, no interpolation"},
            {"candidate": "skeleton shimmer while loading", "verdict": "keep but static opacity, no translate — shimmer is linear motion on a surface that is explicitly telling the user data is NOT arriving"},
            {"candidate": "hover lift on table rows", "verdict": "reject — background change only; transform on a row shifts 1px and reads as jitter at 22px height"},
        ],
    },
}

# --- D1.7 z-index (needed because 4 floaty surfaces collide) ------------------------------
RADIUS = {
    "policy": "inherited from the source system (polymarket.com compiled CSS reads `border-radius: calc(0.7rem - 6px)` etc.), kept as deltas so the whole UI re-scales from one base",
    "base": "0.7rem",
    "chip": "999px",
    "note": "chip is a pill by definition and card is the base; every other step is calc(base + delta). Nothing outside this block may state a radius.",
}

LAYERS = {"dropdown": 1000, "sticky_header": 1100, "drawer_scrim": 1200, "drawer": 1300, "modal": 1400, "toast": 1500, "trade_ticket_sheet": 1450,
          "note": "only these 7 values exist; a component that needs a new one is a layout bug, not a token request"}



# --- D2 inventory: the component and state lists live HERE so the Storybook list, the specimen and
# the gate all read one source. A gate that re-typed this list could pass while the docs disagreed.
COMPONENTS = {
    "states": [
        "default", "hover", "active", "focus-visible", "disabled", "loading",
        "error", "empty", "stale", "disconnected", "insufficient",
    ],
    "state_note": "the prompt's 8 plus the 3 a terminal needs: stale, disconnected, insufficient — each of which can block an order",
    "primitives": [
        "Button", "IconButton", "Icon", "Input", "NumberInput", "Select", "Checkbox", "Toggle",
        "Slider", "Tooltip", "Badge", "Tag", "Avatar", "Skeleton", "Spinner", "Progress",
        "Divider", "Modal", "Sheet", "Popover", "Menu", "Tabs", "SegmentedControl", "Accordion",
        "Toast", "EmptyState", "ErrorState", "DataTable", "VirtualList", "Pagination",
        "CommandPalette",
    ],
    "domain": [
        "PriceCell", "YesNoPair", "OrderBook", "DepthChart", "TapeRow", "TradeTicket",
        "PositionRow", "TraderCard", "AlertRuleBuilder", "CopyConfigPanel", "WalletPanel",
        "MarketCard", "StaleIndicator",
    ],
    "domain_extra_states": {
        "OrderBook": ["balanced", "near-empty", "one-sided", "locked"],
        "YesNoPair": ["normal", "residual-warn", "no-book-one-side"],
        "TapeRow": ["trade", "redeem", "convert"],
        "StaleIndicator": ["watch", "stale", "blocking"],
        "TradeTicket": ["valid", "not-accepting", "below-min", "bad-tick", "insufficient", "confirm-required"],
    },
    "densities": ["dense", "compact", "comfortable"],
    "themes": ["dark", "light"],
    "vision_models": ["normal", "deuter", "protan"],
}

# --- D3 screens: name + route, so the gate can verify every screen has a spec section AND that each
# field line cites a live endpoint shape.
SCREENS = [
    {"n": 1, "name": "Landing / marketing", "route": "/"},
    {"n": 2, "name": "Sign up", "route": "/signup"},
    {"n": 3, "name": "Sign in + 2FA + recovery", "route": "/signin"},
    {"n": 4, "name": "Markets", "route": "/markets"},
    {"n": 5, "name": "Event detail", "route": "/event/{slug}"},
    {"n": 6, "name": "Market detail / terminal", "route": "/market/{condition_id}"},
    {"n": 7, "name": "Live tape", "route": "/tape"},
    {"n": 8, "name": "Traders / leaderboard", "route": "/traders"},
    {"n": 9, "name": "Trader profile", "route": "/trader/{address}"},
    {"n": 10, "name": "Wallet Radar", "route": "/radar"},
    {"n": 11, "name": "Whale tracker", "route": "/whales"},
    {"n": 12, "name": "Portfolio", "route": "/portfolio"},
    {"n": 13, "name": "Copy trading", "route": "/copy"},
    {"n": 14, "name": "Automation", "route": "/automation"},
    {"n": 15, "name": "Alerts", "route": "/alerts"},
    {"n": 16, "name": "Profile & settings", "route": "/settings"},
    {"n": 17, "name": "Wallet / deposit / withdraw / key export", "route": "/wallet"},
    {"n": 18, "name": "Billing", "route": "/billing"},
    {"n": 19, "name": "Referrals", "route": "/referrals"},
    {"n": 20, "name": "Risk disclosure & terms", "route": "/risk"},
    {"n": 21, "name": "404 / 500 / maintenance / rate-limited", "route": "/error"},
    {"n": 22, "name": "Onboarding", "route": "/start/{step}"},
]


P02_SECTIONS = ("color", "semantic", "chart", "typography", "radius", "rules")
# P02's palette/typography/chart blocks are `proposed` in the Brand Lock, i.e. they are reviewed by
# a human, not by this generator. Pin their digest so a foundations rebuild can never quietly move
# a colour, and so --check has an oracle that is NOT the file it is checking.
P02_SHA = "p02_sections_sha256"


def canon(o) -> str:
    return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def p02_digest(d: dict) -> str:
    return hashlib.sha256(canon({k: d.get(k) for k in P02_SECTIONS}).encode()).hexdigest()[:16]


def build(current: dict) -> dict:
    """Pure: output depends only on the literals above, never on the current file. That is what
    makes --check meaningful and re-runs idempotent."""
    out = json.loads(json.dumps(current))
    out["meta"]["foundationsVersion"] = 1
    out["spacing"] = SPACING
    out["border"] = BORDER
    out["elevation"] = ELEVATION
    out["density"] = DENSITY
    out["breakpoints"] = BREAKPOINTS
    out["motion"] = MOTION
    out["layers"] = LAYERS
    out["radius"] = {**(current.get("radius") or {}), **{k: v for k, v in RADIUS.items() if k != "note"}}
    out["components"] = COMPONENTS
    out["screens"] = SCREENS
    return out


def main() -> int:
    check = "--check" in sys.argv
    d = json.load(open(TOKENS))
    want = build(d)
    sections = ("spacing", "border", "elevation", "density", "breakpoints", "motion", "layers",
                "components", "screens")
    added = [k for k in sections if canon(d.get(k)) != canon(want[k])]
    if check:
        pin = (d.get("provenance") or {}).get(P02_SHA)
        if pin != p02_digest(d):
            print(f"DRIFT P02 sections digest is {p02_digest(d)}, pinned {pin!r} — colour/type/chart changed without an audit")
            return 1
        if added:
            print(f"STALE {TOKENS} missing/out of date: {', '.join(added)}")
            print("  fix: python3 tools/build-foundations.py")
            return 1
        print(f"OK   {TOKENS} foundations match the generator; P02 sections pinned at {pin}")
        return 0
    now = p02_digest(d)
    if "--repin" in sys.argv:
        want.setdefault("provenance", {})[P02_SHA] = now
        want["provenance"][P02_SHA + "_note"] = (
            "digest of P02's color/semantic/chart/typography/radius/rules, taken 2026-09-17 after the "
            "colour audit + P02 gate passed. Re-pin only alongside an audited P02 change.")
        json.dump(want, open(TOKENS, "w"), indent=2, ensure_ascii=False); open(TOKENS, "a").write("\n")
        print(f"pinned P02 sections digest {now}" + ("" if added else " (foundations already up to date)"))
        return 0
    if not added:
        print("OK   nothing to do — foundations already present and identical")
        return 0
    for k in P02_SECTIONS:
        assert json.dumps(d.get(k)) == json.dumps(want.get(k)), f"P02 section {k} would have been modified — refusing"
    json.dump(want, open(TOKENS, "w"), indent=2, ensure_ascii=False)
    open(TOKENS, "a").write("\n")
    print("wrote:", ", ".join(added))
    print(f"  spacing {len(SPACING['scale'])} steps · elevation {len(ELEVATION['levels'])} · density {len(DENSITY['setting']['levels'])} · breakpoints {len(BREAKPOINTS['values'])} · layers {len([k for k in LAYERS if k != 'note'])}")
    print("  P02 sections untouched by construction: build() reads none of them; digest now " + p02_digest(want))
    return 0


if __name__ == "__main__":
    sys.exit(main())
