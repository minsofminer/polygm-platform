"use client";
/**
 * The wallet screen: balance, receive (with the QR), deposit progress, the withdrawal ceremony, key export.
 *
 * It is one component with four panes rather than four screens, because the wallet's jobs are a sequence people walk
 * back and forth along in a webview: balance → "I need money in" → the address → "where is it" → progress → balance
 * again. Four routes would need a router, a history, and a way back that the Telegram back button understands; one
 * pane switch needs none of that and keeps the balance card on screen the whole time.
 *
 * What this file does NOT do, and where each of those lives instead:
 *
 *  - No arithmetic, no sentences, no payload shapes: `wallet.ts` is the model and this is the view (the same split as
 *    `TradeSheet`/`trade.ts`).
 *  - No fetch: the page hands in `io`, so the screen is testable without a network and the session story stays in the
 *    page. On a browser outside Telegram there is no session, and `readOnly` turns the money panes into the one
 *    sentence that says where to go.
 *  - No number is ever animated, and nothing here counts anything up. The deposit pane is the one place a value
 *    changes on a timer (confirmations climbing toward 128) and it *re-renders*: no spinner over the number, no
 *    tween, no pulse. Motion is one beat per event, from `motion.ts`, and a buzz only on a fill or a refusal.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { CLASS } from "@/tma/motion";
import { haptic } from "@/tma/haptics";
import { QrCode } from "@/tma/Qr";
import {
  CEREMONY, ceremonyBody, ceremonyFindings, ceremonyKey, ceremonyPosition, ceremonyStart, depositQrPayload,
  depositSentence, nextStep, previousStep,
  EXPORT_CONFIRM_WORD, EXPORT_WARNING, exportFindings, exportReceiptText, exportStart, plainRefusal, stepForRefusal,
  usdcText, type Ceremony, type Chain, type DepositProgress, type DepositQuote, type Destination, type LedgerEntry,
  type WalletCard,
} from "@/tma/wallet";

export type WalletResult<T> = { ok: true; data: T } | { ok: false; code: string; detail?: string };

/** The page's IO: four calls, each already carrying its own idempotency key. */
export type WalletIO = {
  quote: (input: { chain: string; amountUsdc: string; idempotencyKey: string }) => Promise<WalletResult<DepositQuote>>;
  progress: (depositId: number) => Promise<WalletResult<DepositProgress>>;
  withdraw: (input: { body: Record<string, string>; idempotencyKey: string })
    => Promise<WalletResult<{ withdrawalId: number; destination: string; note: string }>>;
  exportKey: (input: { password: string; code: string; typedConfirm: string; idempotencyKey: string })
    => Promise<WalletResult<Record<string, unknown>>>;
  /** A key, minted here so the same ceremony keeps one across a retry. */
  mintKey: () => string;
};

export type WalletScreenProps = {
  card: WalletCard;
  destinations: Destination[];
  entries: LedgerEntry[];
  ledgerNote?: string;
  io: WalletIO;
  /** No session: the balance is a public fact of the account, but nothing can be sent. */
  readOnly?: boolean;
  /** Where the pane switch starts; the deposit deep link opens straight onto the address. */
  initialPane?: Pane;
};

type Pane = "balance" | "receive" | "withdraw" | "keys";

const PANES: readonly { pane: Pane; label: string }[] = [
  { pane: "balance", label: "Balance" },
  { pane: "receive", label: "Deposit" },
  { pane: "withdraw", label: "Withdraw" },
  { pane: "keys", label: "Keys" },
];

/** Poll interval for the deposit progress. Slow on purpose: a chain does not move faster because a phone is anxious. */
const POLL_MS = 5_000;

