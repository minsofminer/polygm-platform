/**
 * Where route-family dictionaries register themselves, so `t()` can stay a synchronous function.
 *
 * The alternative was a hook (`useT`) or async loading, and both change every call site in the app to buy
 * something nobody asked for: the terminal's copy only has to be *present* in the routes that render terminal
 * components, and a module-scope registration is enough for that. A dictionary arrives when its module is
 * imported, which is exactly when the route that imports it needs it — and the bundler reads the same edge, so
 * the bytes follow the same path as the code.
 *
 * Order is not significant: base keys are looked up in `en` first, and a later registration never replaces an
 * earlier one, so two dictionaries cannot quietly disagree about the same key (the i18n check would fail on a
 * duplicate anyway, since it reads both files as one key space).
 */
import { en } from "./en";

const extensions: Record<string, string>[] = [];

export function registerDictionary(dict: Record<string, string>): void {
  extensions.push(dict);
}

export function lookup(key: string): string | undefined {
  const base = (en as Record<string, string>)[key];
  if (base !== undefined) return base;
  for (let i = extensions.length - 1; i >= 0; i--) {
    const hit = extensions[i]?.[key];
    if (hit !== undefined) return hit;
  }
  return undefined;
}
