"use client";
/**
 * D4 · the self-rank strip and the identity control.
 *
 * The kit's D4 is a retention hook and a privacy question, and the screen answers both in one panel:
 *
 *  1. **Your row, on every board, with the gap that would move it.** The nine answers come from one read
 *     (`/v1/leaderboard/me`), so the panel cannot disagree with itself about window or recompute instant.
 *  2. **The strip is pinned only when the row is off the page**, and the sentence says which page it is on
 *     instead. A strip that appears while the row is already visible is noise; one that never appears makes the
 *     reader hunt. The decision is the server's `offPage`, drawn here.
 *  3. **Unranked is a to-do list, not a shrug.** The steps name the numbers (settled markets, verified turnover,
 *     a removed wash) and are rendered as the API wrote them.
 *  4. **The identity control states the half nobody expects**: staying private does not take your wallet off the
 *     board. `doesNotChange` is rendered next to `changes`, from the API, because a privacy control that lists
 *     only what it grants is a control that hides the rest.
 *
 * The handle input is validated by the server (shape, reserved words, collisions) and the panel does not
 * re-implement that: it sends what the reader typed and renders the refusal, which names the field.
 */
import { useCallback, useEffect, useState } from "react";
import { t } from "@/i18n/terminal";
import { request } from "@/api/client";
import { freshnessOf, stampAge, type Stamp } from "@/api/envelope";
import { StaleIndicator } from "@/num/StaleIndicator";
import { Button } from "@/ui/Button";
import { useNow } from "./useTerminal";
import {
  boardRows,
  fieldOf,
  gapOf,
  handleDraft,
  identityDetail,
  identityText,
  nudgeText,
  pinFor,
  pinLine,
  stepsFor,
  type Identity,
  type IdentityView,
  type SelfRank as SelfRankPayload,
} from "./selfRank";

