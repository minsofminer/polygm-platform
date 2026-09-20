/**
 * The internal dashboard's dictionary entry point — the mirror of `./public.ts`, for the operator's route.
 *
 * Importing this module is what puts `admin.*` copy in a route's graph; a component that asks for one of those
 * keys while importing `@/i18n/t` renders the key itself, and `scripts/i18n-check.mjs` fails on exactly that.
 */
import { registerDictionary } from "./registry";
import { enAdmin } from "./en.admin";

registerDictionary(enAdmin);

export { t } from "./t";