export function WalletScreen({
  card, destinations, entries, ledgerNote, io, readOnly = false, initialPane = "balance",
}: WalletScreenProps) {
  const [pane, setPane] = useState<Pane>(initialPane);
  return (
    <section className="pgm-tma-card">
      <header>
        <h1>Your wallet</h1>
        <p role="status">
          <span>{card.availableText} USDC available</span>
          {Number(card.reservedMicro) > 0 ? <span> · {card.reservedText} reserved for open orders</span> : null}
        </p>
        {card.custodyText ? <p><em>{card.custodyText}</em></p> : null}
      </header>

      <div role="group" aria-label="Wallet">
        {PANES.map((entry) => (
          <button
            key={entry.pane}
            type="button"
            aria-pressed={pane === entry.pane}
            onClick={() => setPane(entry.pane)}
          >
            {entry.label}
          </button>
        ))}
      </div>

      {readOnly ? (
        <p role="status">
          Read-only: the balances are real, but a deposit address and a withdrawal both need a session, and a session
          only comes from Telegram. <a href="https://t.me/polygm_bot">Open the bot</a>
        </p>
      ) : null}

      {pane === "balance" ? <BalancePane card={card} entries={entries} note={ledgerNote} /> : null}
      {pane === "receive" ? <ReceivePane card={card} io={io} readOnly={readOnly} /> : null}
      {pane === "withdraw" ? <WithdrawPane card={card} destinations={destinations} io={io} readOnly={readOnly} /> : null}
      {pane === "keys" ? <KeysPane card={card} io={io} readOnly={readOnly} /> : null}
    </section>
  );
}

// ------------------------------------------------------------------------------------------------- the balance pane
function BalancePane({ card, entries, note }: { card: WalletCard; entries: LedgerEntry[]; note?: string }) {
  return (
    <div>
      <dl>
        <dt>Available</dt>
        <dd>{card.availableText} USDC</dd>
        <dt>Reserved for open orders</dt>
        <dd>{card.reservedText} USDC</dd>
        <dt>Locks</dt>
        <dd>{card.lockText}</dd>
      </dl>
      {card.note ? <p><em>{card.note}</em></p> : null}
      <h2>Statement</h2>
      {entries.length === 0 ? (
        <p role="status">Nothing has moved yet. A deposit shows up here the moment it is credited.</p>
      ) : (
        <ul className="pgm-tma-ledger">
          {entries.map((entry) => (
            <li key={`${entry.atMs}-${entry.kind}-${entry.amountText}`}>
              <span>{entry.label}</span> <span>{entry.amountText} USDC</span>
              {/* The reason is the row's own sentence, carried verbatim: "adjust" with no explanation is the row a
                  support ticket is made of. */}
              {entry.reason ? <span> — {entry.reason}</span> : null}
            </li>
          ))}
        </ul>
      )}
      {note ? <p><em>{note}</em></p> : null}
    </div>
  );
}

