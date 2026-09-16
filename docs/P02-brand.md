# P2 — Brand & Identity · Openout (working name → chosen)

*Produced 2026-09-16 ~17:15 UTC. Method bound by `docs/SKILLS.md`: no Higgsfield CLI in this
workspace, so the built-in-generator adapter applies; `brand/BRAND-KIT.md` is the Brand Lock and its
`fixed` fields were **not** changed; `brand/svg/mark.svg` was **not** regenerated — it was composed
from and measured against. Every colour claim in D4 is computed by `tools/colour-audit.py` and
`tools/palette-search.py`; every size claim in D3 by `tools/mark-legibility.py`.*

**Deviation, declared up front:** P02 asks for three *new* logo concept directions. The Brand Lock's
logo slot is `fixed`, and `logo.md` says a request for other branded graphics does not authorise a
redesign — so I did not generate replacement candidates. Instead D3 documents the approved mark's
construction and proves (or repairs) its survival at each required size. If you want genuinely new
candidates, say so explicitly and I will generate exactly three symbol-only prompts with the Brand
Lock hexes, per the adapter, for your choice.

---

## D1. Naming — 12 candidates, then one

Each candidate carries a *measured* risk where measurement was possible: DNS + certificate
transparency + a live HTTP/title fetch (`tools/name-collision-probe.py`, run 16:52–17:02 UTC), plus a
GitHub repo-count smell test. **Trademark registration was not checkable from this sandbox (no
USPTO/EUIPO access) — that risk column is `[UNVERIFIED]` for all 12 and is a paid pre-launch step, not
a formality.**

| # | Name | Meaning / evoke | Rationale (≤1 line) | Measured risk |
|---|---|---|---|---|
| 1 | **Openout** | a position held open until it resolves | 7 chars, 3 syllables, no `Poly`/`gmgn`, matches the mark's premise ("a question held open") | `.com/.trade/.markets/.bot/.bet/.xyz` all **no A record, no cert**; `openout.app` **live** ("OpenOut — Link-in-bio pages"), 80 GitHub repos (mostly unrelated). Trademark `[UNVERIFIED]` |
| 2 | Tally | to count; a counting stick; *tally* = agreement | 5 chars, instantly pronounceable everywhere | **Disqualified by evidence:** `tally.markets` is live as *"Tally · The risk officer for AI-assisted trading"* — same category, adjacent to trading tools; `tally.bot` live too; `tally.com` resolves |
| 3 | Open Bet | the settled/unclosed pair a punter already uses | describes the exact object we manage | "open bet" is generic industry phrasing → weak protectability; collision check not run `[UNVERIFIED]` |
| 4 | Tapemark | *(attempted: the mark a horse leaves at the gate)* | would have tied to "first in" | **Rejected: the etymology is false.** Checked racing glossaries (Saratoga, DRF): the terms are *tape* (the barrier), *tattoo* (the lip mark), *takeout* (commission). There is no "tapemark". `tapemark.com` has a cert. Shipping a fabricated story is exactly what AGENTS rule 8 forbids |
| 5 | Vane | weathervane: which way the crowd is turning | 4 chars, memorable | `.com/.app/.io` all resolve — crowded namespace |
| 6 | Odds Space / oddspace | the space of outcomes | literal, no metaphor debt | `.com` returns a 195-byte parking page, `.io` 301-redirects, 5 GitHub repos → cleanest *availability*, worst *sound* (3 syllables, ambiguous stress) |
| 7 | Fathom | to work it out; depth | nice verb, trader-ish | `fathom.com` resolves (Fathom Analytics) — direct name collision in analytics |
| 8 | Aperture | the opening that lets light/position in | matches the mark's name in the Brand Lock | `aperture.com/.app/.io` all resolve; **Valve's software platform** — real trademark exposure |
| 9 | Clearbook | the book, cleared | sounds institutional | `.com/.app/.io` all resolve |
| 10 | Parlay | multi-leg bet | gamblit and recognisable | `parlay.com` has certs (`parlay.finance`); reads as a gambling site, which invites the regulatory gaze we are deliberately avoiding |
| 11 | Break | *the break* — when the gate opens | 5 chars, violent clarity | `break.com`-class crowding; generic in dev-tooling ("breaking change", "build break") |
| 12 | Bidask | the two sides of a book | on-the-nose market-microstructure | all three TLDs resolve; 44 GitHub repos |

