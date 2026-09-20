/**
 * Mirror `contracts/startapp.json` into a TypeScript module the web app can import.
 *
 * Why a generated mirror instead of importing the JSON directly: the contract lives above the web project's root, and
 * Next resolves imports inside its own root only (`externalDir` is deliberately off — see next.config.mjs, where the
 * same reasoning is recorded for `brand/tokens.css`). Reaching outside the root works until the day the build system
 * changes its mind, and the failure arrives as a bundler error nobody connects to a deep link.
 *
 * So the contract is mirrored, and the mirror is checked: `--check` fails if the committed file differs from what the
 * contract produces, and the web suite's `pretest` runs it. Drift between the parser and the emitter is therefore a
 * red build, not a dead trade button.
 *
 * Usage: node tools/build-startapp-contract.mjs [--check]
 */
import { readFileSync, writeFileSync, existsSync } from "node:fs";
import path from "node:path";

const ROOT = path.resolve(import.meta.dirname, "..");
const CONTRACT = path.join(ROOT, "contracts", "startapp.json");
const TARGET = path.join(ROOT, "web", "src", "telegram", "startapp.gen.ts");
const CHECK = process.argv.includes("--check");

const c = JSON.parse(readFileSync(CONTRACT, "utf8"));

const banner = `/**
 * GENERATED — do not edit. \`node tools/build-startapp-contract.mjs\` writes this file from
 * \`contracts/startapp.json\`, which is the single source both this app and the Python bot read.
 *
 * The contract is mirrored rather than imported because the web project cannot import above its own root. The mirror
 * is verified (\`npm run gen:startapp -- --check\`, run by \`pretest\`), so a change to the grammar that is not a change
 * to both sides fails the suite instead of shipping a link that the parser on the other side rejects.
 */
`;

const body = `${banner}
export type StartappContract = {
  readonly grammar: string;
  readonly separator: string;
  readonly tags: Readonly<Record<string, string>>;
  readonly valueCharsetLiteral: string;
  readonly valueMaxLen: number;
  readonly payloadMaxLen: number;
  readonly examples: Readonly<Record<string, string>>;
};

export const STARTAAPP_CONTRACT: StartappContract = ${JSON.stringify(
  {
    grammar: c.grammar,
    separator: c.separator,
    tags: c.tags,
    valueCharsetLiteral: c.value_charset_literal,
    valueMaxLen: c.value_max_len,
    payloadMaxLen: c.payload_max_len,
    examples: c.examples,
  },
  null,
  2,
)} as const;
`;

if (CHECK) {
  if (!existsSync(TARGET) || readFileSync(TARGET, "utf8") !== body) {
    console.error("startapp contract: FAIL — web/src/telegram/startapp.gen.ts does not match contracts/startapp.json");
    console.error("  run: node tools/build-startapp-contract.mjs");
    process.exit(1);
  }
  console.log("startapp contract: ok (mirror matches the contract)");
} else {
  writeFileSync(TARGET, body);
  console.log(`startapp contract: wrote ${path.relative(ROOT, TARGET)} (grammar ${c.grammar}, charset ${c.value_charset_literal.length} chars)`);
}
