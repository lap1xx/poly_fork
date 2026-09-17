# POLY-ARB

> Сканер арбитражных вилок между Polymarket и российскими букмекерами.

Инструмент для поиска и мониторинга арбитражных ситуаций (вилок) между prediction market **Polymarket** и линиями российских БК: **BetBoom**, **Winline**, **PARI**.

---

## Что умеет

- **Прематч-сканер** — находит перспективные матчи на Polymarket за 30 секунд (800+ рынков)
- **Live-сканер** — показывает идущие прямо сейчас матчи с реальной ликвидностью
- **Мульти-мониторинг** — следит за 10+ матчами одновременно через WebSocket
- **Точный расчёт вилки** — учитывает глубину стакана, комиссии, слиппедж
- **Курс ₽/$** — автоматически с ЦБ РФ
- **Логирование** — все найденные вилки пишутся в SQLite (`arbs.db`)
- **Интерактивные лаунчеры** — запуск в один клик из меню
- **Цветной вывод** — зелёный/жёлтый/красный по уровню спреда и ликвидности
- **Звуковой алерт** — две разные мелодии для прематча и live

---

## Скриншоты

```
=========================================================================================================
  POLY-ARB LIVE launcher   events: 9   fetched: 22:57:50
=========================================================================================================

  [1] [TENNIS  ]  Sao Paulo Open: Cristina Bucsa vs Sara Bejlek
      start: 2026-09-17T19:05:00Z
        a) over=1.010  liq=1,243  Guadalajara Open Akron: Cristina Bucsa vs Sara Bejlek
           wta-bucsa-bejlek-2026-09-16
        b) over=1.080  liq=78     Bucsa vs. Bejlek: Match O/U 21.5
           wta-bucsa-bejlek-2026-09-16-match-total-21pt5

  [2] [CS2     ]  Counter-Strike: EHISTO BASE vs Fluxo W7M (BO3)
        a) over=1.070  liq=32     Map 2 Winner
           cs2-eb1-fxw7-2026-09-17-game2
        b) over=1.090  liq=217    Games Total: O/U 2.5
           cs2-eb1-fxw7-2026-09-17-total-games-2pt5

Commands:
  1a       launch market a of event 1
  1a 1b    launch two markets
  1        launch all liquid markets of event 1
  all      all markets of top-10
  r        refresh
  q        quit
```

Карточка вилки в мониторинге:

```
======================================================================
[+] ARB [CS2] [LIVE] Counter-Strike: NAVI Junior vs Aurora Academy
======================================================================
  BetBoom      NAVI Junior              k=2.100  stake=5,867 rub
  Polymarket   Aurora Academy           k=1.950  stake=4,133 rub

  ROI:      +3.234%
  Profit:   +468 rub
  Type:     LIVE
  Age:      2m 15s
  Session:  5 arbs, +7,240 rub
======================================================================

[P] open PM+sum  [B] copy BK stake  [T] log  [S] skip  [R] reload
```

---

## Установка

```
git clone https://github.com/lap1xx/poly_fork.git
cd poly_fork
pip install -r requirements.txt
```

Требуется **Python 3.10+**.

## Первый запуск

```
# Прематч: сканируем CS2 low-tier на 12 часов вперёд
python scan_2way.py --sport CS2 --only-low --hours 12

# Или через интерактивный лаунчер (проще)
python bot.py --scan --sport CS2
```

---

## Основные команды

### Прематч

```
# Скан CS2 (только низшие лиги)
python scan_2way.py --sport CS2 --only-low --hours 12

# Скан Dota 2
python scan_2way.py --sport DOTA2 --only-low --hours 12

# Скан всего (киберспорт + теннис)
python scan_2way.py --only-low --hours 12 --show 60
```

### Live

```
# Все live-матчи с ликвидностью
python bot_live.py

# Только CS2 live
python bot_live.py --sport CS2

# Только LOW тир (ITF, CCT, ESEA, United21)
python bot_live.py --only-low

# Авто-обновление каждые 30 секунд
python bot_live.py --sport CS2 --refresh 30
```

### Мониторинг конкретного матча

```
# Прематч-режим
python polyarb_v3.py --slugs cs2-forzer-upgrad-2026-09-15

# Live-режим (быстрый poll 0.1с)
python polyarb_v3.py --slugs cs2-navi-aur1-2026-09-17-game2 --live

# Несколько матчей одновременно
python polyarb_v3.py --slugs cs2-aaa,cs2-bbb,wta-ccc --live
```

---

## Интерактивные лаунчеры

Для быстрого запуска в один клик:

| Файл ↕▾ | Что делает ↕▾ |
|---|---|
| −`bot.bat` | Меню по текущему скану |
| −`bot_scan.bat` | Скан всех LOW + меню |
| −`bot_cs2.bat` | Скан CS2 LOW + меню |
| −`bot_dota.bat` | Скан DOTA2 LOW + меню |
| −`bot_tennis.bat` | Скан TENNIS LOW + меню |
| −`bot_live.bat` | Live-скан всех видов |
| −`bot_live_cs2.bat` | CS2 live |
| −`bot_live_dota.bat` | DOTA2 live |
| −`bot_live_tennis.bat` | TENNIS live |
| −`bot_live_cs2_low.bat` | CS2 live + LOW тир |
⚙

---

## Горячие клавиши в мониторинге

