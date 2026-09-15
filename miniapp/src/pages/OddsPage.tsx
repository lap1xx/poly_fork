import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  getWatchSlugs,
  loadLocalBcOdds,
  saveBcOdds,
} from "../api/client";
import type { BcOddsInput } from "../data/types";
import { mockScan } from "../data/mock";
import { hapticFeedbackNotificationOccurred } from "@telegram-apps/sdk-react";

type OutcomeRow = { slug: string; outcome: string; source: string; odds: string };

export function OddsPage() {
  const { t } = useTranslation();
  const watch = getWatchSlugs();
  const markets = useMemo(
    () =>
      mockScan
        .filter((m) => watch.includes(m.slug))
        .concat(mockScan.filter((m) => !watch.includes(m.slug)).slice(0, 2)),
    [watch],
  );

  const [slug, setSlug] = useState(markets[0]?.slug ?? "");
  const market = markets.find((m) => m.slug === slug) ?? markets[0];

  const [rows, setRows] = useState<OutcomeRow[]>([]);
  const [flash, setFlash] = useState(false);

  useEffect(() => {
    if (!market) {
      setRows([]);
      return;
    }
    const saved = loadLocalBcOdds();
    setRows(
      market.outcomes.map((outcome) => {
        const hit = saved.find(
          (s) => s.slug === market.slug && s.outcome === outcome,
        );
        return {
          slug: market.slug,
          outcome,
          source: hit?.source ?? "",
          odds: hit?.odds ?? "",
        };
      }),
    );
  }, [market]);

  if (!market) {
    return <div className="empty">{t("odds.pickMarket")}</div>;
  }

  const update = (outcome: string, patch: Partial<OutcomeRow>) => {
    setRows((prev) =>
      prev.map((r) => (r.outcome === outcome ? { ...r, ...patch } : r)),
    );
  };

  const onSave = async () => {
    const payload: BcOddsInput[] = rows
      .filter((r) => r.odds.trim())
      .map((r) => ({
        slug: market.slug,
        outcome: r.outcome,
        source: r.source.trim() || "BC",
        odds: r.odds.trim().replace(",", "."),
      }));
    await saveBcOdds(payload);
    setFlash(true);
    hapticFeedbackNotificationOccurred.ifAvailable("success");
    window.setTimeout(() => setFlash(false), 1500);
  };

  return (
    <div className="stack rise">
      <div className="page-h">
        <h1>{t("odds.title")}</h1>
        <p>{t("odds.subtitle")}</p>
      </div>

      <div className="field">
        <label>{t("odds.pickMarket")}</label>
        <select value={market.slug} onChange={(e) => setSlug(e.target.value)}>
          {markets.map((m) => (
            <option key={m.slug} value={m.slug}>
              {m.sport} · {m.outcomes.join(" / ")}
            </option>
          ))}
        </select>
      </div>

      <div className="panel">
        <h2>{market.question}</h2>
        {rows.map((r) => {
          const idx = market.outcomes.indexOf(r.outcome);
          const ask = idx >= 0 ? market.asks[idx] : 0;
          return (
            <div key={r.outcome} style={{ marginBottom: 14 }}>
              <div className="meta" style={{ marginBottom: 8 }}>
                {r.outcome} · PM ask {ask ? ask.toFixed(3) : "—"}
              </div>
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "1fr 88px",
                  gap: 8,
                }}
              >
                <div className="field" style={{ marginBottom: 0 }}>
                  <label>{t("odds.source")}</label>
                  <input
                    value={r.source}
                    placeholder="Fonbet"
                    onChange={(e) =>
                      update(r.outcome, { source: e.target.value })
                    }
                  />
                </div>
                <div className="field" style={{ marginBottom: 0 }}>
                  <label>{t("odds.odds")}</label>
                  <input
                    inputMode="decimal"
                    value={r.odds}
                    placeholder="2.15"
                    onChange={(e) =>
                      update(r.outcome, { odds: e.target.value })
                    }
                  />
                </div>
              </div>
            </div>
          );
        })}
        <button
          type="button"
          className="btn btn-go"
          onClick={() => void onSave()}
        >
          {flash ? t("odds.saved") : t("odds.save")}
        </button>
      </div>
    </div>
  );
}
