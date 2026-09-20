"use client";
/**
 * D5 · the referral dashboard, and the referee's side of it on the same page.
 *
 * The kit's D5 is a funnel, a payout reality and a set of defences, and the screen has to make all three
 * checkable by the person they apply to:
 *
 *  1. **The funnel is drawn as the money chain plus one leading count.** Clicks are shown apart and labelled with
 *     the API's own sentence, because they are not a ceiling on signups; the four steps that move money are shown
 *     as a chain, and a chain that is not monotone is reported rather than drawn (`funnelFindings`).
 *  2. **Every bucket is visible, including the two a screen would rather hide** — what was paid, and what was
 *     clawed back with the rule that did it. `hidden.refused` says how many attempts were refused and why they are
 *     not listed here.
 *  3. **The defences are stated in the second person.** The rules list carries the device/funding dedupe, the
 *     clawback and the self-referral revocation ground, and it is rendered from the API rather than paraphrased.
 *  4. **The referee has a screen too.** Applying a code is the same page's other half, and its answer is the API's
 *     sentence: under review is not refused, and refused is not a mystery.
 *
 * The short code is validated by the server. This component deliberately does NOT re-implement the shape rules:
 * it sends what was typed and renders the refusal, which names the rule that broke (`CODE_INVALID`).
 */
import { useCallback, useEffect, useState } from "react";
import { t } from "@/i18n/terminal";
import { request } from "@/api/client";
import { freshnessOf, stampAge, type Stamp } from "@/api/envelope";
import { StaleIndicator } from "@/num/StaleIndicator";
import { Number } from "@/num/Number";
import { microToCents } from "@/money/cents";
import { Button } from "@/ui/Button";
import { useNow } from "./useTerminal";
import {
  SIGNED_OUT,
  applyOutcome,
  earningsLines,
  funnelSteps,
  moneyStepsAreMonotone,
  payoutGap,
  payoutLines,
  qualificationLine,
  type EarningsKey,
  type PayoutKey,
  referrerRows,
  reviewLine,
  type ReferralApply,
  type ReferralCode,
  type ReferralMe,
  type ReferralTerms,
} from "./referrals";

/**
 * The labels, as literal `t()` calls in a table rather than a template key: `scripts/i18n-check.mjs` fails a
 * dynamic lookup (`t(`x.${y}`)`) on purpose, because a key built at runtime is a key no check can see. The maps
 * are typed against the unions `referrals.ts` returns, so a new step or bucket cannot arrive without a label.
 */
const STEP_LABEL: Record<string, string> = {
  clicks: t("terminal.referrals.step.clicks"),
  signups: t("terminal.referrals.step.signups"),
  funded: t("terminal.referrals.step.funded"),
  trading: t("terminal.referrals.step.trading"),
  earned: t("terminal.referrals.step.earned"),
};

const EARNINGS_LABEL: Record<EarningsKey, string> = {
  accrued: t("terminal.referrals.money.accrued"),
  holding: t("terminal.referrals.money.holding"),
  payable: t("terminal.referrals.money.payable"),
  paid: t("terminal.referrals.money.paid"),
  clawedBack: t("terminal.referrals.money.clawedBack"),
};

const PAYOUT_LABEL: Record<PayoutKey, string> = {
  schedule: t("terminal.referrals.payout.schedule"),
  method: t("terminal.referrals.payout.method"),
  taxForm: t("terminal.referrals.payout.taxForm"),
  taxReport: t("terminal.referrals.payout.taxReport"),
};

