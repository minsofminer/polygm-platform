/**
 * The key system from web/DESIGN.md §8: `screen.component.element[#variant][.state]`, en as the source
 * dictionary, and a build-time check that every key a component asks for exists (`scripts/i18n-check.mjs`
 * runs it, so "missing translation" never becomes a runtime string in a screenshot).
 *
 * `t()` returns the key when the entry is absent — that is deliberate. A silent fallback to English of a
 * *different* language would be the bug; here a missing key is visible in the DOM and fails the build.
 */
import { en } from "./en";

export function t(key: string, vars: Record<string, string | number> = {}): string {
  const template = (en as Record<string, string>)[key] ?? key;
  return template.replace(/\{(\w+)\}/g, (whole, name: string) => (name in vars ? String(vars[name]) : whole));
}
