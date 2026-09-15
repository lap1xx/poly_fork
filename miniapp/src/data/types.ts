export type TabId = "desk" | "scan" | "live" | "odds" | "more";

export type MarketStatus = "ARB" | "NEAR" | "LIVE" | "OK" | "WAIT";

export type Leg = {
  source: string;
  sourceKind: "pm" | "bc";
  outcome: string;
  odds: number;
  stakeRub: number;
};

export type ActiveArb = {
  slug: string;
  title: string;
  sport: string;
  meta: string;
  roi: number;
  ageSec: number;
  legs: Leg[];
};

export type WatchRow = {
  slug: string;
  sport: string;
  roi: number;
  status: MarketStatus;
  question?: string;
};

export type ScanCandidate = {
  slug: string;
  sport: string;
  tier: string;
  market: string;
  overround: number;
  volume24h: number;
  question: string;
  asks: [number, number];
  outcomes: [string, string];
};

export type LiveEvent = {
  slug: string;
  sport: string;
  title: string;
  score: string;
  period: string;
  asks: { outcome: string; ask: number; k: number }[];
};

export type BcOddsInput = {
  slug: string;
  outcome: string;
  source: string;
  odds: string;
};

export type FeedItem = {
  ts: string;
  text: string;
  tone?: "accent" | "mute" | "warn";
};

export type HistoryArb = {
  ts: string;
  slug: string;
  sport: string;
  roi: number;
  profit: number;
  src1: string;
  out1: string;
  src2: string;
  out2: string;
};

export type AppConfig = {
  bank: number;
  rate: number;
  slip: number;
  poll: number;
  scan: {
    top: number;
    show: number;
    minVolume: number;
    minNotional: number;
    maxOverround: number;
    sport: string;
  };
};

export type DeskSnapshot = {
  wsReady: boolean;
  marketsCount: number;
  uptime: string;
  todayArbs: number;
  todayProfit: number;
  activeArb: ActiveArb | null;
  watchlist: WatchRow[];
  feed: FeedItem[];
  config: AppConfig;
};
