import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { fetchLive } from "../api/client";
import type { LiveEvent } from "../data/types";

export function LivePage() {
  const { t } = useTranslation();
  const [events, setEvents] = useState<LiveEvent[]>([]);
  const [loading, setLoading] = useState(false);

  const load = async () => {
    setLoading(true);
    setEvents(await fetchLive());
    setLoading(false);
  };

  useEffect(() => {
    void load();
  }, []);

  return (
    <div className="stack rise">
      <div className="page-h">
        <h1>{t("live.title")}</h1>
        <p>{t("live.subtitle")}</p>
      </div>

      <button type="button" className="btn btn-ghost" onClick={() => void load()}>
        {loading ? t("common.loading") : t("live.refresh")}
      </button>

      {events.length === 0 ? (
        <div className="empty">{t("live.empty")}</div>
      ) : (
        events.map((ev) => (
          <article className="scan-card" key={ev.slug}>
            <div className="scan-meta">
              <span className="chip pos">{ev.sport}</span>
              <span>
                {ev.score} · {ev.period}
              </span>
            </div>
            <div className="q">{ev.title}</div>
            {ev.asks.map((a) => (
              <div className="row-market" key={a.outcome}>
                <span>{a.outcome}</span>
                <span>ask {a.ask.toFixed(3)}</span>
                <span className="pos">k {a.k.toFixed(2)}</span>
                <span className="mute">LIVE</span>
              </div>
            ))}
          </article>
        ))
      )}
    </div>
  );
}
