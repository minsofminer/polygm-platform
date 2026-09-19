/**
 * The terminal's wire shapes, transcribed from `contracts/openapi.yaml` (P10's 9 operations).
 *
 * Money rule 2 of the contract: **`price` and `shares` cross as decimal STRINGS** and only the `*Micro` fields
 * are integers. That is not pedantry — a JSON number is a double the moment it is parsed, and `0.1 + 0.2` in a
 * whale's notional is how a badge ends up one unit away from its own threshold. Every screen in
 * `src/screens/terminal/` converts through `@/money/cents`, never through `Number(...)`.
 */

import type { components, paths } from "@/api/schema.gen";

/** One named schema from the generated contract, by its `components.schemas` key. */
type Schema<K extends keyof components["schemas"]> = components["schemas"][K];
/** The envelope every read carries: `asOf` is the data's clock, `staleAfter` is when it stops being true. */
type StampFields = components["schemas"]["Stamped"];

/** One classification label with the rule that produced it and its disclaimer (contract: `LabelFact`). */
export type LabelFact = {
  label: string;
  confidence: number;
  publishable: boolean;
  rule: string;
  disclaimer: string;
  evidence?: Record<string, unknown>;
};

/** A tape row: `TerminalFill` plus the whale judgement the server attached to it. */
export type TerminalFill = {
  tsMs: number;
  conditionId: string;
  tokenId: string;
  marketId: string;
  marketSlug: string;
  question: string;
  category: string;
  tick: string;
  side: "BUY" | "SELL";
  outcome: string;
  /** Decimal string, never a number. Convert with `priceToUnits`. */
  price: string;
  /** Decimal string of SHARES — the venue's unit, not dollars. */
  shares: string;
  notionalMicro: number;
  anonWallet: string;
  labels: LabelFact[];
  source: string;
  lagMs: number;
  thresholdMicro: number;
  thresholdRule: string;
  thresholdReason: "relative" | "absolute_floor" | "absolute_fallback";
  severity: "info" | "notice" | "urgent";
  ratioBps: number;
  rule: string;
  isWhale: boolean;
};

/** The whale feed's own count block (D4): what the filter found, not what the page shows. */
export type WhalesCounts = { overThreshold: number; returned: number; marketsWithFills: number };

export type WhaleThreshold = {
  thresholdMicro: number;
  reason: string;
  sampleOk: boolean;
  relativeMicro: number;
  floorMicro: number;
  p995Micro: number;
  medianMicro: number;
  fills: number;
  mode: string;
  multiple: number | null;
  rule: string;
};

export type TapeCounts = { returned: number; whales: number; hasMore: boolean; overThresholdOnPage: number };

export type FacetBucket = { value: string; fills: number; notionalMicro: number };

export type TapeFacets = {
  fills: number;
  windowMs: number;
  medianNotionalMicro: number;
  p95NotionalMicro: number;
  maxNotionalMicro: number;
  whale: WhaleThreshold;
  severityRule: string;
  sampleNote: string;
  sides: FacetBucket[];
  outcomes: FacetBucket[];
  categories: FacetBucket[];
  sources: FacetBucket[];
  wallets: { anonWallet: string; fills: number; notionalMicro: number; labels: LabelFact[] }[];
  markets: {
    marketId: string;
    slug: string;
    question: string;
    category: string;
    fills: number;
    notionalMicro: number;
    whales: number;
    thresholdMicro: number;
    thresholdRule: string;
    thresholdReason: string;
    sizeBucket: string;
    bucketFloorMicro: number;
  }[];
  classifications: { label: string; wallets: number; fills: number; rule: string; disclaimer: string }[];
  classificationsAll: LabelFact[];
  asOf: number;
  staleAfter: number;
};

export type MetricWindow = {
  fills: number;
  resolvedMarkets: number;
  wins: number;
  /** `null` below the sample gate — never a percentage. */
  winRateBps: number | null;
  insufficientSample: boolean;
  sampleNote: string;
  sampleGate: number;
  volumeMicro: number;
  realisedMicro: number;
  unrealisedMicro: number;
  bestMicro: number;
  worstMicro: number;
  maxDrawdownMicro: number;
  avgHoldMs: number;
  medianHoldMs: number;
  openFills: number;
  matchedPositions: number;
  distinctMarkets: number;
  categories: number;
  asOfMs: number;
};

export type CurvePoint = { tsMs: number; cumMicro: number; peakMicro: number; drawdownMicro: number };

