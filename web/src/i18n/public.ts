/**
 * The public pages' dictionary entry point — the mirror of `./terminal.ts`, for the routes a stranger opens.
 *
 * Importing this module is what puts `public.*` copy in a route's graph; a component that asks for one of those
 * keys while importing `@/i18n/t` renders the key itself, and `scripts/i18n-check.mjs` fails on exactly that.
 */
import { registerDictionary } from "./registry";
import { enPublic } from "./en.public";

registerDictionary(enPublic);

export { t } from "./t";
