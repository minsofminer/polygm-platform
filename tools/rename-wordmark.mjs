#!/usr/bin/env node
/**
 * Rename the wordmark — and enforce the Brand Lock while doing it.
 *
 * brand/BRAND-KIT.md marks the horizontal lockup "fixed (text swaps on rename)": a rename may
 * change the letters and nothing else. Checking that by diffing two hand-maintained files is weak
 * (a diff against yourself always agrees), so this tool does not edit the lockup — it REBUILDS
 * the lockup's mark directly from mark.svg and verifies the geometry against a pinned oracle hash
 * stored in BRAND-KIT.md (`brand_lock.geometry_sha256`).
 *
 *   node tools/rename-wordmark.mjs PolyGM Openout
 *   node tools/rename-wordmark.mjs --check      # exit 1 while the old name still renders / drift
 *   node tools/rename-wordmark.mjs --hash       # print the canonical geometry hash
 */
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';

const ROOT = path.resolve(new URL('..', import.meta.url).pathname);
const LOCKUP = path.join(ROOT, 'brand/svg/lockup-horizontal.svg');
const MARK = path.join(ROOT, 'brand/svg/mark.svg');
const KIT = path.join(ROOT, 'brand/BRAND-KIT.md');
const FALLBACK_CHAIN = "'Instrument Sans Condensed', 'Inter', 'Segoe UI', sans-serif";
const METRICS = path.join(ROOT, 'brand/wordmark-metrics.json');
// The mark occupies 0..100 of the lockup; the original hand-authored file left ~100 units of
// trailing room past the wordmark. Deriving width from measured glyph advances keeps the same
// optical right edge for any name — arithmetic on character counts clipped "Openout" by 29 units.
const TRAIL = 100;
const MARK_BOX = 100;
const OLD_DEFAULT = 'PolyGM';

const args = process.argv.slice(2);
const check = args.includes('--check');
const hashOnly = args.includes('--hash');
const [from, to] = args.filter((a) => !a.startsWith('--'));

const shapes = (s) => (s.match(/<(?:polygon|circle)\b[^>]*\/>/g) || []);
/** Canonical form of the mark's geometry, in mark.svg's own 100-unit space. */
const canonical = (els) => els.map((e) => e.replace(/\s+/g, ' ').trim()).join('\n');
const hashOf = (els) => crypto.createHash('sha256').update(canonical(els)).digest('hex').slice(0, 16);
const read = (p) => fs.readFileSync(p, 'utf8');
const wordOf = (svg) => {
  const t = /<text\b[^>]*>([\s\S]*?)<\/text>/.exec(svg);
  return t ? t[1].replace(/<\/?tspan\b[^>]*>/g, '').replace(/\s+/g, '').trim() : '';
};
const pinnedHash = () => {
  const m = /"geometry_sha256"\s*:\s*"([0-9a-f]{16})"/.exec(read(KIT));
  return m ? m[1] : null;
};

const markEls = shapes(read(MARK));
const markHash = hashOf(markEls);

if (hashOnly) {
  console.log(markHash);
  console.error(`canonical geometry from ${markEls.length} elements in mark.svg (100-unit space)`);
  process.exit(0);
}

const pin = pinnedHash();
const lockup = fs.existsSync(LOCKUP) ? read(LOCKUP) : '';
const lockupWord = wordOf(lockup);
const lockupHash = hashOf(shapes(lockup));
const stale = (from || OLD_DEFAULT) && lockupWord.toLowerCase().includes((from || OLD_DEFAULT).toLowerCase());

if (check || !from || !to) {
  if (check) {
    const pin = pinnedHash();
    const geomOk = lockupHash === markHash && (!pin || pin === markHash);
    console.log(`${stale ? 'STALE' : 'OK   '} wordmark: "${lockupWord || '(none)'}"`);
    console.log(`${geomOk ? 'OK   ' : 'DRIFT'} mark geometry ${lockupHash} vs mark.svg ${markHash}${pin ? ` vs pinned ${pin}` : ' (no pinned hash)'}`);
    console.log(`      rebuild lockup: ${lockupHash === markHash ? 'lockup matches mark.svg ✓' : 'lockup contains hand-edited geometry ✗'}`);
    process.exit(stale || !geomOk ? 1 : 0);
  }
  console.log('usage: node tools/rename-wordmark.mjs <OldName> <NewName> | --check | --hash');
  process.exit(2);
}

