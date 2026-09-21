/**
 * The Mini App's wallet, as data and decisions — no JSX, no fetch, no bridge.
 *
 * `trade.ts` does this job for the order sheet and this module does it for D6: the payloads the six wallet routes
 * return are mapped here, the six-step withdrawal ceremony is a state machine here, and every sentence the screen
 * says is written here. The screen renders and the page fetches; neither invents wording, and the tests can exercise
 * the ladder without a browser.
 *
 * The rules that shaped it, each of which is a rule this build has already paid for:
 *
 *  - **Money is integer micros and strings.** Balances arrive as micro strings and are formatted by `usdcText`, which
 *    mirrors Python's `fmt_usdc` character for character: no thousands separator, up to six decimals, trailing zeros
 *    stripped. A client that says "1,290" where the chat says "1290" is a client whose numbers have to be re-checked
 *    by the person reading them, and the design system's own rule (P08 D6.3) is that a comma next to money is a wrong
 *    number rather than a style choice.
 *  - **Nothing here animates a number.** The deposit progress screen is the one place in the product where a value
 *    changes on a timer (confirmations climbing), and the rule is that it *re-renders*; the circle does not spin, the
 *    digits do not slide, and the card does not pulse. `motion.ts`'s NEVER list is not decoration.
 *  - **A number the user must read back is shown, not summarised.** The withdrawal ceremony makes the customer type
 *    the amount and the destination, and the comparison happens on both sides: here so the button can be honest about
 *    being disabled, and on the server because a client is not a place to enforce a rule about money.
 *  - **The server's vocabulary is the screen's vocabulary.** Refusals go through the same `plainRefusal` table the
 *    trade sheet uses — one table, one Python twin, compared key-for-key by the P12 gate — and the deposit steps are
 *    the four legs the API's own schema names (`detecting`, `confirming`, `bridging`, `crediting`), not a friendlier
 *    progress bar invented in the webview.
 */

import { plainRefusal, toMicro } from "@/tma/trade";

export type Chain = "ethereum" | "base" | "polygon" | "arbitrum";

/** The four chains the deposit route accepts, in the order the API lists them (`_DEPOSIT_CHAINS`). */
export const CHAINS: readonly Chain[] = ["polygon", "base", "arbitrum", "ethereum"];

const CHAIN_NAMES: Record<string, string> = {
  ethereum: "Ethereum",
  base: "Base",
  polygon: "Polygon",
  arbitrum: "Arbitrum",
};

/** "polygon · 128 confirmations" — the chain's own name, and the wait this screen will be reporting on. */
export function chainLabel(chain: string, confirmations?: number): string {
  const name = CHAIN_NAMES[chain] ?? chain;
  return confirmations === undefined ? name : `${name} · ${confirmations} confirmations`;
}

// ----------------------------------------------------------------------------------------------- money display
/**
 * Micro USDC, as the chat prints it. The Python twin is `polygm_core.money.cents.fmt_usdc`, and the two are pinned
 * to each other by test vectors on both sides (`0` → "0", `5_000_000` → "5", `1_290_000_000` → "1290",
 * `80_645_161` → "80.645161").
 *
 * Six decimals, trailing zeros stripped, never rounded up: a truncated balance that reads higher than the ledger is
 * the one formatting error a withdrawal screen cannot afford, because the customer types it back.
 */
export function usdcText(micro: string | number): string {
  const value = typeof micro === "string" ? micro.trim() : String(Math.trunc(micro));
  if (!/^-?\d+$/.test(value)) return "0";
  const negative = value.startsWith("-");
  const digits = (negative ? value.slice(1) : value).padStart(7, "0");
  const whole = digits.slice(0, -6).replace(/^0+(?=\d)/, "");
  const frac = digits.slice(-6).replace(/0+$/, "");
  const text = frac ? `${whole}.${frac}` : whole;
  return negative && /[1-9]/.test(text) ? `-${text}` : text;
}

