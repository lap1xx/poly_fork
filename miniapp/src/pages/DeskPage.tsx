import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { ArbCard } from "../components/ArbCard";
import { DeskHeader } from "../components/DeskHeader";
import { fetchDesk } from "../api/client";
import type { DeskSnapshot } from "../data/types";
import type { BrandId } from "../brand";
import {
  hapticFeedbackNotificationOccurred,
  openLink,
} from "@telegram-apps/sdk-react";

function fmtRoi(n: number) {
  const s = (n * 100).toFixed(2);
  return `${n >= 0 ? "+" : "−"}${Math.abs(Number(s)).toFixed(2)}%`;
}

export function DeskPage({ brandId }: { brandId: BrandId }) {
  const { t } = useTranslation();
  const [desk, setDesk] = useState<DeskSnapshot | null>(null);
  const [skipped, setSkipped] = useState(false);

  useEffect(() => {
    let alive = true;
    let sawArb = false;

    const tick = () => {
      void fetchDesk().then((d) => {
        if (!alive) return;
        setDesk(d);
        if (d.activeArb && !sawArb) {
          sawArb = true;
          hapticFeedbackNotificationOccurred.ifAvailable("success");
        }
        if (!d.activeArb) {
          sawArb = false;
          setSkipped(false);
        }
      });
    };

    tick();
    const id = window.setInterval(tick, 2000);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, []);

  if (!desk) {
    return <div className="empty">{t("common.loading")}</div>;
  }

  const arb = skipped ? null : desk.activeArb;

  return (
    <div className="stack">
      <DeskHeader
        brandId={brandId}
        wsReady={desk.wsReady}
        marketsCount={desk.marketsCount}
        rate={desk.config.rate}
        bank={desk.config.bank}
      />

      {arb ? (
        <ArbCard
          arb={arb}
          onOpen={() => {
            const url = `https://polymarket.com/event/${arb.slug}`;
            try {
              if (openLink.isAvailable()) {
                openLink(url);
                return;
              }
            } catch {
              /* fall through */
            }
            window.open(url, "_blank");
          }}
          onSkip={() => setSkipped(true)}
        />
      ) : (
        <div className="panel rise">
          <h2>{t("desk.noArb")}</h2>
          <p className="meta" style={{ marginBottom: 0 }}>
            {t("desk.noArbHint")}
          </p>
        </div>
      )}

      <div className="panel rise" style={{ animationDelay: "0.05s" }}>
        <h2>{t("desk.watchlist")}</h2>
        {desk.watchlist.map((row) => (
          <div className="row-market" key={row.slug}>
            <span className="slug">{row.slug}</span>
            <span className={row.roi >= 0 ? "pos" : "neg"}>{fmtRoi(row.roi)}</span>
            <span className="mute">{row.sport}</span>
            <span className={row.status === "ARB" ? "pos" : "mute"}>
              {row.status}
            </span>
          </div>
        ))}
      </div>

      <div className="panel rise" style={{ animationDelay: "0.1s" }}>
        <h2>{t("desk.today")}</h2>
        <div className="stats">
          <div className="stat">
            <span>{t("desk.arbs")}</span>
            <b>{desk.todayArbs}</b>
          </div>
          <div className="stat">
            <span>{t("desk.profit")}</span>
            <b className="pos">
              +{(desk.todayProfit / 1000).toFixed(1)}k
            </b>
          </div>
          <div className="stat">
            <span>{t("desk.uptime")}</span>
            <b>{desk.uptime}</b>
          </div>
        </div>
        <div className="meta" style={{ marginTop: 12, marginBottom: 0 }}>
          {t("desk.profitNote")}
        </div>
      </div>

      <div className="panel rise" style={{ animationDelay: "0.15s" }}>
        <h2>{t("desk.feed")}</h2>
        <div className="feed">
          {desk.feed.map((item, i) => (
            <div key={`${item.ts}-${i}`} className={item.tone}>
              <span className="t">{item.ts}</span> {item.text}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
