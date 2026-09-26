/**
 * Every `app/**\/page.tsx` may export only what Next recognises as page exports.
 *
 * Why this file exists: three leaderboard pages exported a `metadataFor` helper, and Next's generated page types
 * allow no extra exports — so `next build` failed its type check with
 * `Property 'metadataFor' is incompatible with index signature`. Nothing caught it for four phases, for two
 * reasons that are both worth writing down:
 *
 *   1. **`next build` in the development sandbox is killed by memory pressure** before its type check runs, so the
 *      failure surfaced as `Error 137` with no diagnosis. A build that dies for environmental reasons cannot also
 *      be the only guard against a compile error.
 *   2. **The CI job that would have caught it had never executed.** `pipeline.yml` was written in P15 D3 and has
 *      not run once (no self-hosted runner, and the manual gate blocks the canary). A check that only runs in CI
 *      is a check that does not run yet.
 *
 * This test is the cheap, always-runnable half: it reads the page modules as text and fails on an export the
 * framework does not accept. It is not a substitute for `next build`; it is the guard that survives an OOM.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const APP = join(process.cwd(), "app");

/** The exports a page module may carry, per Next's own type generation. */
/** `route.ts` is a different module kind: its exports ARE the HTTP methods it serves. */
const HTTP_METHODS = new Set(["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]);

const ALLOWED = new Set([
  "default", "metadata", "generateMetadata", "viewport", "generateViewport", "config", "dynamic",
  "revalidate", "dynamicParams", "prefetch", "runtime", "fetchCache", "generateStaticParams",
  "experimental_ppr", "maxDuration", "instant", "alt", "size", "contentType", "generateImageMetadata",
  "generateSitemaps", "unstable_settings", "experimental_ppr", "loader",
]);

function pageFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) out.push(...pageFiles(full));
    else if (entry === "page.tsx" || entry === "layout.tsx" || entry === "route.ts") out.push(full);
  }
  return out;
}

function exportedNames(source: string): string[] {
  const names: string[] = [];
  const patterns = [
    /export\s+(?:async\s+)?function\s+([A-Za-z_$][\w$]*)/g,
    /export\s+const\s+([A-Za-z_$][\w$]*)/g,
    /export\s+(?:type|interface)\s+([A-Za-z_$][\w$]*)/g,
    /export\s*\{([^}]*)\}/g,
  ];
  for (const rx of patterns) {
    for (const m of source.matchAll(rx)) {
      for (const part of (m[1] || "").split(",")) {
        const name = part.trim().split(/\s+as\s+/).pop()?.trim();
        if (name) names.push(name);
      }
    }
  }
  return names;
}

describe("Next's module contract", () => {
  const files = pageFiles(APP);

  it("finds the page modules to check", () => {
    expect(files.length).toBeGreaterThan(20);
  });

  it("a page, layout or route module exports only things the framework accepts", () => {
    const offenders: string[] = [];
    for (const file of files) {
      const source = readFileSync(file, "utf8");
      for (const name of exportedNames(source)) {
        // A `type`/`interface` export is erased at compile time and cannot conflict with the generated page type,
        // which is why `export type Props` is common in this codebase and stays legal here.
        const isTypeOnly = new RegExp(`export\\s+(?:type|interface)\\s+${name}\\b`).test(source)
          || new RegExp(`import type[\\s\\S]*\\b${name}\\b`).test(source) && !new RegExp(
            `export\\s+(?:async\\s+)?(?:function|const)\\s+${name}\\b`).test(source);
        const allowed = ALLOWED.has(name) || (file.endsWith("route.ts") && HTTP_METHODS.has(name));
        if (!allowed && !isTypeOnly) {
          offenders.push(`${file.replace(APP, "app")} exports ${name}`);
        }
      }
    }
    expect(offenders, "an extra export in a page module fails `next build`'s type check — move it to src/").toEqual([]);
  });
});
