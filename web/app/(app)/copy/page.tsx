import { t } from "@/i18n/t";
import { CopyView } from "@/terminal/CopyView";

export const dynamic = "force-dynamic";

/**
 * Copy trading — D7.
 *
 * The screen is a client surface (it polls a monitor and writes configs), so the route is thin and the phase's
 * two ordering rules live in `CopyView`: the ranking is stated before the list, and the slippage warning is
 * rendered before the confirm. The route reads one query parameter — `source` — because the whale tracker's
 * export and a trader's dossier both link here with a wallet already chosen, and re-finding them by hand in a
 * list is the kind of step that makes a feature unused.
 */
export default async function CopyPage({ searchParams }: { searchParams: Promise<{ source?: string }> }) {
  const params = await searchParams;
  return (
    <main className="pgm-page">
      <h1>{t("terminal.copy.pageTitle")}</h1>
      <CopyView initialSource={typeof params.source === "string" ? params.source : ""} />
    </main>
  );
}