// -------------------------------------------------------------------------------------------- the balance card
export type WalletCard = {
  /** False when the account has no wallet row yet — the screen then says how the bot creates one. */
  present: boolean;
  address: string;
  proxyAddress: string;
  custody: string;
  /** "gasless" and the other custody modes, in words, so the card does not print an enum at a customer. */
  custodyText: string;
  state: string;
  availableMicro: number;
  reservedMicro: number;
  availableText: string;
  reservedText: string;
  locks: { password: boolean; totp: boolean };
  lockText: string;
  note: string;
};

const CUSTODY_WORDS: Record<string, string> = {
  delegated: "Trading wallet — orders are signed for you inside the account's own limits",
  "watch-only": "Watch-only — this wallet can hold and receive, but nothing can be signed from it",
  external: "External wallet — you hold the keys",
};

export function mapWalletCard(payload: Record<string, unknown>): WalletCard {
  const wallet = (payload.wallet ?? null) as Record<string, unknown> | null;
  const locks = (payload.locks ?? {}) as Record<string, unknown>;
  const availableMicro = Number(payload.cashMicro ?? 0);
  const reservedMicro = Number(payload.reservedMicro ?? 0);
  const hasPassword = Boolean(locks.password);
  const hasTotp = Boolean(locks.totp);
  const custody = String(wallet?.custody ?? "");
  return {
    present: Boolean(wallet),
    address: String(wallet?.address ?? ""),
    proxyAddress: String(wallet?.proxyAddress ?? ""),
    custody,
    custodyText: CUSTODY_WORDS[custody] ?? (custody ? `Custody: ${custody}` : ""),
    state: String(wallet?.state ?? ""),
    availableMicro,
    reservedMicro,
    availableText: usdcText(availableMicro),
    reservedText: usdcText(reservedMicro),
    locks: { password: hasPassword, totp: hasTotp },
    // Both locks are named at once, because "one of two" is the state people misread: a withdrawal needs the
    // password *and* the authenticator, and a screen that shows only the missing one invites the belief that
    // setting that one will be enough.
    lockText: hasPassword && hasTotp
      ? "Withdrawals are locked with your password and your authenticator."
      : `Withdrawals need ${hasPassword ? "" : "a password"}${!hasPassword && !hasTotp ? " and " : ""}${hasTotp ? "" : "an authenticator"} — set up in the bot before money can leave.`,
    note: String(payload.note ?? ""),
  };
}

// ------------------------------------------------------------------------------------------------- the ledger
export type LedgerEntry = { kind: string; label: string; amountText: string; positive: boolean; atMs: number; reason: string };

const KIND_WORDS: Record<string, string> = {
  deposit: "Deposit",
  credited: "Deposit credited",
  sell_fill: "Sale",
  buy_fill: "Purchase",
  fill: "Trade",
  fee: "Fee",
  withdrawal: "Withdrawal",
  withdrawal_requested: "Withdrawal requested",
  merge_receipt: "Merged position",
  resolution_payout: "Market resolved",
  adjust: "Adjustment",
  realised: "Realised PnL",
};

/** The statement list. `reason` is carried verbatim because "adjust" with no sentence is the row a ticket is made of. */
export function mapEntries(payload: Record<string, unknown>): { entries: LedgerEntry[]; nextBeforeMs: number; note: string } {
  const rows = Array.isArray(payload.entries) ? payload.entries : [];
  const entries = rows.map((raw) => {
    const row = (raw ?? {}) as Record<string, unknown>;
    const micro = Number(row.deltaMicro ?? 0);
    const kind = String(row.kind ?? "");
    return {
      kind,
      label: KIND_WORDS[kind] ?? kind.replace(/_/g, " "),
      amountText: `${micro < 0 ? "−" : "+"}${usdcText(Math.abs(micro))}`,
      positive: micro > 0,
      atMs: Number(row.atMs ?? 0),
      reason: String(row.reason ?? ""),
    };
  });
  return { entries, nextBeforeMs: Number(payload.nextBeforeMs ?? 0), note: String(payload.note ?? "") };
}

// ------------------------------------------------------------------------------------------------- deposits
export type DepositStep = { key: string; label: string; state: "pending" | "active" | "done" | "stopped" };

