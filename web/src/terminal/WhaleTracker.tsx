"use client";
/**
 * D4 · the whale tracker.
 *
 * Three things have to be true at once for this screen to be honest, and the layout is arranged around them:
 *
 *  * the **threshold** is per market and its sentence is on every row, because a global line would be wrong in
 *    both directions;
 *  * the **severity formula** is stated above the feed and not inferred from the colours — a red badge with no
 *    rule is a mood, not a measurement;
 *  * a **saved view** is the same knobs the user is looking at, so saving and loading are the same object. The
 *    form refuses to offer a channel without a market before the request, because the API refuses it after.
 */
import { useCallback, useMemo, useState } from "react";
import { t } from "@/i18n/terminal";
import { freshnessOf } from "@/api/envelope";
import { Number } from "@/num/Number";
import { StaleIndicator } from "@/num/StaleIndicator";
import { Button } from "@/ui/Button";
import { formatMicro, formatMultiple, priceUnitsFor, thresholdSentence, walletHref } from "./tape";
import { useCreateWhaleView, useNow, useWhaleViews, useWhales } from "./useTerminal";
import {
  CHANNELS,
  EMPTY_WHALE_FILTERS,
  SEVERITIES,
  bucketDefaults,
  budgetText,
  exportTargets,
  feedCounts,
  filtersToBody,
  notifyRefusal,
  parseMultiple,
  ratioText,
  severityTone,
  viewSummary,
  viewToFilters,
  type Channel,
  type Severity,
  type WhaleFilters,
} from "./whales";

/** Literal tables, for the same reason as the dossier's: a computed key cannot be checked by the build. */
const SEVERITY_LABEL: Record<string, string> = {
  info: t("terminal.whales.severityLevel.info"),
  notice: t("terminal.whales.severityLevel.notice"),
  urgent: t("terminal.whales.severityLevel.urgent"),
};

const CHANNEL_LABEL: Record<string, string> = {
  telegram: t("terminal.whales.channel.telegram"),
  email: t("terminal.whales.channel.email"),
  webhook: t("terminal.whales.channel.webhook"),
};

const SIDE_LABEL: Record<string, string> = {
  BUY: t("terminal.whales.side.buy"),
  SELL: t("terminal.whales.side.sell"),
};

const EXPORT_LABEL: Record<string, string> = {
  watchlist: t("terminal.whales.export.watchlist"),
  follow: t("terminal.whales.export.follow"),
  copy: t("terminal.whales.export.copy"),
};

