/**
 * What the initial route actually costs a phone, measured off a running server.
 *
 * `next build` with Turbopack prints a route table with no sizes, and it does not write a per-route client
 * manifest (there is no `app-build-manifest.json`, and `static/chunks` is a flat pool). Two ways out: quote a
 * number nobody can reproduce, or measure the bytes. This measures: start the server, fetch the document,
 * take the `<script>`/`<link rel=stylesheet>` set the browser would fetch, and sum what the wire carries.
 *
 * That is a better number than the build log for two reasons: it is the real set for that route (so the
 * code-splitting claim is a comparison of two sets rather than a hope), and it counts the CSS the same way.
 *
 * Writes docs/verification/P08-bundle.txt, which `tools/p08-gate-check.py` c8 reads. Re-run with
 * `npm run measure` (needs `npm run build` first, because `next start` serves the build).
 */
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { gzipSync } from "node:zlib";
import { writeFileSync, mkdirSync, readFileSync, existsSync, readdirSync } from "node:fs";
import path from "node:path";

const ROOT = path.resolve(import.meta.dirname, "..");
const REPO = path.resolve(ROOT, "..");
const PORT = Number(process.env.PGM_MEASURE_PORT ?? 3111);
const BASE = `http://127.0.0.1:${PORT}`;
const BUDGET_KB = 200;
const ROUTES = ["/", "/markets", "/tma", "/profile"];

// Third-party scripts that are part of the *platform*, not of our payload. Kept as a list with a reason rather
// than a `startsWith("https")` filter: the point is that a new external script shows up in the budget unless
// somebody deliberately adds it here with a sentence saying why it cannot be ours.
const PLATFORM_SCRIPTS = ["https://telegram.org/js/telegram-web-app.js"];
const isPlatformScript = (src) => PLATFORM_SCRIPTS.some((u) => src.startsWith(u));


/**
 * What this artefact describes, as a hash rather than a timestamp.
 *
 * The P15 finding: this file used to be judged current by comparing its mtime against the newest source file, and
 * that check passed for five phases while the tree measured 208 KB against a 200 KB budget — because a checkout
 * touches every file, so "the record is newer" stopped meaning "the record is true". The hash is the claim: these
 * exact sources produced these numbers. `tools/p08-gate-check.py` recomputes it and fails when they differ, which is
 * also why a `git checkout` no longer invalidates a measurement that is still accurate.
 */
function walk(dir) {
  const out = [];
  for (const e of readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, e.name);
    if (e.isDirectory()) out.push(...walk(full));
    else if (e.isFile()) out.push(full);
  }
  return out;
}
function sourceHash() {
  const files = [...walk(path.join(ROOT, "src")), ...walk(path.join(ROOT, "app")),
    path.join(ROOT, "package.json"), path.join(ROOT, "next.config.mjs"), path.join(ROOT, "scripts", "measure-first-load.mjs")]
    .filter((f) => existsSync(f));   // same file list as tools/p08-gate-check.py::bundle_source_hash, which skips
                                     // what a fixture tree does not have
  const h = createHash("sha256");
  for (const f of files.map((f) => path.relative(ROOT, f).split(path.sep).join("/")).sort()) {
    h.update(f);
    h.update("\0");
    h.update(readFileSync(path.join(ROOT, f)));
    h.update("\0");
  }
  return h.digest("hex").slice(0, 16);
}

if (!existsSync(path.join(ROOT, ".next", "BUILD_ID"))) {
  console.error("measure-first-load: no build to serve — run `npm run build` first");
  process.exit(1);
}

// A stale server on this port used to be measured as if it were the build in front of us: `waitReady()`
// asked the port and got an answer, from an OLD process serving an OLD `.next`. The second run of this script
// after a rebuild therefore reported numbers for the tree before it, which is the one failure a measurement
// tool must not have. Speaking to the port before spawning is the check, and a child that dies early (the port
// is held by something that is not HTTP) is a hard error rather than a silent measurement of a stranger.
try {
  const probe = await fetch(BASE + "/", { signal: AbortSignal.timeout(1500) });
  if (probe.status < 500) {
    console.error(`measure-first-load: something is already serving ${BASE} (http ${probe.status}). ` +
      "Kill it first — measuring its build would describe a tree that is not this one.");
    process.exit(1);
  }
} catch {
  /* nothing there: the port is ours */
}

const server = spawn(process.execPath, [path.join(ROOT, "node_modules", "next", "dist", "bin", "next"), "start", "-p", String(PORT), "-H", "127.0.0.1"], {
  cwd: ROOT,
  env: { ...process.env, NEXT_TELEMETRY_DISABLED: "1", NODE_ENV: "production" },
  stdio: ["ignore", "pipe", "pipe"],
});
let serverLog = "";
server.stdout.on("data", (b) => (serverLog += b.toString()));
server.stderr.on("data", (b) => (serverLog += b.toString()));

async function waitReady(deadlineMs = 60_000) {
  const until = Date.now() + deadlineMs;
  while (Date.now() < until) {
    try {
      const r = await fetch(BASE + "/", { headers: { accept: "text/html" } });
      if (r.status < 500) return true;
    } catch {
      /* not up yet */
    }
    await new Promise((r) => setTimeout(r, 250));
  }
  return false;
}