export type TraderPosition = {
  marketId: string;
  marketSlug: string;
  question: string;
  outcome: string;
  size: string;
  avgEntry: string;
  mark: string;
  unrealisedMicro: number;
  unrealisedBps: number;
  onTick: boolean;
  endsInMs: number;
  markSource: "last_fill" | "unknown";
};

export type TraderDossier = {
  anonWallet: string;
  window: string;
  windows: string[];
  sampleGate: number;
  metrics: Record<string, MetricWindow>;
  curve: CurvePoint[];
  curveWindow: string;
  maxDrawdownMicro: number;
  breakdown: { category: string; notionalMicro: number; realisedMicro: number; shareBps: number }[];
  positions: TraderPosition[];
  fills: (TerminalFill & { winner: boolean | null; resolved: boolean; realisedMicro: number })[];
  behaviour: LabelFact[];
  methodology: Record<string, unknown> & { path: string; winRate?: string; drawdown?: string };
  asOf: number;
  staleAfter: number;
};

export type SlippageFacts = {
  samples: number;
  medianSlippageBps: number;
  p90SlippageBps: number;
  worstSlippageBps: number;
  copied: number;
  skipped: number;
  skipRateBps: number;
  default: string;
  warning: string;
};

export type SourceStats = {
  windows: {
    windowDays: number;
    closedTrades: number;
    netAfterFeesMicro: number;
    maxDrawdownMicro: number;
    winRateBps: number | null;
    insufficientSample: boolean;
    riskAdjustedBps: number;
    avgLatencyMs: number;
  }[];
  ranking: string;
};

export type CopyConfig = {
  configId: string;
  sourceAnon: string;
  mode: string;
  ratioBps: number | null;
  maxOrderMicro: number;
  maxDailyMicro: number;
  enabled: boolean;
  createdMs: number;
  dryRun: boolean;
  guardsFromRow: boolean;
  skipIfMovedCents: number;
  doNotEnterWithinHours: number;
  categoryFilter: string;
  minPriceMicro: number | null;
  maxPriceMicro: number | null;
  takeProfitMicro: number | null;
  stopLossMicro: number | null;
  warning: SlippageFacts;
  sourceStats: SourceStats;
};

/** A row of D7's discovery list: the ratio, its denominator, and the gate its win rate passed or failed. */
export type CopySourceRow = {
  anonWallet: string;
  windowDays: number;
  closedTrades: number;
  realisedMicro: number;
  feesMicro: number;
  netAfterFeesMicro: number;
  maxDrawdownMicro: number;
  longestLosingStreak: number;
  avgLatencyMs: number;
  winRateBps: number | null;
  insufficientSample: boolean;
  sampleNote: string;
  sampleGate: number;
  riskAdjustedBps: number;
  riskAdjustedRule: string;
  copierCount: number;
  currentlyCopying: boolean;
  myConfigs: number;
  updatedMs: number;
  rank: number;
};

export type CopyMonitor = {
  configId: string;
  sourceAnon: string;
  live: { action: string; reason: string; deviationBps: number; atMs: number; intentId: string; dryRun: boolean }[];
  wouldDo: {
    action: string;
    shares: string;
    price: string;
    sourcePrice: string;
    deviationBps: number;
    reason: string;
    atMs: number;
    marketId: string;
    dryRun: boolean;
  }[];
  skips: { action: string; reason: string; atMs: number }[];
  slippage: SlippageFacts;
  sourceStats: SourceStats;
  skipReasons: string[];
};

export type PortfolioPosition = {
  tokenId: string;
  marketId: string;
  marketSlug: string;
  question: string;
  outcome: string;
  category: string;
  size: string;
  avgEntry: string;
  mark: string;
  costBasisMicro: number;
  valueMicro: number;
  unrealisedMicro: number;
  unrealisedBps: number;
  onTick: boolean;
  endsInMs: number;
  markSource: string;
  shareOfPortfolioBps: number;
};

export type NegRiskGroup = {
  eventId: string;
  eventTitle: string;
  legs: number;
  exposureMicro: number;
  sumValuesMicro: number;
  maxPayoutMicro: number;
  note: string;
};

export type Portfolio = {
  positions: PortfolioPosition[];
  negRiskGroups: NegRiskGroup[];
  orders: {
    intentId: string;
    marketId: string;
    state: string;
    reason: string | null;
    shares: string;
    price: string;
    createdMs: number;
    notionalMicro: number;
    unknownLifecycle: boolean;
  }[];
  unknownLifecycle: {
    venueOrderId: string;
    intentId: string;
    state: string;
    reason: string;
    showAsWorking: boolean;
    atMs: number;
    unknownLifecycle: boolean;
  }[];
  pnlCurve: CurvePoint[];
  maxDrawdownMicro: number;
  totals: { valueMicro: number; cashMicro: number; equityMicro: number; unrealisedMicro: number; costBasisMicro: number };
  benchmark: { kind: string; valueMicro: number; rateBps: number; note: string };
  csv: { columns: string[]; note: string };
  emptyState: string;
  asOf: number;
  staleAfter: number;
};

