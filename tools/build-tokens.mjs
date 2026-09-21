#!/usr/bin/env node
/**
 * Generates brand/tokens.css from brand/tokens.json.
 *
 * Why a generator rather than a hand-written stylesheet: brand/BRAND-KIT.md's palette and the audit
 * output are the source of truth, and a hand-copied CSS drifts from them silently — usually in the
 * one hex nobody re-checks. Run after editing tokens.json:
 *
 *   node tools/build-tokens.mjs            # writes brand/tokens.css
 *   node tools/build-tokens.mjs --check    # exit 1 if brand/tokens.css is stale
 *
 * P03 D7 rule this file enforces: foundations (spacing, borders, elevation, density, motion,
 * layers, breakpoints) are EMITTED from tokens.json and are never typed as literals here. Through
 * P02 this file carried `--pgm-spacing-1: 4px` and `--pgm-radius-card-dense: 8px` as hardcoded
 * values; assertNoLiterals() now refuses to write if any creep back in.
 */
import fs from 'node:fs';

const ROOT = new URL('..', import.meta.url).pathname;
const tokensPath = `${ROOT}brand/tokens.json`;
const cssPath = `${ROOT}brand/tokens.css`;
const t = JSON.parse(fs.readFileSync(tokensPath, 'utf8'));

/**
 * Brand-name cross-check. The name now lives in three places (tokens.json, the Brand Lock in
 * BRAND-KIT.md, the rendered lockup wordmark), and a rename that updates two of the three is
 * exactly the kind of drift a generator should refuse to build on. Checked here because this file
 * is the only thing every theme build passes through.
 */
function assertBrandName() {
  const declared = t.brand?.name;
  if (typeof declared !== 'string' || !declared) throw new Error('brand/tokens.json: `brand.name` must be a non-empty string');
  const kit = fs.readFileSync(`${ROOT}brand/BRAND-KIT.md`, 'utf8');
  const lockName = /"brand_context":\s*\{\s*"name":\s*"([^"]+)"/.exec(kit)?.[1];
  const lockupPath = `${ROOT}brand/svg/lockup-horizontal.svg`;
  let wordmark = null;
  if (fs.existsSync(lockupPath)) {
    const svg = fs.readFileSync(lockupPath, 'utf8');
    const m = /<text\b[^>]*>([\s\S]*?)<\/text>/.exec(svg);
    wordmark = m ? m[1].replace(/<\/?tspan\b[^>]*>/g, '').replace(/\s+/g, '').trim() : null;
  }
  const problems = [];
  if (lockName && lockName !== declared) problems.push(`BRAND-KIT.md brand_context.name is ${JSON.stringify(lockName)}`);
  if (wordmark && wordmark !== declared) problems.push(`the lockup renders ${JSON.stringify(wordmark)}`);
  if (problems.length) {
    throw new Error(
      `REFUSING to generate: brand name is inconsistent. tokens.json says ${JSON.stringify(declared)}; ` +
        `${problems.join('; ')}. Reconcile them (for an approved rename run tools/rename-wordmark.mjs), ` +
        `then re-run. Do not paper over this by editing one file.`
    );
  }
  return declared;
}

const brandName = assertBrandName();
const kebab = (k) => k.replace(/\./g, '-');
const px = (n) => `${n}px`;
const block = (sel, obj) => `${sel}\n{\n${Object.entries(obj).map(([k, v]) => `  --pgm-${kebab(k)}: ${v};`).join('\n')}\n}`;

const required = (name, cond, hint) => {
  if (!cond) throw new Error(`REFUSING to generate: brand/tokens.json has no usable \`${name}\`. ${hint}`);
};

/* --- spacing: the 4px scale, plus the explicitly tolerated off-grid optical values ------- */
required('spacing', Array.isArray(t.spacing?.scale) && t.spacing.scale.length >= 8, 'run tools/build-foundations.py');
const spacingLines = t.spacing.scale.map((s) => `  --pgm-space-${s.name}: ${px(s.px)}; /* step ${s.name} */`);
spacingLines.push(`  --pgm-space-base: ${px(t.spacing.base)};`);
for (const o of t.spacing.allowed_offgrid || []) {
  spacingLines.push(`  --pgm-space-off-${o.px}: ${px(o.px)}; /* sanctioned: ${o.use} */`);
}
const spacingCss = spacingLines.join('\n');

