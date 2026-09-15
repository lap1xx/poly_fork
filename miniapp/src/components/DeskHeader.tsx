import { useTranslation } from "react-i18next";
import { getBrand, type BrandId } from "../brand";

export function DeskHeader({
  brandId,
  wsReady,
  marketsCount,
  rate,
  bank,
}: {
  brandId: BrandId;
  wsReady: boolean;
  marketsCount: number;
  rate: number;
  bank: number;
}) {
  const { i18n, t } = useTranslation();
  const brand = getBrand(brandId);
  const tagline = i18n.language === "en" ? brand.taglineEn : brand.taglineRu;

  return (
    <header className="desk-top rise">
      <div>
        <div className="brand-title">
          {brand.name}
          <span>{brand.accentWord}</span>
        </div>
        <div className="sub">{tagline.toUpperCase()}</div>
      </div>
      <div className="desk-status">
        <div className="live">
          <i className="dot" />
          <span>
            {wsReady ? t("desk.wsReady") : "WS…"} · {marketsCount}{" "}
            {t("desk.markets")}
          </span>
        </div>
        <span>USD/RUB {rate.toFixed(2)}</span>
        <span>
          {t("desk.bank")} {Math.round(bank).toLocaleString("ru-RU")} ₽
        </span>
      </div>
    </header>
  );
}