// ------------------------------------------------------------------------------------------------- the receive pane
function ReceivePane({ card, io, readOnly }: { card: WalletCard; io: WalletIO; readOnly: boolean }) {
  const [amount, setAmount] = useState("");
  const [chain, setChain] = useState<Chain>("polygon");
  const [quote, setQuote] = useState<DepositQuote | null>(null);
  const [progress, setProgress] = useState<DepositProgress | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const keyRef = useRef("");
  const mounted = useRef(true);
  useEffect(() => () => {
    mounted.current = false;
  }, []);

  const address = quote?.address || card.address;
  const qrText = useMemo(() => depositQrPayload(chain, address, amount || undefined), [chain, address, amount]);

  const ask = useCallback(async () => {
    if (!keyRef.current) keyRef.current = ceremonyKey(io.mintKey);
    setBusy(true);
    setError(null);
    const out = await io.quote({ chain, amountUsdc: amount, idempotencyKey: keyRef.current });
    if (!mounted.current) return;
    setBusy(false);
    if (!out.ok) {
      haptic("reject");
      setError(plainRefusal(out.code, out.detail));
      return;
    }
    keyRef.current = "";
    setQuote(out.data);
  }, [amount, chain, io]);

  // The progress poll: it stops itself on a terminal state, and it is the only timer in this file.
  useEffect(() => {
    if (!quote || !quote.depositId) return;
    let live = true;
    const read = async () => {
      const out = await io.progress(quote.depositId);
      if (!live || !mounted.current || !out.ok) return;
      setProgress(out.data);
      // A credited deposit is the one event in the wallet that buzzes: it is money arriving, and it is the only
      // thing here the user is waiting on that they cannot see happen.
      if (out.data.credited) haptic("fill");
      if (out.data.credited || out.data.steps.some((s) => s.state === "stopped")) live = false;
    };
    void read();
    const timer = setInterval(() => void read(), POLL_MS);
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, [quote, io]);

  if (!card.present) {
    return (
      <div>
        <h2>Deposit</h2>
        <p role="status">{card.note || "No wallet yet — /wallet in the bot creates one, and this screen fills in with it."}</p>
      </div>
    );
  }

  return (
    <div>
      <h2>Deposit USDC</h2>
      <p>{depositSentence(chain, quote?.amountMicro ?? 0)}</p>
      <div role="group" aria-label="Chain">
        {(["polygon", "base", "arbitrum", "ethereum"] as Chain[]).map((option) => (
          <button key={option} type="button" aria-pressed={chain === option} onClick={() => setChain(option)}>
            {option}
          </button>
        ))}
      </div>
      <label>
        Amount you are sending (USDC)
        <input inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)} placeholder="25" />
      </label>

      <QrCode text={qrText || "0x"} label={`Deposit address on ${chain}`} />
      <p>
        <code>{address}</code>
      </p>
      <button
        type="button"
        onClick={() => {
          void navigator.clipboard?.writeText(address).then(() => setCopied(true));
        }}
      >
        {copied ? "Copied" : "Copy address"}
      </button>

      {readOnly ? null : (
        <button type="button" disabled={busy || !amount} onClick={() => void ask()}>
          {busy ? "Starting…" : "I have sent it — watch for it"}
        </button>
      )}
      {error ? <p role="alert">{error}</p> : null}

      {progress ? (
        <div>
          <h3>{progress.credited ? "Credited" : "Watching your transfer"}</h3>
          {/* Four legs, in the API's own words, with their state. The line under them is the sentence the screen
              promises, and it is written by the mapper — a stopped deposit says a human has it. */}
          <ol className="pgm-tma-steps">
            {progress.steps.map((step) => (
              <li key={step.key} data-state={step.state}>
                <span>{step.label}</span>
              </li>
            ))}
          </ol>
          <p role="status">{progress.line}</p>
          {/* Confirmations as text, never as a progress ring: a number that moves on a timer is the one number the
              eye catches, and this one is not the user's to act on. */}
          {!progress.credited && progress.minConfirmations > 0 ? (
            <p><em>{progress.confirmations} of {progress.minConfirmations} confirmations so far.</em></p>
          ) : null}
          {progress.txHash ? <p><em>Transaction {progress.txHash.slice(0, 18)}…</em></p> : null}
        </div>
      ) : null}
    </div>
  );
}

