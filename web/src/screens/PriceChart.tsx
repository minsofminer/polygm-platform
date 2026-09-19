"use client";
import { useMemo, useState } from "react";
import { t } from "@/i18n/t";
import { deriveCandles, INTERVAL_MS, type Candle, type Interval } from "@/lib/ladders";
import { microOf, microToDecimal } from "@/lib/depth";

/**
 * The price chart. Probability on the primary axis, because that is how prediction markets are priced and how
 * their users think: "the market says 34%" is the sentence, and "0.34" is a spelling of it.
 *
 * The two views the prompt asks for that are NOT a bigger version of the same chart:
 *  - **the short-lifecycle view.** A five-minute market whose whole life is one candle under a 1d axis. Below
 *    `SHORT_LIFE_MS` of remaining life the chart switches to the narrowest server interval and drops the
 *    histogram, because a volume histogram over one bucket is a bar chart of "all of it".
 *  - **gaps stay gaps.** A bucket with no fills is absent from the series, and the line is drawn as separate
 *    segments. A continuous line through a bucket nobody traded in is the single most convincing lie a price
 *    chart can tell.
 */
export const SHORT_LIFE_MS = 30 * 60 * 1000;
const WIDTH = 720;
const HEIGHT = 240;
const PAD = 8;

export type Marker = { t: number; priceMicro: number; side: "BUY" | "SELL"; label?: string };
export type Annotation = { t: number; kind: "created" | "resolved" | "dispute"; label: string };

