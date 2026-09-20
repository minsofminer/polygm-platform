"use client";
/**
 * The TradeSheet: the Mini App's one job, and the screen the whole acquisition story points at.
 *
 * What it is designed to do, in order:
 *
 *  1. **Answer "what am I looking at" before anything else.** Question, both outcomes with their prices, the age of
 *     those prices, when it closes. No chart above the fold in a webview — a phone screen's first 300 pixels decide
 *     whether the rest is read.
 *  2. **Make the two decisions obvious and cheap**: which side, how much. Three size chips, one editable amount, no
 *     sliders (a slider cannot be typed into and cannot be read by a screen reader).
 *  3. **Show the whole cost before the tap**: the fee, and this position's worst case, which in a prediction market
 *     is the entire stake. `confirmCopy()` writes that sentence and the sheet renders it verbatim.
 *  4. **Never lie about the price.** The confirm copy names the age of the quote and says the order does not go if
 *     the price has moved — the same promise the chat makes, because it is the same promise.
 *
 * Motion and haptics follow `src/tma/motion.ts` and `src/tma/haptics.ts`: one animation per beat, nothing animating
 * a number, a buzz only on a fill or a refusal. The MainButton is the confirm (see `bridge.usesMainButton`), and the
 * in-page button is a secondary affordance, so the big bar keeps meaning exactly one thing everywhere in the app.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { tma, showMainButton } from "@/telegram/bridge";
import { CLASS, DUR, SEQUENCE } from "@/tma/motion";
import { haptic } from "@/tma/haptics";
import {
  amountFindings, blockers, chooseSize, close, confirmCopy, confirmKey, initial, open, plainRefusal,
  primaryAction, refused, SIZES, sharesFor, submitted, type MarketView, type SheetState, type Side,
} from "@/tma/trade";

export type OrderFn = (input: {
  slug: string;
  side: Side;
  amountUsdc: string;
  idempotencyKey: string;
}) => Promise<{ ok: true; intentId: string } | { ok: false; code: string; detail?: string }>;

export type TradeSheetProps = {
  market: MarketView;
  /** Injected so the component is testable without a network and so the page owns the session/token story. */
  place: OrderFn;
  /** The clipboard-free way to get the address into the customer's hands is the Mini App's own copy button. */
  onNeedDeposit?: () => void;
};