async function gz(text) {
  return gzipSync(Buffer.from(text)).length / 1024;
}

function assets(html) {
  const scripts = [...html.matchAll(/<script[^>]+src="([^"]+)"/g)].map((m) => m[1]);
  const styles = [...html.matchAll(/<link[^>]+href="([^"]+\.css[^"]*)"/g)].map((m) => m[1]);
  return { scripts: [...new Set(scripts)], styles: [...new Set(styles)] };
}

try {
  if (!(await waitReady())) {
    console.error("measure-first-load: the server never answered on port " + PORT + "\n" + serverLog.slice(-1200));
    process.exit(1);
  }
  if (server.exitCode !== null) {
    console.error("measure-first-load: `next start` exited (" + server.exitCode + ") instead of serving\n" + serverLog.slice(-1200));
    process.exit(1);
  }

  const perRoute = new Map();
  for (const route of ROUTES) {
    // Follow the redirect, and record where we ended up: the protected routes answer 307 to /sign-in while
    // nobody is signed in, and measuring the *redirect document* would report a payload no user ever renders.
    const doc = await fetch(BASE + route, { redirect: "follow" });
    const html = await doc.text();
    const { scripts, styles } = assets(html);
    let js = 0;
    let css = 0;
    let bridge = 0;
    const names = [];
    for (const src of scripts) {
      const res = await fetch(new URL(src, BASE).href);
      if (!res.ok) continue;
      const text = await res.text();
      const kb = await gz(text);
      // The Telegram platform bridge is the one script on the page we neither build nor can tree-shake: the Mini
      // App cannot read `initData`, open the MainButton or use haptics without it, and Telegram serves it from
      // telegram.org. It is *counted and printed* on its own line for the routes that need it (see PLATFORM_SCRIPTS)
      // and kept out of the budget, because a budget that includes it stops being a statement about our payload
      // and becomes a statement about what Telegram ships. Everything else, framework baseline included, is ours.
      if (isPlatformScript(src)) {
        bridge += kb;
        continue;
      }
      js += kb;
      names.push(src);
    }
    for (const href of styles) {
      const res = await fetch(new URL(href, BASE).href);
      if (!res.ok) continue;
      css += await gz(await res.text());
    }
    // Does the money layer ship to this route? Match on a string the minifier keeps, not an identifier.
    let moneyHere = false;
    for (const src of scripts) {
      const res = await fetch(new URL(src, BASE).href);
      if (res.ok && (await res.text()).includes("cents must be an integer")) moneyHere = true;
    }
    perRoute.set(route, { js, css, bridge, chunks: names.length, money: moneyHere, status: doc.status, finalUrl: new URL(doc.url).pathname });
  }

  const landing = perRoute.get("/");
  const markets = perRoute.get("/markets");
  const split = landing && markets ? markets.money && !landing.money : false;
  const over = [...perRoute].filter(([, r]) => r.js > BUDGET_KB).map(([r]) => r);

  const lines = [];
  lines.push("P08 first-load report — generated by web/scripts/measure-first-load.mjs against `next start`.");
  lines.push("Numbers are gzipped bytes the browser fetches for that document, not a build-log estimate.");
  lines.push("");
  lines.push(`budget for the initial route: ${BUDGET_KB} KB of first-party JS`);
  const anyBridge = [...perRoute.values()].some((r) => r.bridge > 0);
  if (anyBridge) {
    lines.push(
      `excluded from the budget: ${PLATFORM_SCRIPTS.join(", ")} — fetched from telegram.org, required by the ` +
        "Mini App platform (initData, MainButton, haptics), and not built by us. Its bytes are printed above.",
    );
  }
  for (const [route, r] of perRoute) {
    lines.push(
      `  ${route.padEnd(10)} http ${r.status} · ${r.chunks} chunk(s) · JS ${r.js.toFixed(1)} KB · CSS ${r.css.toFixed(1)} KB · money layer ${r.money ? "present" : "absent"}` +
        (r.bridge ? ` · platform bridge ${r.bridge.toFixed(1)} KB (not budgeted)` : ""),
    );
  }
  lines.push("");
  lines.push(`sources-sha256: ${sourceHash()}`);
  lines.push(`routes over budget: ${over.length ? over.join(", ") : "none"}`);
  lines.push(
    `route-level splitting: ${split ? "proven — the landing document does not fetch the money module and /markets does" : "NOT PROVEN — both routes fetched the same set"}`,
  );
  const status = over.length === 0 && split && perRoute.size === ROUTES.length ? "pass" : "fail";
  lines.push(`status: ${status}`);

  const outDir = path.join(REPO, "docs", "verification");
  mkdirSync(outDir, { recursive: true });
  writeFileSync(path.join(outDir, "P08-bundle.txt"), lines.join("\n") + "\n");
  console.log(lines.join("\n"));
  if (status !== "pass") process.exit(1);
} finally {
  server.kill("SIGTERM");
}