export type DepositQuote = {
  depositId: number;
  chain: string;
  address: string;
  proxyAddress: string;
  amountMicro: number;
  minConfirmations: number;
  status: string;
};

export type DepositProgress = {
  depositId: number;
  chain: string;
  status: string;
  confirmations: number;
  minConfirmations: number;
  steps: DepositStep[];
  /** The one line the progress card shows under the step list: what is happening, or why it stopped. */
  line: string;
  problem: string;
  credited: boolean;
  txHash: string;
  bridgeTxHash: string;
};

export function mapDepositQuote(payload: Record<string, unknown>): DepositQuote {
  return {
    depositId: Number(payload.depositId ?? 0),
    chain: String(payload.chain ?? ""),
    address: String(payload.address ?? ""),
    proxyAddress: String(payload.proxyAddress ?? ""),
    amountMicro: Number(payload.amountMicro ?? 0),
    minConfirmations: Number(payload.minConfirmations ?? 0),
    status: String(payload.status ?? ""),
  };
}

/**
 * The progress screen's whole input, from `GET /v1/wallet/deposit/{id}`.
 *
 * The step list comes from the server rather than being rebuilt here, and that is deliberate: the labels name the
 * chain and its confirmation count ("Waiting for 128 confirmations on polygon"), so a client that assembled its own
 * would print a different sentence about the same deposit. What this adds is the single line under the list, chosen
 * from the *step states* rather than from the status string — a `stopped` deposit says a human has it, whatever else
 * the row says.
 */
export function mapDepositProgress(payload: Record<string, unknown>): DepositProgress {
  const raw = Array.isArray(payload.steps) ? payload.steps : [];
  const steps: DepositStep[] = raw.map((step) => {
    const s = (step ?? {}) as Record<string, unknown>;
    const state = String(s.state ?? "pending");
    return {
      key: String(s.key ?? ""),
      label: String(s.label ?? ""),
      state: (["pending", "active", "done", "stopped"].includes(state) ? state : "pending") as DepositStep["state"],
    };
  });
  const status = String(payload.status ?? "");
  const stopped = steps.some((s) => s.state === "stopped");
  const credited = status === "credited";
  const active = steps.find((s) => s.state === "active");
  const problem = String(payload.problem ?? "");
  const confirmations = Number(payload.confirmations ?? 0);
  const minConfirmations = Number(payload.minConfirmations ?? 0);
  let line: string;
  if (stopped) line = problem || "This transfer stopped and a human is looking at it.";
  else if (credited) line = "Credited — the balance above includes it.";
  else if (active) line = active.label;
  else line = "Watching for your transfer.";
  return {
    depositId: Number(payload.depositId ?? 0),
    chain: String(payload.chain ?? ""),
    status,
    confirmations,
    minConfirmations,
    steps,
    line,
    problem,
    credited,
    txHash: String(payload.txHash ?? ""),
    bridgeTxHash: String(payload.bridgeTxHash ?? ""),
  };
}

/** What the deposit card promises before anything is sent: the address, the wait, and that the amount is a target. */
export function depositSentence(chain: string, amountMicro: number): string {
  return [
    `Send USDC on ${chainLabel(chain)} to the address below.`,
    `The ${usdcText(amountMicro)} is what this screen will watch for, not a fixed requirement — a smaller transfer is still credited, and a larger one is credited in full.`,
    "Nothing to sign, nothing to approve: the address is yours alone.",
  ].join(" ");
}

/**
 * The QR payload for a deposit: an EIP-681 URI when the amount is known, the bare address otherwise.
 *
 * EIP-681 is what a wallet app understands when it is scanned (`ethereum:0x…@137?value=…`), and this encoder's
 * fixtures include one for exactly this reason. The chain id is the *destination* chain's, so a scanner cannot send
 * a Base deposit to an Ethereum address that happens to look right.
 */
export function depositQrPayload(chain: string, address: string, amountUsdc?: string): string {
  if (!address) return "";
  const ids: Record<string, number> = { ethereum: 1, base: 8453, polygon: 137, arbitrum: 42161 };
  const id = ids[chain];
  if (!id || !amountUsdc) return address;
  const micro = toMicro(amountUsdc);
  if (micro <= 0) return address;
  return `ethereum:${address}@${id}?value=${micro}`;
}

