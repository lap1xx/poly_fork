# Poly-ARB Web · v4 (СБП-style redesign)

## Что нового в UI

- Тёмная тема с неоново-зелёным акцентом (`#00ff88`)
- 3-колоночный grid: список вилок | активная вилка | калькулятор
- Табы **ЛАЙВ / ПРЕМАТЧ** в шапке (фильтр)
- Счётчик «N вилок · ТОП +ROI дня» в шапке
- Компактные карточки вилок с БК-чипами и свечением новых
- Стакан PM с подсветкой строк, попадающих в наш ордер
- График цены PM (canvas, last 120 точек)
- SafeForkBanner — зелёная плашка над «Сколько ставить»
- Кнопки: ОТКРЫТЬ BETBOOM / КУПИТЬ НА POLYMARKET / Залился ✓
- Модалки: редактор сделки, журнал, итоги дня, букмекеры, настройки
- Приятный аккорд (C6-E6-G6) на НОВУЮ вилку
- Кнопка выключения бота (⏻)

## Установка

### 1. Файлы в корень проекта

Из архива перенеси файлы так:

```

web_app.py                  ← ЗАМЕНИТЬ существующий
config_patch.txt            ← прочитай, допиши ключи в config.json
README_SITE_V3.md           ← оставь как справку
web_app_patch.py            ← (опционально) ручной патч вместо замены
polyarb_v4_prematch_patch.py ← патчи для прематча

```

Папки `templates/` и `static/` уже у тебя скачаны — просто замени те же файлы.

### 2. config.json

Допиши в корень три ключа (см. `config_patch.txt`). Ничего не удаляй.

### 3. Прематч (опционально, если хочешь таб ПРЕМАТЧ работать)

Открой `polyarb_v4_prematch_patch.py`, примени 3 патча:
1. Замени `fetch_live_pm_markets()` на `fetch_pm_markets()`
2. В `main()` добавь разбивку live/prematch
3. В `bb_bridge.py` — параметр `date_window_days`

### 4. Запуск

```bash
# Терминал 1 (Chrome с remote-debugging)
start_all.bat

# Или вручную:
# 1) Chrome:
chrome.exe --remote-debugging-port=9222 ^
  --user-data-dir="%LOCALAPPDATA%\poly-arb-chrome" ^
  --disable-features=BlockInsecurePrivateNetworkRequests,PrivateNetworkAccessSendPreflights,PrivateNetworkAccessRespectPreflightResults,LocalNetworkAccessChecks ^
  https://betboom.ru/sport/live/esports https://betboom.ru/sport/live/tennis ^
  https://betboom.ru/sport/esports https://betboom.ru/sport/tennis

# 2) Мост:
python chrome_bridge.py

# 3) Бот + веб:
python -B bot_live.py
```

UI: **http://localhost:8080**

## Troubleshooting

**Нет вилок в списке** — проверь `chrome_bridge.py` работает (в консоли `[bridge] stats: in=... out=... clients=1`). Если in=0 — Chrome не подцепился, перезапусти start_all.bat.

**Стакан PM пустой** — PM CLOB иногда тормозит. Кнопка «Форс-фетч» в редакторе сделки делает прямой HTTP-запрос.

**График не рисуется** — нужно ≥2 точки. Подожди ~15 сек.

**«Бот остановлен»** — нажми ⏻. Chrome-мост закрой вручную.

</BDS:create_file>

<BDS:create_file fileName="_root/web_app_patch.py">

```python
# =====================================================================
# web_app_patch.py
# Если НЕ хочешь заменять web_app.py целиком — примени эти патчи вручную.
#
# 1) ЗАМЕНИ _load_config() на версию ниже.
# 2) Вставь блок "НОВЫЕ ЭНДПОИНТЫ" ПЕРЕД `if __name__ == "__main__":`
# =====================================================================

# ---------- 1. ЗАМЕНИТЬ _load_config() ----------
def _load_config():
    """Loads config.json. Supports both v1 flat and v2 nested (scan/bot)."""
    cfg = _load_json(CONFIG_FILE, {})
    if not isinstance(cfg, dict):
        cfg = {}

    rate = float(cfg.get("rate", 0) or 0)
    if rate <= 0:
        rate = 85.0

    bank = float(cfg.get("bank", 2500) or 2500)

    bot_cfg = cfg.get("bot") if isinstance(cfg.get("bot"), dict) else {}
    poll = float((bot_cfg or {}).get("poll", cfg.get("poll", 0.3)) or 0.3)

    sec = cfg.get("secondary_bk")
    if not isinstance(sec, dict):
        sec = {"enabled": False, "name": "1win", "color": "#ff5a5a"}
    else:
        sec.setdefault("enabled", False)
        sec.setdefault("name", "1win")
        sec.setdefault("color", "#ff5a5a")

    return {
        "rate": rate,
        "bank": bank,
        "poll": poll,
        "pm_fee_pct": float(cfg.get("pm_fee_pct", PM_FEE) or PM_FEE),
        "safe_fork_threshold": float(
            cfg.get("safe_fork_threshold", SAFE_FORK_THRESHOLD) or SAFE_FORK_THRESHOLD
        ),
        "secondary_bk": sec,
    }

# ---------- 2. ВСТАВЬ ЭНДПОИНТЫ ----------
# Импорты в начале файла:
#   from fastapi.responses import PlainTextResponse  (добавь к существующим)
#
# Все эндпоинты ниже — смотри полный код в web_app.py v4
# (файл _root/web_app.py). Там всё уже интегрировано.
# Здесь оставлен только текст патча, чтобы ты мог сравнить.

