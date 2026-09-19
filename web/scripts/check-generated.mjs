/**
 * "Type generation from the OpenAPI spec — no hand-written API types" (P08 D1) is only true if the generated
 * file is checked back against the spec it came from. This is that check: regenerate into a temp file and
 * diff. A hand-edited `.gen.ts` and a contract that moved without the client both fail here, which is the
 * only reason either rule survives a deadline.
 */
import { execFileSync } from "node:child_process";
import { readFileSync, mkdtempSync } from "node:fs";
import path from "node:path";
import os from "node:os";

const ROOT = path.resolve(import.meta.dirname, "..");
const SPEC = path.resolve(ROOT, "../contracts/openapi.yaml");
const SHIPPED = path.join(ROOT, "src/api/schema.gen.ts");
const tmp = mkdtempSync(path.join(process.env.TMPDIR || os.tmpdir(), "pgm-gen-"));
const out = path.join(tmp, "schema.gen.ts");

execFileSync("npx", ["--no-install", "openapi-typescript", SPEC, "-o", out, "--header", "generated"], { stdio: "inherit" });

const strip = (text) => text.split("\n").filter((l) => !/^\s*\*?\s*generated/i.test(l) && !l.startsWith("//")).join("\n").trim();
const shipped = readFileSync(SHIPPED, "utf8");
const fresh = readFileSync(out, "utf8");
if (strip(shipped) !== strip(fresh)) {
  const a = strip(shipped).split("\n");
  const b = strip(fresh).split("\n");
  let i = 0;
  while (i < Math.min(a.length, b.length) && a[i] === b[i]) i++;
  console.error(`check-generated: src/api/schema.gen.ts is out of sync with contracts/openapi.yaml`);
  console.error(`  first difference at line ${i + 1}`);
  console.error(`  shipped: ${a[i] ?? "(eof)"}`);
  console.error(`  spec   : ${b[i] ?? "(eof)"}`);
  console.error(`run \`npm run gen:api\` and commit the result — do not edit the generated file by hand.`);
  process.exit(1);
}
console.log(`check-generated: ok (${strip(fresh).split("\n").length} lines match the contract)`);
