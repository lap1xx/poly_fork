import {
  bindMiniAppCssVars,
  bindThemeParamsCssVars,
  bindViewportCssVars,
  expandViewport,
  init,
  isTMA,
  miniAppReady,
  mockTelegramEnv,
  mountMiniAppSync,
  mountThemeParamsSync,
  mountViewport,
} from "@telegram-apps/sdk-react";

const THEME = {
  accent_text_color: "#3dff9a",
  bg_color: "#0c1210",
  bottom_bar_bg_color: "#121a17",
  button_color: "#3dff9a",
  button_text_color: "#04140c",
  destructive_text_color: "#ff5c5c",
  header_bg_color: "#0c1210",
  hint_color: "#7a9286",
  link_color: "#5ec8ff",
  secondary_bg_color: "#121a17",
  section_bg_color: "#121a17",
  section_header_text_color: "#3dff9a",
  section_separator_color: "#1e2c26",
  subtitle_text_color: "#7a9286",
  text_color: "#e8f0eb",
} as const;

function withTimeout<T>(p: Promise<T>, ms: number, label: string): Promise<T> {
  return new Promise((resolve, reject) => {
    const t = window.setTimeout(() => reject(new Error(`${label} timeout ${ms}ms`)), ms);
    p.then(
      (v) => {
        window.clearTimeout(t);
        resolve(v);
      },
      (e) => {
        window.clearTimeout(t);
        reject(e);
      },
    );
  });
}

export function applyFallbackTheme() {
  const root = document.documentElement;
  for (const [key, value] of Object.entries(THEME)) {
    root.style.setProperty(`--tg-theme-${key.replace(/_/g, "-")}`, value);
  }
  root.style.background = THEME.bg_color;
  root.style.color = THEME.text_color;
  document.body.style.background = THEME.bg_color;
  document.body.style.color = THEME.text_color;
  root.dataset.colorScheme = "dark";
}

function mockOutsideTelegram() {
  const theme = encodeURIComponent(JSON.stringify(THEME));
  const user = encodeURIComponent(
    JSON.stringify({
      id: 1,
      first_name: "Dev",
      language_code: "ru",
      allows_write_to_pm: true,
    }),
  );
  const authDate = Math.floor(Date.now() / 1000);
  const initData = [
    `user=${user}`,
    "chat_instance=-1",
    "chat_type=sender",
    `auth_date=${authDate}`,
    "signature=dev_signature",
    "hash=dev_hash",
  ].join("&");

  mockTelegramEnv({
    launchParams: [
      `tgWebAppData=${encodeURIComponent(initData)}`,
      "tgWebAppVersion=8.0",
      "tgWebAppPlatform=tdesktop",
      `tgWebAppThemeParams=${theme}`,
    ].join("&"),
    resetPostMessage: true,
  });
}

/**
 * Never block UI forever — Telegram methods can hang behind tunnels.
 */
export async function bootTelegram(): Promise<{ inTelegram: boolean }> {
  applyFallbackTheme();

  let inTelegram = false;
  try {
    inTelegram = await withTimeout(isTMA("complete"), 1500, "isTMA");
  } catch {
    inTelegram = false;
  }

  if (!inTelegram) {
    try {
      mockOutsideTelegram();
    } catch (e) {
      console.warn("[tg] mock failed", e);
    }
    applyFallbackTheme();
  }

  try {
    init();
  } catch (e) {
    console.warn("[tg] init", e);
  }

  try {
    mountThemeParamsSync();
    bindThemeParamsCssVars();
  } catch (e) {
    console.warn("[tg] themeParams", e);
  }

  try {
    mountMiniAppSync();
    bindMiniAppCssVars();
  } catch (e) {
    console.warn("[tg] miniApp", e);
  }

  try {
    await withTimeout(mountViewport(), 1500, "mountViewport");
    bindViewportCssVars();
    expandViewport();
  } catch (e) {
    console.warn("[tg] viewport skipped", e);
  }

  try {
    miniAppReady();
  } catch (e) {
    console.warn("[tg] ready", e);
  }

  applyFallbackTheme();
  return { inTelegram };
}
