"use client";
import { useId, useState } from "react";
import { t } from "@/i18n/t";
import { Deposit } from "@/screens/Deposit";
import { KeyExport } from "@/screens/KeyExport";
import { WalletAddresses } from "@/screens/WalletAddresses";
import { Withdraw } from "@/screens/Withdraw";

/**
 * The wallet's panels, behind a tablist.
 *
 * The page used to stack five sections — deposit, withdraw, addresses, keys, transactions — as one long column,
 * which made the withdrawal ceremony something a person scrolled past rather than a place they went. The
 * balance card stays above the tabs either way: it is the answer to "how much", and a tab is a place to *do*
 * something, not a place to find out.
 *
 * The tablist is implemented here rather than as a `src/ui/` component because the shell has exactly one, and a
 * component with one consumer is a second place for the same markup to drift. The ARIA is the standard pair:
 * `role="tablist"`/`role="tab"` with `aria-selected`, and each panel labelled by its own tab.
 */
const TABS = ["deposit", "withdraw", "addresses", "keys"] as const;
type Tab = (typeof TABS)[number];

export function WalletPanels() {
  const [tab, setTab] = useState<Tab>("deposit");
  const id = useId();
  const label = (which: Tab) => t(which === "deposit" ? "wallet.deposit.title" : which === "withdraw" ? "wallet.withdraw.title" : which === "addresses" ? "wallet.addresses.title" : "wallet.keys.title");
  return (
    <>
      <div role="tablist" aria-label={t("wallet.balance.title")} style={{ display: "flex", gap: "var(--pgm-space-1)", flexWrap: "wrap" }}>
        {TABS.map((which) => (
          <button
            key={which}
            type="button"
            role="tab"
            id={`${id}-tab-${which}`}
            aria-selected={tab === which}
            aria-controls={`${id}-panel-${which}`}
            tabIndex={tab === which ? 0 : -1}
            className="button"
            onClick={() => setTab(which)}
          >
            {label(which)}
          </button>
        ))}
      </div>
      {TABS.map((which) => (
        <div
          key={which}
          role="tabpanel"
          id={`${id}-panel-${which}`}
          aria-labelledby={`${id}-tab-${which}`}
          hidden={tab !== which}
        >
          {which === "deposit" ? <Deposit /> : null}
          {which === "withdraw" ? <Withdraw /> : null}
          {which === "addresses" ? <WalletAddresses /> : null}
          {which === "keys" ? <KeyExport /> : null}
        </div>
      ))}
    </>
  );
}
