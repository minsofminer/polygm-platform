"use client";
/**
 * The Mini App's only screen: the market in a deep link, and the sheet that trades it.
 *
 * The shape of the page is the argument of the phase:
 *
 *  1. **`?startapp=` is read before anything else** — client-side, from the URL, the same way the shell does it.
 *  2. **The session is silent.** `silentReauth()` trades the signed `initData` for a bearer once per cold start; if
 *     it fails the page says so in a sentence that names the fix, rather than showing a confirm button that will
 *     refuse. A webview that answers "not authorized" to a tap is worse than one that never offered the tap.
 *  3. **Two reads, two stamps, the older one shown.** The public market read resolves the slug; the book read prices
 *     the side. The freshness shown is the older of the two, because a minute-old book beside a fresh row is still a
 *     minute-old price.
 *  4. **One mutation route**: `POST /v1/telegram/order` — slug, side, amount, idempotency key — the same order path
 *     the bot's confirm tap takes, so the chat and the webview cannot drift on the rules.
 *
 * The page owns no money arithmetic and no wording: `market.ts` maps payloads, `trade.ts` decides amounts and
 * sentences, `TradeSheet` renders. What lives here is the sequencing, which is the part that needs a browser.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { request } from "@/api/client";
import { silentReauth } from "@/telegram/reauth";
import { parseStartapp } from "@/telegram/startapp";
import { TradeSheet, type OrderFn } from "@/tma/TradeSheet";
import { loadMarketForSheet, type TmaGet, type TmaRead } from "@/tma/market";
import type { MarketView } from "@/tma/trade";

type Ready = { state: "loading" } | { state: "ready"; view: MarketView } | { state: "refused"; why: string };

export default function TmaPage() {
  const startapp = useMemo(() => {
    if (typeof window === "undefined") return null;
    // Read from the URL rather than a layout prop: `searchParams` reaches pages, not layouts, and a deep link that
    // resolves to "no market" with no error anywhere is exactly the bug this phase is about.
    return new URLSearchParams(window.location.search).get("startapp");
  }, []);
  // The payload is used directly rather than the target helper's `href`: this page needs the market *key* the link
  // carried, and re-parsing our own route string back into a slug is how a deep link quietly stops matching.
  const payload = useMemo(() => (startapp ? parseStartapp(startapp) : null), [startapp]);
  const slug = payload?.kind === "market" ? payload.marketId : null;
  const notice = payload?.kind === "none" ? null : payload?.kind === "rejected" ? payload.reason : null;
  const [ready, setReady] = useState<Ready>({ state: "loading" });
  const [session, setSession] = useState<"checking" | "ok" | "no">("checking");

  useEffect(() => {
    void (async () => {
      // One attempt per cold start, by construction (`silentReauth` de-dupes on the state object). A browser tab has
      // no `initData`, so this lands on "no" there — which is the correct answer: nothing can be placed unsigned.
      const marker = {};
      const outcome = await silentReauth(marker);
      // Three outcomes, and only one of them is usable: a session was minted (`ok`), or it was not — whether that
      // is "not inside Telegram", "already tried this cold start" or "the payload was refused". The screen says the
      // same thing for all three, because the user's next move is the same: come back through the bot.
      setSession(outcome.attempted && outcome.ok ? "ok" : "no");
    })();
  }, []);

  // The adapter the mapping module wants: route key + params in, payload + its stamp out. The stamp travels with the
  // data because the sheet's freshness line is the book's age, not the time this page happened to render.
  const get = useCallback<TmaGet>(async (key, params) => {
    const res = await request<Record<string, unknown>>({ key, params });
    if (!res.ok) return { ok: false, code: res.error.code, message: res.error.message };
    return { ok: true, data: { ...(res.data as object), asOf: Number((res.data as { asOf?: number }).asOf ?? 0) } };
  }, []);

  useEffect(() => {
    if (!slug) {
      setReady({ state: "refused",
                 why: notice ?? "Open this from a trade link or the bot's menu — there is nothing to trade here yet." });
      return;
    }
    void (async () => {
      const out = await loadMarketForSheet(get, slug);
      if ("error" in out) setReady({ state: "refused", why: out.error });
      else setReady({ state: "ready", view: out.view });
    })();
  }, [slug, notice, get]);

  const place = useCallback<OrderFn>(async (input) => {
    const res = await request<{ intentId?: string }>({
      key: "telegramOrder",
      body: { slug: input.slug, side: input.side, amountUsdc: input.amountUsdc },
      idempotencyKey: input.idempotencyKey,
    });
    if (!res.ok) return { ok: false, code: res.error.code, detail: res.error.message };
    return { ok: true, intentId: String(res.data.intentId ?? "accepted") };
  }, []);

  if (ready.state === "loading") {
    // A skeleton with no number in it — the rule from `motion.ts`, applied to the first frame the user sees.
    return <p className="pgm-tma-skeleton" role="status">Opening the market…</p>;
  }
  if (ready.state === "refused") {
    return (
      <section className="pgm-tma-card">
        <h1>Nothing here to trade</h1>
        <p role="status">{ready.why}</p>
        <p><a href="https://t.me/polygm_bot">Back to the bot</a></p>
      </section>
    );
  }
  if (session === "no") {
    return (
      <section className="pgm-tma-card">
        <h1>{ready.view.question}</h1>
        <p role="alert">
          Telegram could not confirm this session, so nothing can be placed from here. Open the bot and tap Trade
          again — if it keeps happening, check that you came from the bot itself rather than a copied link.
        </p>
        <p><a href="https://t.me/polygm_bot">Open the bot</a></p>
      </section>
    );
  }
  return (
    <main>
      <TradeSheet market={ready.view} place={place} />
    </main>
  );
}
