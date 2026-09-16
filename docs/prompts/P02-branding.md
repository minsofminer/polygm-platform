# P2 — Brand & Identity

> Paste `00-SHARED-CONTEXT.md` first, then this.

## Role
You are a brand designer who has built identities for trading products (not lifestyle brands). You design for density, legibility at 11px, and instant recognition in a Telegram chat list. You dislike decoration.

## Objective
Produce a complete, implementable brand identity for the product, **inspired by Polymarket's visual language but legally original**.

## Hard constraints — read before designing

1. **Do not reproduce Polymarket's logo, wordmark, icon, or marketing copy.** We are taking inspiration from their *palette and typographic register*, not their identity. If a design could be mistaken for an official Polymarket product, it is wrong — and it will get us a C&D the week we get traction.
2. **Do not reproduce gmgn's logo, mascot, icons, or copy.** We are rebuilding their information architecture, not their art.
3. The name must not contain "Polymarket", "Poly" as a standalone prefix suggesting affiliation, or "gmgn". ⚠️ Note that most competitors already use "Poly-" prefixes (PolyCop, Polyfox, PolyTrack, PolyMonit, Polywhaler, PolySharks, Polygun, PolyCopy, Polycopybot, Polylerts). That namespace is exhausted and it makes every product look like a Polymarket subsidiary. **Prefer a name outside it.**
4. Must survive: 16px favicon, Telegram bot avatar at 512px and at 40px in a chat list, a dark terminal, a light marketing page, and a monochrome fax.

---

## Deliverables

### D1. Naming — 12 candidates, then one
For each: the name, what it means/evokes, a one-line rationale, and the risk (trademark collision likelihood, pronunciation ambiguity, domain availability assumption).

Constraints: ≤9 characters preferred, pronounceable by a non-native English speaker (our audience is global — heavy India, SEA, LatAm, CIS), works as a Telegram `@handle`, no unfortunate meaning in Hindi/Gujarati/Spanish/Portuguese/Russian/Arabic/Chinese.

Then pick one and justify. Include a fallback if the domain is taken.

### D2. Positioning statement
One sentence, ≤15 words, of the form: *For [who], [product] is the [category] that [unique benefit], unlike [alternative] which [weakness].*

Then: the three messages for the landing page, in the voice of a trader talking to a trader. No "revolutionising", no "seamless", no "empowering".

### D3. Logo system
Describe precisely enough to brief an illustrator or generate:
- **Primary mark** — concept, geometry, construction grid, minimum size
- **Wordmark** — typeface treatment, letter-spacing, capitalisation
- **Stacked and horizontal lockups**
- **Monochrome and single-colour variants**
- **Avatar crop** (the square Telegram/Discord version) — must read at 40px
- What the mark must NOT do (no candlestick clichés, no rocket, no crystal ball, no dice, no chart-arrow-through-a-letter)

Provide 3 distinct concept directions with rationale, then recommend one.

### D4. Colour system
Build on the extracted Polymarket tokens (§7 of shared context) but make it ours:

- **Semantic tokens** with exact hex for **dark and light**: `bg.base`, `bg.elevated`, `bg.inset`, `border.default`, `border.strong`, `text.primary`, `text.secondary`, `text.muted`, `text.inverse`
- **Brand:** primary, primary-hover, primary-active, primary-subtle, on-primary
- **Trading semantics** — this is the important one. Prediction markets are YES/NO, not long/short:
  - `yes` / `no` (outcome colours)
  - `buy` / `sell` (action colours)
  - `profit` / `loss` (PnL colours)
  - Decide explicitly: **do YES/NO and BUY/SELL use the same green/red?** In gmgn they do. In prediction markets that creates real ambiguity — a red "NO" position that is profitable. Resolve this and explain the resolution.
- **Chart palette** — 8 distinct categorical colours that survive colour-blind deuteranopia and protanopia simulation, readable on both themes, and distinct from the yes/no semantics
- **Alert severity:** info / watch / high / critical
- Contrast ratios for every text-on-background pair, to WCAG AA minimum (4.5:1 body, 3:1 large). Show the numbers.

Output as CSS custom properties in both `:root` (light) and `[data-theme="dark"]`, plus a JSON token file for the design system.

### D5. Typography
- Type scale: 10 named steps from 11px to 32px with line-height and letter-spacing for each
- **Numeric display rules** — prices, PnL, volume, percentages. Tabular figures mandatory. Define the exact treatment for: prices in cents (`63.5¢`), USD (`$1.24M`), signed PnL (`+$412.09` / `−$88.40`), percentages, and timestamps
- Body, UI, headline, and mono assignments
- Webfont strategy: subsetting, `font-display`, fallback stack with metric overrides, total payload budget in KB
- The condensed-headline trick Polymarket uses (Instrument Sans Condensed) — decide whether we adopt an equivalent and name a licensed/self-hostable substitute

### D6. Iconography & icon set spec
Specify the icon set we need, one line each, with the visual metaphor: markets, tape, whales, traders, radar, portfolio, alerts, copy, automation, settings, deposit, withdraw, export-key, verified, suspicious, insider, sniper-equivalent, new-wallet.

Style rules: grid, stroke weight, corner treatment, optical corrections. Must be drawable in a single colour.

### D7. Voice & microcopy
- The tone in three adjectives, and three adjectives we are avoiding
- **Write the actual copy** for: empty states (no positions, no alerts, no results), loading, error, rate-limited, stale data, order rejected, insufficient balance, key export confirmation, withdrawal confirmation, first-trade onboarding, risk disclosure
- Rules for numbers in copy (always tabular, always signed for PnL, never round a loss in the user's favour)
- What we never say: "guaranteed", "safe", "sure thing", "insider tip", "free money"

### D8. Brand assets checklist
Favicon set, OG image (1200×630) concept, Telegram bot avatar, Discord emoji set, 404 page concept, email templates (welcome, key-export warning, large-withdrawal confirmation).

---

## Quality gate
Hand this to a developer with no design taste and they should produce something coherent. Every colour has a hex. Every text pair has a contrast ratio. Every screen state has copy. Nothing says "TBD".
