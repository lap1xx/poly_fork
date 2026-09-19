# POLY-ARB

> Сканер арбитражных вилок между **Polymarket** и **BetBoom**.

Инструмент для поиска и мониторинга арбитражных ситуаций (вилок) между prediction market **Polymarket** и линией **BetBoom** в режиме live. Поддерживает Match Winner, Map N Winner (CS2 / Dota 2 / LoL) и теннисные рынки.

---

## Что умеет

- **Live WS-стрим BetBoom** — подключение к `ru-ws2.sporthub.bet` (tree_ws, protobuf), подписка на киберспорт и теннис
- **Авто-матчинг PM↔BB** — по именам команд + времени старта, с fuzzy-скором
- **Карточные рынки** — Map N Winner для CS2 / Dota 2 / LoL (через `f16 → f17/f30`)
- **Теннисные сеты** — Set N Winner, Match Winner
- **--watch** — авто-подхват новых PM-рынков каждые 20–60 сек
- **Двухнотный вывод** — ROI, ставки в ₽ и $, профит, время жизни вилки
- **Логирование** — SQLite (`arbs.db`), CSV, JSONL
- **PM depth check** — утилита `check_pm.py` для проверки глубины стакана перед крупной ставкой

---

## Скриншот

```

==========================================================================
[+] ARB FOUND  [CS2] [LIVE] [MAP2]
==========================================================================
Counter-Strike: MIBR vs FURIA - Map 2 Winner
slug: cs2-mibr-furia-2026-09-19-game2

Polymarket                MIBR                       k=3.226  stake=$12.67
BetBoom      (1s) FURIA                      k=2.400  stake=1433 RUB

ROI:      +37.61%
Profit:   940 RUB
Payout:   3440 RUB
Type:     LIVE
==========================================================================

```

---

## Установка

```

git clone [https://github.com/lap1xx/poly_fork.git](https://github.com/lap1xx/poly_fork.git)
cd poly_fork
pip install -r requirements.txt

```

Требуется **Python 3.10+**.

---

## Запуск

### AUTO (рекомендуется)

```cmd
python -B bot_live.py
```

Запускает `polyarb_v4.py` с флагами `--watch --live --poll 0.3 --alert-gap 30 --min-roi 7.0` без меню. Остановка — `Ctrl+C`.

### Свои параметры

```
python polyarb_v4.py --watch --live --poll 0.3 --alert-gap 30 --min-roi 7.0
```

| Флаг ↕▾ | Что делает ↕▾ |
|---|---|
| −`--watch` | авто-подхват новых PM-рынков |
| −`--watch-sec 20` | как часто рефрешить PM-рынки (по умолчанию 60) |
| −`--live` | только live-матчи |
| −`--poll 0.3` | интервал опроса BB (сек) |
| −`--alert-gap 30` | не повторять один и тот же слаг чаще N сек |
| −`--min-roi 7.0` | минимальный ROI для алерта (%) |
| −`--rate 85` | курс ₽/$ (0 = с ЦБ РФ) |
| −`--bank 2000` | банк сделки в ₽ |
⚙

### Проверка PM-стакана перед ставкой

```
python check_pm.py cs2-mibr-furia-2026-09-19-game2
```

Показывает `best_bid` / `best_ask` / `size` по обеим сторонам PM. Нужно перед ставкой >$50 — влезет ли вся сумма в ask.

---

## Структура проекта

```
poly_fork/
├── polyarb_v4.py         # главный сканер (WS BB + PM, матчинг, расчёт арба)
├── bb_bridge.py          # клиент BetBoom tree_ws (protobuf, парсинг f29/f30/f17)
├── bot_live.py           # AUTO-лаунчер (subprocess polyarb_v4.py)
├── check_pm.py           # утилита: смотрим стакан PM по слагу
├── config.json           # настройки (создаётся автоматически)
├── bk_profiles.json      # профили БК: комиссии, min_bet, update_seconds
├── requirements.txt
├── legacy/               # старые сканеры (не поддерживаются)
│   ├── scan_2way.py
│   ├── scan_live.py
│   └── polyarb_v3.py
└── arbs.db / arbs.jsonl  # логи (в .gitignore)
```

---

## Как это работает

### Live (главный режим)