/* --- radius: deltas against one base, so the whole UI re-scales from a single number ----- */
required('radius', t.radius?.base && t.radius?.chip, 'P03 D1 adds base + chip; the rest are signed deltas');
const DELTA_STEPS = ['xs', 'sm', 'md', 'xl'];
const radiusLines = [`  --pgm-radius-base: ${t.radius.base};`, `  --pgm-radius-card: ${t.radius.base}; /* = base */`, `  --pgm-radius-chip: ${t.radius.chip}; /* pill, by definition */`];
for (const k of DELTA_STEPS) {
  const d = t.radius[k];
  required(`radius.${k}`, typeof d === 'string' && /^[+-]\d+(\.\d+)?px$/.test(d), `must be a signed px delta like "-4px", got ${JSON.stringify(d)}`);
  radiusLines.push(`  --pgm-radius-${k}: calc(var(--pgm-radius-base) ${d}); /* ${d} off base */`);
}
for (const k of ['2xl', '3xl']) if (t.radius[k]) radiusLines.push(`  --pgm-radius-${k}: ${t.radius[k]};`);
const radiusCss = radiusLines.join('\n');

/* --- borders / elevation ------------------------------------------------------------------ */
required('border.widths', Array.isArray(t.border?.widths), 'run tools/build-foundations.py');
const borderCss = t.border.widths.map((b) => `  --pgm-border-w-${b.name}: ${px(b.px)}; /* ${b.use} */`).join('\n');
required('elevation.levels', Array.isArray(t.elevation?.levels), 'run tools/build-foundations.py');
const elevCss = t.elevation.levels.map((l) => `  --pgm-shadow-${l.name}: ${l.shadow}; /* ${l.use} */`).join('\n');

/* --- density: three modes, emitted so [data-density] can switch them wholesale ------------ */
required('density', t.density?.row_heights_px && t.density?.padding_px, 'run tools/build-foundations.py');
const MODES = t.density.setting.levels;
const densityLines = [];
for (const [row, by] of Object.entries(t.density.row_heights_px)) {
  for (const m of MODES) densityLines.push(`  --pgm-row-${row.replace(/_/g, '-')}-${m}: ${px(by[m])};`);
}
for (const [k, by] of Object.entries(t.density.padding_px)) {
  for (const m of MODES) densityLines.push(`  --pgm-pad-${k.replace(/_/g, '-')}-${m}: ${px(by[m])};`);
}
for (const m of MODES) densityLines.push(`  --pgm-font-${m}: ${px(t.density.font_size_px[m])};`);
densityLines.push(`  --pgm-min-touch-target: ${px(t.density.constants.min_touch_target)}; /* never scaled down by density */`);
if (t.density.constants["shell-max-width"]) densityLines.push(`  --pgm-shell-max-width: ${px(t.density.constants["shell-max-width"])};`);
// P09. The rail widths are emitted from the same constants block as the shell ceiling, and that is the point:
// they were used by web/src/globals.css against an `auto` fallback before they existed here, so the layout
// looked right for as long as nobody asked which two widths it was actually laying out against. A token that
// only exists in a `var(..., fallback)` is a token the design system does not have.
// The ladder's ceiling. 560px is ~12 rows at the 44px touch target, which is the book the "2xl" breakpoint in
// this same file already promises ("12 book levels"); rows never shrink with density, so one value is honest
// where the rail widths needed none. A bounded book is what makes src/lib/anchor.ts load-bearing: a re-ladder
// inside a scroll container has to put the reader back, and the page-level anchoring cannot do it for us.
if (t.density.constants["book_max_block"]) densityLines.push(`  --pgm-book-max-block: ${px(t.density.constants["book_max_block"])};`);
for (const [key, name] of [["rail_left", "rail-left"], ["rail_right", "rail-right"]]) {
  if (t.density.constants[key]) densityLines.push(`  --pgm-${name}: ${px(t.density.constants[key])};`);
}
for (const m of MODES) {
  densityLines.push(`\n[data-density="${m}"] {`);
  for (const [row, by] of Object.entries(t.density.row_heights_px)) densityLines.push(`  --pgm-row-active-${row.replace(/_/g, '-')}: var(--pgm-row-${row.replace(/_/g, '-')}-${m});`);
  for (const [k, by] of Object.entries(t.density.padding_px)) densityLines.push(`  --pgm-pad-active-${k.replace(/_/g, '-')}: var(--pgm-pad-${k.replace(/_/g, '-')}-${m});`);
  densityLines.push(`  --pgm-font-active: var(--pgm-font-${m});`);
  densityLines.push(`}`);
}
const densityCss = densityLines.join('\n');

