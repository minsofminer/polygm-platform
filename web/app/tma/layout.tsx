import type { Metadata } from "next";
import type { ReactNode } from "react";

/**
 * The Mini App entry. Same shell, same components, same routes — one codebase (P08 D2). What differs is the
 * document: the Telegram bridge script is loaded here and nowhere else, and the CSP for this subtree allows
 * Telegram to frame it (see next.config.mjs) while every other path has `frame-ancestors 'none'`.
 */
export const metadata: Metadata = {
  title: "Openout",
  robots: { index: false },
};

export default function TmaLayout({ children }: { children: ReactNode }) {
  return (
    <>
      <script src="https://telegram.org/js/telegram-web-app.js" async />
      {children}
    </>
  );
}
