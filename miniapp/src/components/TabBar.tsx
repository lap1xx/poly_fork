import type { TabId } from "../data/types";
import { useTranslation } from "react-i18next";

const ICONS: Record<TabId, string> = {
  desk: "■",
  scan: "▣",
  live: "◎",
  odds: "≡",
  more: "☰",
};

export function TabBar({
  tab,
  onChange,
}: {
  tab: TabId;
  onChange: (t: TabId) => void;
}) {
  const { t } = useTranslation();
  const items: TabId[] = ["desk", "scan", "live", "odds", "more"];

  return (
    <nav className="tabbar" aria-label="Main">
      {items.map((id) => (
        <button
          key={id}
          type="button"
          className={tab === id ? "on" : undefined}
          onClick={() => onChange(id)}
        >
          <span className="ico">{ICONS[id]}</span>
          {t(`tabs.${id}`)}
        </button>
      ))}
    </nav>
  );
}