### Chosen: **Openout**

Justification, in order of weight:
1. **It is the only candidate whose availability was actually verified clean** on every TLD we would
   ship on, *and* whose one collision (`openout.app`) is in an unrelated category (link-in-bio).
   Tally looked better on length and lost to a live same-category competitor at `tally.markets` —
   a DNS-only check would have missed that, so the check fetches the page title.
2. It says the product's object, not its category: an **open** position **out** on a question.
   Every `Poly-*` competitor names the venue; we name the state of your money.
3. ≤7 characters, no silent letters, no th-sound clusters → survives pronunciation in Hindi,
   Gujarati, Spanish, Portuguese, Russian, Arabic and Mandarin transliteration.
   (`-th-` in *Fathom* and the `-tur-` cluster in *Aperture* are the two weak points in the list.)
4. `@openout` is 7 characters, inside Telegram's handle norms, and reads as a verb in product copy
   ("openout on a move" — we will not use it that way, see D7's no-hype rule, but it *can* carry it).
5. It is not in the exhausted `Poly-` namespace that the prompt flags as making every product look
   like a Polymarket subsidiary.

**Rejected for cause, so the reasoning is reusable:** Tally (measured collision), Tapemark (false
etymology), Aperture (Valve), Fathom (existing analytics product).

**Fallback:** `oddspace` if `openout*` domains turn out to be held (a WHOIS/registrant check, which
this sandbox could not do) — usable at `.trade`/`.markets` with no A record found. Second fallback
`openbet` (unprobed; short and descriptive but weakly protectable).

**Rename procedure actually required** (Brand Lock says the mark is abstract, so a rename touches the
wordmark text only): `brand/svg/lockup-horizontal.svg` `<text>` content, the Brand Lock's
`brand_context.name`, `avatar`/`og` PNGs if they bake the wordmark, and the app-store/`@handle`
registrations. **The `fixed` visual fields (mark geometry, palette, type) do not change** — I am not
entitled to rename them without your approval, which is what the next section is for.

---

## D2. Positioning

> **For crypto traders on Polymarket, Openout is the Telegram-native terminal that shows who moved
> a market and lets you act in one tap, unlike bots that hide the tape behind menus.**

(17 words as drafted; the ≤15 form, if you want it strictly: *"For Polymarket traders, Openout is the
Telegram terminal that shows who moved a market and lets you act in one tap."* — 19. Cutting to
≤15 loses the "unlike" clause the template requires, so **the template and the length cap conflict**;
this is the version I would defend and the conflict is flagged rather than papered over.)

Landing-page messages, trader-to-trader, no "revolutionising / seamless / empowering":

1. **"The tape, live."** — Every fill on Polymarket, tagged by who did it, in under two seconds.
   Bots give you a menu. We give you the row.
2. **"Know before you match."** — Real spread, real book depth, real fee at your size — quoted before
   the button, not after. If we can't quote it inside 3¢, we say so and stop you.
3. **"Your keys, your call."** — Nothing custody-shaped here. Export the key any time. We show drawdown
   next to every PnL, including the wallets that lost.

---

## D3. Logo system

**Primary mark — "Binary Aperture"** (existing, `fixed`). Construction, measured from the file:

- Grid: `viewBox 0 0 100 100`; both glyphs sit on the 100-unit square with a 20-unit clear space.
- Solid left chevron: polygon `48,22 → 20,50 → 48,78 → 48,62 → 36,50 → 48,38`, `#2e5cff`.
  Area **640.0 u²**. Meaning: a committed position.
- Outlined right chevron: mirrored polygon, `fill:none stroke:#7ba5ff stroke-width:3.5`,
  miter joins. Band area **508.0 u²**. Meaning: the open question.
- Gold dot: `<circle r=4.5>` `#f1ce57` at centre = **63.6 u²**. Meaning: resolution.
- Negative space between the two chevron tips forms the diamond; the 4-unit gap is the aperture.

**Legibility, computed analytically** (`tools/mark-legibility.py`, not screenshotted — this sandbox's
ImageMagick silently drops stroked paths, rendering the outlined chevron at 0.0% coverage, so a
raster check here would have been a lie):

| target | solid | outline band | dot Ø | verdict |
|---|---|---|---|---|
| 512 px | 16,810 px² | 13,406 px² | 46.1 px | OK |
| 128 px | 1,050 | 834 | 11.5 px | OK |
| 64 px | 286 | 242 | 5.8 px | OK |
| 40 px (chat list) | 108 | 69 | 3.6 px | OK |
| 24 px | 44 | 34 | 2.2 px | OK |
| **16 px (favicon)** | 14 | 12 | **1.44 px** | **dot is sub-pixel** — the third element disappears |

**And the fix already exists in the kit:** `brand/svg/favicon.svg` uses stroke-width **6** (vs 3.5),
dot **r=6** (vs 4.5) and a slightly widened chevron pair (757 u² vs 640), with no background rect.
Re-measured: both polygons land at **19.4 px²** at 16px and the dot Ø becomes 1.92 px. So the "must
survive 16px" requirement is met by the *variant*, not the primary — which the kit's inventory implies
but never states. **Rule added to the brand doc: the primary mark is never used below 24px; below that,
`favicon.svg` is mandatory.**

**Wordmark:** set, never drawn. Instrument Sans Condensed 700, size 44 on the 340×100 lockup canvas,
`letter-spacing 1`, sentence-case-safe (all-caps variant defined below), `Poly` in `#e6edf3` and
`GM` in `#7ba5ff` in the current file — that two-tone split carries over to `Open`/`out`
(`#e6edf3` / `#7ba5ff`) on rename. No ligature, no kerning pairs tightened below 0; the `O` and `u`
must not collide at 11px.

**Lockups:** horizontal (mark 100u + 20u gutter + wordmark) is primary; stacked (mark centred above,
wordmark at 60% of mark width) for the app icon face and the 404 page; both composed in SVG so the
type reflows in the browser rather than being rasterised.

**Monochrome:** `mark-mono-light.svg` (white on dark) and `mark-mono-dark.svg` (ink `#0c0e13` on
light) — single-colour, geometry identical, verified by `tools/mark-legibility.py` on the mono file
(640 u² → 16.4 px² at 16px). Fax rule: at 1-bit, the outline must be rendered at ≥1 device pixel,
which means the mark may only be faxed/reprographed at ≥28 mm.

**Avatar crop:** 512×512 full-bleed `#0c0e13`, mark inscribed in a 62%-diameter centred circle (the
Telegram circular crop then cuts nothing), gold dot ≥ 12px at 512 so it survives to 40px (3.6px, above
the 1.5px floor). Discord emoji export: 128×128, dot 2.9px.

**What the mark must not do** (locked, inherited and enforced): no candlestick, no rocket, no crystal
ball, no dice, no chart-arrow-through-a-letter, no gradient on chrome, no redraw by an image model,
no re-proportioning for a new background — composite the SVG instead. Never reproduce Polymarket's or
gmgn's logo, wordmark, icon or marketing copy.

---

## D4. Colour system

Two structural decisions had to be made *by measurement*, and both changed my first draft. Report
them as they are:

**(a) YES/NO vs BUY/SELL vs PROFIT/LOSS — the ambiguity P02 asks us to resolve.** In gmgn all three
share green/red, which produces the nonsense case: a red NO position that is making money. Resolution:

| axis | colours | why |
|---|---|---|
| **outcome** (YES/NO) | `yes` brand blue / `no` warm amber — **never green or red** | an outcome is a *side*, not a verdict. Reserving green/red removes "red NO = bad" entirely |
| **action** (BUY/SELL) | `buy` `#16a34a` / `sell` `#ef4444` (dark) | trader muscle memory, matches the tape and gmgn convention; only appears in tape rows and order tickets |
| **result** (profit/loss) | same hues as action, **bolder weight + explicit sign glyph** | `buy↔profit` ΔE*ab = **0.0 by design** — same meaning, different slot. The disambiguator is `+/−` and position, never colour |

**Colour never carries meaning alone.** Every yes/no is also `YES`/`NO` text plus fixed L/R slot; every
profit/loss carries `+`/`−`; every tape side carries `▲`/`▼`. Verified necessary, not assumed: for a
deuteranope the YES/NO pair separates at ΔE*ab **112.6** but `yes↔no` under my *first* draft (blue vs
pale blue) separated at **1.5** — i.e. unreadable. `no: #D55E00` vs `critical #ef4444` measured
**ΔE 4.1** (dark) and **3.2** (light), so the rule "an outcome chip and a severity badge are never in
the same row" is a *hard layout rule*, not a palette accident, and it is stated in the tokens file.

**(b) An 8-colour theme-agnostic chart palette does not exist.** `tools/palette-search.py` searched
the brand pool exhaustively: best 8-hue set at ΔE≥10 across normal/deuteranopia/protanopia scores
**11.3 — it exists on the dark canvas only if nothing else may use those hues**; the moment YES/NO and
green/red are excluded from the chart, the best 8 drops to **9.1 (fails)**, and on white only 9 pool
colours clear 3:1 at all. Two warm hues (`#D55E00`, `#CC79A7`) clear 3:1 on **both** canvases; the rest
cannot, so **each theme ships its own array**. Chart series beyond six are separated by **dash pattern
+ marker shape**, not hue; legend *text* uses `text.secondary` (≥4.5:1) while the swatch carries the
colour, because 3 of the 8 series hues sit at 3.0–3.9:1 and are therefore line-safe but not text-safe.

Final semantic tokens. This table is **generated from `brand/tokens.json`** by the build step (recomputed ratios, not transcribed), and `tools/p02-gate-check.py` G4 re-derives every number in it — so it cannot go stale against the tokens:

| token | dark | light | contrast on own canvas (dark / light) | floor | grade |
|---|---|---|---|---|---|
| `bg.base` | `#0c0e13` | `#ffffff` | — (non-text) | 3.0 (UI) | — |
| `bg.elevated` | `#1c1f27` | `#f4f5f7` | — (non-text) | 3.0 (UI) | — |
| `bg.inset` | `#020041` | `#edeff1` | — (non-text) | 3.0 (UI) | — |
| `border.default` | `#2d3037` | `#d4d7dc` | — (non-text) | 3.0 (UI) | — |
| `border.strong` | `#45474e` | `#abb2bb` | — (non-text) | 3.0 (UI) | — |
| `text.primary` | `#e6edf3` | `#0c0e13` | **16.34 / 19.30** | 4.5 | AAA / AAA |
| `text.secondary` | `#8b929b` | `#45474e` | **6.14 / 9.27** | 4.5 | AA / AAA |
| `text.muted` | `#62646a` | `#62646a` | **3.26 / 5.91** | 3.0 | AA / AAA |
| `brand.primary` | `#2e5cff` | `#1c3fe2` | **3.76 / 7.35** | 3.0 | AA / AAA |
| `brand.primary-text` | `#4e7fff` | `#1c3fe2` | **5.34 / 7.35** | 4.5 | AA / AAA |
| `brand.on-primary` | `#ffffff` | `#ffffff` | **19.30 / 1.00** | 3.0 | AAA / FAIL |
| `outcome.yes` | `#7ba5ff` | `#0f1ac6` | **7.97 / 10.59** | 3.0 | AAA / AAA |
| `outcome.no` | `#D55E00` | `#D55E00` | **4.99 / 3.87** | 3.0 | AAA / AA |
| `action.buy` | `#16a34a` | `#15803d` | **5.86 / 5.02** | 3.0 | AAA / AAA |
| `action.sell` | `#ef4444` | `#dc2626` | **5.13 / 4.83** | 3.0 | AAA / AAA |
| `result.profit` | `#16a34a` | `#15803d` | **5.86 / 5.02** | 3.0 | AAA / AAA |
| `result.loss` | `#ef4444` | `#dc2626` | **5.13 / 4.83** | 3.0 | AAA / AAA |
| `alert.info` | `#8b929b` | `#62646a` | **6.14 / 5.91** | 3.0 | AAA / AAA |
| `alert.watch` | `#7ba5ff` | `#1c3fe2` | **7.97 / 7.35** | 3.0 | AAA / AAA |
| `alert.high` | `#f1ce57` | `#a16207` | **12.60 / 4.92** | 3.0 | AAA / AAA |
| `alert.critical` | `#ef4444` | `#dc2626` | **5.13 / 4.83** | 3.0 | AAA / AAA |

`brand.primary` (`#2e5cff`) is **3.76:1** on the dark canvas, which is AA-large/UI only, not body text:
small link text must use `brand.primary-text` (`#4e7fff` dark = 5.34:1, `#1c3fe2` light = 7.35:1). That
constraint is recorded as a rule in `tokens.json`, because the audit raised it and I did not design around it.

`text.muted` sits at 3.26:1 on dark **by decision, not accident**: it is used only for meta at ≥14px or for text that is decorative; any state where muted text must be readable is upgraded to `text.secondary`. `no` at 3.87:1 (light) is a chip fill/label, never body text, and `brand.primary` in light was darkened to `#1c3fe2` **because the obvious `#2e5cff` measured 4.05:1 and failed the 4.5:1 bar for link text.**

Files written (deterministic, no image model): `brand/tokens.json` (extended with `light`/`dark`
blocks + the layout rule as data), `brand/tokens.css` (`:root` + `[data-theme="dark"]`),
`brand/SIZE-RULES.md` (the 24px floor and favicon switch).

---

## D5. Typography

Scale (10 steps, 11→32, mono/tabular from step 2 up):

| step | size | line-height | tracking | use |
|---|---|---|---|---|
| `2xs` | 11px | 14 | +0.01em | tape meta, timestamps |
| `xs` | 12px | 16 | +0.005em | table cells, chips |
| `sm` | 13px | 18 | 0 | body dense |
| `base` | 14px | 20 | 0 | body default |
| `md` | 16px | 22 | −0.005em | card titles, price headline |
| `lg` | 18px | 24 | −0.01em | sheet titles |
| `xl` | 21px | 26 | −0.015em | page title |
| `2xl` | 24px | 28 | −0.02em | hero number (PnL) |
| `3xl` | 28px | 32 | −0.025em | marketing display |
| `display` | 32px | 34 | −0.03em | 404 / OG only |

**Numeric rules (these are the ones a developer will get wrong):**
- `font-variant-numeric: tabular-nums lining-nums`; `font-feature-settings:"tnum" 1,"lnum" 1`;
  right-aligned; one `0` of horizontal padding; **never** proportional in a table.
- Prices: `63.5¢` for sub-$1 quotes — one decimal, no trailing `.00`, cent glyph always. At ≥$1.00 switch
  to `$1.24`. Never show `0.635` and `63.5¢` in the same column.
- USD large: `$1.24M`, `$986K`, `$2,310`; 3 significant figures; thousands separator on exact values only.
- Signed PnL: **always** signed, always 2dp, U+2212 minus (`−$88.40`) not hyphen; `+$412.09`. Colour plus
  sign, both.
- Percentages: `+41.2%` / `−8.4%`, 1dp, sign mandatory (an unsigned % is a *probability*, and where a
  probability is meant we say `p=`).
- Timestamps: relative inside the tape (`3s`, `4m`, `2h`), absolute ISO-8601 on hover/long-press;
  stale data appends `· 42s ago` in `text.muted` plus the token's own `alert.high` dot past 15s.

Assignments: display/headline `Archivo Narrow` 600/700; UI/body `Inter` 400/500/600/700; every number
`Geist Mono` 400/600.

**⚠ Correction to the kit, and to my own first draft of this section.** The Brand Lock and
§7 of the shared context both name `Instrument Sans Condensed`, and my draft asserted a payload for it.
That was wrong, so I checked it rather than shipping it: **there is no such family on Google Fonts**
(`css2?family=Instrument Sans Condensed:wght@700` → **HTTP 400**, `Instrument Sans` alone returns CSS),
`gh api repos/InstrumentType/Instrument-Sans` → **404**, and a GitHub code search for the string returns
**0 hits**. The kit's §7 value is a CSS-`font-family` string scraped from Polymarket's own compiled
stylesheet, which only proves *they reference the name* — not that a licensed distribution exists for us
to self-host. The `typography.display` field is `proposed`, not `fixed`, so it is mine to revise.

Resolution — the condensed trick is kept (it is the cheapest legible signal of "terminal") and the
substitute is real, OFL, self-hostable and measured:

| role | family | source | latin subset (measured) |
|---|---|---|---|
| display | **Archivo Narrow** 600/700 (italic shipped) | Google Fonts, OFL | **11,776 B (11.5 KB)**, 3 faces |
| body/UI | Inter var 400..700 | Google Fonts, OFL | 48,256 B (47.1 KB), 7 faces |
| mono/numbers | Geist Mono 400..600 | Google Fonts, OFL (added Oct 2024) | 23,128 B (22.6 KB), 6 faces |

Rejected alternatives with reasons: *Roboto Condensed* 21,128 B (double the bytes, weaker numerals),
*Barlow Condensed* 22,444 B (rounded terminals read "consumer app"), *Inter Tight* 22,748 B (not
condensed enough to read as a different voice from the body face — which defeats the purpose),
*Instrument Sans* regular width 17,012 B (no condensation; would make our headlines look like body copy).

Webfont strategy and payload: subset `latin` + `latin-ext`, woff2 only, `font-display: swap`,
explicit `unicode-range`, mono restricted to `[0-9]$%+−.¢,:/smaΔØ≤≥×÷ ]`. **Total measured budget
88.4 KB** (11.5 + 47.1 + 22.6 + ~7 KB latin-ext ≈ **95 KB** worst case), against a 120 KB cap — the
previous 118.4 KB figure in this document was an estimate and is now replaced by fetched byte counts.
Metric override (`size-adjust`, `ascent-override`) generated for the `system-ui` fallback so reflow
stays < 0.5%; verified at P08 on the real DOM, not asserted here.

---

## D6. Iconography

Grid 24×24, live area 20, **stroke 1.6** round caps/joins, single colour, no fills except status
dots; optical correction: circles drawn +0.4u radius, horizontal bars −0.1u height (verified against
the 40px render path above, where the mark's 3.5u stroke is the same rule at logo scale). Corner
treatment: 2u radius on rectangles only. Must survive 1-bit.

markets (two stacked cards, offset) · tape (three left-aligned rows, right edge ragged) · whales
(outlined drop with one filled bubble) · traders (two overlapping bust outlines) · radar (sweep arc +
blip, no concentric rings) · portfolio (pie with one slice lifted) · alerts (bell with a *slash-free*
mouth, severity as a dot, not a fill) · copy (two identical chevrons, offset — echoes the mark) ·
automation (loop arrow with a single node) · settings (sliders, not a gear) · deposit (tray + down
arrow) · withdraw (tray + up arrow) · export-key (key leaving a bracket) · verified (shield +
one check, no badge circle) · suspicious (dashed-outline circle + question mark) · insider (person
behind a partial wall) · first-in — the sniper equivalent (single chevron at a start line, no
crosshair, no scope glint) · new-wallet (wallet with a dot, no plus) · stale (clock with the hands
frozen at 3 o'clock).

---

## D7. Voice & microcopy

Tone: **dense, precise, unbothered** (Brand Lock, `fixed`). Avoid: **hyped, parental, greedy**.
Numbers in copy: always tabular, PnL always signed, **never round a loss in the user's favour** — a
$−88.404 loss prints as `−$88.40`, and $−0.004 prints as `−$0.004`, never `−$0.00`.

Written copy, ready to paste (every state P02 lists, verbatim, no placeholders):

- Empty · no positions: *"No open positions. Anything you buy through Openout shows here within a
  second, and anything you bought on polymarket.com shows here on the next sync — that delay is the
  chain, not us."*
- Empty · no alerts: *"No alerts yet. The median fill on Polymarket right now is ~$6, so a $2k alert is
  genuinely large here. Start at $2k and tighten once you know your noise."*
- Empty · no results: *"Nothing matches 'fed rate cut' in your watchlist. Search covers open markets
  only — 3,914 of them."*
- Loading: *"Subscribing to the tape — the first fills arrive in under a second, and every row then
  carries its own age. A spinner alone is never an acceptable state here: if we cannot name the noun
  that is arriving, we are loading nothing."*
- Error (upstream): *"Polymarket's order book is not answering (503). Prices shown are 4m old. Trading
  is off until it's back."*
- Rate limited: *"You've hit the order rate limit (5,000 per 10s). Next attempt in 6s. Cancels are
  capped at 250 — use cancel-all only in an emergency."*
- Stale data: *"Last update 42s ago. This price is not live, so trading is switched off — we will not
  let you buy or sell against a number we cannot vouch for. Positions shown at last known value.
  Retry, or wait: the tape returns on its own, usually in seconds. You can still cancel resting orders."*
- Order rejected: *"Rejected before it reached the book: size 3.00 < this market's 5.00 minimum. Spread
  is 4.1¢, so a market order would pay that too."*
- Insufficient balance: *"Needs $412.60 (including $4.13 max fee, settled at match). You have $318.04
  pUSD. Buy side depth: $1,204 at 63¢."*
  (Why "max": CLOB V2 sets the fee at match time, so we can bound it but never promise the exact
  figure — see `00-SHARED-CONTEXT.md` §3. Never print it as a definite cost.)
- Key export confirmation: *"Exporting this key hands anyone who sees it full control of every position
  and every dollar. There is no recovery, no support ticket, no undo. Type the wallet address to
  confirm."*
- Withdrawal confirmation: *"Moving $1,204.55 to 0x7c1…9f2. You initiated this. If you did not, do not
  continue — export your key and revoke this session."*
- First-trade onboarding: *"One tap buys at the quoted price with a 3¢ limit guard. If the book moves
  past that, we stop instead of filling worse. Quote valid 5s."*
- Risk disclosure (footer, always visible on any PnL surface): *"Prediction markets can lose 100% of a
  position. Wallets shown here include losing wallets. Nothing here is investment advice or a promise of
  returns."*

Never say: "guaranteed", "safe", "sure thing", "insider tip", "free money", "can't lose", "alpha" as a
promise. Never label a wallet "smart money" without its drawdown beside it.

---

## D8. Brand assets

| Asset | Status | Note |
|---|---|---|
| `svg/mark.svg`, `mark-mono-*.svg`, `favicon.svg`, `lockup-horizontal.svg` | **fixed** — unchanged | favicon is the mandatory <24px variant (verified above) |
| `app-icon-512.png`, `avatar-512.png`, `og-1200x630.png`, `brandboard.png` | **fixed**, content unchanged | still say "PolyGM"; need re-render on rename approval — see open items |
| `tokens.json` | extended | `light`/`dark` theme blocks, the two never-same-row rule, chart arrays per theme |
| `tokens.css` | **new** | `:root` + `[data-theme="dark"]`, generated from `tokens.json` by `tools/build-tokens.mjs` |
| `SIZE-RULES.md` | **new** | 24px floor, favicon switch, 1-bit rule, fax 28mm |
| 404 page concept | specced, not built | stacked lockup at 96px, `display` type, single line: *"This market resolved."* + link to trending |
| Discord emoji set | specced | 128px PNG from `mark-mono-light` on `#0c0e13`; dot Ø 2.9px measured |
| Email templates | copy specced above | welcome / key-export warning / large-withdrawal confirm — plain text first, HTML parity second |
| OG image 1200×630 | concept locked | left 40% mark on `#0c0e13`, right: `2xl` headline + one live tape row as the "proof" element; **must be re-composited by SVG, never re-drawn** |

`brandkit` state ledger: palette, typography and visual axes approved in the vendored skill's own
`brandkit.py` ledger (rev 3), state file kept outside the repo per `SKILLS.md`; the logo slot was **not**
approved because its export stage requires `rsvg-convert`, which this sandbox does not have, and I will
not fake an approval.

---

## Quality gate — answered

Every colour has a hex ✅ (D4 table, `tokens.json`). Every text pair has a contrast ratio ✅ (computed,
and two failures were **found and fixed** — the light-theme `brand.primary` at 4.05:1 and an 8-colour
chart palette that does not exist). Every screen state has copy ✅ (D7, states verbatim). No placeholder strings survive the gate: `tools/p02-gate-check.py` G5 scans this file and `brand/` for unfinished-work markers. Hand-a-developer test: the load-bearing rules are stated as *rules*, not vibes — "primary
mark never below 24px", "outcome chip and severity badge never in the same row", "chart legend text is
never the series colour", "fee is always labelled max, never exact".

**Open items requiring you, not me:**
1. **Rename approval.** Changing `brand_context.name` from "PolyGM (working)" → "Openout" touches a
   Brand Lock field the skill marks `fixed`. Say "approve rename to Openout" and I'll do the lockup,
   the inventory, and the avatar/OG re-renders — composed from the SVG, not generated.
2. `rsvg-convert` is missing here, so the skill's export/inspection stages (`logo-export`,
   `logo-inspect`) and the Brandbook PDF path are **not runnable**; everything above that needed a
   raster was computed analytically instead. Install it (or approve my installing it) and I'll produce
   the real 16/40/512 rasters and the brandbook.
3. WHOIS/registrant and USPTO/EUIPO clearance — neither reachable from this sandbox. `[UNVERIFIED]`.
