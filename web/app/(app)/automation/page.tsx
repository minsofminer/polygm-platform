import { t } from "@/i18n/terminal";
import { serverRead } from "@/api/server-read";
import { AutomationView } from "@/terminal/AutomationView";
import type { AutomationList, TemplateCatalog } from "@/terminal/wire";

export const dynamic = "force-dynamic";

/**
 * The automation console — D8.
 *
 * Both reads happen on the server because both carry stamps a screen must not re-derive: the rule list carries
 * the halt state (which stops every money path, so it must not depend on a client round trip to appear) and the
 * template catalog carries the fee arithmetic that decides whether the 5-minute crypto entry is offered at all.
 * The mutations are client actions, because each one spends a dry run or arms a rule.
 */
export default async function AutomationPage() {
  const list = await serverRead<AutomationList>("GET", "/v1/automations");
  const catalog = await serverRead<TemplateCatalog>("GET", "/v1/automations/templates");
  return (
    <main className="pgm-page">
      {list.ok === false ? (
        <p className="unavailable" role="status">
          <span>{t("terminal.automation.listUnavailable")}</span>
        </p>
      ) : null}
      <AutomationView initial={list.ok ? list.data : null} catalog={catalog.ok ? catalog.data : null} />
    </main>
  );
}
