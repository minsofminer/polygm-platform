/**
 * The terminal's wire shapes, transcribed from `contracts/openapi.yaml` (P10's 9 operations).
 *
 * Money rule 2 of the contract: **`price` and `shares` cross as decimal STRINGS** and only the `*Micro` fields
 * are integers. That is not pedantry — a JSON number is a double the moment it is parsed, and `0.1 + 0.2` in a
 * whale's notional is how a badge ends up one unit away from its own threshold. Every screen in
 * `src/screens/terminal/` converts through `@/money/cents`, never through `Number(...)`.
 */

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
export type RadarResult = {
  markets: string[];
  rankings: Record<string, RadarRow[]>;
  rankingsMeta: RadarRanking[];
  quota: { usedToday: number; perDay: number; cached: boolean; jobId: string | null; note: string };
  costNote: string;
};
