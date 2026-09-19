#!/usr/bin/env node
/**
 * Copies the token layer into the web root so the bundler can reach it, and refuses to let the copy go stale.
 *
 * Why a copy exists at all: Turbopack resolves CSS `@import` inside the project root only — `externalDir`
 * covers JS/TS imports, not stylesheet ones — so `@import "../../brand/tokens.css"` is a module-not-found at
 * build time. The alternative that looks tidier is to hand `brand/tokens.css` its own `<link>`, which puts the
 * design system behind a network request on the critical path of the first paint, and the LCP budget in P08 D9
 * does not survive that on a mid-range Android.
 *
 * So the copy is generated and checked, exactly like `web/tailwind.preset.cjs` already is: one source of truth
 * (brand/tokens.json), two generated consumers, and a `--check` that fails the build when either drifts.
 * `tools/p03-gate-check.py` G6.8 lists this path as generated, which is why the hexes in it are expected.
 */
import { readFileSync, writeFileSync, mkdirSync, existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const SRC = path.join(ROOT, "brand", "tokens.css");
const DEST = path.join(ROOT, "web", "styles", "tokens.css");
// `<meta name="theme-color">` cannot read a custom property: the value is a literal in the document head, and
// the browser chrome it paints is outside the stylesheet. So the literal is generated from the token, which is
// the difference between "the shell hardcodes a colour" and "the shell renders what brand/tokens.css says".
const THEME_DEST = path.join(ROOT, "web", "styles", "theme-colors.json");
const BANNER = "/* GENERATED from brand/tokens.css by tools/build-web-tokens.mjs. Do not edit — the copy is\n"
             + "   the second place a token would live, and a second place is the one that goes stale. */\n";

const source = readFileSync(SRC, "utf8");
const wanted = BANNER + source;
const check = process.argv.includes("--check");

function themeColors(css) {
  const light = /--pgm-bg-base:\s*(#[0-9a-fA-F]{6})/.exec(css)?.[1];
  const darkBlock = /\[data-theme="dark"\][^{]*\{([^}]*)\}/.exec(css)?.[1] ?? "";
  const dark = /--pgm-bg-base:\s*(#[0-9a-fA-F]{6})/.exec(darkBlock)?.[1];
  if (!light || !dark) throw new Error("build-web-tokens: could not read --pgm-bg-base for both themes out of brand/tokens.css");
  return JSON.stringify({ note: "generated from brand/tokens.css by tools/build-web-tokens.mjs — theme-color needs a literal, so this is the token, not a copy of it", light, dark }, null, 2) + "\n";
}

const wantedTheme = themeColors(source);

if (check) {
  if (!existsSync(DEST)) {
    console.error("build-web-tokens: web/styles/tokens.css is missing — run `node tools/build-web-tokens.mjs`");
    process.exit(1);
  }
  const have = readFileSync(DEST, "utf8");
  if (!existsSync(THEME_DEST) || readFileSync(THEME_DEST, "utf8") !== wantedTheme) {
    console.error("build-web-tokens: web/styles/theme-colors.json is stale or missing — rerun the generator");
    process.exit(1);
  }
  if (have !== wanted) {
    const a = have.split("\n");
    const b = wanted.split("\n");
    let i = 0;
    while (i < Math.min(a.length, b.length) && a[i] === b[i]) i++;
    console.error("build-web-tokens: web/styles/tokens.css is STALE relative to brand/tokens.css");
    console.error(`  first difference at line ${i + 1}: have ${JSON.stringify((a[i] ?? "(eof)").trim().slice(0, 60))}`);
    console.error(`                                 want ${JSON.stringify((b[i] ?? "(eof)").trim().slice(0, 60))}`);
    console.error("  run `node tools/build-web-tokens.mjs` and commit both files.");
    process.exit(1);
  }
  console.log(`build-web-tokens: web/styles/tokens.css matches brand/tokens.css (${wanted.split("\n").length} lines)`);
  process.exit(0);
}

mkdirSync(path.dirname(DEST), { recursive: true });
writeFileSync(DEST, wanted);
writeFileSync(THEME_DEST, wantedTheme);
console.log(`build-web-tokens: wrote ${path.relative(ROOT, DEST)} (${wanted.split("\n").length} lines from ${path.relative(ROOT, SRC)})`);
