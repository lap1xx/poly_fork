import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { BRANDS, type BrandId } from "../brand";
import { setAppLanguage } from "../i18n";
import {
  fetchConfig,
  fetchHistory,
  probeApi,
  saveConfig,
} from "../api/client";
import type { AppConfig, HistoryArb } from "../data/types";

export function MorePage({
  brandId,
  onBrand,
  onOpenHistory,
}: {
  brandId: BrandId;
  onBrand: (id: BrandId) => void;
  onOpenHistory: () => void;
}) {
  const { t, i18n } = useTranslation();
  const [cfg, setCfg] = useState<AppConfig | null>(null);
  const [saved, setSaved] = useState(false);
  const [apiOk, setApiOk] = useState<boolean | null>(null);

  useEffect(() => {
    void fetchConfig().then(setCfg);
    void probeApi().then(setApiOk);
  }, []);

  if (!cfg) return <div className="empty">{t("common.loading")}</div>;

  const patch = (p: Partial<AppConfig>) => setCfg({ ...cfg, ...p });

  return (
    <div className="stack rise">
      <div className="page-h">
        <h1>{t("more.title")}</h1>
        <p>{t("more.about")}</p>
      </div>

      <div className="panel">
        <h2>{t("more.language")}</h2>
        <div className="seg">
          <button
            type="button"
            className={`btn btn-ghost ${i18n.language === "ru" ? "on" : ""}`}
            onClick={() => setAppLanguage("ru")}
          >
            RU
          </button>
          <button
            type="button"
            className={`btn btn-ghost ${i18n.language === "en" ? "on" : ""}`}
            onClick={() => setAppLanguage("en")}
          >
            EN
          </button>
        </div>
      </div>

      <div className="panel">
        <h2>{t("more.brand")}</h2>
        <div className="seg">
          {BRANDS.map((b) => (
            <button
              key={b.id}
              type="button"
              className={`btn btn-ghost ${brandId === b.id ? "on" : ""}`}
              onClick={() => {
                onBrand(b.id);
                localStorage.setItem("pf-brand", b.id);
              }}
            >
              {b.name}
              {b.accentWord}
            </button>
          ))}
        </div>
      </div>

      <div className="panel">
        <h2>{t("more.config")}</h2>
        <div className="field">
          <label>{t("more.bank")}</label>
          <input
            inputMode="decimal"
            value={cfg.bank}
            onChange={(e) => patch({ bank: Number(e.target.value) || 0 })}
          />
        </div>
        <div className="field">
          <label>{t("more.slip")}</label>
          <input
            inputMode="decimal"
            value={cfg.slip}
            onChange={(e) => patch({ slip: Number(e.target.value) || 0 })}
          />
        </div>
        <div className="field">
          <label>{t("more.poll")}</label>
          <input
            inputMode="decimal"
            value={cfg.poll}
            onChange={(e) => patch({ poll: Number(e.target.value) || 1 })}
          />
        </div>
        <div className="field">
          <label>{t("more.rate")}</label>
          <input
            inputMode="decimal"
            value={cfg.rate}
            onChange={(e) => patch({ rate: Number(e.target.value) || 0 })}
          />
        </div>
        <button
          type="button"
          className="btn btn-go"
          onClick={() => {
            void saveConfig(cfg).then(() => {
              setSaved(true);
              window.setTimeout(() => setSaved(false), 1200);
            });
          }}
        >
          {saved ? "OK" : t("more.save")}
        </button>
      </div>

      <button type="button" className="btn btn-ghost" onClick={onOpenHistory}>
        {t("more.history")}
      </button>

      <div className="panel">
        <h2>{t("more.api")}</h2>
        <p className="meta" style={{ marginBottom: 0 }}>
          {apiOk === null
            ? "…"
            : apiOk
              ? "API live · /api → :8787"
              : t("more.mockMode")}
        </p>
      </div>
    </div>
  );
}

export function HistoryPage({ onBack }: { onBack: () => void }) {
  const { t } = useTranslation();
  const [rows, setRows] = useState<HistoryArb[]>([]);

  useEffect(() => {
    void fetchHistory().then(setRows);
  }, []);

  return (
    <div className="stack rise">
      <button type="button" className="btn btn-ghost" onClick={onBack}>
        ← {t("common.back")}
      </button>
      <div className="page-h">
        <h1>{t("history.title")}</h1>
      </div>
      {rows.length === 0 ? (
        <div className="empty">{t("history.empty")}</div>
      ) : (
        <div className="panel">
          {rows.map((r) => (
            <div className="row-market" key={r.ts + r.slug} style={{ gridTemplateColumns: "1fr auto" }}>
              <div>
                <div className="mute" style={{ fontSize: 10 }}>
                  {r.ts} · {r.sport}
                </div>
                <div>
                  {r.src1}/{r.out1} · {r.src2}/{r.out2}
                </div>
              </div>
              <div style={{ textAlign: "right" }}>
                <div className="pos">+{(r.roi * 100).toFixed(2)}%</div>
                <div className="warn">
                  +{Math.round(r.profit)} {t("history.profit")}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
