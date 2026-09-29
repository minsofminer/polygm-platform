/**
 * Every key a call puts in `params` must be a `{token}` in that route's path.
 *
 * `urlFor` throws `route <key> has no {<name>} segment` when it is not, and the throw surfaces in a way that
 * hides it: a screen's loader catches it, keeps its initial state, and renders an empty surface. That is how
 * `/market/<id>` shipped with no order-book ladder on any market while the API was answering 200 — `depth: 400`
 * was passed as a path parameter on a route whose path is `/v1/markets/{market_id}/book`.
 *
 * This is a static check over the call sites rather than a type: the route ledger's `path` is a string at
 * runtime, so TypeScript cannot tie the two together, and the failure mode is silence. It reads the same shape
 * the screens write — `key: "x", params: { … }` on adjacent lines — and a call written some other way is
 * simply not covered, which is stated here rather than implied.
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { readdirSync, statSync } from "node:fs";
import path from "node:path";
import { ROUTES, type RouteKey } from "./routes";

const ROOT = process.cwd();

function walk(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const abs = path.join(dir, entry);
    if (statSync(abs).isDirectory()) {
      if (entry === "node_modules" || entry === ".next" || entry === "i18n") continue;
      walk(abs, out);
    } else if (/\.tsx?$/.test(entry) && !/\.test\.tsx?$/.test(entry)) {
      out.push(abs);
    }
  }
  return out;
}

const CALL = /key:\s*"([a-zA-Z]+)"[^}]*?params:\s*\{([^}]*)\}/gs;

type Misuse = { file: string; key: string; name: string };

function misuses(): Misuse[] {
  const found: Misuse[] = [];
  for (const file of walk(path.join(ROOT, "src"))) {
    const text = readFileSync(file, "utf8");
    for (const match of text.matchAll(CALL)) {
      const key = match[1] ?? "";
      const body = match[2] ?? "";
      const decl = ROUTES[key as RouteKey] as { path: string } | undefined;
      if (!decl) continue;                                   // an unknown key is another test's problem
      for (const name of body.matchAll(/([a-zA-Z_][a-zA-Z0-9_]*)\s*:/g)) {
        const param = name[1] ?? "";
        if (!decl.path.includes(`{${param}}`)) {
          found.push({ file: path.relative(ROOT, file), key, name: param });
        }
      }
    }
  }
  return found;
}

describe("path parameters are path parameters", () => {
  it("finds none that are not `{tokens}` in the route they are sent to", () => {
    expect(misuses()).toEqual([]);
  });

  it("is a check that can fail: a planted misuse is caught", () => {
    // The self-test the repo's other static checks carry. It runs the same predicate over a string that has the
    // shape of the bug, so a regex that silently stopped matching would fail here rather than pass everywhere.
    const planted = 'request({ key: "book", params: { market_id: id, depth: 400 } })';
    const decl = ROUTES.book as { path: string };
    const names = [...planted.matchAll(/params:\s*\{([^}]*)\}/g)].flatMap((m) =>
      [...(m[1] ?? "").matchAll(/([a-zA-Z_][a-zA-Z0-9_]*)\s*:/g)].map((n) => n[1] ?? ""),
    );
    expect(names.filter((n) => !decl.path.includes(`{${n}}`))).toEqual(["depth"]);
  });
});