export function PriceChart({
  candles,
  interval,
  endTs,
  markers = [],
  annotations = [],
  averageCostMicro,
  entryMicro,
  legend,
}: {
  candles: Candle[];
  interval: Interval;
  endTs?: number | null;
  markers?: Marker[];
  annotations?: Annotation[];
  /** The user's own average cost and entry, drawn so their position is visible against the market's. */
  averageCostMicro?: number | null;
  entryMicro?: number | null;
  legend?: string;
}) {
  const [shape, setShape] = useState<"candle" | "line">("candle");

  const series = useMemo(() => {
    if (interval === "6h" || interval === "1d") return deriveCandles(candles, interval);
    return candles;
  }, [candles, interval]);

  const geometry = useMemo(() => {
    if (series.length === 0) return null;
    const lows = series.map((c) => microOf(c.l));
    const highs = series.map((c) => microOf(c.h));
    const notional = series.map((c) => microOf(c.notional));
    const width = WIDTH - PAD * 2;
    const height = HEIGHT - PAD * 2;
    const min = Math.min(...lows);
    const max = Math.max(...highs);
    const span = Math.max(1, max - min);
    const step = width / Math.max(1, series.length);
    const x = (index: number) => PAD + index * step + step / 2;
    const y = (micro: number) => PAD + height - ((micro - min) / span) * height;
    return { min, max, step, x, y, width, height, peakNotional: Math.max(1, ...notional) };
  }, [series]);

  const remaining = endTs ? endTs - Date.now() : null;
  const shortLife = remaining !== null && remaining > 0 && remaining <= SHORT_LIFE_MS;

  const EMPTY_TEXT = { short: t("markets.chart.shortLife.empty"), long: t("markets.chart.empty") };
  if (!geometry) {
    return (
      <section aria-label={t("markets.chart.title")}>
        <p className="refusal" role="status">
          {shortLife ? EMPTY_TEXT.short : EMPTY_TEXT.long}
        </p>
      </section>
    );
  }

  // Split the series wherever a bucket is missing, so the drawn line has a hole in it instead of a straight
  // edge across a period nobody traded. `previous` is tracked rather than indexed, because the last element of
  // the previous segment is exactly the thing a `segments[segments.length - 1]` lookup gets wrong when the
  // series starts mid-gap.
  const segments: Candle[][] = [];
  let previous: Candle | null = null;
  for (const candle of series) {
    if (previous === null || candle.t - previous.t > INTERVAL_MS[interval]) segments.push([candle]);
    else segments[segments.length - 1]?.push(candle);
    previous = candle;
  }

  return (
    <section aria-label={t("markets.chart.title")}>
      <header className="pgm-chart-head">
        <h2>{t("markets.chart.title")}</h2>
        <div role="group" aria-label={t("markets.chart.shape")} className="pgm-segmented">
          <button type="button" aria-pressed={shape === "candle"} onClick={() => setShape("candle")}>
            {t("markets.chart.candles")}
          </button>
          <button type="button" aria-pressed={shape === "line"} onClick={() => setShape("line")}>
            {t("markets.chart.line")}
          </button>
        </div>
      </header>

      {shortLife ? <p className="pgm-fine" role="status">{t("markets.chart.shortLife.notice")}</p> : null}
      {legend ? <p className="pgm-fine">{legend}</p> : null}

      <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} role="img" aria-label={t("markets.chart.alt", { count: String(series.length) })}
           className="pgm-chart">
        {segments.map((segment, index) =>
          shape === "line" ? (
            <polyline
              key={segment[0]?.t ?? index}
              className="pgm-chart-line"
              points={segment
                .map((candle) => `${geometry.x(series.indexOf(candle))},${geometry.y(microOf(candle.c))}`)
                .join(" ")}
              data-segment={index}
            />
          ) : (
            segment.map((candle) => {
              const i = series.indexOf(candle);
              const open = microOf(candle.o);
              const close = microOf(candle.c);
              const top = geometry.y(Math.max(open, close));
              const bottom = geometry.y(Math.min(open, close));
              return (
                <g key={candle.t} className={close >= open ? "pgm-chart-up" : "pgm-chart-down"}>
                  <line x1={geometry.x(i)} x2={geometry.x(i)} y1={geometry.y(microOf(candle.h))}
                        y2={geometry.y(microOf(candle.l))} />
                  <rect x={geometry.x(i) - geometry.step / 4} width={Math.max(1, geometry.step / 2)}
                        y={top} height={Math.max(1, bottom - top)} />
                  {/* the histogram is omitted on a short-life market: one bucket of volume is all of it */}
                  {!shortLife ? (
                    <rect className="pgm-chart-volume" x={geometry.x(i) - geometry.step / 4}
                          width={Math.max(1, geometry.step / 2)}
                          y={PAD + geometry.height - (microOf(candle.notional) / geometry.peakNotional) * geometry.height / 4}
                          height={(microOf(candle.notional) / geometry.peakNotional) * geometry.height / 4} />
                  ) : null}
                </g>
              );
            })
          ),
        )}

        {averageCostMicro ? (
          <line className="pgm-chart-avg" x1={PAD} x2={WIDTH - PAD} y1={geometry.y(averageCostMicro)}
                y2={geometry.y(averageCostMicro)} />
        ) : null}
        {entryMicro ? <circle className="pgm-chart-entry" cx={PAD} cy={geometry.y(entryMicro)} r={3} /> : null}

        {markers.map((marker) => {
          const index = series.findIndex((candle) => candle.t <= marker.t && marker.t < candle.t + INTERVAL_MS[interval]);
          if (index < 0) return null;
          return (
            <circle
              key={`${marker.t}-${marker.side}`}
              className={marker.side === "BUY" ? "pgm-chart-buy" : "pgm-chart-sell"}
              cx={geometry.x(index)}
              cy={geometry.y(marker.priceMicro)}
              r={2}
            >
              <title>{marker.label ?? marker.side}</title>
            </circle>
          );
        })}

        {annotations.map((annotation) => {
          const index = series.findIndex((candle) => candle.t <= annotation.t && annotation.t < candle.t + INTERVAL_MS[interval]);
          if (index < 0) return null;
          return (
            <line
              key={`${annotation.kind}-${annotation.t}`}
              className={`pgm-chart-mark pgm-chart-mark-${annotation.kind}`}
              x1={geometry.x(index)}
              x2={geometry.x(index)}
              y1={PAD}
              y2={PAD + geometry.height}
            >
              <title>{annotation.label}</title>
            </line>
          );
        })}
      </svg>
      <p className="pgm-fine">
        {t("markets.chart.axis", { low: microToDecimal(geometry.min, 3), high: microToDecimal(geometry.max, 3) })}
      </p>
    </section>
  );
}
