# PolyFork Telegram Mini App

Live Desk UI (`ui_examples/01-live-desk.html`) as a Telegram Mini App.

## Stack (best practice)

- Vite + React 19 + TypeScript
- `@telegram-apps/sdk` / `@telegram-apps/sdk-react` — `init`, `themeParams.bindCssVars`, `viewport.expand`, `ready`, MainButton, Haptic
- `i18next` — RU / EN
- Optional FastAPI bridge in `../server/api_bridge.py`

## Brand options (Settings → Brand)

| ID | Mark |
|----|------|
| `polyfork` | **POLY**FORK (default, Live Desk) |
| `forkdesk` | **FORK**DESK |
| `edgewire` | **EDGE**WIRE |
| `arbpulse` | **ARB**PULSE |

## Screens

1. **Desk** — active arb, watchlist ROI, today stats, feed  
2. **Scanner** — 2-way candidates, add to watch  
3. **Live** — in-play events  
4. **Odds** — bookmaker odds input (replaces CLI `input`)  
5. **More** — language, brand, config, history, API mode  

## Dev

В PowerShell у Windows часто блокируется `npm.ps1`. Используй:

```bash
cd miniapp
npm.cmd run dev -- --host
```

Открой http://localhost:5173/

### Optional API

```bash
pip install fastapi uvicorn
uvicorn server.api_bridge:app --reload --port 8787
```

```bash
# miniapp/.env
VITE_API_URL=http://127.0.0.1:8787
```

Without `VITE_API_URL` the app uses built-in mock data.

## Telegram Bot (next step)

1. Create bot via BotFather → `/newapp` → set Web App URL to your HTTPS host (`npm run build` → `dist/`).
2. Prefer Cloudflare Tunnel / Vercel / nginx TLS for the Mini App URL.
3. Send push on arb via Bot API `sendMessage` + `web_app` button (bridge later).

## Note

Monitoring only — no auto-betting. Same product model as `polyarb_v3.py`.
