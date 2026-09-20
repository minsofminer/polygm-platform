"use client";
/**
 * D7 · the anti-gaming dashboard, for the operator.
 *
 * The screen has one job: make a decision *reviewable*. Four things follow from that, and each one is a rule
 * rather than a preference:
 *
 *  1. **Both readings are always on the row.** The rule the finding came from and the innocent explanation of
 *     the same shape are rendered side by side, from the API's own strings. A screen that shows only the damning
 *     reading is a screen whose output gets acted on before it is read, and this is the surface where acting
 *     wrongly costs somebody their standing.
 *  2. **The evidence is the row's own numbers**, not a summary: the rank it came from, the sample behind it, the
 *     overlap and the denominator, the fee counts. The operator can disagree with the rule instead of guessing.
 *  3. **A decision carries a reason, and the button is disabled without one.** The row that lands is what answers
 *     an appeal; "suspicious" is not a record. The form says so before the click, not after.
 *  4. **The token is held in memory for this tab and never stored.** There is no cookie, no `localStorage`, and
 *     a reload asks for it again — an operator token that survives the browser being handed to somebody else is
 *     the whole risk of having an internal tool.
 *
 * What this component deliberately does NOT do: decide anything. Which actions a row offers, what body a click
 * sends and whether the reason is long enough to be a record all live in `gaming.ts`, where they are unit-tested;
 * this file renders what that module returns and posts it.
 */
import { useCallback, useState } from "react";
import { t } from "@/i18n/admin";
import { request } from "@/api/client";
import { Number } from "@/num/Number";
import { microToCents } from "@/money/cents";
import { Button } from "@/ui/Button";
import {
  SECTIONS,
  actionsFor,
  decideTarget,
  decision,
  pairing,
  severityLabel,
  subjects,
  type Dashboard,
  type Finding,
  type SectionKey,
} from "./gaming";

const SECTION_TITLE: Record<SectionKey, string> = {
  climbers: t("admin.gaming.section.climbers"),
  clusters: t("admin.gaming.section.clusters"),
  chains: t("admin.gaming.section.chains"),
  builder: t("admin.gaming.section.builder"),
};

/** Literal `t()` calls, never a template key: `scripts/i18n-check.mjs` fails a dynamically built key on purpose. */
const KIND_LABEL: Record<string, string> = {
  fast_climb: t("admin.gaming.finding.fast-climb"),
  correlated_cluster: t("admin.gaming.finding.correlated-cluster"),
  synthetic_chain: t("admin.gaming.finding.synthetic-chain"),
  builder_anomaly: t("admin.gaming.finding.builder-anomaly"),
};

const ACTION_LABEL: Record<string, string> = {
  exclude: t("admin.gaming.exclude"),
  flag: t("admin.gaming.flag"),
  include: t("admin.gaming.include"),
  clear: t("admin.gaming.include"),
};