export function TradeSheet({ market, place, onNeedDeposit }: TradeSheetProps) {
  const [state, setState] = useState<SheetState>(() => initial("yes"));
  const [input, setInput] = useState("50");
  const [tick, setTick] = useState(0);          // bumps a CSS class so re-renders can re-run an animation
  const mounted = useRef(true);
  useEffect(() => () => {
    mounted.current = false;
  }, []);

  const price = state.side === "yes" ? market.yesAsk : market.noAsk;
  const shares = useMemo(() => sharesFor(input, price), [input, price]);
  const problems = amountFindings(input);
  const blocked = blockers({ endsSoon: market.endsSoon, yesAsk: market.yesAsk, noAsk: market.noAsk });
  const primary = primaryAction(state);

  const onConfirm = useCallback(
    async (token: string) => {
      setState((s) => ({ ...confirmKey(s, () => token), step: "submitting" }));
      setTick((t) => t + 1);
      const key = `tma-${token}`;
      const result = await place({ slug: market.slug, side: state.side, amountUsdc: input, idempotencyKey: key });
      if (!mounted.current) return;
      if (result.ok) {
        haptic("fill");
        setState((s) => submitted(s, result.intentId));
      } else {
        // The refusal path gets the error buzz and the nudge — a *small* one, because a violent shake on a money
        // error reads as a crash rather than as "no".
        haptic("reject");
        setState((s) => refused(s, result.code, plainRefusal(result.code, result.detail)));
      }
    },
    [input, market.slug, place, state.side],
  );

  // The MainButton is bound to whatever the primary action currently is; `showMainButton` returns its own teardown,
  // so the binding is stated once per state rather than toggled from three places.
  useEffect(() => {
    // `primaryAction` answers `null` while an order is in flight, which is why there is no "submitting" case here:
    // the MainButton is hidden rather than showing a label the user could tap twice.
    if (!primary) return;
    const release = showMainButton(primary.label, () => {
      if (primary.kind === "open") {
        setState((s) => (s.step === "closed" ? open(s, s.side) : chooseSize(s, input)));
      } else if (primary.kind === "confirm") {
        void onConfirm(crypto.randomUUID().replace(/-/g, "").slice(0, 16));
      } else if (primary.kind === "retry") {
        setState((s) => chooseSize(s, input));
      } else {
        setState((s) => close(s));
      }
    });
    return release;
  }, [primary, input, onConfirm]);

  const reduce = typeof window !== "undefined" && window.matchMedia
    ? window.matchMedia("(prefers-reduced-motion: reduce)").matches
    : false;

  return (
    <section className="pgm-tma-card" data-step={state.step}>
      <header>
        <h1>{market.question}</h1>
        <p role="status">
          <span>YES {market.yesAsk}</span> · <span>NO {market.noAsk}</span> · spread {market.spread}
        </p>
        {/* The age travels with the price on every surface in this product, and the Mini App is not an exception. */}
        <p><em>{market.ageText}</em>{market.closesText ? ` · closes ${market.closesText}` : ""}</p>
      </header>

      {blocked.length > 0 ? (
        <p className={tick % 2 === 0 ? CLASS.reject : undefined} role="alert">{blocked.join(" ")}</p>
      ) : null}

      <div role="group" aria-label="Choose a side">
        {(["yes", "no"] as Side[]).map((side) => (
          <button
            key={side}
            type="button"
            aria-pressed={state.side === side}
            onClick={() => setState((s) => (s.step === "closed" ? open(s, side) : { ...s, side }))}
          >
            {side === "yes" ? `Buy YES ${market.yesAsk}` : `Buy NO ${market.noAsk}`}
          </button>
        ))}
      </div>

      {state.step !== "closed" ? (
        <div className={CLASS.sheet} role="dialog" aria-label="Confirm your order">
          <span className={CLASS.grabber} aria-hidden="true" />
          <div role="group" aria-label="Choose an amount">
            {SIZES.map((size) => (
              <button key={size} type="button" aria-pressed={input === size}
                      onClick={() => { setInput(size); setState((s) => chooseSize(s, size)); }}>
                ${size}
              </button>
            ))}
          </div>
          <label>
            Amount in USDC
            <input
              inputMode="decimal"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onBlur={() => setState((s) => (amountFindings(input).length === 0 ? chooseSize(s, input) : s))}
              aria-describedby="pgm-tma-amount-help"
            />
          </label>
          <p id="pgm-tma-amount-help">
            {problems.length > 0
              ? problems.join(" ")
              : `${(shares / 1_000_000).toFixed(2)} ${state.side.toUpperCase()} shares at ${price}`}
          </p>

          {state.step === "submitting" ? (
            <p className={CLASS.skeleton} role="status">Placing your order…</p>
          ) : null}
          {state.step === "placed" ? (
            <p className={reduce ? undefined : CLASS.ack} role="status">
              Order accepted. You will get a message here the moment it fills or is refused.
            </p>
          ) : null}
          {state.step === "refused" && state.error ? (
            <p className={reduce ? CLASS.reject : `${CLASS.reject} pgm-tma-ack`} role="alert">
              {state.error}
              {state.refusalCode ? <code> {state.refusalCode}</code> : null}
            </p>
          ) : null}

          {state.step === "confirm" ? (
            <div>
              <p aria-live="polite">{confirmCopy({ ...state, amountUsdc: input }, market)}</p>
              {/* The secondary affordance: the MainButton confirms, this row just makes the same decision tappable
                  inside the sheet for anyone who does not look at the bottom bar. */}
              <button type="button" onClick={() => void onConfirm(crypto.randomUUID().replace(/-/g, "").slice(0, 16))}>
                Confirm ${input}
              </button>
              <button type="button" onClick={() => setState((s) => close(s))}>Cancel</button>
            </div>
          ) : null}

          {state.step === "placed" && onNeedDeposit ? (
            <button type="button" onClick={onNeedDeposit}>Fund with more USDC</button>
          ) : null}
        </div>
      ) : null}

      <footer>
        <p>
          <em>
            Odds are a market, not a forecast. Prices move; the price on your confirm is the one that rules. Never
            trade more than you can afford to lose.
          </em>
        </p>
      </footer>
    </section>
  );
}

export default TradeSheet;
