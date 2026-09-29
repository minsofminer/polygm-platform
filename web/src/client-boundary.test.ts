/**
 * A `"use client"` module may export a component or a hook — never a plain function.
 *
 * Why this file exists (2026-09-29): `/terminal` answered **500** in the browser. The cause was one line in
 * `app/(app)/terminal/page.tsx` — a server component calling `marketTitle(...)`, a pure helper that happened to
 * live in `TerminalScreen.tsx`, which is a `"use client"` module. Next's runtime refuses that at render time
 * ("Attempted to call marketTitle() from the server but marketTitle is on the client"), so the route was blank
 * — and the page's own doc comment promised the opposite: *"A failed read is not a failed page … the difference
 * between 'the API is down' and 'this route is blank'"*. The failure happened before the resilience it described
 * could matter.
 *
 * Nothing failed at build time, and nothing failed in the test suite, because the boundary is a *runtime* rule.
 * So this test states the rule statically, where it can run every time:
 *
 *   * exporting a **component** (`PascalCase`) from a client module is the whole point;
 *   * exporting a **hook** (`use*`) is normal;
 *   * exporting anything else — a helper, a constant, a formatter — is a trap: a server component *can* import
 *     it, and nothing stops it until the page 500s in production.
 *
 * The fix that makes an offender pass is always the same and always cheap: move the pure thing into a plain
 * module beside it (`src/terminal/market-view.ts` is the worked example) and import it from both sides.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { describe, expect, it } from "vitest";

const ROOTS = [join(process.cwd(), "src"), join(process.cwd(), "app")];

function walk(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) walk(path, out);
    else if (/\.tsx?$/.test(path) && !/\.test\.tsx?$/.test(path)) out.push(path);
  }
  return out;
}

/** `export function name`, `export const name = () => …`, `export class Name` — the shapes a caller can invoke. */
function exportedCallables(source: string): string[] {
  const names: string[] = [];
  for (const m of source.matchAll(/^export\s+(?:async\s+)?function\s+(\w+)/gm)) if (m[1]) names.push(m[1]);
  for (const m of source.matchAll(/^export\s+const\s+(\w+)\s*=\s*(?:async\s*)?\(/gm)) if (m[1]) names.push(m[1]);
  for (const m of source.matchAll(/^export\s+const\s+(\w+)\s*=\s*(?:async\s*)?function/gm)) if (m[1]) names.push(m[1]);
  for (const m of source.matchAll(/^export\s+class\s+(\w+)/gm)) if (m[1]) names.push(m[1]);
  return names;
}

const isComponent = (name: string) => /^[A-Z]/.test(name);
const isHook = (name: string) => /^use[A-Z]/.test(name);

/**
 * Is this module a client module? The directive must be the first *statement*, and comments are allowed above
 * it — `src/ui/Dialog.tsx`, `src/num/Number.tsx` and four others all have long doc comments first. The first
 * draft of this test sliced the first 200 characters and therefore skipped those six modules entirely: a guard
 * that cannot see the files it is guarding is the same failure it exists to catch, one level up.
 */
function isClientModule(source: string): boolean {
  let i = 0;
  for (;;) {
    const m = /^\s*(\/\*[\s\S]*?\*\/|\/\/[^\n]*\n|\n)/.exec(source.slice(i));
    if (!m) break;
    i += m[0].length;
  }
  return /^["']use client["']/.test(source.slice(i));
}

describe("the client/server boundary", () => {
  const files = ROOTS.flatMap((root) => walk(root));

  it("scans the components it is supposed to scan", () => {
    const clientFiles = files.filter((f) => isClientModule(readFileSync(f, "utf8")));
    expect(clientFiles.length).toBeGreaterThan(20);
  });

  it("no client module exports a plain function a server component could call", () => {
    const offenders: string[] = [];
    for (const file of files) {
      const source = readFileSync(file, "utf8");
      if (!isClientModule(source)) continue;
      for (const name of exportedCallables(source)) {
        if (isComponent(name) || isHook(name)) continue;
        offenders.push(`${relative(process.cwd(), file)} exports ${name}()`);
      }
    }
    expect(
      offenders,
      "a plain function exported from a `\"use client\"` module is callable from a server component and 500s at " +
        "render time — move it to a plain module beside it (see src/terminal/market-view.ts)",
    ).toEqual([]);
  });
});
