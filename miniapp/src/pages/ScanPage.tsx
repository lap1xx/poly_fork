import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { fetchScan, getWatchSlugs, setWatchSlugs } from "../api/client";
import type { ScanCandidate } from "../data/types";

export function ScanPage() {
  const { t } = useTranslation();
  const [rows, setRows] = useState<ScanCandidate[]>([]);
  const [loading, setLoading] = useState(false);
  const [watch, setWatch] = useState<string[]>(() => getWatchSlugs());

  const load = async () => {
    setLoading(true);
    const data = await fetchScan();
    setRows(data);
    setLoading(false);
  };

  useEffect(() => {
    void load();
  }, []);

  const toggleWatch = (slug: string) => {
    const next = watch.includes(slug)
      ? watch.filter((s) => s !== slug)
      : [...watch, slug];
    setWatch(next);
    setWatchSlugs(next);
  };

  return (
    <div className="stack rise">
      <div className="page-h">
        <h1>{t("scan.title")}</h1>
        <p>{t("scan.subtitle")}</p>
      </div>

      <button type="button" className="btn btn-go" onClick={() => void load()} disabled={loading}>
        {loading ? t("common.loading") : t("scan.run")}
      </button>

      {rows.map((r) => (
        <article className="scan-card" key={r.slug}>
          <div className="scan-meta">
            <span className="chip">
              {r.sport}/{r.tier}/{r.market}
            </span>
            <span>
              {t("scan.over")} {r.overround.toFixed(4)}
            </span>
            <span>
              {t("scan.vol")} {Math.round(r.volume24h).toLocaleString("ru-RU")}
            </span>
          </div>
          <div className="q">{r.question}</div>
          <div className="scan-meta">
            {r.outcomes.map((o, i) => (
              <span key={o}>
                {o}: ask {r.asks[i].toFixed(3)} · k{" "}
                {(1 / r.asks[i]).toFixed(2)}
              </span>
            ))}
          </div>
          <div className="cta">
            <button
              type="button"
              className={`btn btn-ghost ${watch.includes(r.slug) ? "on" : ""}`}
              onClick={() => toggleWatch(r.slug)}
            >
              {t("scan.addWatch")}
            </button>
          </div>
        </article>
      ))}
    </div>
  );
}
