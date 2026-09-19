/**
 * web/DESIGN.md §8: "missing translation fails the build, not the runtime". This is the check.
 *
 * Key shape is `screen.component.element#variant.state`. The check is two-way on purpose: an unused key is
 * a report (it means somebody deleted a component and left the copy behind) and a used-but-absent key is a
 * failure (it means the UI would render the key itself, which is how "common.button.submit" reaches a
 * screenshot in a launch review).
 *
 * A dynamic key (`t(prefix + ".title")`) is also a failure, and that is the whole reason this file exists in
 * its second revision: the first matcher only recognised `t("key")` with no other argument, so every call
 * that interpolated — `t("x.y.z", { count })` — was invisible to it. Those keys reported as "declared and
 * unused", which looked like dead copy, and a *missing* one would have been reported as nothing at all. A
 * checker that silently reads too little is worse than no checker, because the build still says ok.
 *
 * `--self-test` plants the two cases that must fail and fails if they do not.
 */
import { readFileSync, readdirSync, statSync, existsSync, mkdirSync, writeFileSync, rmSync } from "node:fs";
import path from "node:path";
import { tmpdir } from "node:os";

const ROOT = path.resolve(import.meta.dirname, "..");
const USED_KEY = /(?:^|[^\w$.])t\(\s*"([^"]+)"\s*[,)]/g;
const ANY_CALL = /(?:^|[^\w$.])t\(\s*([^)"\s][^)]*)[)]/g;
const KEY_SHAPE = /^[a-z][a-zA-Z0-9]*(?:\.[a-z][a-zA-Z0-9-]+){2,3}(?:#[a-z][a-zA-Z0-9-]+)?$/;

function* walk(dir, skip = []) {
  for (const e of readdirSync(dir)) {
    if (skip.includes(e) || e.endsWith(".test.tsx") || e.endsWith(".test.ts")) continue;
    const p = path.join(dir, e);
    if (statSync(p).isDirectory()) yield* walk(p, skip);
    else if (/\.(ts|tsx)$/.test(p)) yield p;
  }
}

/**
 * Strip comments before matching, so prose cannot create or hide a usage.
 *
 * Both directions matter. A `t("key")` written in a docstring is not a call: counting it as one makes a
 * missing key look present, which is the failure mode that hides a bug. And an example of a *bad* call in a
 * comment ("never write t(someVariable)") is not a violation: flagging it teaches people to disable the
 * check. Strings are tracked so a URL or a regex containing `//` survives.
 */
export function stripComments(text) {
  let out = "";
  let i = 0;
  let quote = null;
  while (i < text.length) {
    const c = text[i];
    const two = text.slice(i, i + 2);
    if (quote) {
      out += c;
      if (c === "\\") { out += text[i + 1] ?? ""; i += 2; continue; }
      if (c === quote) quote = null;
      i++;
      continue;
    }
    if (two === "//") {
      while (i < text.length && text[i] !== "\n") i++;
      continue;
    }
    if (two === "/*") {
      const end = text.indexOf("*/", i + 2);
      const stop = end === -1 ? text.length : end + 2;
      // Newlines survive so line numbers in a report stay roughly true.
      out += text.slice(i, stop).replace(/[^\n]/g, "");
      i = stop;
      continue;
    }
    if (c === '"' || c === "'" || c === "`") quote = c;
    out += c;
    i++;
  }
  return out;
}

/** The whole check, over an arbitrary root, so --self-test can run it against a planted tree. */
export function checkTree(root) {
  const dictPath = path.join(root, "src/i18n/en.ts");
  if (!existsSync(dictPath)) return { fatal: "src/i18n/en.ts missing" };
  const dictText = readFileSync(dictPath, "utf8");
  const declared = new Set([...dictText.matchAll(/^\s{2}"([^"]+)":/gm)].map((m) => m[1]));
  const used = new Map();
  const dynamic = [];
  for (const dir of ["src", "app"]) {
    const abs = path.join(root, dir);
    if (!existsSync(abs)) continue;
    for (const file of walk(abs, ["i18n"])) {
      const text = stripComments(readFileSync(file, "utf8"));
      for (const m of text.matchAll(USED_KEY)) {
        if (!used.has(m[1])) used.set(m[1], path.relative(root, file));
      }
      for (const m of text.matchAll(ANY_CALL)) {
        const arg = m[1].trim();
        if (arg.startsWith("{")) continue;                       // t<generic> in a type position, not a call
        if (arg.startsWith('"') || arg.startsWith("'")) continue; // counted above
        dynamic.push(`${path.relative(root, file)}: t(${arg.slice(0, 40)})`);
      }
    }
  }
  const missing = [...used.keys()].filter((k) => !declared.has(k)).sort();
  const unused = [...declared].filter((k) => !used.has(k)).sort();
  const malformed = [...declared].filter((k) => !KEY_SHAPE.test(k)).sort();
  return { declared, used, missing, unused, malformed, dynamic };
}

function report(result) {
  if (result.fatal) {
    console.error(`i18n-check: ${result.fatal}`);
    return 1;
  }
  for (const k of result.malformed) console.error(`i18n-check: key "${k}" is not screen.component.element[#variant][.state]`);
  for (const k of result.missing) console.error(`i18n-check: ${result.used.get(k)} asks for "${k}" and en.ts has no such key`);
  for (const d of result.dynamic) console.error(`i18n-check: dynamic key, unchecked by definition — ${d}`);
  for (const k of result.unused) console.warn(`i18n-check (advisory): "${k}" is declared and unused`);
  if (result.missing.length || result.malformed.length || result.dynamic.length) {
    console.error(`i18n-check: FAIL (${result.missing.length} missing, ${result.malformed.length} malformed, ${result.dynamic.length} dynamic)`);
    return 1;
  }
  console.log(`i18n-check: ok (${result.declared.size} keys, ${result.used.size} used, ${result.unused.length} unused-advisory)`);
  return 0;
}

/**
 * Two planted components, both asking for a key the dictionary does not have. One is a plain `t("k")`, one
 * carries interpolation variables — the second is the case the first version of this file could not see, so
 * the self-test fails if either is missed rather than reporting a pass on the easy half.
 */
function selfTest() {
  const dir = path.join(tmpdir(), `pgm-i18n-selftest-${process.pid}`);
  try {
    mkdirSync(path.join(dir, "src/i18n"), { recursive: true });
    mkdirSync(path.join(dir, "src/ui"), { recursive: true });
    writeFileSync(path.join(dir, "src/i18n/en.ts"), 'export const en = {\n  "widget.plain.found": "here",\n};\n');
    writeFileSync(
      path.join(dir, "src/ui/Planted.tsx"),
      'import { t } from "@/i18n/t";\n' +
      'export function Planted({ n }: { n: number }) {\n' +
      '  // t("widget.plain.inComment") is prose, not a call, and never a "used" key;\n' +
      '  // nor is the bad example `t(someKeyVariable)` in this comment a dynamic lookup.\n' +
      '  return <p>{t("widget.plain.missing")}<span>{t("widget.plain.missingVars", { n })}</span></p>;\n' +
      "}\n",
    );
    const out = checkTree(dir);
    const caughtMissing = out.missing?.includes("widget.plain.missing") && out.missing?.includes("widget.plain.missingVars");
    // A comment-only key must not appear as used, and a comment's example of a dynamic call must not be
    // reported as one. Both are the "checker reads too much" failure, which P08 met already in §2.8.
    const clean = !out.used?.has("widget.plain.inComment") && (out.dynamic?.length ?? 0) === 0;
    const ok = caughtMissing && clean;
    console.log(ok
      ? "i18n-check --self-test: ok (planted plain + interpolated keys caught; prose in comments ignored)"
      : `i18n-check --self-test: FAIL — missing=${JSON.stringify(out.missing ?? out.fatal)} usedComment=${out.used?.has("widget.plain.inComment")} dynamic=${JSON.stringify(out.dynamic)}`);
    return ok ? 0 : 1;
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

if (process.argv.includes("--self-test")) process.exit(selfTest());
process.exit(report(checkTree(ROOT)));
