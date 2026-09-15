import type {
  AppConfig,
  DeskSnapshot,
  HistoryArb,
  LiveEvent,
  ScanCandidate,
} from "./types";

export const defaultConfig: AppConfig = {
  bank: 20_000,
  rate: 84.34,
  slip: 0,
  poll: 1,
  scan: {
    top: 800,
    show: 40,
    minVolume: 100,
    minNotional: 50,
    maxOverround: 1.08,
    sport: "",
  },
};

export const mockDesk: DeskSnapshot = {
  wsReady: true,
  marketsCount: 10,
  uptime: "3:18",
  todayArbs: 7,
  todayProfit: 4200,
  activeArb: {
    slug: "cs2-forzer-upgrad-2026-09-15",
    title: "FORZE Reload vs UPGRADE",
    sport: "CS2",
    meta: "CS2 · CIS LAN · Age 14s · Place both legs NOW",
    roi: 0.0082,
    ageSec: 14,
    legs: [
      {
        source: "POLYMARKET",
        sourceKind: "pm",
        outcome: "FORZE Reload",
        odds: 1.92,
        stakeRub: 10420,
      },
      {
        source: "FONBET",
        sourceKind: "bc",
        outcome: "UPGRADE",
        odds: 2.15,
        stakeRub: 9580,
      },
    ],
  },
  watchlist: [
    { slug: "wta-linana-llorca", sport: "TENNIS", roi: 0.0012, status: "LIVE" },
    { slug: "cs2-forzer-upgrad", sport: "CS2", roi: 0.0082, status: "ARB" },
    { slug: "cs2-baks-tsa", sport: "CS2", roi: -0.0041, status: "OK" },
    { slug: "wta-carle-gorgodz", sport: "TENNIS", roi: -0.0018, status: "OK" },
    { slug: "wta-stefani-podoros", sport: "TENNIS", roi: -0.0055, status: "OK" },
  ],
  feed: [
    { ts: "15:24:01", text: "ROI=−0.210% cs2-baks-tsa", tone: "mute" },
    { ts: "15:24:02", text: "ROI=+0.821% cs2-forzer · ARB", tone: "accent" },
    { ts: "15:24:02", text: "haptic · logged arbs.db", tone: "accent" },
    { ts: "15:24:03", text: "waiting confirm…", tone: "warn" },
    { ts: "15:23:58", text: "WS book update ×12", tone: "mute" },
    { ts: "15:23:50", text: "PING ok", tone: "mute" },
  ],
  config: defaultConfig,
};

export const mockScan: ScanCandidate[] = [
  {
    slug: "wta-linana-llorca-2026-09-14",
    sport: "TENNIS",
    tier: "MID",
    market: "MATCH",
    overround: 1.01,
    volume24h: 347039,
    question: "Valencia: Alicia Herrero Linana vs Lucia Cortez Llorca",
    asks: [0.72, 0.29],
    outcomes: ["Linana", "Llorca"],
  },
  {
    slug: "cs2-forzer-upgrad-2026-09-15",
    sport: "CS2",
    tier: "MID",
    market: "MATCH",
    overround: 1.01,
    volume24h: 80645,
    question: "Counter-Strike: FORZE Reload vs UPGRADE (BO3)",
    asks: [0.52, 0.49],
    outcomes: ["FORZE Reload", "UPGRADE"],
  },
  {
    slug: "cs2-baks-tsa-2026-09-15",
    sport: "CS2",
    tier: "MID",
    market: "MATCH",
    overround: 1.01,
    volume24h: 56429,
    question: "BakS eSports vs Spirit Academy (BO3)",
    asks: [0.41, 0.6],
    outcomes: ["BakS", "Spirit Academy"],
  },
  {
    slug: "atp-pereira-pieczon-2026-09-15",
    sport: "TENNIS",
    tier: "MID",
    market: "MATCH",
    overround: 1.02,
    volume24h: 7329,
    question: "Szczecin: Tiago Pereira vs Filip Pieczonka",
    asks: [0.48, 0.54],
    outcomes: ["Pereira", "Pieczonka"],
  },
];

export const mockLive: LiveEvent[] = [
  {
    slug: "wta-linana-llorca-2026-09-14",
    sport: "TENNIS",
    title: "Linana vs Llorca · Valencia",
    score: "2-6, 7-5, 4-4",
    period: "S3",
    asks: [
      { outcome: "Linana", ask: 0.72, k: 1.39 },
      { outcome: "Llorca", ask: 0.29, k: 3.45 },
    ],
  },
];

export const mockHistory: HistoryArb[] = [
  {
    ts: "2026-09-15 15:24:02",
    slug: "cs2-forzer-upgrad-2026-09-15",
    sport: "CS2",
    roi: 0.0082,
    profit: 164,
    src1: "Polymarket",
    out1: "FORZE",
    src2: "Fonbet",
    out2: "UPGRADE",
  },
  {
    ts: "2026-09-15 12:01:44",
    slug: "wta-ruggeri-vedder-2026-09-14",
    sport: "TENNIS",
    roi: 0.0041,
    profit: 82,
    src1: "Polymarket",
    out1: "Ruggeri",
    src2: "BC",
    out2: "Vedder",
  },
];

export const defaultWatchSlugs = [
  "wta-linana-llorca-2026-09-14",
  "cs2-forzer-upgrad-2026-09-15",
  "wta-carle-gorgodz-2026-09-15",
  "cs2-baks-tsa-2026-09-15",
  "wta-selekhm-schunk-2026-09-15",
  "wta-ruggeri-vedder-2026-09-14",
  "wta-stefani-podoros-2026-09-14",
  "cs2-eace-phanto-2026-09-15",
  "wta-maneiro-salkova-2026-09-14",
  "wta-brace-sierra-2026-09-14",
];
