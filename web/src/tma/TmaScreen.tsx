"use client";
/**
 * The Mini App's trade screen, as a component rather than a route.
 *
 * It is a component because the deployment is not the same as the route: on the main site it lives at `/tma`, and on
 * the Mini App's own domain it is the root document, because the URL registered with BotFather has to be the thing a
 * customer lands on. Two entries, one screen, no copy — the alternative is two URLs that drift.
 *
 * What it does, in order: read `?startapp=` from the URL (client-side, because `searchParams` reaches pages and a
 * deep link that silently resolves to "no market" is the failure this phase exists to prevent), mint a session
 * silently from the signed `initData`, read the market and its book, and hand both to the sheet. The screen owns no
 * money arithmetic and no wording: `market.ts` maps payloads, `trade.ts` decides amounts and sentences, `TradeSheet`
 * renders. What lives here is the sequencing, which is the part that needs a browser.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { request } from "@/api/client";
import { silentReauth } from "@/telegram/reauth";
import { parseStartapp, startappTarget } from "@/telegram/startapp";
import { TradeSheet, type OrderFn } from "@/tma/TradeSheet";
import { loadMarketForSheet, type TmaGet, type TmaRead } from "@/tma/market";
import { READ_ONLY_SENTENCE, type MarketView } from "@/tma/trade";

type Ready = { state: "loading" } | { state: "ready"; view: MarketView } | { state: "refused"; why: string };

export function TmaScreen() {
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
  // The notice comes from the shared helper, which is the only place a refusal reason becomes a sentence: this line
  // used to render `payload.reason` directly, so a malformed link showed the user the string "bad-characters".
  const target = useMemo(() => (payload ? startappTarget(payload, "miniapp") : null), [payload]);
  const notice = payload?.kind === "none" ? null : target?.notice ?? null;
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
  // A market is public; a *trade* is not. So the card renders either way and only the placing of an order is guarded,
  // which is the right shape for three readers at once: the customer who tapped the link inside Telegram (session
  // present, nothing extra on screen), the one who opened the same link in a plain browser (sees the real prices and
  // is told where to go to act on them), and the one whose session expired on the way (the confirm slot says so in a
  // sentence). Blocking the whole screen without a session was the first version, and it made this URL — the one
  // registered with BotFather — look broken in a browser, which is the failure the phase exists to prevent.
  //
  // `checking` counts as read-only too: the sheet must not offer a confirm during the window where the session is
  // still being minted, and the banner waits for the answer so nobody sees "read-only" flash past inside Telegram.
  const readOnly = session !== "ok";
  return (
    <main>
      {session === "no" ? (
        <p className="pgm-tma-card" role="status">
          {READ_ONLY_SENTENCE} <a href="https://t.me/polygm_bot">Open the bot</a>
        </p>
      ) : null}
      <TradeSheet market={ready.view} place={place} readOnly={readOnly} />
    </main>
  );
}