function whenText(ms: number, nowMs: number): string {
  const mins = Math.max(0, Math.round((nowMs - ms) / 60_000));
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min ago`;
  return `${Math.floor(mins / 60)} h ago`;
}

function FindingRow({
  finding,
  rules,
  token,
  onDecided,
  nowMs,
}: {
  finding: Finding;
  rules: Dashboard["rules"];
  token: string;
  onDecided: () => void;
  nowMs: number;
}) {
  const [reason, setReason] = useState("");
  const [problem, setProblem] = useState("");
  const pair = pairing(finding, rules);
  const names = subjects(finding);
  const tooShort = reason.trim().length < 8;

  const act = useCallback(
    async (action: string) => {
      const body = decision(finding, action, reason);
      if (!body) {
        setProblem(t("admin.gaming.reasonNeeded"));
        return;
      }
      setProblem("");
      const out = await request<{ action: string; board: string; atMs: number }>({
        key: body.key,
        body: body.body,
        idempotencyKey: body.idempotencyKey,
        headers: { "x-admin-token": token },
      });
      if (!out.ok) {
        setProblem(t("admin.gaming.decideFailed").replace("{reason}", out.error.message));
        return;
      }
      setReason("");
      onDecided();
    },
    [finding, reason, token, onDecided],
  );

  return (
    <li className="pgm-gaming__row" data-kind={finding.kind} data-severity={finding.severity}>
      <header className="pgm-gaming__rowHead">
        <span className="pgm-gaming__kind">{KIND_LABEL[finding.kind] ?? finding.kind}</span>
        <span className="pgm-gaming__severity" data-sev={severityLabel(finding.severity)}>
          {t("admin.gaming.severity").replace("{n}", String(finding.severity))}
        </span>
        {names.map((n) => (
          <code key={n} className="pgm-gaming__anon">
            {n}
          </code>
        ))}
        {/* The raw wallet is shown BECAUSE this screen is the one place a decision can be keyed by it — and it
            sits next to the pseudonym, which is what every sentence written afterwards quotes. */}
        <code className="pgm-gaming__wallet" title="the id the exclusion table is keyed by">
          {decideTarget(finding)}
        </code>
      </header>

      {pair ? (
        <>
          <p className="pgm-gaming__rule">
            <strong>{t("admin.gaming.ruleLabel")}:</strong> {pair.rule}
          </p>
          <p className="pgm-gaming__innocent">
            <strong>{t("admin.gaming.innocentLabel")}:</strong> {pair.innocent}
          </p>
        </>
      ) : (
        <p className="pgm-gaming__rule is-bad">
          {t("admin.gaming.refused").replace("{reason}", "the finding arrived without its rule")}
        </p>
      )}

      <dl className="pgm-gaming__evidence">
        <dt>{t("admin.gaming.evidenceLabel")}</dt>
        {finding.evidence.map((line) => (
          <dd key={line}>{line}</dd>
        ))}
        {finding.board ? (
          <dd>
            board: <code>{finding.board}</code> · {finding.fromRank} → {finding.toRank} ({finding.climb} places)
          </dd>
        ) : null}
        {finding.worstOverlapBps !== undefined ? (
          <dd>
            overlap {Math.round(finding.worstOverlapBps / 100)}% of {finding.walletCount} wallet(s) ·{" "}
            {finding.coTimed} co-timed fill(s)
          </dd>
        ) : null}
        {finding.accrualMicro ? (
          <dd>
            accruals at stake <Number kind="money" value={microToCents(finding.accrualMicro)} />
          </dd>
        ) : null}
        {finding.volumeMicro ? (
          <dd>
            attributed volume <Number kind="money" value={microToCents(finding.volumeMicro)} />
          </dd>
        ) : null}
      </dl>

      {finding.decided ? (
        <p className="pgm-gaming__reviewed">
          {t("admin.gaming.reviewed")
            .replace("{action}", finding.decided.action)
            .replace("{when}", whenText(finding.decided.atMs, nowMs))}
        </p>
      ) : null}

      <div className="pgm-gaming__act">
        <label>
          <span>{t("admin.gaming.reasonLabel")}</span>
          <input
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder={t("admin.gaming.reasonPlaceholder")}
            aria-label={t("admin.gaming.reasonLabel")}
          />
        </label>
        {actionsFor(finding).map((action, i) => (
          <Button
            key={action}
            variant={i === 0 ? "primary" : "default"}
            disabled={tooShort}
            onClick={() => void act(action)}
          >
            {ACTION_LABEL[action] ?? action}
          </Button>
        ))}
      </div>
      {problem ? <p className="pgm-gaming__problem">{problem}</p> : null}
    </li>
  );
}

export function GamingView() {
  const [token, setToken] = useState("");
  const [board, setBoard] = useState<Dashboard | null>(null);
  const [problem, setProblem] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");

  const read = useCallback(async (tok: string) => {
    setBusy(true);
    setProblem("");
    const out = await request<Dashboard>({ key: "adminGaming", headers: { "x-admin-token": tok } });
    setBusy(false);
    if (!out.ok) {
      setBoard(null);
      setProblem(t("admin.gaming.refused").replace("{reason}", out.error.message));
      return;
    }
    setBoard(out.data);
  }, []);

  return (
    <section className="pgm-gaming" aria-labelledby="gaming-title">
      <header className="pgm-gaming__head">
        <h2 id="gaming-title">{t("admin.gaming.title")}</h2>
        <p className="pgm-gaming__lede">{t("admin.gaming.lede")}</p>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void read(token);
          }}
        >
          <label>
            <span>{t("admin.gaming.token")}</span>
            <input
              type="password"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              aria-label={t("admin.gaming.token")}
              autoComplete="off"
            />
          </label>
          <Button type="submit" variant="primary" disabled={busy}>
            {board ? t("admin.gaming.refresh") : t("admin.gaming.load")}
          </Button>
          <p className="pgm-gaming__hint">{t("admin.gaming.tokenHint")}</p>
        </form>
      </header>

      {problem ? <p className="pgm-gaming__problem is-bad">{problem}</p> : null}
      {notice ? <p className="pgm-gaming__notice">{notice}</p> : null}

      {board ? (
        <>
          <p className="pgm-gaming__stamp">
            {t("admin.gaming.evidence")
              .replace("{rows}", String(board.evidence.historyRows))
              .replace("{fills}", String(board.evidence.fills))
              .replace("{decisions}", String(board.evidence.decisions))}{" "}
            ·{" "}
            {t("admin.gaming.stale").replace(
              "{when}",
              board.evidence.newestSnapshotMs ? whenText(board.evidence.newestSnapshotMs, board.atMs) : "never",
            )}
          </p>
          {SECTIONS.map((key: SectionKey) => {
            const rows: Finding[] = board[key];
            return (
              <section key={key} className="pgm-gaming__section" aria-label={SECTION_TITLE[key]}>
                <h3>
                  {SECTION_TITLE[key]} <span className="pgm-gaming__count">{rows.length}</span>
                </h3>
                {rows.length === 0 ? (
                  <p className="pgm-gaming__empty">{t("admin.gaming.empty")}</p>
                ) : (
                  <ul className="pgm-gaming__list">
                    {rows.map((f, i) => (
                      <FindingRow
                        key={`${f.kind}-${decideTarget(f)}-${i}`}
                        finding={f}
                        rules={board.rules}
                        token={token}
                        nowMs={board.atMs}
                        onDecided={() => {
                          setNotice(t("admin.gaming.note"));
                          void read(token);
                        }}
                      />
                    ))}
                  </ul>
                )}
              </section>
            );
          })}
          <p className="pgm-gaming__note">{t("admin.gaming.note")}</p>
        </>
      ) : null}
    </section>
  );
}
