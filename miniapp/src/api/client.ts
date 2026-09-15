import type { DeskSnapshot, AppConfig, BcOddsInput } from "../data/types";
import {
  defaultConfig,
  defaultWatchSlugs,
  mockDesk,
  mockHistory,
  mockLive,
  mockScan,
} from "../data/mock";

/** Same-origin `/api` (Vite proxy → :8787). Optional absolute override. */
const API = (import.meta.env.VITE_API_URL as string | undefined) ?? "";

async function getJson<T>(path: string): Promise<T | null> {
  try {
    const r = await fetch(`${API}${path}`);
    if (!r.ok) return null;
    return (await r.json()) as T;
  } catch {
    return null;
  }
}

export async function fetchDesk(): Promise<DeskSnapshot> {
  const data = await getJson<DeskSnapshot>("/api/desk");
  return data ?? mockDesk;
}

export async function fetchScan() {
  const data = await getJson<typeof mockScan>("/api/scan");
  return data ?? mockScan;
}

export async function fetchLive() {
  const data = await getJson<typeof mockLive>("/api/live");
  return data ?? mockLive;
}

export async function fetchHistory() {
  const data = await getJson<typeof mockHistory>("/api/history");
  return data ?? mockHistory;
}

export async function fetchConfig(): Promise<AppConfig> {
  const data = await getJson<AppConfig>("/api/config");
  return data ?? defaultConfig;
}

export async function saveConfig(cfg: AppConfig): Promise<void> {
  const r = await fetch(`${API}/api/config`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(cfg),
  });
  if (!r.ok) {
    localStorage.setItem("pf-config", JSON.stringify(cfg));
  }
}

export async function saveBcOdds(rows: BcOddsInput[]): Promise<void> {
  const r = await fetch(`${API}/api/bc-odds`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ rows }),
  });
  if (!r.ok) {
    localStorage.setItem("pf-bc-odds", JSON.stringify(rows));
  }
}

export function loadLocalBcOdds(): BcOddsInput[] {
  try {
    const raw = localStorage.getItem("pf-bc-odds");
    return raw ? (JSON.parse(raw) as BcOddsInput[]) : [];
  } catch {
    return [];
  }
}

export function getWatchSlugs(): string[] {
  try {
    const raw = localStorage.getItem("pf-watch");
    if (raw) return JSON.parse(raw) as string[];
  } catch {
    /* ignore */
  }
  return [...defaultWatchSlugs];
}

export function setWatchSlugs(slugs: string[]) {
  localStorage.setItem("pf-watch", JSON.stringify(slugs));
  void fetch(`${API}/api/watch`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ slugs }),
  }).catch(() => undefined);
}

export async function probeApi(): Promise<boolean> {
  const h = await getJson<{ ok?: boolean }>("/api/health");
  return Boolean(h?.ok);
}

export function isMockMode(): boolean {
  return false;
}