// --------------------------------------------------------------------------------------------- withdrawals
/**
 * The ceremony, in the order the locks are — and the order is the design, not a layout choice.
 *
 * The server's ladder is allowlist → cooldown → typed confirmation → password → authenticator (see `wallet_withdraw`).
 * The screen asks for the same things in the same sequence so that a refusal arrives at the step that caused it: a
 * destination still in its hold is refused before the customer types it back, not after they have fetched their
 * phone. The two steps that exist to defeat a clipboard swap — typing the amount and typing the destination — are
 * separate from the fields that hold them, because a value the user pastes into the same box they read it from is
 * not a check.
 */
export type WithdrawStep = "amount" | "destination" | "typed" | "password" | "code" | "done";

export const CEREMONY: readonly { step: WithdrawStep; title: string; help: string }[] = [
  { step: "amount", title: "How much", help: "Up to your available balance. The fee is shown before anything is sent." },
  { step: "destination", title: "Where", help: "One of your allowlisted destinations. A new one cannot be used for 24 hours." },
  { step: "typed", title: "Type it back", help: "Type the amount and the destination exactly as shown. This is what defeats a clipboard swap." },
  { step: "password", title: "Password", help: "The same password you sign in with." },
  { step: "code", title: "Authenticator", help: "The 6-digit code from your authenticator app." },
  { step: "done", title: "Requested", help: "" },
] as const;

export type Ceremony = {
  step: WithdrawStep;
  amountUsdc: string;
  addressId: string;
  typedAmount: string;
  typedAddress: string;
  password: string;
  code: string;
  /** Minted once per ceremony and reused on retry, so a lost response is not a second withdrawal. */
  key: string;
  busy: boolean;
  refusalCode?: string;
  error?: string;
};

export type Destination = { id: string; label: string; display: string; usable: boolean; cooldownText: string };

export function mapDestinations(payload: Record<string, unknown>): Destination[] {
  const rows = Array.isArray(payload.items) ? payload.items : [];
  return rows.map((raw) => {
    const row = (raw ?? {}) as Record<string, unknown>;
    const remaining = Number(row.cooldown_remaining_ms ?? row.cooldownRemainingMs ?? 0);
    return {
      id: String(row.id ?? ""),
      label: String(row.label ?? "") || "Unlabelled",
      display: String(row.display ?? ""),
      usable: Boolean(row.usable),
      cooldownText: remaining > 0 ? `usable in ${hoursText(remaining)}` : "",
    };
  });
}

