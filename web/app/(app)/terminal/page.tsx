import { Tape } from "@/screens/Tape";
import { WidgetBoundary } from "@/ui/WidgetBoundary";
import { TradeTicket } from "@/screens/TradeTicket";
import { t } from "@/i18n/t";

export default function TerminalPage() {
  return (
    <div style={{ display: "grid", gap: "var(--pgm-space-4)" }}>
      <h1>{t("shell.nav.terminal")}</h1>
      <p className="refusal">
        The book, chart and ladder are P09/P10. The frame, the feed, the gate and the ticket are here, and the
        ticket refuses to send anything until the connection says the quote is current.
      </p>
      <WidgetBoundary label={t("shell.nav.trade")}>
        <TradeTicket />
      </WidgetBoundary>
      <Tape rows={12} />
    </div>
  );
}