// --- hard preconditions ---------------------------------------------------------------
if (!lockupWord && lockup) throw new Error('REFUSING: lockup exists but no <text> was parsed — refusing to guess the live name.');
// metrics freshness is checked FIRST: a stale measurement is the more expensive mistake, since it
// would only surface as clipped artwork after the write rather than as a refused rename.
let width_em = null;
if (fs.existsSync(METRICS)) {
  const m = JSON.parse(read(METRICS));
  if (m.text === to) width_em = m.width_em;
  else throw new Error(`REFUSING: ${path.basename(METRICS)} measures ${JSON.stringify(m.text)}, not ${JSON.stringify(to)}. Re-run: python3 tools/wordmark-width.py ${to} --json > brand/wordmark-metrics.json`);
} else {
  throw new Error('REFUSING: brand/wordmark-metrics.json is missing. Generate it with tools/wordmark-width.py — never size a lockup from character counts.');
}
if (lockup && !lockupWord.toLowerCase().includes(from.toLowerCase())) {
  throw new Error(`REFUSING: ${JSON.stringify(from)} is not the rendered wordmark (found ${JSON.stringify(lockupWord)}).`);
}
// The pinned hash is the oracle: if the master itself was edited AND re-pinned in the same change,
// a rename would still run and launder it. Refuse unless mark.svg still matches the pin.
if (pin && pin !== markHash) {
  throw new Error(`REFUSING: brand/svg/mark.svg (${markHash}) no longer matches the pinned Brand Lock hash (${pin}). ` +
    `The master was edited — a rename must not launder that. Restore mark.svg or record a deliberate, approved lock change.`);
}
if (lockup && lockupHash !== markHash) {
  throw new Error(`REFUSING: lockup mark geometry (${lockupHash}) has already drifted from mark.svg (${markHash}). Fix the drift first; a rename must not launder it.`);
}
if (!pin) throw new Error('REFUSING: no geometry_sha256 pinned in BRAND-KIT.md — run node tools/rename-wordmark.mjs --hash and record it in the Brand Lock.');

// --- rebuild: mark geometry is COPIED from mark.svg, never retyped ---------------------
const splitAt = lockupWord.length > 4 && from.length > 4 ? 4 : Math.ceil(to.length / 2);
const head = to.slice(0, splitAt);
const tail = to.slice(splitAt);
const wordmark = tail ? `${head}<tspan fill="#7ba5ff">${tail}</tspan>` : head;

const textW = (width_em + (1.0 / 44.0) * to.length) * 44; // advances + 1px letter-spacing per glyph
if (textW > 200) throw new Error(`REFUSING: measured wordmark is ${textW.toFixed(1)} units; the mark+text layout tolerates 200. Pick a shorter name or re-layout deliberately.`);
const W = Math.round(MARK_BOX + textW + TRAIL);
const out =
`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${W} 100" width="${W}" height="100" role="img" aria-label="${to} — horizontal lockup">
  <title>${to} — horizontal lockup</title>
  <!-- GENERATED by tools/rename-wordmark.mjs — do not hand-edit.
       Mark geometry is copied verbatim from brand/svg/mark.svg (the fixed master);
       renaming changes only the wordmark text. Regenerate: node tools/rename-wordmark.mjs ${from} ${to} -->
  ${canonical(markEls).split('\n').join('\n  ')}
  <text x="100" y="63" font-family="${FALLBACK_CHAIN}"
        font-weight="700" font-size="44" letter-spacing="1" fill="#e6edf3">${wordmark}</text>
</svg>
`;

// --- postconditions, checked on the rendered string before it hits disk ---------------
const afterEls = shapes(out);
if (hashOf(afterEls) !== markHash) throw new Error('REFUSING TO WRITE: generated geometry does not match mark.svg.');
const afterWord = wordOf(out);
if (afterWord !== to) throw new Error('REFUSING TO WRITE: wordmark rendered as "' + afterWord + '", wanted "' + to + '" (case preserved).');
if (!/<text\b[^>]*font-size="44"[^>]*letter-spacing="1"/.test(out)) throw new Error('REFUSING TO WRITE: text metrics changed.');
if (100 + textW > W - 8) throw new Error(`REFUSING TO WRITE: wordmark would clip (ends at ${(100 + textW).toFixed(1)} of ${W}).`);
if (!out.includes(FALLBACK_CHAIN)) throw new Error('REFUSING TO WRITE: font fallback chain lost.');

fs.writeFileSync(LOCKUP, out);
const rt = read(LOCKUP);
if (hashOf(shapes(rt)) !== markHash || wordOf(rt) !== to) throw new Error('post-write verification failed');
console.log('wordmark : "' + (lockupWord || '(new)') + '" → "' + wordOf(rt) + '" (case preserved, two-tone at offset ' + splitAt + ')');
console.log('geometry : copied from mark.svg, ' + afterEls.length + ' elements, hash ' + markHash + ' ✓');
console.log('viewBox  : 0 0 ' + W + ' 100 — wordmark measures ' + textW.toFixed(1) + ' units (hmtx), ' + (W - 100 - textW).toFixed(1) + ' units trailing room');
console.log('pinned   : ' + (pinnedHash() === markHash ? 'BRAND-KIT.md hash matches ✓' : 'BRAND-KIT.md has no/pinned-mismatched hash — run: node tools/rename-wordmark.mjs --hash'));