Когда вилка найдена:

| Клавиша ↕▾ | Действие ↕▾ |
|---|---|
| −`P` | Открыть Polymarket в браузере + скопировать $ сумму |
| −`B` | Скопировать сумму для БК в буфер |
| −`T` | Записать сделку в БД |
| −`S` | Skip — просто продолжить |
| −`R` | Reload — пересчитать ROI |
| −`Enter` | Продолжить мониторинг |
⚙

---

## Структура проекта

```
poly_fork/
├── scan_2way.py         # прематч-сканер (800+ рынков за 30 сек)
├── scan_live.py         # live-сканер с цветным выводом
├── bot_live.py          # интерактивный live-лаунчер (параллельные запросы)
├── bot.py               # интерактивный прематч-лаунчер
├── polyarb_v3.py        # мульти-мониторинг через WebSocket
├── telegram_sender.py   # отправка алертов в Telegram
├── telegram_bot.py      # команды /stats /last /bank
├── config.json          # настройки (создаётся автоматически)
├── bk_profiles.json     # профили БК с комиссиями
├── requirements.txt
└── launchers/           # .bat файлы для быстрого запуска
```

---

## Как это работает

### Прематч

1. **Скан** — `scan_2way.py` тянет 800 рынков с Polymarket параллельно (20 потоков)
2. **Фильтр** — оставляет только киберспорт/теннис, отсеивает прошедшие, тонкие, экстремальные
3. **Сортировка** — по `overround / sport_weight` (CS2 и Dota в приоритете)
4. **Мониторинг** — `polyarb_v3.py` следит за матчем через WebSocket, обновляя кэфы
5. **Сравнение** — вводишь кэф БК вручную, бот считает ROI
6. **Алерт** — при ROI > 0 мелодия + карточка с суммами

### Live

1. **Скан** — `bot_live.py` тянет все live-события с PM (фильтр `live=true`)
2. **Параллельные запросы** — 20 потоков запрашивают стаканы CLOB одновременно
3. **Фильтр** — оставляет только события с ликвидностью > $20 на обе стороны
4. **Меню** — показывает все рынки каждого события (`a`, `b`, `c`...)
5. **Запуск** — выбираешь нужный рынок, бот стартует с `--live` автоматически

---

## Стек

- **Python 3.10+**
- **requests** — HTTP к Polymarket API
- **websocket-client** — real-time стакан Polymarket (WSS)
- **sqlite3** — база данных вилок
- **matplotlib** (опционально) — графики профита

---

## Настройки (`config.json`)

```
{
  "bank": 20000,           // размер банка по умолчанию
  "rate": 0,               // курс ₽/$ (0 = автоматически с ЦБ РФ)
  "scan": {
    "top": 800,            // сколько рынков сканировать
    "show": 40,            // сколько показывать
    "min_volume": 100,     // минимум объёма за 24ч ($)
    "min_notional": 50,    // минимум ликвидности на ногу ($)
    "max_overround": 1.05  // максимальный спред
  },
  "bot": {
    "poll": 1.0,           // интервал опроса (сек)
    "slip": 0.0            // запас на слиппедж
  }
}
```

## Профили БК (`bk_profiles.json`)

```
{
  "BetBoom": {"comm": 0.0, "min_bet": 50, "update_seconds": 1},
  "Winline": {"comm": 0.0, "min_bet": 100, "update_seconds": 2},
  "PARI":    {"comm": 0.0, "min_bet": 100, "update_seconds": 4}
}
```

Поле `update_seconds` — как быстро БК обновляет линию. Для live важно: BetBoom (1с) лучше, чем PARI (4с).

---

## Важно понимать

**Polymarket не букмекер.** Это prediction market на блокчейне Polygon с расчётом в USDC. При ставках на PM нужно:

- Кошелёк (MetaMask, Rabby и др.)
- USDC на балансе
- Геоблок для некоторых регионов

**Все ставки делаются вручную.** Бот только находит вилку и показывает суммы — ставишь ты сам.

**БК могут резать лимиты.** BetBoom / Winline / PARI не любят профессиональных арбитражников. Если ставишь регулярно — могут урезать лимиты или заблокировать счёт.

**Не используйте автоставки на БК.** Это противоречит правилам и ведёт к бану. Полуавтоматический режим (только PM) — безопаснее.

---

## Что НЕ входит

- Автоматическая простановка на PM (требует приватный ключ кошелька)
- Скрапер БК (хрупко, ToS-violating)
- Telegram Mini App (это отдельный проект — `miniapp/`)

---

## Roadmap

- ☑  
Прематч-сканер с фильтрами
- ☑  
Live-сканер
- ☑  
Мульти-мониторинг через WebSocket
- ☑  
Логирование в SQLite
- ☑  
Интерактивные лаунчеры
- ☑  
Горячие клавиши P/B/T/S/R
- □  
Live Map/Set приоритет
- □  
Telegram-алерты
- □  
Интеграция с Mini App
- □  
Авто-покупка PM через py-clob-client

---

## Лицензия

MIT

## Контакты

GitHub: [@lap1xx](https://github.com/lap1xx)

---

**Дисклеймер:** инструмент создан в образовательных целях. Автор не несёт ответственности за любые финансовые потери. Арбитраж — рискованная деятельность, ставьте только те средства, которые готовы потерять.