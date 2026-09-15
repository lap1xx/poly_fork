# POLY-ARB

Сканер арбитражных вилок между Polymarket и российскими БК (PARI, BetBoom, Winline).

## Что умеет

- Поиск плотных рынков на Polymarket — киберспорт + теннис
- Live-сканер идущих матчей (CS2, Dota, LoL, Valorant, теннис)
- Мульти-мониторинг через WebSocket (10+ матчей одновременно)
- Логирование всех вилок в SQLite (arbs.db)
- Курс ₽/$ подтягивается с ЦБ РФ
- Звуковой алерт + заморозка при находке вилки

## Установка

pip install -r requirements.txt

## Запуск

Прематч-сканер:

python scan_2way.py --sport CS2

Live-сканер:

python scan_live.py --sport TENNIS

Бот на найденных матчах:

python polyarb_v3.py --slugs slug1,slug2,slug3 --bank 20000

## Структура

- scan_2way.py — прематч-сканер
- scan_live.py — live-сканер
- polyarb_v3.py — бот (WebSocket + SQLite)
- config.json — настройки (создаётся автоматически)

## Требования

- Python 3.10+
- requests, websocket-client