export function WhaleTracker({ markets }: { markets: { marketId: string; question: string }[] }) {
  const [filters, setFilters] = useState<WhaleFilters>(EMPTY_WHALE_FILTERS);
  const [name, setName] = useState("");
  const [channel, setChannel] = useState<Channel | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const { rows, counts, severityRule, stamp, err, loading } = useWhales(filters);
  const { views, refresh, err: viewsErr } = useWhaleViews();
  const { create, busy } = useCreateWhaleView();
  const now = useNow(1_000);
  const fresh = freshnessOf(stamp, now);
  const refusal = useMemo(() => notifyRefusal(filters, channel), [filters, channel]);

  const visible = useMemo(
    () => (filters.label ? rows.filter((r) => (r.labels ?? []).some((l) => l.label === filters.label)) : rows),
    [rows, filters.label],
  );

  const loadView = useCallback((view: (typeof views)[number]) => {
    setFilters({ ...viewToFilters(view), label: "" });
    setName(view.name);
    setChannel((view.channel as Channel | null) ?? null);
    setNote(t("terminal.whales.viewLoaded", { name: view.name }));
  }, []);

  const saveView = useCallback(async () => {
    const body = filtersToBody(name, filters, channel);
    const out = await create(body);
    if (!out.ok) {
      setNote(out.error);
      return;
    }
    setNote(out.view.notifies
      ? t("terminal.whales.savedNotifies", { name: out.view.name, channel: out.view.channel ?? "" })
      : t("terminal.whales.savedFilter", { name: out.view.name }));
    await refresh();
  }, [channel, create, filters, name, refresh]);

  return (
    <section className="pgm-whales" aria-label={t("terminal.whales.label")}>
      <header className="pgm-whales__head">
        <h1>{t("terminal.whales.title")}</h1>
        <StaleIndicator freshness={fresh} ageMs={stamp ? Math.max(0, now - stamp.asOf) : null} />
      </header>

      {/* The rule, stated where it is used. Every row below carries its own market's version of this sentence. */}
      <p className="pgm-whales__rule" role="note">
        {severityRule || t("terminal.whales.rulePending")}
      </p>

      <div className="pgm-whales__modes" role="group" aria-label={t("terminal.whales.mode")}>
        <button type="button" aria-pressed={filters.scope === "global"} onClick={() => setFilters({ ...filters, scope: "global", marketId: "" })}>
          {t("terminal.whales.mode.global")}
        </button>
        <button type="button" aria-pressed={filters.scope === "market"} onClick={() => setFilters({ ...filters, scope: "market" })}>
          {t("terminal.whales.mode.market")}
        </button>
        {filters.scope === "market" ? (
          <label>
            {t("terminal.whales.market")}
            <select value={filters.marketId} onChange={(e) => setFilters({ ...filters, marketId: e.target.value })}>
              <option value="">{t("terminal.whales.pickMarket")}</option>
              {markets.map((m) => (
                <option key={m.marketId} value={m.marketId}>{m.question || m.marketId}</option>
              ))}
            </select>
          </label>
        ) : null}
        <label>
          {t("terminal.whales.severity")}
          <select value={filters.minSeverity} onChange={(e) => setFilters({ ...filters, minSeverity: e.target.value as Severity })}>
            {SEVERITIES.map((s) => (
              <option key={s} value={s}>{SEVERITY_LABEL[s]}</option>
            ))}
          </select>
        </label>
        <label title={t("terminal.whales.multipleHint")}>
          {t("terminal.whales.multiple")}
          <input
            type="number"
            min={2}
            max={1000}
            value={filters.multiple ?? ""}
            placeholder={t("terminal.whales.multiplePlaceholder")}
            onChange={(e) => setFilters({ ...filters, multiple: parseMultiple(e.target.value) })}
          />
        </label>
      </div>

      <p className="pgm-whales__counts" role="status">
        {loading ? t("terminal.whales.loading") : feedCounts(counts)}
        {filters.multiple ? ` · ${t("terminal.whales.multipleActive", { n: filters.multiple })}` : ""}
      </p>
      {err ? <p className="pgm-whales__error" role="status">{err}</p> : null}

      <ul className="pgm-whales__feed">
        {visible.map((row) => (
          <li key={`${row.tokenId}-${row.tsMs}-${row.anonWallet}`} className={`pgm-whales__row is-${severityTone(row.severity)}`}>
            <span className="pgm-whales__ratio" title={row.rule}>
              <strong>{ratioText(row.ratioBps)}</strong>
              <em>{SEVERITY_LABEL[severityTone(row.severity)]}</em>
            </span>
            <a className="pgm-whales__wallet" href={walletHref(row.anonWallet)}>{row.anonWallet}</a>
            {(row.labels ?? []).map((fact) => (
              // The chip names the label; the sentence under the row states the rule and what the label does NOT
              // claim. Both are text on the screen: a classification a user can only read by hovering is a
              // classification they will screenshot without its caveat.
              <span key={fact.label} className="pgm-badge" title={`${fact.rule} · ${fact.disclaimer}`}>
                {fact.label}
                <small>{`${fact.rule} · ${fact.disclaimer}`}</small>
              </span>
            ))}
            <a className="pgm-whales__market" href={`/market/${encodeURIComponent(row.marketId)}`}>{row.question || row.marketId}</a>
            <span className="pgm-whales__side">{SIDE_LABEL[row.side] ?? row.side}</span>
            <span className="pgm-whales__outcome">{row.outcome}</span>
            <Number kind="price" value={priceUnitsFor(row.price, row.tick)} tick={row.tick} freshness={fresh} />
            <span className="pgm-whales__notional">
              {formatMicro(row.notionalMicro)}
              <small> · {t("terminal.whales.threshold", { multiple: formatMultiple(row.ratioBps), value: formatMicro(row.thresholdMicro) })}</small>
            </span>
            {/* The threshold sentence, verbatim from the server: the same text the tape's tooltip shows, because a
                second rendering of one rule is how two surfaces start disagreeing. */}
            <span className="pgm-whales__why" title={thresholdSentence(row)}>{thresholdSentence(row)}</span>
            <span className="pgm-whales__export">
              {exportTargets(row).map((target) => (
                <a key={target.id} href={target.href} title={target.note}>{EXPORT_LABEL[target.id]}</a>
              ))}
            </span>
          </li>
        ))}
      </ul>

      <section className="pgm-whales__views" aria-label={t("terminal.whales.views")}>
        <h2>{t("terminal.whales.views")}</h2>
        {viewsErr ? <p role="status">{viewsErr}</p> : null}
        <ul>
          {views.map((view) => (
            <li key={view.viewId}>
              <button type="button" onClick={() => loadView(view)}>{t("terminal.whales.loadView")}</button>
              <span>{viewSummary(view)}</span>
              <span className="pgm-whales__budget">{budgetText(view)}</span>
            </li>
          ))}
          {views.length === 0 ? <li>{t("terminal.whales.noViews")}</li> : null}
        </ul>

        <form
          className="pgm-whales__form"
          onSubmit={(e) => {
            e.preventDefault();
            void saveView();
          }}
        >
          <label>
            {t("terminal.whales.name")}
            <input value={name} onChange={(e) => setName(e.target.value)} maxLength={60} placeholder={t("terminal.whales.namePlaceholder")} />
          </label>
          <label>
            {t("terminal.whales.channel")}
            <select value={channel ?? ""} onChange={(e) => setChannel((e.target.value || null) as Channel | null)}>
              <option value="">{t("terminal.whales.channel.none")}</option>
              {CHANNELS.map((c) => (
                <option key={c} value={c}>{CHANNEL_LABEL[c]}</option>
              ))}
            </select>
          </label>
          <Button type="submit" disabled={!name.trim() || busy || Boolean(refusal)} why={refusal ?? undefined}>
            {t("terminal.whales.save")}
          </Button>
          {/* The refusal is beside the control that causes it, before the request: a form that can be filled in and
              then rejected is a form that wasted the user's attention. */}
          {refusal ? <p className="pgm-whales__refusal" role="note">{refusal}</p> : null}
        </form>
        {note ? <p role="status" className="pgm-whales__note">{note}</p> : null}
      </section>

      <section className="pgm-whales__buckets" aria-label={t("terminal.whales.buckets")}>
        <h2>{t("terminal.whales.buckets")}</h2>
        <ul>
          {bucketDefaults().map((b) => (
            <li key={b.id}>{b.sentence}</li>
          ))}
        </ul>
      </section>
    </section>
  );
}

/** The market picker's options, from the tape's own facets: the markets with fills, biggest first. */
export function marketOptions(facets: { markets?: { marketId: string; question: string }[] } | null | undefined) {
  return (facets?.markets ?? []).map((m) => ({ marketId: m.marketId, question: m.question }));
}
