/**
 * The pair of reads the automation console needs, in one place, **in parallel**.
 *
 * The two calls were sequential in the page: `await list; await catalog;`. They are independent — neither
 * argument is derived from the other — so the page paid the sum of two round trips when it only owed the
 * slower one. Vercel's `async-parallel` rule is the reason, but the reason the rule exists is the one worth
 * writing down: on a cold API each read is a real HTTP hop through the proxy, and this page is on the way to
 * the screen that stops trading.
 *
 * The reader is a **parameter** rather than an import, and that is not ceremony: `serverRead` pulls in
 * `server-only`, which cannot be imported from a test, so a module that imported it could only be checked by
 * reading its source. Passing the reader in makes the parallelism a thing a test can actually observe — start
 * order, and whether the second read began before the first resolved.
 *
 * The two results stay separate (`{ list, catalog }`, not a merged object) because the page's error handling
 * depends on it: a failed catalog must not blank the rule list.
 */
import type { ServerRead } from "@/api/server-read";
import type { AutomationList, TemplateCatalog } from "@/terminal/wire";

/** The shape of a server read, structurally — so this module never has to import the transport itself. */
export type Reader = <T>(method: "GET", path: string) => Promise<ServerRead<T>>;

export async function loadAutomation(read: Reader): Promise<{
  list: ServerRead<AutomationList>;
  catalog: ServerRead<TemplateCatalog>;
}> {
  const [list, catalog] = await Promise.all([
    read<AutomationList>("GET", "/v1/automations"),
    read<TemplateCatalog>("GET", "/v1/automations/templates"),
  ]);
  return { list, catalog };
}
