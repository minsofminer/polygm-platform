/**
 * What the contract says about a session: the routes it serves to nobody, and the ones it gates behind an admin.
 *
 * This is a *reader of the contract*, not part of the app: nothing under `app/` imports it, and its only callers
 * are the two suites that hold the route ledger against `contracts/openapi.yaml`
 * (`src/api/public-reads.test.ts` and `src/auth/anonymous-read.test.ts`). It lives here rather than inside one of
 * them because two tests parsing the same file two ways is how the two of them would come to disagree, and the
 * whole point of the pair is that the ledger and the contract cannot.
 *
 * The parser is deliberately small and strict — this repository's layout (a two-space `/v1/...` key, a four-space
 * method, a six-space `x-auth`) — and the suites assert it found the routes it expects, so a layout change fails a
 * test rather than quietly checking nothing.
 */
import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";

/** `x-auth` values that mean "no session is required": `none` (no credential) and `public` (served to everyone). */
export const NO_SESSION_NEEDED = new Set(["none", "public"]);

export type ContractRoute = { method: string; path: string; auth: string };

/** Where the contract is, from wherever the suite is running (the suite always runs from `web/`). */
export function contractPath(): string {
  const root = resolve(process.cwd(), "..");
  return existsSync(resolve(root, "contracts", "openapi.yaml"))
    ? resolve(root, "contracts", "openapi.yaml")
    : resolve(process.cwd(), "contracts", "openapi.yaml");
}

/** Every operation in the contract that declares an `x-auth`. */
export function contractRoutes(): ContractRoute[] {
  const lines = readFileSync(contractPath(), "utf8").split("\n");
  const rows: ContractRoute[] = [];
  let currentPath: string | null = null;
  for (let i = 0; i < lines.length; i += 1) {
    const pathMatch = /^ {2}(\/v1\/[^:]+):\s*$/.exec(lines[i] ?? "");
    if (pathMatch) {
      currentPath = pathMatch[1] ?? null;
      continue;
    }
    const methodMatch = /^ {4}(get|post|put|delete|patch):\s*$/.exec(lines[i] ?? "");
    if (!methodMatch || currentPath === null) continue;
    for (let j = i + 1; j < lines.length; j += 1) {
      const line = lines[j] ?? "";
      if (/^ {4}\S/.test(line) || /^ {2}\/v1\//.test(line)) break;      // the operation ended without an x-auth
      const authMatch = /^ {6}x-auth:\s*(\S+)/.exec(line);
      if (authMatch) {
        rows.push({ method: methodMatch[1]!.toUpperCase(), path: currentPath, auth: authMatch[1] ?? "" });
        break;
      }
    }
  }
  return rows;
}

/** The reads a stranger can make: `GET`s the contract serves with no session. Writes are never anonymous reads. */
export function contractPublicReads(): ContractRoute[] {
  return contractRoutes().filter((r) => r.method === "GET" && NO_SESSION_NEEDED.has(r.auth));
}
