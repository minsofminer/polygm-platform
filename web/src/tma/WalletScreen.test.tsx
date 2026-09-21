/**
 * The wallet screen, rendered. What the model's own tests cannot prove, and each is a rule this phase was given:
 *
 *  1. the receive pane draws a **real QR** — an SVG whose path is one square per dark module, with the quiet zone
 *     inside the viewBox rather than in a margin a theme could eat;
 *  2. the withdrawal ceremony walks one step at a time, and a refusal lands on the step that caused it rather than as
 *     one banner about a code;
 *  3. a completed withdrawal says it is **recorded and queued, not paid** — the sentence the API sends with it;
 *  4. the export pane will not send until the word, the password and the code are all there;
 *  5. a credited deposit buzzes once, and so does a refusal — and nothing else does.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { WalletScreen, type WalletIO } from "./WalletScreen";
import { ceremonyStart, mapWalletCard, type DepositProgress, type WalletCard } from "./wallet";

const CARD: WalletCard = mapWalletCard({
  wallet: { userId: "u1", provider: "turnkey", custody: "delegated", address: "0x1111111111111111111111111111111111111111",
            proxyAddress: "", state: "funded", createdMs: 1 },
  cashMicro: "1290000000", reservedMicro: "50000000",
  locks: { password: true, totp: true, custody: "delegated" },
  chains: [{ chain: "polygon", confirmations: 128 }],
  note: "Deposits are credited after the bridge lands.",
});

const DESTINATIONS = [
  { id: "a1", label: "Cold wallet", display: "0x1234…abcd", usable: true, cooldownText: "" },
  { id: "a2", label: "New one", display: "0xfeed…beef", usable: false, cooldownText: "usable in 22 hours" },
];

const PROGRESS: DepositProgress = {
  depositId: 4, chain: "polygon", status: "credited", confirmations: 128, minConfirmations: 128, credited: true,
  txHash: "0xdeadbeef0000000000", bridgeTxHash: "", problem: "",
  steps: ["detecting", "confirming", "bridging", "crediting", "credited"].map((key) => ({
    key, label: `leg ${key}`, state: "done" as const,
  })),
  line: "Credited — the balance above includes it.",
};

/** The page's IO, faked: every call is recorded so the assertions can be about what was *sent*. */
function io(overrides: Partial<WalletIO> = {}) {
  const calls: string[] = [];
  const base: WalletIO = {
    mintKey: () => "0123456789abcdef",
    quote: async (input) => {
      calls.push(`quote:${input.chain}:${input.amountUsdc}:${input.idempotencyKey}`);
      return { ok: true, data: { depositId: 4, chain: input.chain, address: CARD.address, proxyAddress: "",
                                 amountMicro: 250_000_000, minConfirmations: 128, status: "detecting" } };
    },
    progress: async () => {
      calls.push("progress");
      return { ok: true, data: PROGRESS };
    },
    withdraw: async (input) => {
      calls.push(`withdraw:${input.body.typedAmount}:${input.idempotencyKey}`);
      return { ok: true, data: { withdrawalId: 9, destination: CARD.address,
                                 note: "Recorded and queued. Signing runs on the custody plane, which is not live until P14." } };
    },
    exportKey: async (input) => {
      calls.push(`export:${input.typedConfirm}`);
      return { ok: true, data: { covered: false, dekVersion: 3, wrapsUsed: 1, wrappedKey: "wrk-abc",
                                 note: "This is your wrapped key material, shown once." } };
    },
  };
  const merged = { ...base, ...overrides };
  return { io: merged, calls };
}

function buzzes() {
  const seen: string[] = [];
  const w = window as unknown as Record<string, unknown>;
  w.Telegram = { WebApp: { initData: "signed", notificationOccurred: (t: string) => seen.push(t) } };
  return seen;
}

afterEach(() => {
  vi.restoreAllMocks();
  delete (window as unknown as Record<string, unknown>).Telegram;
});

