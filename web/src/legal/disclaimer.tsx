/**
 * The three sentences that have to be on every surface, in one place.
 *
 * P16 D8 asks for the "we are not affiliated with Polymarket" disclaimer in the footer of every page, and D9 asks
 * that what we say about returns never implies one. Both are copy rules, and copy rules that live in three files
 * drift: one surface gets the new wording, the other two keep the old one, and nobody notices until a lawyer or a
 * user does. So the sentences exist once, here, and each surface renders them.
 *
 * Deliberately not in the i18n dictionaries. Those exist so copy can be *translated* and so route families do not
 * ship each other's strings; a disclaimer is a legal statement whose English wording is the operative text, and
 * putting it in `en.public.ts` would let a future dictionary "translate" the one sentence that must not be softened.
 * The trade-off is that it reaches both bundles — 3 sentences, ~400 bytes gzipped, against a 200 KB budget the
 * P08 gate measures on every build.
 *
 * The wording is checked mechanically by `tools/p16-gtm-check.py`: the phrases in `config/gtm.json`'s
 * `copy_rules.required_phrases` must appear verbatim here, and the banned list must not appear in any launch copy.
 */
export const DISCLAIMER_AFFILIATION =
  "Openout is an independent product and is not affiliated with Polymarket. We read Polymarket's public data; " +
  "we do not speak for them, and they have not endorsed anything here.";

export const DISCLAIMER_ODDS =
  "Odds are a market, not a forecast. No one can tell you what a market will do, and nothing on this site is " +
  "financial advice.";

export const DISCLAIMER_RISK =
  "Prediction markets resolve to zero all the time. You can lose everything you deposit, and the number in the " +
  "trade ticket is the most you can lose on that trade.";

/** In order, because the order is the argument: who we are, what a number means, what a loss is. */
export const DISCLAIMER_LINES: readonly string[] = [DISCLAIMER_AFFILIATION, DISCLAIMER_ODDS, DISCLAIMER_RISK];

/**
 * Rendered by the landing page, the Mini App and every public page's footer. It is a plain server-safe component
 * with no client hooks: the public pages are crawled, and a disclaimer that needs JavaScript to appear is a
 * disclaimer a crawler does not see.
 */
export function DisclaimerFooter({ compact = false }: { compact?: boolean }) {
  return (
    <footer className="pgm-disclaimer" role="contentinfo" aria-label="Risk and affiliation disclosure">
      {DISCLAIMER_LINES.map((line) => (
        <p key={line} className={compact ? "pgm-disclaimer__line pgm-disclaimer__line--compact" : "pgm-disclaimer__line"}>
          {line}
        </p>
      ))}
    </footer>
  );
}