1. **PM scan** — `polyarb_v4.py` тянет все live-рынки с Polymarket (2000+ слагов)
2. **BB connect** — установка WS к `wss://ru-ws2.sporthub.bet/api/tree_ws/v1` (subprotocol: protobuf)
3. **BB subscribe** — `settings_set` → `f4` (tree) → `f6` (state_subscribe_sports) для `sport_id=1 (Esports)` и `sport_id=4 (Tennis)`
4. **BB stream** — сервер шлёт `f29 newsletters_match` — поток матчей со stakes (кэфы в `ModelsStake.f10`)
5. **PM↔BB match** — матчинг по именам команд + дате старта (fuzzy score ≥0.85)
6. **F16 для карт** — при `kind=='map_winner'` вызывается `f16 matches_subscribe_full(match_id)` → сервер отдаёт `f17/f30` с полным матчем включая карточные стейки
7. **Расчёт** — `compute()` считает ROI по обеим ногам с учётом комиссии и слиппеджа
8. **Alert** — при ROI ≥ `min_roi` — принт + звук + запись в SQLite

### Матчинг сторон

PM-сторона (например `MIBR` на Map 2) матчится с BB-стороной (`FURIA`) — они **противоположны**. Отсеиваются ложные ситуации, когда PM и BB указывают на одну и ту же команду.

### PM refresh

Каждые `--watch-sec` секунд `market_map` перечитывается из Gamma API — все слаги (не только новые) обновляются свежими `outcomePrices`. Это критично для арбитража: PM стоит, а BB двигается — из этого и рождается вилка.

---

## Настройки (`config.json`)

```
{
  "bank": 2000,
  "rate": 0,
  "scan": {
    "min_volume": 100,
    "min_notional": 50
  },
  "bot": {
    "poll": 0.3,
    "slip": 0.0
  }
}
```

## Профили БК (`bk_profiles.json`)

```
{
  "BetBoom": {"comm": 0.0, "min_bet": 50, "update_seconds": 1}
}
```

Поле `update_seconds` — как быстро БК обновляет линию. BetBoom = 1с, это важно для live.

---

## Важно понимать

**Polymarket — не букмекер.** Prediction market на Polygon с расчётом в USDC. При ставках на PM нужно: кошелёк (MetaMask/Rabby), USDC на балансе, обход геоблока.

**Ставки делаются вручную.** Сканер находит вилку и показывает суммы — ставишь ты сам.

**PM часто «стоит» — это норма.** Prediction market менее ликвиден, чем БК. Если BB двигается, а PM стоит — это и есть источник арбитража. Но перед ставкой >$50 — обязательно проверь `check_pm.py <slug>`: есть ли ask по нужной цене с достаточным объёмом.

**БК режут лимиты.** BetBoom не любит арбитражников. При регулярных ставках — урезают лимиты, могут забанить счёт.

**Никаких автоставок.** Это против правил БК и против правил PM (если через API). Только полуавтомат: PM-сумму копируешь, БК ставишь руками.

---

## Что НЕ входит

- Автоставка на PM (нужен py-clob-client + приватный ключ)
- Автоставка на БК (ToS-violating, бан)
- Скрапер 1win/Winline (отложено)

---

## Roadmap

- ☑  
Live WS-стрим BetBoom через tree_ws (protobuf)
- ☑  
Авто-матчинг PM↔BB по именам команд
- ☑  
Match Winner (CS2 / Dota 2 / LoL / Tennis)
- ☑  
Map N Winner (CS2 / Dota 2 / LoL) через f16 → f17/f30
- ☑  
Теннисные сеты (Set N Winner)
- ☑  
`--watch` — авто-подхват новых PM-рынков
- ☑  
`market_map` refresh каждые N секунд
- ☑  
`check_pm.py` — глубина PM-стака
- □  
Telegram-алерты
- □  
`--stats` команда (dashboard из arbs.db)
- □  
Интеграция с Winline, Pari и 1Win (для расширения рынка)
- □  
Авто-покупка PM через py-clob-client

---

## Лицензия

MIT

## Контакты

GitHub: [@lap1xx](https://github.com/lap1xx)

---

**Дисклеймер:** инструмент создан в образовательных целях. Автор не несёт ответственности за любые финансовые потери. Арбитраж — рискованная деятельность, ставьте только те средства, которые готовы потерять.

