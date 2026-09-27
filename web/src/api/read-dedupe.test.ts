/**
 * The server-side half of the react-best-practices pass.
 *
 * Two findings, both about the same route pattern — a page that reads twice when it only needed to read once,
 * and twice *in turn* when the reads were independent. The second is observable here and is tested directly.
 * The first is not: `React.cache` attaches to a React request, and this environment has no request, so the
 * honest test is a source assertion plus the reasoning recorded next to the code and in `plans/react-review.md`.
 * Saying that out loud is better than a test that passes for the wrong reason.
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import path from "node:path";
import { loadAlerts } from "@/terminal/alertsData";
import { loadAutomation } from "@/terminal/automationData";
import type { Reader } from "@/terminal/automationData";
import type { ServerRead } from "@/api/server-read";

const src = (rel: string) => readFileSync(path.join(process.cwd(), rel), "utf8");
const ok = <T,>(data: T): ServerRead<T> => ({ ok: true, data, stampLabel: "as of now", freshness: "live", ageMs: 0 });

/** A reader that records when each path starts and finishes, and can hold responses open on demand. */
function recordingReader() {
  const started: string[] = [];
  const finished: string[] = [];
  const gates = new Map<string, () => void>();
  const read: Reader = async <T,>(_method: "GET", p: string): Promise<ServerRead<T>> => {
    started.push(p);
    if (!gates.has(p)) {
      let release: () => void = () => {};
      const held = new Promise<void>((resolve) => { release = resolve; });
      gates.set(p, release);
      await held;
    } else {
      gates.get(p)!();
    }
    finished.push(p);
    return ok({ path: p } as T);
  };
  return { read, started, finished, release: (p: string) => gates.get(p)?.() };
}

describe("the automation page's reads", () => {
  it("starts both before either finishes — parallel, not sequential", async () => {
    const r = recordingReader();
    const promise = loadAutomation(r.read);
    // Let the async bodies run to their first await.
    await Promise.resolve();
    expect(r.started).toEqual(["/v1/automations", "/v1/automations/templates"]);
    // Neither has finished: the second read began while the first was still open. That is the whole finding —
    // the previous code could not have both started at this point.
    expect(r.finished).toEqual([]);
    r.release("/v1/automations");
    r.release("/v1/automations/templates");
    await promise;
    expect(r.finished).toHaveLength(2);
  });

  it("keeps the two results separate, so one failure cannot blank the other", async () => {
    const fails: Reader = async <T,>(_m: "GET", p: string): Promise<ServerRead<T>> =>
      p.includes("templates")
        ? ({ ok: false, code: "INTERNAL", message: "no catalog", status: 500, stampLabel: "no data",
             freshness: "unknown", ageMs: null } as ServerRead<T>)
        : ok({ items: [] } as T);
    const { list, catalog } = await loadAutomation(fails);
    expect(list.ok).toBe(true);
    expect(catalog.ok).toBe(false);
  });
});

describe("the alerts page's reads", () => {
  it("starts the delivery history before the rule list has answered", async () => {
    const r = recordingReader();
    const promise = loadAlerts(r.read);
    await Promise.resolve();
    expect(r.started).toEqual(["/v1/alerts", "/v1/alerts/deliveries"]);
    expect(r.finished).toEqual([]);
    r.release("/v1/alerts");
    r.release("/v1/alerts/deliveries");
    const { list, history } = await promise;
    expect(list.ok && history.ok).toBe(true);
  });

  it("still reports the two reads independently", async () => {
    const historyFails: Reader = async <T,>(_m: "GET", p: string): Promise<ServerRead<T>> =>
      p.includes("deliveries")
        ? ({ ok: false, code: "NETWORK", message: "down", status: 0, stampLabel: "no data",
             freshness: "unknown", ageMs: null } as ServerRead<T>)
        : ok({ rules: [] } as T);
    const { list, history } = await loadAlerts(historyFails);
    expect(list.ok).toBe(true);
    expect(history.ok).toBe(false);
  });
});

describe("the pages call the parallel loaders", () => {
  it("both RSC pages use their loader rather than awaiting two reads in turn", () => {
    for (const [file, name] of [
      ["app/(app)/alerts/page.tsx", "loadAlerts"],
      ["app/(app)/automation/page.tsx", "loadAutomation"],
    ] as const) {
      const page = src(file);
      expect(page).toContain(`await ${name}(serverRead)`);
      // The pattern this pass removed: two `await serverRead` lines in one body.
      expect(page.match(/await serverRead/g)).toBeNull();
    }
  });

  it("the client's alerts refresh fetches the list and the history together", () => {
    const view = src("src/terminal/AlertsView.tsx");
    expect(view).toMatch(/Promise\.all\(\[\s*request<AlertsPayload>/);
    expect(view).not.toMatch(/await request<AlertsPayload>\(\{ key: "alerts" \}\);\s*\n\s*if \(res\.ok\) setPayload/);
  });
});

describe("the per-request read memo", () => {
  it("wraps both server readers in React's per-request cache", () => {
    for (const file of ["src/api/public-read.ts", "src/api/server-read.ts"]) {
      const t = src(file);
      expect(t).toContain('import { cache } from "react";');
      expect(t).toMatch(/const cached\w+ = cache\(/);
      // The public facade keeps its generic signature; the impl is the uncached body.
      expect(t).toMatch(/export function \w+<T>\(method: "GET", path: string\): Promise<ServerRead<T>>/);
    }
  });

  it("memoizes per request and no further — `cache` is a pass-through outside a render", async () => {
    // This is the property that makes it safe to put a cache in this module: without a React request, nothing
    // is retained, so a test, a script or a stray call cannot be served stale data by another call's entry.
    const { cache } = await import("react");
    let calls = 0;
    const memo = cache((x: number) => { calls += 1; return x; });
    memo(1); memo(1); memo(1);
    expect(calls).toBe(3);
  });

  it("is the reason the memo exists: those pages read the same path twice per request", () => {
    // If a future change makes the metadata read a different path from the body, the memo stops paying for
    // itself and this test is the place that says so.
    for (const [file, p] of [
      ["app/market/[market]/page.tsx", "/v1/public/market/"],
      ["app/trader/[who]/page.tsx", "/v1/public/trader/"],
    ] as const) {
      const page = src(file);
      const hits = page.split(`publicRead<`).length - 1;
      expect(hits).toBe(2);
      expect(page).toContain(p);
    }
  });
});
