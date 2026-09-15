import { useEffect, useState, type ReactNode } from "react";
import { TabBar } from "./components/TabBar";
import { DeskPage } from "./pages/DeskPage";
import { ScanPage } from "./pages/ScanPage";
import { LivePage } from "./pages/LivePage";
import { OddsPage } from "./pages/OddsPage";
import { HistoryPage, MorePage } from "./pages/MorePage";
import type { TabId } from "./data/types";
import { DEFAULT_BRAND, type BrandId } from "./brand";
import {
  mountMainButton,
  onMainButtonClick,
  setMainButtonParams,
  unmountMainButton,
} from "@telegram-apps/sdk-react";
import { useTranslation } from "react-i18next";
import { fetchDesk } from "./api/client";

function readBrand(): BrandId {
  const raw = localStorage.getItem("pf-brand");
  if (
    raw === "polyfork" ||
    raw === "forkdesk" ||
    raw === "edgewire" ||
    raw === "arbpulse"
  ) {
    return raw;
  }
  return DEFAULT_BRAND;
}

export default function App() {
  const { t } = useTranslation();
  const [tab, setTab] = useState<TabId>("desk");
  const [brandId, setBrandId] = useState<BrandId>(readBrand);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [hasArb, setHasArb] = useState(false);

  useEffect(() => {
    void fetchDesk().then((d) => setHasArb(Boolean(d.activeArb)));
  }, []);

  useEffect(() => {
    let off: (() => void) | undefined;
    try {
      const canMount = Boolean(mountMainButton.isAvailable?.());
      const canSet = Boolean(setMainButtonParams.isAvailable?.());
      if (!canMount || !canSet) return;

      mountMainButton();
      if (hasArb && tab === "desk") {
        setMainButtonParams({
          text: t("desk.placeNow"),
          isVisible: true,
          isEnabled: true,
        });
      } else {
        setMainButtonParams({ isVisible: false });
      }
      if (onMainButtonClick.isAvailable?.()) {
        off = onMainButtonClick(() => setTab("desk"));
      }
    } catch (e) {
      console.warn("[mainbutton]", e);
    }
    return () => {
      try {
        off?.();
        setMainButtonParams.ifAvailable?.({ isVisible: false });
        unmountMainButton();
      } catch {
        /* ignore */
      }
    };
  }, [hasArb, tab, t]);

  let body: ReactNode;
  if (historyOpen) {
    body = <HistoryPage onBack={() => setHistoryOpen(false)} />;
  } else if (tab === "desk") {
    body = <DeskPage brandId={brandId} />;
  } else if (tab === "scan") {
    body = <ScanPage />;
  } else if (tab === "live") {
    body = <LivePage />;
  } else if (tab === "odds") {
    body = <OddsPage />;
  } else {
    body = (
      <MorePage
        brandId={brandId}
        onBrand={setBrandId}
        onOpenHistory={() => setHistoryOpen(true)}
      />
    );
  }

  return (
    <div className="app-shell">
      <div
        style={{
          fontSize: 10,
          letterSpacing: "0.08em",
          textTransform: "uppercase",
          color: "var(--pf-accent, #3dff9a)",
          marginBottom: 10,
        }}
      >
        POLYFORK · LIVE DESK · ONLINE
      </div>
      {body}
      {!historyOpen && <TabBar tab={tab} onChange={setTab} />}
    </div>
  );
}