// ------------------------------------------------------------------------------------------------ the withdraw pane
function WithdrawPane({ card, destinations, io, readOnly }:
  { card: WalletCard; destinations: Destination[]; io: WalletIO; readOnly: boolean }) {
  const [ceremony, setCeremony] = useState<Ceremony>(() => ceremonyStart());
  const [receipt, setReceipt] = useState<{ withdrawalId: number; destination: string; note: string } | null>(null);
  const mounted = useRef(true);
  useEffect(() => () => {
    mounted.current = false;
  }, []);

  const findings = ceremonyFindings(ceremony, card);
  const position = ceremonyPosition(ceremony.step);
  const here = CEREMONY.find((entry) => entry.step === ceremony.step);
  const destination = destinations.find((d) => d.id === ceremony.addressId);

  const send = useCallback(async () => {
    // The key is minted on the send and reused by any retry of the same ceremony. (An earlier version minted it on
    // the first tap and returned, so the first tap of "Send withdrawal" sent nothing — a button that looks like it
    // worked and did not.)
    const key = ceremony.key || ceremonyKey(io.mintKey);
    setCeremony((c) => ({ ...c, key, busy: true, error: undefined, refusalCode: undefined }));
    const out = await io.withdraw({ body: ceremonyBody(ceremony), idempotencyKey: key });
    if (!mounted.current) return;
    if (out.ok) {
      haptic("fill");
      setReceipt(out.data);
      // The key stays on the done state until the pane is reset: the receipt is the answer to *this* ceremony, and a
      // re-send of the same key is answered from the stored body rather than performing a second withdrawal.
      setCeremony((c) => ({ ...c, busy: false, step: "done" }));
      return;
    }
    haptic("reject");
    const step = stepForRefusal(out.code);
    setCeremony((c) => ({
      ...c,
      busy: false,
      // The key is cleared, exactly as the server abandons a refused key: keeping it would make the retry answer
      // IDEM_CONFLICT against our own refusal.
      key: "",
      refusalCode: out.code,
      error: plainRefusal(out.code, out.detail),
      step: step ?? c.step,
    }));
  }, [ceremony, io]);

  if (receipt) {
    return (
      <div>
        <h2>Withdrawal requested</h2>
        <p role="status">
          {usdcText(ceremony.amountUsdc)} USDC to {receipt.destination.slice(0, 6)}…{receipt.destination.slice(-4)} —
          recorded and queued.
        </p>
        {/* The one sentence this pane must not soften: nothing has been signed, so nothing has been paid. */}
        <p><em>{receipt.note}</em></p>
        <button type="button" onClick={() => { setReceipt(null); setCeremony(ceremonyStart()); }}>Done</button>
      </div>
    );
  }

  return (
    <div>
      <h2>Withdraw USDC</h2>
      <p><em>{card.lockText}</em></p>
      {destinations.length === 0 ? (
        <p role="status">
          No destinations yet. An address can only be added from the bot, and it cannot be used for 24 hours after —
          that hold is what makes a changed destination visible before money moves.
        </p>
      ) : null}

      <p role="status">Step {position.index} of {position.total} · {here?.title}</p>
      <p><em>{here?.help}</em></p>

      {ceremony.step === "amount" ? (
        <label>
          Amount in USDC
          <input inputMode="decimal" value={ceremony.amountUsdc}
                 onChange={(e) => setCeremony((c) => ({ ...c, amountUsdc: e.target.value }))} />
        </label>
      ) : null}

      {ceremony.step === "destination" ? (
        <ul>
          {destinations.map((option) => (
            <li key={option.id}>
              <button type="button" aria-pressed={ceremony.addressId === option.id}
                      onClick={() => setCeremony((c) => ({ ...c, addressId: option.id }))}>
                {option.label} · {option.display}
              </button>
              {option.usable ? null : <span> ({option.cooldownText})</span>}
            </li>
          ))}
        </ul>
      ) : null}

      {ceremony.step === "typed" ? (
        <div>
          <p>
            Amount <strong>{ceremony.amountUsdc}</strong> · destination{" "}
            <strong>{destination?.display ?? "—"}</strong>
          </p>
          {/* Two fields, and neither may be pasted from the line above without being read: the check is that a
              clipboard swap changes one of them, and the server refuses a mismatch with a 422 that names the field. */}
          <label>
            Type the amount
            <input inputMode="decimal" value={ceremony.typedAmount}
                   onChange={(e) => setCeremony((c) => ({ ...c, typedAmount: e.target.value }))} />
          </label>
          <label>
            Type the destination
            <input value={ceremony.typedAddress}
                   onChange={(e) => setCeremony((c) => ({ ...c, typedAddress: e.target.value }))} />
          </label>
          <p><em>The full destination is what you type; the list above shows it shortened, and that is on purpose.</em></p>
        </div>
      ) : null}

      {ceremony.step === "password" ? (
        <label>
          Password
          <input type="password" value={ceremony.password}
                 onChange={(e) => setCeremony((c) => ({ ...c, password: e.target.value }))} />
        </label>
      ) : null}

      {ceremony.step === "code" ? (
        <label>
          Authenticator code
          <input inputMode="numeric" value={ceremony.code} maxLength={6}
                 onChange={(e) => setCeremony((c) => ({ ...c, code: e.target.value }))} />
        </label>
      ) : null}

      {ceremony.error ? (
        <p className={CLASS.reject} role="alert">
          {ceremony.error}
          {ceremony.refusalCode ? <code> {ceremony.refusalCode}</code> : null}
        </p>
      ) : null}

      {!readOnly && ceremony.step !== "done" ? (
        <div role="group" aria-label="Withdrawal">
          {findings.length > 0 && ceremony.step !== "code" ? <p><em>{findings.join(" ")}</em></p> : null}
          {ceremony.step !== "amount" ? (
            <button
              type="button"
              onClick={() => setCeremony((c) => ({
                ...c, step: previousStep(c.step), error: undefined, refusalCode: undefined,
              }))}
            >
              Back
            </button>
          ) : null}
          <button
            type="button"
            disabled={ceremony.busy || findings.length > 0}
            onClick={() => {
              // The last step is the request: `code` is where the ladder ends, and everything before it is a local
              // walk with no network at all.
              const next = nextStep(ceremony.step);
              if (next === null) void send();
              else setCeremony((c) => ({ ...c, step: next, error: undefined, refusalCode: undefined }));
            }}
          >
            {ceremony.step === "code" ? (ceremony.busy ? "Sending…" : "Send withdrawal") : "Continue"}
          </button>
        </div>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------------------------------- the keys pane
function KeysPane({ card, io, readOnly }: { card: WalletCard; io: WalletIO; readOnly: boolean }) {
  const [draft, setDraft] = useState(exportStart());
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const keyRef = useRef("");
  const mounted = useRef(true);
  useEffect(() => () => {
    mounted.current = false;
  }, []);

  const findings = exportFindings(draft, card.locks.totp);

  const send = useCallback(async () => {
    if (!keyRef.current) keyRef.current = ceremonyKey(io.mintKey);
    setBusy(true);
    setError(null);
    const out = await io.exportKey({ ...draft, idempotencyKey: keyRef.current });
    if (!mounted.current) return;
    setBusy(false);
    if (!out.ok) {
      haptic("reject");
      keyRef.current = "";
      setError(plainRefusal(out.code, out.detail));
      return;
    }
    haptic("fill");
    keyRef.current = "";
    setResult(out.data);
  }, [draft, io]);

  if (result) {
    return (
      <div>
        <h2>Key material</h2>
        <p role="status">{exportReceiptText(result)}</p>
        <pre>{String(result.wrappedKey ?? "")}</pre>
        <p><em>{String(result.note ?? "")}</em></p>
        <button type="button" onClick={() => { setResult(null); setDraft(exportStart()); }}>I have stored it</button>
      </div>
    );
  }

  return (
    <div>
      <h2>Export your key</h2>
      <p role="alert">{EXPORT_WARNING}</p>
      <label>
        Type {EXPORT_CONFIRM_WORD} to continue
        <input value={draft.typedConfirm} onChange={(e) => setDraft((d) => ({ ...d, typedConfirm: e.target.value }))} />
      </label>
      <label>
        Password
        <input type="password" value={draft.password} onChange={(e) => setDraft((d) => ({ ...d, password: e.target.value }))} />
      </label>
      <label>
        Authenticator code
        <input inputMode="numeric" maxLength={6} value={draft.code}
               onChange={(e) => setDraft((d) => ({ ...d, code: e.target.value }))} />
      </label>
      {error ? <p className={CLASS.reject} role="alert">{error}</p> : null}
      {readOnly ? null : (
        <div>
          {findings.length > 0 ? <p><em>{findings.join(" ")}</em></p> : null}
          <button type="button" disabled={busy || findings.length > 0} onClick={() => void send()}>
            {busy ? "Preparing…" : "Export key material"}
          </button>
        </div>
      )}
    </div>
  );
}

export default WalletScreen;