/* --- motion ------------------------------------------------------------------------------- */
required('motion', t.motion?.easing && t.motion?.duration_ms, 'run tools/build-foundations.py');
const DUR = Object.entries(t.motion.duration_ms).filter(([k]) => k !== 'forbidden');
const motionLines = DUR.map(([k, v]) => `  --pgm-dur-${k}: ${v.min}ms; /* range ${v.min}–${v.max}ms — ${v.use} */`);
motionLines.push(`  --pgm-ui-ceiling: ${px(t.motion.ui_ceiling_ms)}; /* nothing on a data surface may exceed this */`);
for (const [k, v] of Object.entries(t.motion.easing)) {
  if (k.startsWith('ease-')) motionLines.push(`  --pgm-ease-${k.slice(5)}: ${v};`);   /* strip the redundant 'ease-' prefix: --pgm-ease-out */
}
const flash = t.motion.flash_on_change;
motionLines.push(`  --pgm-flash-in: ${flash.in_ms}ms;`, `  --pgm-flash-out: ${flash.out_ms}ms;`);
motionLines.push(`  --pgm-flash-bg-up: var(--pgm-action-buy);`, `  --pgm-flash-bg-down: var(--pgm-action-sell);`);
motionLines.push(`  /* number_policy: ${t.motion.number_policy.rule} — ${t.motion.number_policy.includes[0]} */`);
/* The Telegram Mini App's distances (P12 D2/D4): the same design system in a webview, so the values live with the
   other motion foundations and the classes below read them as tokens. D6 found this file's output drift because
   these three had been typed straight into brand/tokens.css — the generated file is not a place to add a value. */
const mini = t.motion.mini_app;
required('motion.mini_app', mini && mini.rise_sm_px && mini.nudge_px && mini.scrim, 'run tools/build-foundations.py');
motionLines.push(`  --pgm-rise-sm: ${px(mini.rise_sm_px)}; /* ${mini.surfaces} */`,
                 `  --pgm-nudge: ${px(mini.nudge_px)};`,
                 `  --pgm-scrim: ${mini.scrim};`);
const motionCss = motionLines.join('\n');

/* --- layers ------------------------------------------------------------------------------- */
const utilityLines = Object.entries(t.typography.utility || {})
  .filter(([k]) => k !== "note")
  .map(([k, v]) => `  --pgm-${k}: ${typeof v === "number" ? px(v) : v};`);
const utilityCss = utilityLines.join("\n");
const layerCss = Object.entries(t.layers).filter(([k]) => k !== 'note').map(([k, v]) => `  --pgm-z-${k}: ${v};`).join('\n');

/* --- breakpoints: the tokens that CAN be media queries are emitted as such ---------------- */
required('breakpoints', Array.isArray(t.breakpoints?.values) && t.breakpoints.values.length >= 6, 'run tools/build-foundations.py');
const bpValues = t.breakpoints.values;
const bpVars = bpValues.map((b) => `  --pgm-bp-${b.name}: ${px(b.min ?? b.max)};${b.min ? '' : ' /* max-width breakpoint */'}`).join('\n');
const COLS = { 1024: 3, 1280: 3, 1536: 4, 1680: 4 };
const TAPE = { 640: 6, 768: 8, 1024: 12, 1280: 16, 1536: 20, 1680: 24 };
const bpCss = [
  `:root { --pgm-cols: 1; --pgm-tape-rows: 4; }`,
  ...bpValues.filter((b) => b.min).map((b) =>
    `@media (min-width: ${b.min}px) {\n  :root { --pgm-cols: ${COLS[b.min] ?? 1}; --pgm-tape-rows: ${TAPE[b.min] ?? 4}; } /* ${b.name}: ${b.layout.slice(0, 46)}… */\n}`  // the closer is P08's fix: seven of these were emitted unclosed, and every text-based check in the design system passed the result
  ),
].join('\n') + `\n\n/* breakpoint widths, as custom properties, so a component can ask which breakpoint it is\n   in without a second copy of the numbers in JS. They belong *inside* a selector: at top level they are\n   not declarations at all, they are a parse error — and until P08 ran this file through a real CSS parser\n   (Turbopack, via the stylesheet import), the unclosed @media blocks above made the loose text look fine. */\n:root {\n${bpVars}\n}`;

