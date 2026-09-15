/** Brand options — pick one in Settings; default matches Live Desk mock. */
export type BrandId = "polyfork" | "forkdesk" | "edgewire" | "arbpulse";

export type Brand = {
  id: BrandId;
  name: string;
  accentWord: string;
  taglineRu: string;
  taglineEn: string;
};

export const BRANDS: Brand[] = [
  {
    id: "polyfork",
    name: "POLY",
    accentWord: "FORK",
    taglineRu: "Live Desk · PM ↔ БК монитор арбитража",
    taglineEn: "Live Desk · PM ↔ BC arb monitor",
  },
  {
    id: "forkdesk",
    name: "FORK",
    accentWord: "DESK",
    taglineRu: "Операторский стол вилок Polymarket",
    taglineEn: "Operator desk for Polymarket forks",
  },
  {
    id: "edgewire",
    name: "EDGE",
    accentWord: "WIRE",
    taglineRu: "Сигнальный провод PM ↔ букмекер",
    taglineEn: "Signal wire PM ↔ bookmaker",
  },
  {
    id: "arbpulse",
    name: "ARB",
    accentWord: "PULSE",
    taglineRu: "Пульс ROI по выбранным рынкам",
    taglineEn: "ROI pulse across watched markets",
  },
];

export const DEFAULT_BRAND: BrandId = "polyfork";

export function getBrand(id: BrandId): Brand {
  return BRANDS.find((b) => b.id === id) ?? BRANDS[0];
}
