/**
 * The internal dashboard's dictionary — the fourth route family, and the only one whose audience is us.
 *
 * `/admin/gaming` is read by an operator deciding whether a wallet belongs on a board, so the copy here is
 * operational rather than promotional: what was measured, what the threshold is, and what happens when the
 * operator clicks. The two readings of every finding (the rule and the innocent explanation) come from the API,
 * not from this file — they are the finding's own words, and a dictionary that could reword them would be a
 * dictionary that could soften them.
 *
 * `scripts/i18n-check.mjs` holds the rule that makes the split safe: an `admin.*` key asked for by a component
 * that does not import `@/i18n/admin` renders the key itself, and the check fails the build on it.
 */
export const enAdmin: Record<string, string> = {
  "admin.gaming.title": "Anti-gaming",
  "admin.gaming.lede":
    "four questions we ask our own tape: who is climbing faster than their record explains, which wallets trade like one wallet, which referral trees look manufactured, and whose volume reaches our builder code in a shape that is not a person's trading",
  "admin.gaming.token": "operator token",
  "admin.gaming.tokenHint": "kept in this tab only; never written to storage",
  "admin.gaming.load": "read the tape",
  "admin.gaming.refresh": "re-read",
  "admin.gaming.refused": "the tape was not read: {reason}",
  "admin.gaming.evidence": "{rows} snapshot row(s), {fills} fill(s), {decisions} decision(s) on record",
  "admin.gaming.stale": "newest snapshot {when}",
  "admin.gaming.empty": "nothing on this list — which is the answer, not a failure",
  "admin.gaming.ruleLabel": "the rule",
  "admin.gaming.innocentLabel": "the same shape when nothing is wrong",
  "admin.gaming.evidenceLabel": "what it was measured from",
  "admin.gaming.exclude": "exclude from rankings",
  "admin.gaming.flag": "flag for review",
  "admin.gaming.include": "put back",
  "admin.gaming.boardAll": "every board",
  "admin.gaming.severity": "severity {n} of 3",
  "admin.gaming.reviewed": "reviewed: {action} at {when}",
  "admin.gaming.decided": "recorded — {action} on {board}",
  "admin.gaming.decideFailed": "the decision was not recorded: {reason}",
  "admin.gaming.reasonLabel": "why (8 characters or more; this is the record)",
  "admin.gaming.reasonPlaceholder": "what you checked, not what you suspect",
  "admin.gaming.reasonNeeded": "a decision needs its reason: the row is what answers the appeal",
  "admin.gaming.section.climbers": "climbing faster than the record explains",
  "admin.gaming.section.clusters": "wallets that trade like one wallet",
  "admin.gaming.section.chains": "referral trees that look manufactured",
  "admin.gaming.section.builder": "volume that reaches our builder code unusually",
  "admin.gaming.finding.fast-climb": "fast climb",
  "admin.gaming.finding.correlated-cluster": "correlated cluster",
  "admin.gaming.finding.synthetic-chain": "synthetic chain",
  "admin.gaming.finding.builder-anomaly": "builder anomaly",
  "admin.gaming.note": "an exclusion is one append-only row with your reason on it; the board replays the newest row, so a wrong click costs one more click",
};