const css = `/* ${brandName} design tokens — GENERATED by tools/build-tokens.mjs. Do not edit by hand.
   Colour values: tools/colour-audit.py (WCAG) + tools/palette-search.py (Brettel ΔE*ab).
   Foundations values: tools/build-foundations.py. Light is :root, dark-first is [data-theme="dark"],
   per brand/BRAND-KIT.md. */
${block(':root', t.semantic.light)}

${block('[data-theme="dark"]', t.semantic.dark)}

/* chart arrays — per theme, because no theme-agnostic 8-hue set clears ΔE≥10 once outcome and
   money hues are excluded (measured: best such set is 9.1) */
:root { --pgm-chart-count: ${t.chart.perThemeCount.light};
${t.chart.light.map((c, i) => `  --pgm-chart-${i + 1}: ${c};`).join('\n')} }
[data-theme="dark"] { --pgm-chart-count: ${t.chart.perThemeCount.dark};
${t.chart.dark.map((c, i) => `  --pgm-chart-${i + 1}: ${c};`).join('\n')} }

/* rules the palette cannot express as colour */
:root, [data-theme="dark"] {
  --pgm-mark-min-size-px: ${t.rules['mark-min-size-px']};   /* below this, brand/svg/favicon.svg is mandatory */
${radiusCss}

${spacingCss}

${borderCss}

${elevCss}

${densityCss}

${motionCss}

${layerCss}

${utilityCss}
}

/* breakpoint-scoped layout: only column count + tape row budget change here */
${bpCss}

/* NEVER put .pgm-outcome--no and .pgm-severity--critical in the same row (see tokens.json rules) */
.pgm-outcome--yes { color: var(--pgm-outcome-yes); }
.pgm-outcome--no  { color: var(--pgm-outcome-no); }
.pgm-outcome--yes::before { content: "▲ "; }   /* colour is never the only channel */
.pgm-outcome--no::before  { content: "● "; }

/* flash-on-change: background only — the glyph and the digits never move (number_policy) */
@keyframes pgm-flash-up { from { background: var(--pgm-flash-bg-up); } to { background: transparent; } }
@keyframes pgm-flash-down { from { background: var(--pgm-flash-bg-down); } to { background: transparent; } }
.pgm-flash--up   { animation: pgm-flash-up   var(--pgm-flash-out) var(--pgm-ease-out) 1; }
.pgm-flash--down { animation: pgm-flash-down var(--pgm-flash-out) var(--pgm-ease-out) 1; }

/* -----------------------------------------------------------------------------------------------------------
   Telegram Mini App motion (P12 D2/D4).

   Two things are worth stating where they are defined rather than in a doc:

   * **Every duration and easing here is an existing token.** The Mini App is not a second design system; it is the
     same one in a webview, and a chat next to a sheet that moves at a different speed reads as two products.
   * **Nothing animates a number.** The card rises, the sheet slides, the confirm row flashes its *background*, and
     the digits never move and never count up — the same \`number_policy\` the flash-on-change rule already encodes,
     restated here because a confirmation is the most tempting place in the product to break it.

   The skeletons animate opacity only, and they carry no values at all: a placeholder that shows a made-up price is
   worse than an empty one.
   ----------------------------------------------------------------------------------------------------------- */
@keyframes pgm-card-in { from { opacity: 0; transform: translateY(var(--pgm-rise-sm)); } to { opacity: 1; transform: none; } }
@keyframes pgm-sheet-in { from { transform: translateY(100%); } to { transform: translateY(0); } }
@keyframes pgm-sheet-out { from { transform: translateY(0); } to { transform: translateY(100%); } }
@keyframes pgm-ack { from { background: var(--pgm-flash-bg-up); } to { background: transparent; } }
@keyframes pgm-soft-pulse { 0% { opacity: 1; } 50% { opacity: 0.55; } 100% { opacity: 1; } }
@keyframes pgm-reject-nudge { 0% { transform: none; } 25% { transform: translateX(var(--pgm-nudge)); }
                             75% { transform: translateX(calc(var(--pgm-nudge) * -1)); } 100% { transform: none; } }

.pgm-tma-card   { animation: pgm-card-in var(--pgm-dur-small) var(--pgm-ease-out) 1; }
.pgm-tma-sheet  { animation: pgm-sheet-in var(--pgm-dur-drawer) var(--pgm-ease-drawer) 1; }
.pgm-tma-sheet--closing { animation: pgm-sheet-out var(--pgm-dur-drawer) var(--pgm-ease-drawer) 1 forwards; }
.pgm-tma-ack    { animation: pgm-ack var(--pgm-dur-medium) var(--pgm-ease-out) 1; }
.pgm-tma-reject { animation: pgm-reject-nudge var(--pgm-dur-medium) var(--pgm-ease-in-out) 1; }
.pgm-tma-skeleton { animation: pgm-soft-pulse var(--pgm-dur-large) var(--pgm-ease-in-out) infinite; }

/* The sheet is a bottom sheet with a visible grabber, because a webview has no native affordance to tell a user
   that the thing under their thumb can move. \`touch-action: none\` on the grabber only: making the whole sheet
   non-scrollable to make dragging easier is how a sheet becomes a trap on a long market question. */
.pgm-tma-scrim  { background: var(--pgm-scrim); animation: pgm-card-in var(--pgm-dur-small) var(--pgm-ease-out) 1; }
.pgm-tma-grabber { touch-action: none; }

@media (prefers-reduced-motion: reduce) {
  .pgm-flash--up, .pgm-flash--down { animation: none; }
  .pgm-tma-card, .pgm-tma-sheet, .pgm-tma-sheet--closing, .pgm-tma-reject { animation: none; }
  .pgm-tma-skeleton { animation: pgm-soft-pulse var(--pgm-flash-in) var(--pgm-ease-in-out) infinite; }
  * { transition-duration: 1ms !important; }
}
`;

