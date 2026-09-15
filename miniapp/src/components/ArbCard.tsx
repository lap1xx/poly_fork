import { useTranslation } from "react-i18next";
import type { ActiveArb } from "../data/types";
import { hapticFeedbackImpactOccurred } from "@telegram-apps/sdk-react";

function fmtRoi(n: number) {
  const s = (n * 100).toFixed(2);
  return `${n >= 0 ? "+" : ""}${s}%`;
}

function fmtMoney(n: number) {
  return `${Math.round(n).toLocaleString("ru-RU")} ₽`;
}

export function ArbCard({
  arb,
  onOpen,
  onSkip,
}: {
  arb: ActiveArb;
  onOpen: () => void;
  onSkip: () => void;
}) {
  const { t } = useTranslation();

  const open = () => {
    hapticFeedbackImpactOccurred.ifAvailable("medium");
    onOpen();
  };

  return (
    <section className="arb">
      <div className="arb-head">
        <div className="arb-title">{arb.title}</div>
        <div className="roi">{fmtRoi(arb.roi)}</div>
      </div>
      <div className="meta">{arb.meta}</div>
      <div className="legs">
        {arb.legs.map((leg) => (
          <div className="leg" key={`${leg.source}-${leg.outcome}`}>
            <span className={leg.sourceKind === "pm" ? "src-pm" : "src-bc"}>
              {leg.source}
            </span>
            <span>{leg.outcome}</span>
            <span className="k">k {leg.odds.toFixed(2)}</span>
            <span className="stake">{fmtMoney(leg.stakeRub)}</span>
          </div>
        ))}
      </div>
      <div className="cta">
        <button type="button" className="btn btn-go" onClick={open}>
          {t("desk.openBets")}
        </button>
        <button type="button" className="btn btn-ghost" onClick={onSkip}>
          {t("desk.skip")}
        </button>
      </div>
    </section>
  );
}
