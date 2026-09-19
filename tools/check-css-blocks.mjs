#!/usr/bin/env node
/**
 * Does this CSS open more blocks than it closes?
 *
 * This exists because `brand/tokens.css` shipped from P03 with seven `@media (min-width: …) {` blocks that
 * were never closed — 36 `{` against 30 `}` — and every check in the design system passed it, because they
 * all read the file as *text* (regex for a token name, presence of a line). A browser's parser then silently
 * auto-closed at EOF, which swallowed the rest of the file into the last media query: on any viewport below
 * 1680px, `.pgm-outcome--yes/--no`, the flash keyframes and the `prefers-reduced-motion` override did not
 * apply at all. P08 found it by trying to run the stylesheet through a bundler, which is the first tool in
 * the chain that actually parses CSS.
 *
 * So: parse the shape, not the prose. Balanced blocks, and no at-rule left unclosed at EOF.
 *
 *   node tools/check-css-blocks.mjs brand/tokens.css web/styles/tokens.css
 *   node tools/check-css-blocks.mjs --self-test
 */
import { readFileSync } from "node:fs";

export function analyse(css, label = "<inline>") {
  const problems = [];
  const stack = [];
  let line = 1;
  let i = 0;
  while (i < css.length) {
    const ch = css[i];
    if (ch === "\n") {
      line++;
      i++;
      continue;
    }
    if (ch === "/" && css[i + 1] === "*") {
      const end = css.indexOf("*/", i + 2);
      const chunk = end < 0 ? css.slice(i) : css.slice(i, end + 2);
      line += (chunk.match(/\n/g) ?? []).length;
      if (end < 0) {
        problems.push(`${label}: comment opened at line ${line} is never closed`);
        return problems;
      }
      i = end + 2;
      continue;
    }
    if (ch === '"' || ch === "'") {
      const close = css.indexOf(ch, i + 1);
      const chunk = css.slice(i, close < 0 ? undefined : close + 1);
      line += (chunk.match(/\n/g) ?? []).length;
      i = close < 0 ? css.length : close + 1;
      continue;
    }
    if (ch === "{") {
      // remember what opened it, so an unclosed @media names itself instead of a line number
      const head = css.slice(Math.max(0, i - 60), i).split("\n").pop() ?? "";
      stack.push({ line, at: /@([a-z-]+)\s*\(?[^)]*\)?\s*$/.exec(head.trim())?.[1] ?? null });
      i++;
      continue;
    }
    if (ch === "}") {
      if (!stack.length) problems.push(`${label}: stray } at line ${line}`);
      else stack.pop();
      i++;
      continue;
    }
    i++;
  }
  for (const open of stack) {
    problems.push(
      `${label}: block opened at line ${open.line} is never closed` +
        (open.at ? ` — an unclosed @${open.at} swallows every rule after it into that condition` : ""),
    );
  }
  return problems;
}

const args = process.argv.slice(2);
if (args.includes("--self-test")) {
  const cases = {
    "an unclosed @media is reported": analyse("@media (min-width: 640px) {\n  :root { --a: 1; }\n").length > 0,
    "a balanced file is not": analyse(":root { --a: 1; }\n@media (min-width: 2px) { :root { --b: 2; } }\n").length === 0,
    "a brace inside a comment is not a block": analyse("/* { */\n:root { --a: 1; }\n").length === 0,
    "a brace inside a string is not a block": analyse('.a::before { content: "{"; }\n').length === 0,
    "a stray close is reported": analyse("}\n").length > 0,
    "the unterminated comment is reported": analyse("/* never ends").length > 0,
  };
  let failed = 0;
  for (const [name, ok] of Object.entries(cases)) {
    if (!ok) {
      console.error("  FAIL", name);
      failed++;
    }
  }
  console.log(`check-css-blocks self-test: ${Object.keys(cases).length - failed}/${Object.keys(cases).length} canaries fire`);
  process.exit(failed ? 1 : 0);
}

const files = args.filter((a) => !a.startsWith("--"));
if (!files.length) {
  console.error("usage: check-css-blocks.mjs <file.css> [...] | --self-test");
  process.exit(2);
}
let bad = 0;
for (const f of files) {
  const problems = analyse(readFileSync(f, "utf8"), f);
  if (problems.length) {
    bad += problems.length;
    for (const p of problems) console.error("  " + p);
  } else {
    console.log(`check-css-blocks: ${f} balanced`);
  }
}
if (bad) {
  console.error(`${bad} structural problem(s). A stylesheet nobody parses is a stylesheet nobody reads.`);
  process.exit(1);
}