export type WhaleView = {
  viewId: string;
  name: string;
  filters: Record<string, unknown>;
  channel: string | null;
  severity: string;
  scope: "global" | "market";
  marketId: string | null;
  ruleId: string | null;
  createdMs: number;
  notifies: boolean;
  firesPerWindow: number | null;
  ruleWindowMs: number | null;
  ruleEnabled: boolean | null;
};

/** The four Wallet Radar rankings. The sample gate applies to the profit ranking only, and it says so. */
export type RadarRanking = { id: "active" | "profit" | "earliest" | "overlap"; label: string };
export type RadarRow = {
  anonWallet: string;
  matched: { marketId: string; question: string }[];
  boughtMicro: number;
  soldMicro: number;
  realisedMicro: number;
  winRateBps: number | null;
  insufficientSample: boolean;
  labels: LabelFact[];
  rank: number;
  reason: string;
};

/** A wallet the prof ranking EXCLUDED for being under the sample gate, with the reason. Never hidden. */
export type RadarUnranked = {
  anonWallet: string;
  fills: number;
  markets: string[];
  realisedMicro: number;
  winRateBps: number | null;
  insufficientSample: boolean;
  reason: string;
};

export type RadarQuota = {
  plan?: string;
  usedToday: number;
  perDay: number;
  cached: boolean;
  jobId: string | null;
  pollMs?: number;
  note: string;
};

export type RadarResult = {
  /** The selected ranking's rows, for the tab the caller asked for. */
  items?: RadarRow[];
  ranking?: string;
  markets: string[];
  rankings: Record<string, RadarRow[]>;
  rankingsMeta: RadarRanking[];
  /** Below the gate: returned rather than dropped, because a gate that hides its exclusions is a filter. */
  unranked?: RadarUnranked[];
  scanned?: number;
  sampleGate?: number;
  quota: RadarQuota;
  costNote: string;
};

/**
 * D8's payloads, derived from the contract for the same reason D9's are: the builder's vocabulary is a served
 * shape with fifteen field types inside it, and a transcription of it is a transcription that goes stale the
 * first time the engine learns a new trigger.
 *
 * `HaltState` keeps its old name because the daily-loss halt is what the banner reads: it is `loss_halts` as the
 * API reports it, read once, never recomputed.
 */
export type BuilderField = Schema<"BuilderField">;
export type BuilderKind = Schema<"BuilderKind">;
export type BuilderVocabulary = Schema<"BuilderVocabulary">;
export type AutomationRule = Schema<"AutomationRule">;
export type AutomationRunRow = Schema<"AutomationRunRow">;
export type AutomationTemplate = Schema<"AutomationTemplate">;
export type FeeArithmetic = Schema<"FeeArithmetic">;
export type HaltState = Schema<"LossHalt">;
/** The console's read, stamped. */
export type AutomationList = components["schemas"]["AutomationList"] & StampFields;
/** The template catalog, stamped. */
export type TemplateCatalog = components["schemas"]["TemplateCatalog"] & StampFields;

/**
 * D9's payloads, DERIVED from the contract rather than transcribed.
 *
 * The rest of this file is a transcription (P10's D1–D7 predate the schemas they read), and that is the reason
 * these three are not: the P08 gate refuses a hand-typed body by name, and the honest fix is not a rename — it is
 * to read the shape the contract already publishes, so a field added to the API cannot go missing here without
 * `npm run check:api` failing first.
 */
export type AlertPlanRow = Schema<"AlertPlan">;
export type AlertRule = Schema<"AlertRule">;
export type AlertDeliveryRow = Schema<"AlertDelivery">;
export type NotificationSettings = Schema<"NotificationSettings">;
/** The list read: the contract's own `AlertsList`, stamped. */
export type AlertsPayload = components["schemas"]["AlertsList"] & StampFields;
/** The delivery read, whose `rows` are the same `AlertDelivery` rows the rule list links to. */
export type DeliveryPage = paths["/v1/alerts/deliveries"]["get"]["responses"][200]["content"]["application/json"];
/** The test-fire read: the plan rows, per channel. */
export type AlertTestResult = paths["/v1/alerts/test"]["post"]["responses"][200]["content"]["application/json"];
