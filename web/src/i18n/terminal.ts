/**
 * The terminal dictionary's entry point: importing this module is what puts `terminal.*` copy in a route.
 *
 * Terminal components and the pages that render them import `t` from here rather than from `@/i18n/t`. That is
 * not a style preference — it is the load-bearing part of the split. A file that imports `@/i18n/t` alone and
 * asks for `terminal.tape.empty` would render the literal key, because the dictionary it needs has never been
 * registered in that route's graph. `scripts/i18n-check.mjs` fails the build on exactly that combination, so
 * the mistake is caught at check time rather than in a screenshot.
 */
import { registerDictionary } from "./registry";
import { enTerminal } from "./en.terminal";

registerDictionary(enTerminal);

export { t } from "./t";
