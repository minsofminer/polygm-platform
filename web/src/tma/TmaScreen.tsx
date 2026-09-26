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
 *
 * D6 added a second view on the same document: **the wallet** (`?view=wallet`). A view rather than a route, because
 * the Mini App's surface gate 404s everything that is not the root document — that 404 is what stops this deployment
 * becoming a second indexable copy of the site — and the BotFather URL has to be the root. The trade view stays the
 * default, and the switch keeps the URL honest (`history.replaceState`), so a customer who reloads lands where they
 * were and a link shared from the wallet view opens the wallet.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { request } from "@/api/client";
import { silentReauth } from "@/telegram/reauth";
import { parseStartapp, startappTarget } from "@/telegram/startapp";
import dynamic from "next/dynamic";
import { DisclaimerFooter } from "@/legal/disclaimer";
import type { OrderFn } from "@/tma/TradeSheet";
import type { WalletIO } from "@/tma/WalletScreen";

// The two views behind the first frame, split out of the document that opens them.
//
// Both are reached by an interaction — the wallet by the view switch, the sheet once a market has loaded — so
// neither belongs in the payload that paints the Mini App. Neither is put at risk by being deferred: the switch
// itself stays local (it renders before either view resolves), and the sheet's own data has to arrive first
// anyway. Measured cost of not doing this: 14 KB of trade logic and the wallet views on the initial route.
const TradeSheet = dynamic(() => import("@/tma/TradeSheet").then((m) => m.TradeSheet));
const WalletScreen = dynamic(() => import("@/tma/WalletScreen").then((m) => m.WalletScreen));
import { loadMarketForSheet, type TmaGet, type TmaRead } from "@/tma/market";
import { mapDepositProgress, mapDepositQuote, loadWallet, type WalletRead } from "@/tma/wallet";
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
  // Which view of the Mini App this is. The URL is the source of the initial answer and the state is the source
  // afterwards, with the URL kept in step — a `view` that only lived in the URL would re-read it on every render,
  // and one that only lived in state would make a reload lose the pane.
  const [view, setView] = useState<"trade" | "wallet">(() => "trade");
  const [wallet, setWallet] = useState<{ state: "idle" | "loading" } | { state: "ready"; read: WalletRead }
    | { state: "refused"; why: string }>({ state: "idle" });

  useEffect(() => {
    if (typeof window === "undefined") return;
    if (new URLSearchParams(window.location.search).get("view") === "wallet") setView("wallet");
  }, []);

  const showView = useCallback((next: "trade" | "wallet") => {
    setView(next);
    if (typeof window === "undefined") return;
    const url = new URL(window.location.href);
    if (next === "wallet") url.searchParams.set("view", "wallet");
    else url.searchParams.delete("view");
    // replaceState, not pushState: the back gesture belongs to Telegram, and a history entry per pane tap would
    // make "back" a way to re-open the pane you just left.
    window.history.replaceState(null, "", url.toString());
  }, []);

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

  // The wallet's read, and its IO. The reads go through the same `request()` the sheet uses, so the session and the
  // idempotency-key rules are the client's, not a second set written for this screen.
  const walletGet = useCallback(async (key: "balance" | "transactions" | "addressList", params: Record<string, string>) => {
    const res = await request<Record<string, unknown>>({ key, query: params });
    if (!res.ok) return { ok: false as const, code: res.error.code, message: res.error.message };
    return { ok: true as const, data: res.data as Record<string, unknown> };
  }, []);

  useEffect(() => {
    if (view !== "wallet" || session !== "ok" || wallet.state !== "idle") return;
    setWallet({ state: "loading" });
    void (async () => {
      const out = await loadWallet(walletGet);
      if ("error" in out) setWallet({ state: "refused", why: out.error });
      else setWallet({ state: "ready", read: out.wallet });
    })();
  }, [view, session, wallet.state, walletGet]);

  const walletIO = useMemo<WalletIO>(() => ({
    mintKey: () => crypto.randomUUID().replace(/-/g, "").slice(0, 16),
    quote: async ({ chain, amountUsdc, idempotencyKey }) => {
      const res = await request<Record<string, unknown>>({
        key: "deposit", body: { chain, amountUsdc }, idempotencyKey,
      });
      if (!res.ok) return { ok: false, code: res.error.code, detail: res.error.message };
      return { ok: true, data: mapDepositQuote(res.data as Record<string, unknown>) };
    },
    progress: async (depositId) => {
      const res = await request<Record<string, unknown>>({
        key: "depositProgress", params: { deposit_id: depositId },
      });
      if (!res.ok) return { ok: false, code: res.error.code, detail: res.error.message };
      return { ok: true, data: mapDepositProgress(res.data as Record<string, unknown>) };
    },
    withdraw: async ({ body, idempotencyKey }) => {
      const res = await request<Record<string, unknown>>({ key: "withdraw", body, idempotencyKey });
      if (!res.ok) return { ok: false, code: res.error.code, detail: res.error.message };
      return { ok: true, data: { withdrawalId: Number(res.data.withdrawalId ?? 0),
                                 destination: String(res.data.destination ?? ""),
                                 note: String(res.data.note ?? "") } };
    },
    exportKey: async ({ password, code, typedConfirm, idempotencyKey }) => {
      const res = await request<Record<string, unknown>>({
        key: "keyExport", body: { password, code, typedConfirm }, idempotencyKey,
      });
      if (!res.ok) return { ok: false, code: res.error.code, detail: res.error.message };
      return { ok: true, data: res.data as Record<string, unknown> };
    },
  }), []);

  const place = useCallback<OrderFn>(async (input) => {
    const res = await request<{ intentId?: string }>({
      key: "telegramOrder",
      body: { slug: input.slug, side: input.side, amountUsdc: input.amountUsdc },
      idempotencyKey: input.idempotencyKey,
    });
    if (!res.ok) return { ok: false, code: res.error.code, detail: res.error.message };
    return { ok: true, intentId: String(res.data.intentId ?? "accepted") };
  }, []);

  // The switch renders before either view is resolved, so the wallet is one tap away from a market link and the
  // market is one tap away from the balance — the two things a customer moves between in this app.
  const views = (
    <div role="group" aria-label="Mini App">
      <button type="button" aria-pressed={view === "trade"} onClick={() => showView("trade")}>Trade</button>
      <button type="button" aria-pressed={view === "wallet"} onClick={() => showView("wallet")}>Wallet</button>
    </div>
  );

  if (view === "wallet") {
    if (session === "no" || session === "checking") {
      // No session, so there is nothing to read: the wallet routes are session-scoped and a deposit address is not
      // a public fact about an account. The sentence names the one route in rather than describing the failure.
      return (
        <main>
          {views}
          <section className="pgm-tma-card">
            <h1>Your wallet</h1>
            <p role="status">{READ_ONLY_SENTENCE} <a href="https://t.me/polygm_bot">Open the bot</a></p>
          </section>
        </main>
      );
    }
    if (wallet.state === "refused") {
      return (
        <main>
          {views}
          <section className="pgm-tma-card">
            <h1>Your wallet</h1>
            <p role="alert">{wallet.why}</p>
            <p><a href="https://t.me/polygm_bot">Back to the bot</a></p>
          </section>
        </main>
      );
    }
    if (wallet.state !== "ready") {
      return <main>{views}<p className="pgm-tma-skeleton" role="status">Opening your wallet…</p></main>;
    }
    return (
      <main>
        {views}
        <WalletScreen card={wallet.read.card} destinations={wallet.read.destinations} entries={wallet.read.entries}
                      ledgerNote={wallet.read.ledgerNote} io={walletIO} />
      </main>
    );
  }

  if (ready.state === "loading") {
    // A skeleton with no number in it — the rule from `motion.ts`, applied to the first frame the user sees.
    return <main>{views}<p className="pgm-tma-skeleton" role="status">Opening the market…</p></main>;
  }
  if (ready.state === "refused") {
    return (
      <main>
      {views}
      <section className="pgm-tma-card">
        <h1>Nothing here to trade</h1>
        <p role="status">{ready.why}</p>
        <p><a href="https://t.me/polygm_bot">Back to the bot</a></p>
      </section>
      </main>
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
      {views}
      {session === "no" ? (
        <p className="pgm-tma-card" role="status">
          {READ_ONLY_SENTENCE} <a href="https://t.me/polygm_bot">Open the bot</a>
        </p>
      ) : null}
      <TradeSheet market={ready.view} place={place} readOnly={readOnly} />
      {/* Every view, not just the wallet: the person reading this is holding a live position, and the loss
          sentence belongs next to the trade sheet rather than one tap away. `compact` keeps it to three lines
          on the smallest screen this runs on. */}
      <DisclaimerFooter compact />
    </main>
  );
}