/** Milliseconds as the longest true unit: "3 hours", "2 days". Never "0.3 days", never a countdown. */
export function hoursText(ms: number): string {
  const minutes = Math.max(0, Math.round(ms / 60_000));
  if (minutes < 60) return `${minutes} minute${minutes === 1 ? "" : "s"}`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours} hour${hours === 1 ? "" : "s"}`;
  return `${Math.round(hours / 24)} days`;
}

export function ceremonyStart(): Ceremony {
  return { step: "amount", amountUsdc: "", addressId: "", typedAmount: "", typedAddress: "", password: "", code: "",
           key: "", busy: false };
}

export function ceremonyAt(ceremony: Ceremony, step: WithdrawStep): Ceremony {
  return { ...ceremony, step, refusalCode: undefined, error: undefined };
}

/**
 * What is wrong with the ceremony right now, as sentences — and an empty list means the button may be pressed.
 *
 * The one comparison that happens on both sides is the typed pair. Here it keeps the button honest ("still typing" is
 * not a refusal), and on the server it is the rule: `wallet_withdraw` refuses a typed amount or destination that does
 * not match exactly, because the whole point of typing it back is that the client cannot be trusted to have shown the
 * right thing.
 */
export function ceremonyFindings(ceremony: Ceremony, card: Pick<WalletCard, "availableMicro" | "locks">): string[] {
  const out: string[] = [];
  const micro = toMicro(ceremony.amountUsdc);
  switch (ceremony.step) {
    case "amount": {
      if (!ceremony.amountUsdc.trim()) out.push("Enter an amount.");
      else if (micro <= 0) out.push("Use a plain number, like 25 or 12.50 — no symbols or commas.");
      else if (micro > card.availableMicro) {
        out.push(`That is more than your available ${usdcText(card.availableMicro)} USDC — the rest is reserved against open orders.`);
      }
      break;
    }
    case "destination":
      if (!ceremony.addressId) out.push("Pick a destination from your allowlist.");
      break;
    case "typed": {
      if (ceremony.typedAmount.trim() !== ceremony.amountUsdc.trim()) out.push("The amount you typed does not match the amount above.");
      if (!ceremony.typedAddress.trim()) out.push("Type the destination as it was shown.");
      break;
    }
    case "password":
      if (!ceremony.password) out.push(card.locks.password ? "Enter your password." : "No withdrawal password is set — set one in the bot first.");
      break;
    case "code":
      if (!/^\d{6}$/.test(ceremony.code.trim())) out.push("Enter the 6-digit code from your authenticator app.");
      else if (!card.locks.totp) out.push("No authenticator is enrolled on this account — set it up in the bot first.");
      break;
    case "done":
      break;
  }
  return out;
}

/**
 * The steps a person actually walks: `CEREMONY` minus its final `done` entry.
 *
 * `done` is in the list because the pane renders a title for it, and out of the ladder because it is a *receipt*
 * state — the request is what ends the ceremony, and a "Continue" that advanced into `done` would render the
 * finished screen with nothing having been sent. That is exactly the bug this constant exists to prevent: the first
 * version of the walk used `index + 1` over the whole list, so the last tap showed "Step 6 of 5 · Requested".
 */
export const CEREMONY_STEPS: readonly WithdrawStep[] = CEREMONY.filter((entry) => entry.step !== "done")
  .map((entry) => entry.step);

/** The step before `step` in the ladder, or the first step when there is none — the ceremony's own "Back". */
export function previousStep(step: WithdrawStep): WithdrawStep {
  const index = CEREMONY_STEPS.indexOf(step);
  const previous = CEREMONY_STEPS[Math.max(0, index - 1)];
  return previous ?? "amount";
}

/** The step after `step`, or null at the end of the ladder (where the request is made instead of advancing). */
export function nextStep(step: WithdrawStep): WithdrawStep | null {
  const index = CEREMONY_STEPS.indexOf(step);
  if (index < 0 || index + 1 >= CEREMONY_STEPS.length) return null;
  return CEREMONY_STEPS[index + 1] ?? null;
}

/** "Step 3 of 5" — 1-based, over the steps a person walks, never over the receipt state. */
export function ceremonyPosition(step: WithdrawStep): { index: number; total: number } {
  const index = CEREMONY_STEPS.indexOf(step);
  return { index: Math.max(0, index) + 1, total: CEREMONY_STEPS.length };
}

/** Which ceremony step a refusal belongs to, so the screen can put the sentence where the mistake was made. */
export function stepForRefusal(code: string): WithdrawStep | null {
  switch (code) {
    case "ADDRESS_COOLDOWN":
    case "ADDRESS_NOT_ALLOWED":
    case "NOT_FOUND":
      return "destination";
    case "BAD_FIELD":
      return "typed";
    case "PASSWORD_REQUIRED":
    case "PASSWORD_WRONG":
      return "password";
    case "TOTP_REQUIRED":
    case "TOTP_INVALID":
    case "TOTP_LOCKED":
      return "code";
    case "INSUFFICIENT_BALANCE":
    case "BAD_AMOUNT":
    case "ZERO_SIZE":
      return "amount";
    default:
      return null;
  }
}

/** The request the ceremony sends once every lock has been answered. Amounts stay strings; the server parses them. */
export function ceremonyBody(ceremony: Ceremony): Record<string, string> {
  return {
    amountUsdc: ceremony.amountUsdc.trim(),
    addressId: ceremony.addressId,
    // The typed pair goes as the user typed it, not as the fields hold it: a client that "helps" by normalising the
    // whitespace is a client proving the server checked its own copy against itself.
    typedAmount: ceremony.typedAmount.trim(),
    typedAddress: ceremony.typedAddress.trim(),
    password: ceremony.password,
    code: ceremony.code.trim(),
  };
}

/** `tma-wd-<random>`, minted once per ceremony: the same shape the order path uses, and the same reason. */
export function ceremonyKey(mint: () => string): string {
  return `tma-wd-${mint()}`;
}

// ------------------------------------------------------------------------------------------------ key export
export type ExportDraft = { typedConfirm: string; password: string; code: string };

export function exportStart(): ExportDraft {
  return { typedConfirm: "", password: "", code: "" };
}

export function exportFindings(draft: ExportDraft, hasTotp: boolean): string[] {
  const out: string[] = [];
  if (draft.typedConfirm.trim() !== "EXPORT") out.push("Type EXPORT (in capitals) to confirm you understand what this is.");
  if (!draft.password) out.push("Enter your password.");
  if (!hasTotp) out.push("No authenticator is enrolled on this account — set it up in the bot first.");
  else if (!/^\d{6}$/.test(draft.code.trim())) out.push("Enter the 6-digit code from your authenticator app.");
  return out;
}

/**
 * The key-export warning, as one block of prose the screen shows above the form.
 *
 * It is deliberately blunt about two things: what the user is about to hold, and what this build cannot yet do with
 * it. Signing runs on the custody plane (P13/P14), so what comes back is *wrapped* material — the honest sentence is
 * "here is your material, and here is why it does not sign anything yet", not "your private key".
 */
export const EXPORT_WARNING = [
  "This is the key material for this account, and whoever holds it can move everything the account holds, on every chain.",
  "It comes back wrapped: the custody plane unwraps it, and that ceremony is part of go-live, so nothing here will sign yet.",
  "Support will never ask you for it. If someone does, that message is the report.",
].join(" ");

export const EXPORT_CONFIRM_WORD = "EXPORT";

/** Where the export screen's numbers land: what was used, and how many wraps remain before the key needs re-wrapping. */
export function exportReceiptText(payload: Record<string, unknown>): string {
  const version = Number(payload.dekVersion ?? 0);
  const wraps = Number(payload.wrapsUsed ?? 0);
  return `Key version ${version} · ${wraps} wrap${wraps === 1 ? "" : "s"} used. Store this offline — it is shown once.`;
}

export { plainRefusal };

// ----------------------------------------------------------------------------------------- what the page passes in
/**
 * The page's adapter: a route key and its params in, a payload or a refusal out.
 *
 * The same shape `market.ts` uses for the sheet, and for the same reason — the screen is testable without a network,
 * and the session/token story stays in the page where the session lives.
 */
export type WalletGet = (
  key: "balance" | "transactions" | "addressList",
  params: Record<string, string>,
) => Promise<{ ok: true; data: Record<string, unknown> } | { ok: false; code: string; message: string }>;

export type WalletRead = { card: WalletCard; destinations: Destination[]; entries: LedgerEntry[]; ledgerNote: string };

/**
 * Everything the wallet screen needs before its first frame.
 *
 * The card is the spine: if the balance read fails there is nothing honest to render, so that failure is the screen's
 * refusal. The ledger and the allowlist are allowed to come back empty — a statement with no rows is a new account,
 * and a ceremony with no destinations is a state the screen already explains (add one from the bot, wait 24 hours).
 */
export async function loadWallet(get: WalletGet): Promise<{ wallet: WalletRead } | { error: string }> {
  const [balance, ledger, addresses] = await Promise.all([
    get("balance", {}),
    get("transactions", { limit: "25" }),
    get("addressList", {}),
  ]);
  if (!balance.ok) return { error: plainRefusal(balance.code, balance.message) };
  const ledgerRows = ledger.ok ? mapEntries(ledger.data) : { entries: [], nextBeforeMs: 0, note: "" };
  return {
    wallet: {
      card: mapWalletCard(balance.data),
      destinations: addresses.ok ? mapDestinations(addresses.data) : [],
      entries: ledgerRows.entries,
      ledgerNote: ledgerRows.note,
    },
  };
}
