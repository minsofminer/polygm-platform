/**
 * Build-time assertion: no secret can reach the client bundle.
 *
 * Two independent failures are prevented, and the second is the one people forget:
 *   (a) a variable named like a credential is given a NEXT_PUBLIC_ prefix, so Next inlines it into the
 *       client bundle at build time (this is the shape that actually ships secrets);
 *   (b) product code reads `process.env.<anything else>` in a client module, where the value is
 *       undefined at runtime and the developer "fixes" it by adding the prefix from (a).
 *
 * A CI grep over `.next/` runs after the build (tools/p08-gate-check.py c7 shells out to
 * tools/ci-log-scan.py, the same scanner that guards the logs) so a value that arrives by some third
 * route — a .env committed by mistake, a define() somebody adds — is still caught.
 */
import { readFileSync, existsSync, readdirSync, statSync } from "node:fs";
import path from "node:path";

const SECRETISH = /(SECRET|TOKEN|KEY|PASSWORD|PASSWD|PRIVATE|CRED|SEED|MNEMONIC|APIKEY|AUTH)/i;
const ROOT = path.resolve(import.meta.dirname, "..");
const SCAN_DIRS = ["src", "app"];

const declared = [];
for (const file of [".env.local", ".env", ".env.development", ".env.production"]) {
  const p = path.join(ROOT, file);
  if (!existsSync(p)) continue;
  for (const line of readFileSync(p, "utf8").split("\n")) {
    const m = line.match(/^\s*([A-Z0-9_]+)\s*=/);
    if (m) declared.push([file, m[1]]);
  }
}

const problems = [];
for (const [file, name] of declared) {
  if (name.startsWith("NEXT_PUBLIC_") && SECRETISH.test(name)) {
    problems.push(`${file}: ${name} is secret-shaped and client-inlined by its prefix`);
  }
}

function* walk(dir) {
  for (const entry of readdirSync(dir)) {
    const p = path.join(dir, entry);
    if (statSync(p).isDirectory()) yield* walk(p);
    else if (/\.(ts|tsx)$/.test(p)) yield p;
  }
}

const serverOnly = /(^|\/)(api\/session|server\/|.*\.server\.)/;
for (const dir of SCAN_DIRS) {
  const abs = path.join(ROOT, dir);
  if (!existsSync(abs)) continue;
  for (const file of walk(abs)) {
    const rel = path.relative(ROOT, file);
    const text = readFileSync(file, "utf8");
    const lines = text.split("\n");
    lines.forEach((line, i) => {
      const m = line.match(/process\.env\.([A-Z0-9_]+)/g);
      if (!m) return;
      for (const token of m) {
        const name = token.slice("process.env.".length);
        // The bundler-enforced marker wins; the path regex is the backstop for a module nobody marked.
        const isClientModule = !text.includes('import "server-only"') && !text.includes('"use server"') && !serverOnly.test(rel);
        if (!name.startsWith("NEXT_PUBLIC_") && isClientModule && !rel.includes("lib/env")) {
          problems.push(`${rel}:${i + 1}: ${name} is not NEXT_PUBLIC_ but sits in a module a client can import`);
        }
      }
    });
  }
}

if (problems.length) {
  console.error("assert-env: FAIL");
  for (const p of problems) console.error("  " + p);
  console.error(`${problems.length} problem(s). A secret-shaped NEXT_PUBLIC_ variable is inlined into`);
  console.error("the client bundle by the bundler, not by an attacker: the prefix is the boundary.");
  process.exit(1);
}
console.log(`assert-env: ok (${declared.length} declared env keys checked, ${SCAN_DIRS.join("/")} scanned)`);