export function SelfRank({ board, category = "" }: { board: string; category?: string }) {
  const [me, setMe] = useState<SelfRankPayload | null>(null);
  const [stamp, setStamp] = useState<Stamp | null>(null);
  const [view, setView] = useState<IdentityView | null>(null);
  const [signedOut, setSignedOut] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const now = useNow(1_000);

  const load = useCallback(async () => {
    const [a, b] = await Promise.all([
      request<SelfRankPayload>({ key: "leaderboardMe" }),
      request<IdentityView>({ key: "leaderboardIdentity" }),
    ]);
    if (a.ok === false) {
      // 401 is not an error state on a public page: it is the honest answer for a visitor, and the panel says
      // what signing in would show rather than rendering an empty panel with a spinner.
      if (a.error.status === 401) setSignedOut(true);
      else setErr(a.error.message);
      return;
    }
    setSignedOut(false);
    setErr(null);
    setMe(a.data);
    setStamp(a.stamp);
    if (b.ok) setView(b.data);
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const identity: Identity | null = me?.identity ?? view?.identity ?? null;
  const wallet = me?.wallets?.[0] ?? null;
  const pin = me ? pinFor(me, board, category) : null;
  const fresh = freshnessOf(stamp, now);

  async function save(state: "private" | "listed") {
    setBusy(true);
    const body: Record<string, string> = { state };
    // The draft rule is in `selfRank.handleDraft`, not here: an empty box means "keep the handle you already
    // claimed" only when there is one, and on a first listing it means the server decides the handle from the
    // pseudonym. Typing a name and then opting in is the flow the nudge is asking for.
    const wanted = handleDraft(state, typed, view?.handle.claimed ?? "");
    if (wanted) body.handle = wanted;
    const res = await request<{ identity: Identity; note: string }>({
      key: "leaderboardIdentitySet",
      body,
    });
    setBusy(false);
    if (!res.ok) {
      setErr(res.error.message);
      return;
    }
    setErr(null);
    await load();
  }

  return (
    <section className="pgm-selfrank" aria-labelledby="selfrank-title">
      <header className="pgm-selfrank__head">
        <h2 id="selfrank-title">{t("terminal.selfrank.title")}</h2>
        <p>{t("terminal.selfrank.blurb")}</p>
        {stamp ? <StaleIndicator freshness={fresh} ageMs={stampAge(stamp, now)} /> : null}
      </header>

      {signedOut ? (
        <p className="refusal">{t("terminal.selfrank.signedOut")}</p>
      ) : (
        <>
          {err ? <p className="refusal">{err}</p> : null}
          {!me ? <p className="refusal">{t("common.state.loading")}</p> : null}
          {me ? (
            <>
              <p className="pgm-selfrank__identity">
                <strong>{identityText(identity)}</strong> — {identityDetail(identity)}
              </p>
              {wallet ? (
                <table className="pgm-selfrank__boards">
                  <caption>{t("terminal.selfrank.caption")}</caption>
                  <thead>
                    <tr>
                      <th scope="col">{t("terminal.selfrank.col.board")}</th>
                      <th scope="col">{t("terminal.selfrank.col.placing")}</th>
                      <th scope="col">{t("terminal.selfrank.col.gap")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {boardRows(wallet).map((r) => (
                      <tr key={r.key} data-pinned={r.pinned ? "true" : undefined}>
                        <th scope="row">{r.label}</th>
                        <td>{r.placing}</td>
                        <td>{r.gap}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <p className="refusal">{me.note}</p>
              )}
              {stepsFor(wallet).length ? (
                <ol className="pgm-selfrank__steps">
                  {stepsFor(wallet).map((s) => (
                    <li key={s}>{s}</li>
                  ))}
                </ol>
              ) : null}
              {view ? (
                <div className="pgm-selfrank__identity-control">
                  <h3>{t("terminal.selfrank.identityTitle")}</h3>
                  <p>{nudgeText(view)}</p>
                  <label>
                    {t("terminal.selfrank.handleLabel")}
                    <input
                      type="text"
                      value={typed || view.handle.claimed}
                      onChange={(e) => setTyped(e.target.value)}
                      maxLength={24}
                      aria-describedby="handle-rules"
                    />
                  </label>
                  <p id="handle-rules">{view.handle.rules}</p>
                  {(view.handle.reserved ?? []).length ? (
                    <p>
                      {t("terminal.selfrank.reservedLabel")} {(view.handle.reserved ?? []).join(", ")}
                    </p>
                  ) : null}
                  <div className="pgm-selfrank__buttons">
                    <Button
                      pending={busy}
                      disabled={identity?.state === "private"}
                      onClick={() => void save("private")}
                    >
                      {t("terminal.selfrank.stayPrivate")}
                    </Button>
                    <Button
                      variant="primary"
                      pending={busy}
                      disabled={identity?.state === "listed"}
                      onClick={() => void save("listed")}
                    >
                      {t("terminal.selfrank.beListed")}
                    </Button>
                  </div>
                  <h4>{t("terminal.selfrank.changesTitle")}</h4>
                  <ul className="pgm-selfrank__changes">
                    {view.changes.map((c) => (
                      <li key={c}>{c}</li>
                    ))}
                  </ul>
                  <h4>{t("terminal.selfrank.doesNotChange")}</h4>
                  <ul className="pgm-selfrank__nochange">
                    {view.doesNotChange.map((c) => (
                      <li key={c}>{c}</li>
                    ))}
                  </ul>
                </div>
              ) : null}
            </>
          ) : null}
        </>
      )}

      {pin && pin.offPage ? (
        <div className="pgm-selfrank__pin" role="status">
          <strong>{t("terminal.selfrank.pinLabel")}</strong> <span>{pinLine(pin)}</span>
          {fieldOf(pin) ? <span className="pgm-selfrank__pin-field">{fieldOf(pin)}</span> : null}
          {gapOf(pin) ? <span className="pgm-selfrank__pin-gap">{gapOf(pin)}</span> : null}
        </div>
      ) : null}
    </section>
  );
}