/* --- the generator's own anti-literal guard ------------------------------------------------ */
/* A prefix allowlist was the first attempt and it is wrong: `--pgm-row-height-fudge` matched the
   `--pgm-row-` prefix and sailed through. So the rule is exact instead: every emitted value is
   built from a named section, and this registry is the complete list of names allowed to exist.
   Anything in the output that is not registered is a literal typed into this file. */
function registryFrom(sections) {
  const names = new Set();
  for (const text of sections) for (const m of text.matchAll(/(--pgm-[a-z0-9-]+)\s*:/g)) names.add(m[1]);
  return names;
}
function assertExactRegistry(cssText, allowedNames) {
  const offenders = [];
  const seen = new Set();
  for (const line of cssText.split('\n')) {
    const m = /^\s*(--pgm-[a-z0-9-]+)\s*:\s*(.+);/.exec(line);
    if (!m) continue;
    const [, name] = m;
    // duplicates are expected and correct here — a name is legitimately defined once per theme
    // block (:root and [data-theme="dark"]), and the P02 colour audit checks those pairs.
    if (!allowedNames.has(name)) offenders.push(`${name}: ${m[2].trim()} — has no tokens.json home`);
    seen.add(name);
  }
  if (offenders.length) {
    throw new Error(
      'REFUSING to generate: the stylesheet contains custom properties this generator cannot account for:\n  ' +
        offenders.join('\n  ') +
        '\nAdd the value to tools/build-foundations.py (or tokens.json for colours) and emit it from a named section — do not type it into a template.'
    );
  }
  return seen;
}
const SEMANTIC = [...Object.keys(t.semantic.light), ...Object.keys(t.semantic.dark)].map((k) => `--pgm-${kebab(k)}`);
const allowedNames = registryFrom([
  spacingCss, radiusCss, borderCss, elevCss, densityCss, motionCss, layerCss, bpCss, utilityCss,
  SEMANTIC.map((n) => `${n}: x`).join('\n'),
  `${brandName}: x`,   // header comment holds no property; keep the registry honest about the rest
  ...[0, 1, 2, 3].map((i) => `--pgm-shadow-${t.elevation.levels[i].name}: x`),
  `--pgm-mark-min-size-px: x`,
  [...t.chart.light, ...t.chart.dark].map((_, i) => `--pgm-chart-${(i % 8) + 1}: x`).join('\n') + '\n--pgm-chart-count: x',
]);
assertExactRegistry(css, allowedNames);

if (process.argv.includes('--check')) {
  const cur = fs.existsSync(cssPath) ? fs.readFileSync(cssPath, 'utf8') : '';
  if (cur !== css) {
    console.error('brand/tokens.css is stale — run: node tools/build-tokens.mjs');
    const want = css.split('\n').length, got = cur.split('\n').length;
    console.error(`  (generated ${want} lines vs on disk ${got} lines)`);
    process.exit(1);
  }
  console.log('brand/tokens.css is up to date with brand/tokens.json');
  process.exit(0);
}
fs.writeFileSync(cssPath, css);
console.log(`wrote ${cssPath} (${Buffer.byteLength(css)} bytes, ${css.split('\n').length} lines, chart: dark=${t.chart.perThemeCount.dark}, light=${t.chart.perThemeCount.light})`);
