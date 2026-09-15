import { StrictMode, Component, type ErrorInfo, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import "./i18n";
import "./styles/global.css";
import "./styles/desk.css";
import App from "./App";
import { applyFallbackTheme, bootTelegram } from "./telegram/boot";

class ErrorBoundary extends Component<
  { children: ReactNode },
  { error: string | null }
> {
  state = { error: null as string | null };

  static getDerivedStateFromError(error: Error) {
    return { error: error.message || String(error) };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("[ui]", error, info);
  }

  render() {
    if (this.state.error) {
      return (
        <div
          style={{
            padding: 24,
            fontFamily: "monospace",
            color: "#3dff9a",
            background: "#0c1210",
            minHeight: "100vh",
          }}
        >
          <div style={{ fontWeight: 700, marginBottom: 8 }}>UI ERROR</div>
          <pre style={{ whiteSpace: "pre-wrap" }}>{this.state.error}</pre>
        </div>
      );
    }
    return this.props.children;
  }
}

applyFallbackTheme();

const rootEl = document.getElementById("root");
if (rootEl) {
  rootEl.innerHTML =
    '<div style="padding:20px;font-family:monospace;color:#7a9286">Loading Live Desk…</div>';
}

createRoot(rootEl!).render(
  <StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </StrictMode>,
);

void bootTelegram().catch((e) => console.warn("[boot]", e));
