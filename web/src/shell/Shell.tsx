"use client";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { ConnectionDot } from "./ConnectionDot";
import { CommandPalette } from "./CommandPalette";
import { ShortcutsOverlay } from "./ShortcutsOverlay";
import { dispatchFor, TAB_HREFS } from "./shortcuts";
import { useRails } from "./useRails";
import { RAIL_MAX, RAIL_MIN, RAIL_STEP } from "./rails";
import { useLive } from "@/live/useLive";
import { useConnection } from "./connection";
import { Button } from "@/ui/Button";
import { Toasts } from "@/ui/Toast";
import { t } from "@/i18n/t";
import { usesMainButton, hapticConfirm } from "@/telegram/bridge";
import { parseStartapp, startappTarget } from "@/telegram/startapp";

/**
 * The frame. Desktop: three columns with resizable rails. Mobile: five tabs. Both: the connection dot, the
 * palette, the help overlay, and the trade gate — one store, so the dot and the ticket cannot disagree.
 */
export function Shell({ children }: { children: React.ReactNode }) {
  const frame = useRef<HTMLDivElement | null>(null);
  const [palette, setPalette] = useState(false);
  const [help, setHelp] = useState(false);
  const { fractions, collapsed, beginDrag, nudge, applyRail, toggle } = useRails(frame);
  // The shell owns the tape feed: every widget's freshness derives from the same stamp the dot shows, which
  // is the only way "trading is disabled while disconnected" and the indicator can be the same fact.
  const feed = useLive("tape");
  const setConn = useConnection((s) => s.set);
  const canTrade = useConnection((s) => s.canTrade());
  void setConn;
  useEffect(() => {
    useConnection.getState().set({
      mode: feed.mode,
      connected: feed.connected,
      freshness: feed.freshness,
      gapCount: feed.gapCount,
      lastGapCount: feed.lastGapCount,
      resyncing: feed.resyncing,
      blockReason: feed.blockReason,
      staleMs: feed.stamp ? Math.max(0, Date.now() - feed.stamp.asOf) : null,
    });
  }, [feed]);

  // `?startapp=` is read on the client, not from the layout's props: the App Router gives `searchParams` to
  // pages, not layouts, and a layout prop that is silently `undefined` is how deep links stop working with no
  // error anywhere. These routes are dynamic anyway (the auth check reads cookies), so this costs nothing.
  const params = useSearchParams();
  const router = useRouter();
  const startapp = params?.get("startapp") ?? null;
  const target = startapp ? startappTarget(parseStartapp(startapp)) : null;
  const notice = target?.notice ?? null;
  const deepLink = target?.href ?? null;
  // A deep link that resolves to a market has to *go* there. Until P12 this layout read the payload, computed the
  // notice, and dropped the href on the floor: `?startapp=` on the main site showed a toast (or nothing) and left the
  // user wherever they already were. `replace` rather than `push` so the back button does not bounce between the
  // landing page and the market the link named.
  const went = useRef(false);
  useEffect(() => {
    if (!deepLink || went.current) return;
    went.current = true;
    router.replace(deepLink);
  }, [deepLink, router]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      const inField = !!target && (target.isContentEditable || /^(input|textarea|select)$/i.test(target.tagName));
      const hit = dispatchFor(event, inField);
      if (!hit) return;
      event.preventDefault();
      switch (hit.action) {
        case "palette":
          setPalette(true);
          break;
        case "help":
          setHelp(true);
          break;
        case "close":
          setPalette(false);
          setHelp(false);
          break;
        case "cancel-all":
          if (usesMainButton("trade-confirm")) hapticConfirm();
          window.dispatchEvent(new CustomEvent("openout:cancel-all"));
          break;
        case "trade":
          window.dispatchEvent(new CustomEvent("openout:focus-ticket"));
          break;
        default:
          window.location.assign(TAB_HREFS[hit.action] ?? "/markets");
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  /**
   * The keyboard half of a resize handle, and the ARIA half with it.
   *
   * `role="separator"` plus `tabIndex=0` makes this element a *window splitter*: a focusable separator is required
   * to carry `aria-valuenow`/`aria-valuemin`/`aria-valuemax` and to answer the arrow keys. The shell's two handles
   * were focusable, labelled, value-less and pointer-only — Tab landed on a control that did nothing, which
   * `plans/design-review.md` recorded as the highest-severity finding of the review. The terminal's own handles
   * have had this since P10; this is the same behaviour on the same step.
   *
   * The delta is inverted for the right rail: ArrowRight widens the LEFT rail and narrows the RIGHT one, because
   * the arrow describes the direction the separator moves, and a separator that moves right takes width from the
   * panel on its right.
   */
  const handleKeys = (side: "left" | "right") => (event: React.KeyboardEvent<HTMLSpanElement>) => {
    const growing = side === "left" ? 1 : -1;
    if (event.key === "Home" || event.key === "End") {
      event.preventDefault();
      applyRail(side, event.key === "Home" ? RAIL_MIN : RAIL_MAX);
      return;
    }
    if (event.key === "Enter" || event.key === " ") {
      // The same action the rail's own button performs, reachable from where the user already is.
      event.preventDefault();
      toggle(side)();
      return;
    }
    const sign = { ArrowLeft: -1, ArrowRight: 1 }[event.key];
    if (!sign) return;
    event.preventDefault();
    nudge(side)(sign * growing * RAIL_STEP, event.shiftKey);
  };

  return (
    <>
      {/* The rails and the topbar come before <main> in the document, so a keyboard user tabbed through the whole
          rail on every navigation before reaching the page. A skip link is the standard answer and the guideline
          asks for it by name; it is the first focusable element and it is visible when focused. */}
      <a className="skip" href="#content">
        {t("shell.skip.content")}
      </a>
      <header className="topbar">
        <Link href="/markets" aria-label={t("shell.title.brand")}>
          <strong>{t("shell.title.brand")}</strong>
        </Link>
        <Button onClick={() => setPalette(true)} aria-keyshortcuts="Meta+K Control+K">
          {t("shell.palette.placeholder")}
        </Button>
        <Button onClick={() => setHelp(true)} aria-keyshortcuts="?">
          {t("shell.shortcut.title")}
        </Button>
        <ConnectionDot />
        {!canTrade ? (
          <span className="refusal" role="status">
            {t("shell.connection.cancelStillWorks")}
          </span>
        ) : null}
      </header>
      {notice ? <p className="refusal">{notice}</p> : null}
      <div ref={frame} className="frame" data-can-trade={canTrade ? "yes" : "no"}>
        <aside className={`rail${collapsed.left ? " rail--collapsed" : ""}`} aria-label={t("shell.nav.markets")}>
          <button type="button" className="button" onClick={toggle("left")}>
            {collapsed.left ? t("shell.rail.expand") : t("shell.rail.collapse")}
          </button>
          <nav>{children ? null : null}</nav>
        </aside>
        <main id="content" tabIndex={-1} style={{ minWidth: 0 }}>{children}</main>
        <aside className={`rail${collapsed.right ? " rail--collapsed" : ""}`} aria-label={t("shell.nav.profile")} />
        <span
          role="separator"
          aria-orientation="vertical"
          aria-label={t("shell.rail.persisted")}
          aria-valuenow={Math.round(fractions.left * 100)}
          aria-valuemin={Math.round(RAIL_MIN * 100)}
          aria-valuemax={Math.round(RAIL_MAX * 100)}
          aria-valuetext={t("shell.rail.width").replace("{percent}", String(Math.round(fractions.left * 100)))}
          tabIndex={0}
          className="handle"
          onPointerDown={beginDrag("left") as unknown as never}
          onKeyDown={handleKeys("left")}
        />
        <span
          role="separator"
          aria-orientation="vertical"
          aria-label={t("shell.rail.persisted")}
          aria-valuenow={Math.round(fractions.right * 100)}
          aria-valuemin={Math.round(RAIL_MIN * 100)}
          aria-valuemax={Math.round(RAIL_MAX * 100)}
          aria-valuetext={t("shell.rail.width").replace("{percent}", String(Math.round(fractions.right * 100)))}
          tabIndex={0}
          className="handle"
          onPointerDown={beginDrag("right") as unknown as never}
          onKeyDown={handleKeys("right")}
        />
      </div>
      <nav className="tabs" aria-label={t("shell.nav.markets")}>
        {[
          { href: "/markets", label: t("shell.nav.markets") },
          { href: "/tape", label: t("shell.nav.tape") },
          { href: "/terminal", label: t("shell.nav.trade") },
          { href: "/portfolio", label: t("shell.nav.portfolio") },
          { href: "/profile", label: t("shell.nav.profile") },
        ].map((tab) => (
          <Link key={tab.href} href={tab.href} className="tab">
            {tab.label}
          </Link>
        ))}
      </nav>
      <CommandPalette open={palette} onClose={() => setPalette(false)} />
      <ShortcutsOverlay open={help} onClose={() => setHelp(false)} />
      <Toasts />
    </>
  );
}