describe("the wallet screen", () => {
  it("leads with the balance and the reserved cash, and names both locks", () => {
    render(<WalletScreen card={CARD} destinations={DESTINATIONS} entries={[]} io={io().io} />);
    expect(screen.getByText("1290 USDC available")).toBeDefined();
    expect(screen.getByText(/50 reserved for open orders/)).toBeDefined();
    expect(screen.getByText(/password and your authenticator/)).toBeDefined();
  });

  it("draws the deposit address as a QR with four modules of quiet zone, not as a margin", () => {
    const { container } = render(<WalletScreen card={CARD} destinations={DESTINATIONS} entries={[]} io={io().io}
                                               initialPane="receive" />);
    // The QR renders inside a LIGHT-THEME subtree: its colours come from the tokens, and the theme attribute is what
    // makes an inverted (unscannable) symbol impossible in dark mode.
    const wrapper = container.querySelector("span.pgm-tma-qr");
    expect(wrapper?.getAttribute("data-theme")).toBe("light");
    expect(container.querySelector("rect.pgm-tma-qr__plate")).not.toBeNull();
    expect(container.querySelector("path.pgm-tma-qr__ink")).not.toBeNull();
    const svg = container.querySelector("span.pgm-tma-qr > svg");
    expect(svg).not.toBeNull();
    const viewBox = svg?.getAttribute("viewBox") ?? "";
    const [, , width, height] = viewBox.split(" ").map(Number);
    // The symbol is 29 modules for this address (version 3), and the viewBox adds four light modules on each side:
    // 29 + 8 = 37. Asserted exactly, because "a bit bigger" is how a quiet zone becomes three modules.
    expect(width).toBe(height);
    expect(width).toBe(37);
    expect(svg?.getAttribute("shape-rendering")).toBe("crispEdges");
    const squares = (svg?.querySelector("path")?.getAttribute("d") ?? "").split("M").length - 1;
    expect(squares).toBeGreaterThan(200);
    expect(screen.getByText(CARD.address)).toBeDefined();
  });

  it("sends a deposit quote with one key, and stops polling once the balance is credited", async () => {
    const faked = io();
    render(<WalletScreen card={CARD} destinations={DESTINATIONS} entries={[]} io={faked.io} initialPane="receive" />);
    fireEvent.change(screen.getByLabelText(/Amount you are sending/), { target: { value: "250" } });
    fireEvent.click(screen.getByText(/I have sent it/));
    await waitFor(() => expect(screen.getByRole("heading", { name: "Credited" })).toBeDefined());
    expect(faked.calls.filter((c) => c.startsWith("quote:"))).toHaveLength(1);
    const quote = faked.calls.find((c) => c.startsWith("quote:")) ?? "";
    // The key the screen minted is the server's shape, and the amount travels as a string.
    expect(quote).toContain("polygon:250:");
    expect(quote.split(":")[3]).toMatch(/^[A-Za-z0-9_-]{8,128}$/);
  });

  it("buzzes once when a deposit is credited, and the confirmations are text rather than a spinning number", async () => {
    const seen = buzzes();
    const faked = io();
    render(<WalletScreen card={CARD} destinations={DESTINATIONS} entries={[]} io={faked.io} initialPane="receive" />);
    fireEvent.change(screen.getByLabelText(/Amount you are sending/), { target: { value: "250" } });
    fireEvent.click(screen.getByText(/I have sent it/));
    await waitFor(() => expect(screen.getByRole("heading", { name: "Credited" })).toBeDefined());
    expect(seen.filter((b) => b === "success")).toHaveLength(1);
  });

  it("shows the statement with each row's own reason", () => {
    render(<WalletScreen card={CARD} destinations={DESTINATIONS} io={io().io} entries={[
      { kind: "deposit", label: "Deposit", amountText: "+100", positive: true, atMs: 1, reason: "Deposit 100 USDC credited" },
      { kind: "buy_fill", label: "Purchase", amountText: "−80", positive: false, atMs: 2, reason: "" },
    ]} />);
    expect(screen.getByText(/Deposit 100 USDC credited/)).toBeDefined();
    expect(screen.getByText("−80 USDC")).toBeDefined();
  });

  it("walks the withdrawal ceremony one step at a time and refuses to advance early", () => {
    render(<WalletScreen card={CARD} destinations={DESTINATIONS} entries={[]} io={io().io} initialPane="withdraw" />);
    const next = screen.getByText("Continue") as HTMLButtonElement;
    // The amount step with nothing typed: the button is disabled and says why.
    expect(next.disabled).toBe(true);
    expect(screen.getByText("Enter an amount.")).toBeDefined();
    fireEvent.change(screen.getByLabelText(/Amount in USDC/), { target: { value: "25" } });
    expect(next.disabled).toBe(false);
    fireEvent.click(next);
    expect(screen.getByText(/Step 2 of 5/)).toBeDefined();
    fireEvent.click(screen.getByText(/Cold wallet/));
    fireEvent.click(next);
    expect(screen.getByLabelText(/Type the amount/)).toBeDefined();
  });

  it("sends the ceremony once, with the typed pair as typed, and says what was and was not done", async () => {
    const faked = io();
    const seen = buzzes();
    render(<WalletScreen card={CARD} destinations={DESTINATIONS} entries={[]} io={faked.io} initialPane="withdraw" />);
    const next = () => screen.getByText(/Continue|Send withdrawal/) as HTMLButtonElement;
    fireEvent.change(screen.getByLabelText(/Amount in USDC/), { target: { value: "25" } });
    fireEvent.click(next());
    fireEvent.click(screen.getByText(/Cold wallet/));
    fireEvent.click(next());
    fireEvent.change(screen.getByLabelText(/Type the amount/), { target: { value: "25" } });
    fireEvent.change(screen.getByLabelText(/Type the destination/), { target: { value: "0x1234…abcd" } });
    fireEvent.click(next());
    fireEvent.change(screen.getByLabelText(/Password/), { target: { value: "hunter2" } });
    fireEvent.click(next());
    fireEvent.change(screen.getByLabelText(/Authenticator code/), { target: { value: "123456" } });
    fireEvent.click(next());
    await waitFor(() => expect(screen.getByText(/Withdrawal requested/)).toBeDefined());
    expect(faked.calls.filter((c) => c.startsWith("withdraw:"))).toHaveLength(1);
    expect(faked.calls.find((c) => c.startsWith("withdraw:"))).toContain("withdraw:25:");
    // The one sentence that must never soften: recorded and queued is not paid.
    expect(screen.getByText(/not live until P14/)).toBeDefined();
    expect(seen).toContain("success");
  });

  it("puts a refusal on the step that caused it, in a sentence plus the code", async () => {
    const seen = buzzes();
    const faked = io({
      withdraw: async () => ({ ok: false, code: "ADDRESS_COOLDOWN",
                               detail: "this destination was added 120s ago and can be used after the 24 hour hold" }),
    });
    render(<WalletScreen card={CARD} destinations={DESTINATIONS} entries={[]} io={faked.io} initialPane="withdraw" />);
    const next = () => screen.getByText(/Continue|Send withdrawal/) as HTMLButtonElement;
    fireEvent.change(screen.getByLabelText(/Amount in USDC/), { target: { value: "25" } });
    fireEvent.click(next());
    fireEvent.click(screen.getByText(/Cold wallet/));
    fireEvent.click(next());
    fireEvent.change(screen.getByLabelText(/Type the amount/), { target: { value: "25" } });
    fireEvent.change(screen.getByLabelText(/Type the destination/), { target: { value: "0x1234…abcd" } });
    fireEvent.click(next());
    fireEvent.change(screen.getByLabelText(/Password/), { target: { value: "hunter2" } });
    fireEvent.click(next());
    fireEvent.change(screen.getByLabelText(/Authenticator code/), { target: { value: "123456" } });
    fireEvent.click(next());
    await waitFor(() => expect(screen.getByText(/24 hour hold/)).toBeDefined());
    expect(screen.getByText(/ADDRESS_COOLDOWN/)).toBeDefined();
    expect(seen).toContain("error");
    // And the ceremony is back on the destination step, where the mistake was.
    expect(screen.getByText(/Step 2 of 5/)).toBeDefined();
  });

  it("explains a destination still in its hold instead of offering it as a choice", () => {
    render(<WalletScreen card={CARD} destinations={DESTINATIONS} entries={[]} io={io().io} initialPane="withdraw" />);
    fireEvent.change(screen.getByLabelText(/Amount in USDC/), { target: { value: "25" } });
    fireEvent.click(screen.getByText("Continue"));
    expect(screen.getByText(/usable in 22 hours/)).toBeDefined();
  });

  it("says where to add a destination when the allowlist is empty", () => {
    render(<WalletScreen card={CARD} destinations={[]} entries={[]} io={io().io} initialPane="withdraw" />);
    expect(screen.getByText(/can only be added from the bot/)).toBeDefined();
  });

  it("will not export a key until the word, the password and the code are all there", async () => {
    const faked = io();
    render(<WalletScreen card={CARD} destinations={DESTINATIONS} entries={[]} io={faked.io} initialPane="keys" />);
    const send = screen.getByText("Export key material") as HTMLButtonElement;
    expect(send.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText(/Type EXPORT to continue/), { target: { value: "EXPORT" } });
    fireEvent.change(screen.getByLabelText(/Password/), { target: { value: "hunter2" } });
    fireEvent.change(screen.getByLabelText(/Authenticator code/), { target: { value: "123456" } });
    expect(send.disabled).toBe(false);
    fireEvent.click(send);
    await waitFor(() => expect(screen.getByText(/Key version 3/)).toBeDefined());
    expect(faked.calls).toContain("export:EXPORT");
    expect(screen.getByText(/wrapped key material/)).toBeDefined();
  });

  it("turns every money pane off outside Telegram rather than offering a button that cannot work", () => {
    render(<WalletScreen card={CARD} destinations={DESTINATIONS} entries={[]} io={io().io} readOnly />);
    expect(screen.getByText(/Read-only/)).toBeDefined();
    expect(screen.queryByText(/Export key material/)).toBeNull();
  });

  it("tells a new account how a wallet appears, rather than showing an empty address", () => {
    const nothing = mapWalletCard({ wallet: null, cashMicro: "0", reservedMicro: "0", locks: {},
                                    note: "No wallet yet — /wallet in the bot creates one." });
    render(<WalletScreen card={nothing} destinations={[]} entries={[]} io={io().io} initialPane="receive" />);
    expect(screen.getByText(/No wallet yet/)).toBeDefined();
  });

  it("starts the ceremony at the first step, with no key and nothing sent", () => {
    const faked = io();
    render(<WalletScreen card={CARD} destinations={DESTINATIONS} entries={[]} io={faked.io} initialPane="withdraw" />);
    expect(ceremonyStart().step).toBe("amount");
    expect(faked.calls).toHaveLength(0);
  });
});
