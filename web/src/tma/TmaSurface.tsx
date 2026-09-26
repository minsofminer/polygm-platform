"use client";
/**
 * The Mini App screen, behind a chunk boundary, so the *main site* never ships it.
 *
 * `app/page.tsx` has to branch on `PGM_SURFACE` for the Mini App's own deployment (its `/` is the market card, not
 * a marketing page) — and it branches on the server, deliberately, so a phone never renders the wrong product for
 * a frame. What that server-side branch could not do by itself was keep the code out of the landing document:
 * a static `import { TmaScreen }` puts every module the Mini App reaches — the trade sheet, the USDC parsing, the
 * QR encoder, the wallet views — into the chunk graph of `/`, so the marketing page shipped the trading terminal
 * to visitors who will never open Telegram. Measured: `/` carried ~14 KB of it plus the sheet it pulls in.
 *
 * `next/dynamic` is the boundary. On the main site the server never renders this component, so the RSC payload
 * never references the chunk and the browser never fetches it; on the Mini App deployment it renders and the chunk
 * arrives, which is correct — that deployment *is* the terminal. The component stays server-rendered (`ssr` is the
 * default): a Mini App that paints an empty shell and then hydrates is the flash this surface was built to avoid.
 */
import dynamic from "next/dynamic";

const TmaScreen = dynamic(() => import("@/tma/TmaScreen").then((m) => m.TmaScreen));

export function TmaSurface() {
  return <TmaScreen />;
}