export function ReferralsView() {
  const [me, setMe] = useState<ReferralMe | null>(null);
  const [terms, setTerms] = useState<ReferralTerms | null>(null);
  const [stamp, setStamp] = useState<Stamp | null>(null);
  const [signedOut, setSignedOut] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [typed, setTyped] = useState("");
  const [refereeCode, setRefereeCode] = useState("");
  const [busy, setBusy] = useState(false);
  const now = useNow(1_000);

  const load = useCallback(async () => {
    // The terms are PUBLIC, so they are read even when the dashboard is not: a visitor gets the model, the rules
    // and the payout reality, which is the half of this page that can be read without an account.
    const [a, b] = await Promise.all([
      request<ReferralMe>({ key: "referralsMe" }),
      request<{ terms: ReferralTerms; note: string }>({ key: "referralsTerms" }),
    ]);
    if (b.ok) setTerms(b.data.terms);
    if (a.ok === false) {
      if (a.error.status === 401) setSignedOut(true);
      else setErr(a.error.message);
      return;
    }
    setSignedOut(false);
    setErr(null);
    setMe(a.data);
    setStamp(a.stamp);
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const fresh = freshnessOf(stamp, now);

  async function claim() {
    setBusy(true);
    setNotice(null);
    const res = await request<ReferralCode>({ key: "referralCode", body: { code: typed } });
    setBusy(false);
    if (!res.ok) {
      setErr(res.error.message);
      return;
    }
    setErr(null);
    setNotice(res.data.note || t("terminal.referrals.codeSaved"));
    await load();
  }

  async function apply() {
    setBusy(true);
    setNotice(null);
    const res = await request<ReferralApply>({ key: "referralApply", body: { code: refereeCode } });
    setBusy(false);
    if (!res.ok) {
      // A refusal is an answer, not a crash: `SELF_REFERRAL` and `REFUSED` are sentences written for the person,
      // and `ALREADY_REFERRED` says an appeal is the way back rather than a second code.
      setErr(res.error.message);
      return;
    }
    setErr(null);
    setNotice(applyOutcome(res.data));
    await load();
  }

  const shown = terms ?? me?.terms ?? null;

  return (
    <section className="pgm-referrals" aria-labelledby="referrals-title">
      <header className="pgm-referrals__head">
        <p>{t("terminal.referrals.blurb")}</p>
        {stamp ? <StaleIndicator freshness={fresh} ageMs={stampAge(stamp, now)} /> : null}
      </header>

      {signedOut ? <p className="refusal">{SIGNED_OUT}</p> : null}
      {signedOut ? null : err ? <p className="refusal">{err}</p> : null}
      {signedOut ? null : !me ? <p className="refusal">{t("common.state.loading")}</p> : null}
      {notice ? <p className="pgm-referrals__notice">{notice}</p> : null}

      {me ? (
        <>
          <h3>{t("terminal.referrals.funnelTitle")}</h3>
          <table className="pgm-referrals__funnel">
            <caption>{t("terminal.referrals.funnelCaption")}</caption>
            <thead>
              <tr>
                <th scope="col">{t("terminal.referrals.col.step")}</th>
                <th scope="col">{t("terminal.referrals.col.count")}</th>
                <th scope="col">{t("terminal.referrals.col.means")}</th>
              </tr>
            </thead>
            <tbody>
              {funnelSteps(me).map((step) => (
                <tr key={step.key} data-leading={step.leading ? "true" : undefined}>
                  <th scope="row">{STEP_LABEL[step.key]}</th>
                  <td>
                    <Number kind="count" value={step.count} />
                  </td>
                  <td>{step.meaning}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {moneyStepsAreMonotone(me) ? null : <p className="refusal">{me.note}</p>}
          {me.funnelFindings.length ? (
            <ul className="pgm-referrals__findings">
              {me.funnelFindings.map((f) => (
                <li key={f}>{f}</li>
              ))}
            </ul>
          ) : null}
          <p className="pgm-referrals__hidden">{me.hidden.note}</p>

          <h3>{t("terminal.referrals.earningsTitle")}</h3>
          <dl className="pgm-referrals__money">
            {earningsLines(me).map((row) => (
              <div key={row.key}>
                <dt>{EARNINGS_LABEL[row.key]}</dt>
                <dd>
                  <Number kind="money" value={microToCents(row.micro)} />
                </dd>
              </div>
            ))}
          </dl>
          <p>
            <strong>
              <Number kind="money" value={microToCents(payoutGap(me).micro)} />
            </strong>{" "}
            — {payoutGap(me).sentence}
          </p>
          <p>{me.earnings.note}</p>

          <h3>{t("terminal.referrals.payoutTitle")}</h3>
          <dl className="pgm-referrals__payout">
            {payoutLines(me).map((row) => (
              <div key={row.key}>
                <dt>{PAYOUT_LABEL[row.key]}</dt>
                <dd>{row.value}</dd>
              </div>
            ))}
            <div>
              <dt>{t("terminal.referrals.payout.next")}</dt>
              <dd>{new Date(me.payout.nextAtMs).toISOString().slice(0, 10)}</dd>
            </div>
          </dl>
          <p>{me.payout.note}</p>

          {reviewLine(me) ? <p className="pgm-referrals__review">{reviewLine(me)}</p> : null}

          <h3>{t("terminal.referrals.linkTitle")}</h3>
          <p>{t("terminal.referrals.linkBlurb")}</p>
          <p className="pgm-referrals__link">{me.link.url}</p>
          <p>
            {me.link.code ? (
              <>
                {t("terminal.referrals.shortCodeLabel")} <code>{me.link.shortUrl}</code>
              </>
            ) : (
              t("terminal.referrals.noCode")
            )}
          </p>
          <label>
            {t("terminal.referrals.codeLabel")}
            <input
              type="text"
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
              maxLength={24}
              aria-describedby="referral-code-rules"
            />
          </label>
          <p id="referral-code-rules">{shown ? shown.rules[0] : ""}</p>
          <Button variant="primary" pending={busy} disabled={!typed.trim()} onClick={() => void claim()}>
            {t("terminal.referrals.codeSave")}
          </Button>

          <h3>{t("terminal.referrals.refereeTitle")}</h3>
          <p>{t("terminal.referrals.refereeBlurb")}</p>
          <label>
            {t("terminal.referrals.refereeLabel")}
            <input
              type="text"
              value={refereeCode}
              onChange={(e) => setRefereeCode(e.target.value)}
              maxLength={64}
            />
          </label>
          <Button pending={busy} disabled={!refereeCode.trim()} onClick={() => void apply()}>
            {t("terminal.referrals.refereeApply")}
          </Button>

          <h3>{t("terminal.referrals.refereesTitle")}</h3>
          <table className="pgm-referrals__rows">
            <thead>
              <tr>
                <th scope="col">{t("terminal.referrals.col.referee")}</th>
                <th scope="col">{t("terminal.referrals.col.state")}</th>
                <th scope="col">{t("terminal.referrals.col.term")}</th>
                <th scope="col">{t("terminal.referrals.col.earned")}</th>
              </tr>
            </thead>
            <tbody>
              {referrerRows(me).map((r) => (
                <tr key={r.key} data-state={r.state}>
                  <th scope="row">{r.referee}</th>
                  <td>{r.stateText}</td>
                  <td>{r.daysLeft === null ? t("terminal.referrals.term.notYet") : `${r.daysLeft} ${t("terminal.referrals.term.days")}`}</td>
                  <td>
                    <Number kind="money" value={microToCents(r.earnedMicro)} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {referrerRows(me).length === 0 ? <p className="refusal">{t("terminal.referrals.refereesEmpty")}</p> : null}
        </>
      ) : null}

      {shown ? (
        <>
          <h3>{t("terminal.referrals.modelTitle")}</h3>
          <p>{shown.modelSentence}</p>
          <p>{qualificationLine(shown)}</p>
          <p>{shown.paidFrom}</p>
          <h4>{t("terminal.referrals.rejectedTitle")}</h4>
          <ul className="pgm-referrals__rejected">
            {shown.rejectedModels.map((r) => (
              <li key={r}>{r}</li>
            ))}
          </ul>
          <h4>{t("terminal.referrals.rulesTitle")}</h4>
          <ul className="pgm-referrals__rules">
            {shown.rules.map((r) => (
              <li key={r}>{r}</li>
            ))}
          </ul>
          <h4>{t("terminal.referrals.leaderboardTitle")}</h4>
          <p>{shown.noReferrerLeaderboard}</p>
        </>
      ) : null}
    </section>
  );
}
