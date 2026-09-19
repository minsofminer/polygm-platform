/**
 * THE number component. No component in this app renders a raw number: the P08 gate c4 fails the build on a
 * numeric literal or a `.toFixed()`/`Intl.NumberFormat` call in JSX anywhere else under `web/src`.
 *
 * What it guarantees, all of it behavioural:
 *  - tabular figures and right alignment, so digits do not shuffle sideways as they change (web/DESIGN.md
 *    §9's "no layout shift from live data": tabular figures make same-magnitude updates shift-free, and a
 *    change in digit count is a real change in width, not something to hide);
 *  - money and sizes render through `src/money/cents.ts`, so precision comes from the tick and floats cannot
 *    participate;
 *  - a change flashes the background only, per `src/num/flash.ts`;
 *  - an unguarded price does not exist: any price/size is accompanied by `StaleIndicator` when the caller
 *    supplies a freshness, and the component refuses to render a `price` kind without a tick, because
 *    defaulting to 2dp is how a 0.001 market gets displayed as 0.42 when it is 0.425.
 */
"use client";

import { useEffect, useRef, useState } from "react";
import { formatNumber, type TickSize } from "@/money/cents";
import { decideFlash, type Direction } from "./flash";
import { StaleIndicator } from "./StaleIndicator";
import type { Freshness } from "@/api/envelope";
import { t } from "@/i18n/t";

export type NumberProps = {
  kind: "money" | "price" | "size" | "pnl" | "percent" | "count";
  /** Cents for money/pnl; tick units for price (integer); raw integer for size/count. */
  value: number;
  previous?: number | null;
  tick?: TickSize;
  signed?: boolean;
  /** Dot-less price: ".42" for the ladder, where the leading zero repeats 20 times a second. */
  compact?: boolean;
  freshness?: Freshness;
  staleMs?: number | null;
  /** "rest" disables flashing by policy, at the component that could otherwise lie. */
  source?: "ws" | "rest";
  direction?: Direction;
  className?: string;
  label?: string;
};

export function Number(props: NumberProps) {
  // No `decimals` prop, on purpose: precision is a property of the kind and the market's tick, never of the
  // caller's taste. A screen that can choose its own rounding can round a 0.001 market to 0.42.
  const { kind, value, previous = null, tick, signed, compact, freshness, staleMs, source = "ws", className = "", label } = props;
  const ref = useRef<HTMLSpanElement | null>(null);
  const lastFlashAt = useRef<number | null>(null);
  const [flash, setFlash] = useState<Direction | null>(null);

  let text: string;
  let renderError: string | null = null;
  try {
    if (kind === "price" && !tick) throw new Error("a price needs a tick: precision is the market's, not ours");
    text = formatNumber({
      kind,
      value,
      tick,
      priceUnits: kind === "price" ? value : undefined,
      compact,
      signed,
    });
  } catch (cause) {
    text = "—";
    renderError = cause instanceof Error ? cause.message : String(cause);
  }

  // Per-frame writes go to the element, not to state (web/DESIGN.md §4). `setFlash` is called at most once
  // per 120ms per cell by the policy, which is the only reason state is tolerable here at all.
  useEffect(() => {
    const el = ref.current;
    if (!el || renderError) return;
    const decision = decideFlash({
      prev: previous,
      next: value,
      nowMs: Date.now(),
      lastFlashAtMs: lastFlashAt.current,
      source,
      ...(props.direction ? { pin: props.direction } : {}),
    });
    if (!decision.flash) return;
    lastFlashAt.current = Date.now();
    el.dataset.flash = decision.flash;
    setFlash(decision.flash);
    const clear = window.setTimeout(() => {
      delete el.dataset.flash;
      setFlash(null);
    }, 320);
    return () => window.clearTimeout(clear);
  }, [value, previous, source, renderError, props.direction]);

  const cls = [
    "num",
    kind === "pnl" ? "num--signed" : "",
    freshness && freshness !== "live" ? "num--stale" : "",
    flash ? `num--flash-${flash}` : "",
    className,
  ].filter(Boolean).join(" ");

  return (
    <span
      ref={ref}
      className={cls}
      data-flash={flash ?? undefined}
      data-kind={kind}
      data-freshness={freshness ?? "unstamped"}
      title={renderError ?? undefined}
      aria-label={label ? `${label}: ${text}` : undefined}
    >
      {kind === "pnl" ? <PnlGlyph value={value} /> : null}
      <span className="num__value">{text}</span>
      {freshness && freshness !== "live" ? <StaleIndicator freshness={freshness} ageMs={staleMs ?? null} /> : null}
    </span>
  );
}

/**
 * The direction marker for PnL. Colour is not the signal: profit and loss differ by ΔL 0.040 in dark and
 * 0.008 in light (web/DESIGN.md §2), so the magnitude carries the text colour and this carries the
 * meaning, in a glyph plus the sign that `formatNumber` already put in the string.
 */
function PnlGlyph({ value }: { value: number }) {
  const dir = value > 0 ? "up" : value < 0 ? "down" : "flat";
  return (
    <span className="pnl-glyph" data-dir={dir} aria-hidden="true">
      {dir === "up" ? "▲" : dir === "down" ? "▼" : "◆"}
    </span>
  );
}

export function NumberList({ items }: { items: NumberProps[] }) {
  return (
    <div className="num-list" role="group" aria-label={t("num.list.grouped")}>
      {items.map((item, i) => (
        <Number key={i} {...item} />
      ))}
    </div>
  );
}
