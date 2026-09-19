"use client";
/**
 * D3 · the rating panel: your standing, the traders you follow, and a comparison of up to three.
 *
 * The screen's order is the argument, the same way D7's copy screen argues its own:
 *
 *  1. **The board's rule comes first** — `formula`, `gate` and the sample gate, printed from the API rather than
 *     paraphrased, because the question every leaderboard gets is "how is this ranked".
 *  2. **A standing is never a bare number.** The badge carries the board's size, the gap carries the field it is
 *     measured in, and the sparkline renders only when there is history to draw. A wallet that is not on the
 *     board gets its refusal sentence with the number in it.
 *  3. **Follows are watches and the screen says so**, in the panel's own note and on every acknowledgement, so
 *     nobody reads the button as "trade like them". Turning a follow into a copy is D7's screen, with its own
 *     dry-run and guards.
 *  4. **A comparison is one read of one board.** The pairwise sentences come from the engine, and the screen
 *     repeats no arithmetic of its own — the numbers on it are the API's strings, through the number layer.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { t } from "@/i18n/terminal";
import { request } from "@/api/client";
import { freshnessOf, type Stamp } from "@/api/envelope";
import { SelfRank } from "./SelfRank";
import { Number } from "@/num/Number";
import { StaleIndicator } from "@/num/StaleIndicator";
import { Button } from "@/ui/Button";
import { useNow } from "./useTerminal";
import { bpsText } from "./copy";
import {
  badgeText,
  comparisonVerdict,
  fieldText,
  followAction,
  followCells,
  followStateText,
  gapSentence,
  historySentence,
  percentileText,
  refusalLines,
  sparkPath,
  stateText,
  type Standing,
} from "./board";

type FollowRow = {
  anon: string;
  label: string;
  state: "ranked" | "unranked" | "absent";
  rank: number | null;
  rankBadge?: { rank: number; rankedTotal: number; text: string } | null;
  realisedMicro?: number | null;
  realised?: string | null;
  drawdown?: string | null;
  maxDrawdownMicro?: number | null;
  winRateBps?: number | null;
  insufficientSample?: boolean | null;
  sampleNote?: string;
  reasons?: string[];
};

type Follows = { rows: FollowRow[]; total: number; note: string; states: Record<string, number> };
type Comparison = {
  rows: Record<string, unknown>[];
  unranked: { anon: string; reasons: string[] }[];
  unknown: string[];
  order: { a: string; b: string; why: string; aAbove?: boolean }[];
  verdict: string;
  note: string;
  disclaimer: string;
  formula?: string;
  gate?: string;
  window: string;
  label: string;
};

const SPARK_W = 120;
const SPARK_H = 24;

/** The panel's input: which wallet, once. D4 turns this into "your own", which is why it is a prop and not state. */
export function BoardPanel({ focus = "" }: { focus?: string }) {
  const [anon, setAnon] = useState(focus);
  const [standing, setStanding] = useState<Standing | null>(null);
  const [board, setBoard] = useState("risk_adjusted");
  const [windowKey, setWindowKey] = useState("30d");
  const [follows, setFollows] = useState<Follows | null>(null);
  const [cmp, setCmp] = useState<Comparison | null>(null);
  const [picked, setPicked] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const loadFollows = useCallback(async () => {
    const res = await request<Follows>({ key: "leaderboardFollows", query: { board, window: windowKey } });
    if (!res.ok) {
      setErr(res.error.message);
      return;
    }
    setErr(null);
    setFollows(res.data);
  }, [board, windowKey]);

  const loadStanding = useCallback(async (who: string) => {
    if (!who) {
      setStanding(null);
      return;
    }
    const res = await request<Standing>({ key: "leaderboardRank", query: { anon: who, board, window: windowKey } });
    if (!res.ok) {
      // A 404 is not an empty standing: there is no standing, and the screen says that rather than showing a
      // badge with a dash in it.
      setStanding(null);
      setErr(res.error.code === "NO_SUCH_RESOURCE" ? t("terminal.board.unknownWallet") : res.error.message);
      return;
    }
    setErr(null);
    setStanding(res.data);
  }, [board, windowKey]);

  useEffect(() => {
    void loadFollows();
  }, [loadFollows]);

  useEffect(() => {
    void loadStanding(anon);
  }, [anon, loadStanding]);

  const toggleFollow = useCallback(
    async (who: string, following: boolean) => {
      setBusy(true);
      const res = await request<{ state: string }>({
        key: "leaderboardFollow",
        body: { anon: who, state: followAction(following) },
      });
      setBusy(false);
      if (!res.ok) {
        setErr(res.error.message);
        return;
      }
      await loadFollows();
    },
    [loadFollows],
  );

  const compare = useCallback(
    async (who: string[]) => {
      if (who.length < 2) return;
      const res = await request<Comparison>({
        key: "leaderboardCompare",
        query: { anons: who.join(","), board, window: windowKey },
      });
      if (!res.ok) {
        setErr(res.error.message);
        return;
      }
      setErr(null);
      setCmp(res.data);
    },
    [board, windowKey],
  );

  const now = useNow(1_000);
  const spark = useMemo(() => sparkPath(standing?.history?.points ?? [], SPARK_W, SPARK_H), [standing]);
  // The stamp belongs to whichever read is on screen, and every one of them is stamped by the same middleware.
  // A stamp that is missing (a hook that has not resolved yet) is `unknown` freshness, which is the honest
  // default: the panel does not claim the data is fresh because it does not know when it was read.
  const stamp = (follows ?? cmp ?? standing) as unknown as Stamp | null;
  const fresh = freshnessOf(stamp, now);

  return (
    <section className="pgm-board" aria-label={t("terminal.board.title")}>
      <header className="pgm-board__head">
        <h2>{t("terminal.board.title")}</h2>
        <p className="pgm-board__note">{t("terminal.board.blurb")}</p>
        <div className="pgm-board__picks">
          <label>
            {t("terminal.board.walletLabel")}
            <input
              value={anon}
              onChange={(e) => setAnon(e.target.value.trim())}
              placeholder={t("terminal.board.walletPlaceholder")}
              aria-label={t("terminal.board.walletLabel")}
            />
          </label>
          <label>
            {t("terminal.board.boardLabel")}
            <select value={board} onChange={(e) => setBoard(e.target.value)} aria-label={t("terminal.board.boardLabel")}>
              <option value="risk_adjusted">risk_adjusted</option>
              <option value="win_rate">win_rate</option>
              <option value="volume">volume</option>
              <option value="rising">rising</option>
              <option value="category">category</option>
              <option value="copied">copied</option>
            </select>
          </label>
          <label>
            {t("terminal.board.windowLabel")}
            <select value={windowKey} onChange={(e) => setWindowKey(e.target.value)} aria-label={t("terminal.board.windowLabel")}>
              <option value="24h">24h</option>
              <option value="7d">7d</option>
              <option value="30d">30d</option>
              <option value="90d">90d</option>
            </select>
          </label>
        </div>
        <StaleIndicator freshness={fresh} ageMs={stamp ? Math.max(0, now - stamp.asOf) : null} />
      </header>

      {/* D4 · the reader's own standing, on the board selected above. Inside the panel rather than beside it: the
          strip is pinned to the board the controls say, and two components holding two copies of "which board is
          on screen" is how the strip ends up describing a board the table is not showing. */}
      <SelfRank board={board} />

      {err ? (
        <p className="refusal" role="note">
          <strong>{t("terminal.board.refused")}</strong> <span>{err}</span>
        </p>
      ) : null}

      {standing ? (
        <article className="pgm-board__standing" aria-label={t("terminal.board.standingTitle")}>
          <h3>{t("terminal.board.standingTitle")}</h3>
          <p className="pgm-board__badge">
            <strong>{badgeText(standing)}</strong>
            {percentileText(standing) ? <small> {percentileText(standing)}</small> : null}
          </p>
          <p className="pgm-board__state">{stateText(standing)}</p>
          {standing.formula ? <p className="pgm-board__formula">{standing.formula}</p> : null}
          {standing.gate ? <p className="pgm-board__gate">{t("terminal.board.gateNote", { gate: standing.gate })}</p> : null}

          {standing.state === "ranked" && standing.gap ? (
            <p className="pgm-board__gap">{gapSentence(standing.gap, standing.orderUnits)}</p>
          ) : null}

          {refusalLines(standing).map((line) => (
            <p key={line} className="pgm-board__refusal">
              {line}
            </p>
          ))}

          {spark.length > 0 ? (
            <svg
              className="pgm-board__spark"
              width={SPARK_W}
              height={SPARK_H}
              viewBox={`0 0 ${SPARK_W} ${SPARK_H}`}
              role="img"
              aria-label={t("terminal.board.sparkLabel")}
            >
              <polyline
                points={spark.map((p) => `${p.x},${p.y}`).join(" ")}
                fill="none"
                stroke="currentColor"
                strokeWidth="1.5"
              />
            </svg>
          ) : null}
          <p className="pgm-board__history">{historySentence(standing)}</p>
          <p className="pgm-board__neighbours">
            {standing.above ? (
              <a href={`/trader/${encodeURIComponent(standing.above.anon)}`}>
                {t("terminal.board.above", { anon: standing.above.anon })}
              </a>
            ) : null}
            {standing.below ? (
              <a href={`/trader/${encodeURIComponent(standing.below.anon)}`}>
                {t("terminal.board.below", { anon: standing.below.anon })}
              </a>
            ) : null}
          </p>
          <p className="pgm-board__note">{standing.note}</p>
        </article>
      ) : null}

      <article className="pgm-board__follows" aria-label={t("terminal.board.followsTitle")}>
        <h3>{t("terminal.board.followsTitle")}</h3>
        <p className="pgm-board__note">{follows?.note ?? ""}</p>
        <table className="pgm-table">
          <thead>
            <tr>
              <th>{t("terminal.board.col.wallet")}</th>
              <th>{t("terminal.board.col.state")}</th>
              <th>{t("terminal.board.col.realised")}</th>
              <th>{t("terminal.board.col.drawdown")}</th>
              <th>{t("terminal.board.col.winRate")}</th>
              <th>{t("terminal.board.col.pick")}</th>
            </tr>
          </thead>
          <tbody>
            {(follows?.rows ?? []).map((row) => (
              <tr key={row.anon}>
                <td>
                  <a href={`/trader/${encodeURIComponent(row.anon)}`}>{row.label || row.anon}</a>
                </td>
                <td>{followStateText(row)}</td>
                {followCells(row).map((cell) => (
                  <td key={cell.label} className={cell.bad ? "is-bad" : ""}>
                    <Number value={cell.label === "realised PnL" ? (row.realisedMicro ?? 0) : (row.maxDrawdownMicro ?? 0)} kind="money" />
                    <small className="pgm-board__why"> {cell.value}</small>
                  </td>
                ))}
                <td>
                  {/* The rate is the server's bps through the app's one formatter: a win rate computed by
                      dividing in the component is a second opinion about a number that already has an owner. */}
                  {row.insufficientSample ? (
                    <small>{row.sampleNote}</small>
                  ) : row.winRateBps === null || row.winRateBps === undefined ? (
                    "—"
                  ) : (
                    bpsText(row.winRateBps)
                  )}
                </td>
                <td>
                  <label>
                    <input
                      type="checkbox"
                      checked={picked.includes(row.anon)}
                      onChange={(e) =>
                        setPicked((p) => (e.target.checked ? [...p, row.anon].slice(-3) : p.filter((x) => x !== row.anon)))
                      }
                      aria-label={t("terminal.board.pickWallet", { anon: row.anon })}
                    />
                  </label>
                  <Button
                    disabled={busy}
                    onClick={() => void toggleFollow(row.anon, true)}
                    aria-label={t("terminal.board.unfollow", { anon: row.anon })}
                  >
                    {t("terminal.board.unfollowLabel")}
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {follows && follows.total === 0 ? <p role="status">{t("terminal.board.followsEmpty")}</p> : null}
        <div className="pgm-board__actions">
          <Button disabled={picked.length < 2} onClick={() => void compare(picked)}>
            {t("terminal.board.compare")}
          </Button>
          <small>{t("terminal.board.compareCap")}</small>
        </div>
      </article>

      {cmp ? (
        <article className="pgm-board__compare" aria-label={t("terminal.board.compareTitle")}>
          <h3>{t("terminal.board.compareTitle")}</h3>
          <p className="pgm-board__verdict">{comparisonVerdict(cmp)}</p>
          <table className="pgm-table">
            <thead>
              <tr>
                <th>{t("terminal.board.col.wallet")}</th>
                <th>{t("terminal.board.col.rank")}</th>
                <th>{t("terminal.board.col.formula")}</th>
                <th>{t("terminal.board.col.state")}</th>
              </tr>
            </thead>
            <tbody>
              {cmp.rows.map((row) => (
                <tr key={String(row.anon)}>
                  <td>
                    <a href={`/trader/${encodeURIComponent(String(row.anon))}`}>{String(row.anon)}</a>
                  </td>
                  <td>{row.rank ? `#${String(row.rank)}` : "—"}</td>
                  <td>{String((row.components as { formula?: string } | undefined)?.formula ?? "")}</td>
                  <td>{String(row.state ?? "")}</td>
                </tr>
              ))}
              {cmp.unranked.map((row) => (
                <tr key={row.anon}>
                  <td>{row.anon}</td>
                  <td>{t("terminal.board.compareUnranked")}</td>
                  <td colSpan={2}>{row.reasons.join("; ")}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {cmp.unknown.length ? (
            <p className="pgm-board__unknown">{t("terminal.board.unknownList", { list: cmp.unknown.join(", ") })}</p>
          ) : null}
          <p className="pgm-board__note">{cmp.note}</p>
          <p className="pgm-board__note">{cmp.disclaimer}</p>
        </article>
      ) : null}
    </section>
  );
}
