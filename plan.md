# План: событийный пересчёт и live-доставка вилок

## Цель и ограничения

Снизить локальную задержку между обновлением PM/1win котировки и карточкой в панели через точечный пересчёт затронутых PM-рынков и прямую передачу scanner state в backend по аутентифицированному WebSocket. Поддерживаемые группы остаются CS2, Dota 2, LoL, Valorant и Tennis. Проект строго read-only: никаких ставок, купонов или действий аккаунта.

## Архитектура реализации

- При запуске выполняется полный начальный проход: загрузка каталога, binding источников, индексы token/match → PM slug и первичная оценка каждого рынка.
- PM CLOB feed и 1win odds feed после фиксации изменений кэша ставят ID в потокобезопасную coalescing-очередь. Единственный dispatcher разрешает события через обратные индексы и последовательно пересчитывает только затронутые PM slug.
- Для цены Polymarket freshness привязывается только к полному `book` snapshot и `price_change` level delta. `last_trade_price` хранится как отдельный execution факт и не продлевает TTL resting bids/asks, не будит arb recalculation; недостающий/устаревший стакан восстанавливается через CLOB REST book endpoint.
- Индексы перестраиваются при первичной привязке и изменении PM-каталога/1win binding. Повторные события объединяются; обновление, пришедшее во время расчёта, остаётся в очереди.
- Периодическое обслуживание сохраняет каталог, повторную привязку, freshness/status и expiry-контроль активных сигналов, но не возвращает полный обход рынка в горячий цикл. Gamma catalog fetch ограничен по времени и выполняет категории параллельно; partial refresh сохраняет прежние неполученные записи.
- Сопоставление остаётся fail-closed. Ручные aliases допускаются только для подтверждённых вариантов имени той же команды/игрока; пороги, защита от неоднозначных кандидатов, время-corrobation и фильтры типов рынка не ослабляются.
- Matching diagnostics разделяют общее число открытых котировок в кэше и число свежих двухсторонних пар для текущих bound PM-slugs, проверенных тем же `live_bc` path, который используется scanner. Best ROI последнего dispatch-пакета показывается отдельно от retained-market cache ROI и сопровождается возрастом наблюдения.
- Сканер сохраняет атомарные JSON snapshots для восстановления/диагностики и посылает актуальный согласованный snapshot через постоянный loopback WebSocket. Backend принимает версионированные scanner snapshots с Bearer token из child-process environment; browser `/ws` остаётся read-only.
- Backend будит broadcast-loop сразу после scanner update. При отсутствии свежего scanner heartbeat возвращается файловый fallback; publisher после reconnect повторно отправляет последний snapshot.

## Структура проекта и ответственность

| Файл | Ответственность |
| --- | --- |
| `polyarb_v4.py` | Совместимый scanner facade, dirty queue/indexes, единый dispatcher, точечный расчёт, scoped ROI и bounded five-minute inactive signal-drop history с последними PM/БК котировками. |
| `pm_market_data.py` | Polymarket CLOB WebSocket feed, shared local order book/helpers, freshness diagnostics и ограниченный REST book recovery; dispatch callback вызывается после фиксации книги. |
| `pm_catalog.py` | Gamma keyset catalog acquisition, bounded pagination/retries, completeness diagnostics, market normalization и conservative partial-refresh reconciliation. |
| `arb_calculations.py` | Pure ROI/odds/stake math and alert timing; PM depth and best-price operations are explicit dependencies supplied by the scanner facade. |
| `arb_candidates.py` | Построение и fail-closed проверка пар PM↔одна выбранная БК; функции получают PM fee/book runtime-зависимости через явные callables, а scanner сохраняет прежний фасад. |
| `onewin_bridge.py` | Read-only каталог/odds feed, callback после фиксации цены, fail-closed matching и alias normalization. |
| `team_aliases.json` | Проверенные вручную эквивалентные имена команд и игроков; не объединять разные составы или сущности. |
| `bk_router.py` | 1win-first routing, callback, read-only quote lookup и подсчёт свежих пар только для текущих PM bindings. |
| `web_app.py` | Аутентифицированный `/ws/scanner`, in-memory latest state, быстрый browser fanout и файловый fallback. |
| `bot_live.py` | Генерация эфемерного Bearer token и передача его только процессам backend/scanner через environment. |
| `static/app.js` | Русская health panel, свежие bound pairs, транспорт, latency, cache/batch ROI и причины снятия сигналов без добавления их в active list. |
| `tests/` | Регрессии coalescing/index dispatch, callbacks, WS protocol, fail-closed aliases, quote freshness, signal-drop diagnostics, ROI и bankroll guards. |

## Сохранение совместимости и риски

Публичный browser WebSocket `/ws` и его snapshot schema сохраняются. Значение min ROI по умолчанию у launcher — 3%, соответствуя пользовательскому порогу; его можно переопределить через CLI. Допустимая свежесть PM/БК котировок (30 секунд), глубина/impact checks, 40%-й ориентир банка и manual event-link не меняются. Снятые сигналы доступны до пяти минут только в отдельной неактивной диагностике; они не возвращаются в активный список и не могут использовать устаревшие цены как свежие. Сетевые partial results должны сохранять предыдущий каталог, а stale/disconnected scanner не должен маркироваться как свежий. Файловые snapshots остаются аварийным источником. Для BetBoom fallback без callback-интеграции сохраняется ограниченный polling, не возвращающий полный горячий цикл.


## Статус выполнения и оставшиеся задачи

Обновлено: 24 ноября 2026 года. Это статус архитектурного плана; point-in-time показатели не следует трактовать как постоянные характеристики feeds или end-to-end latency.

| Этап | Статус | Что подтверждено / что остаётся |
| --- | --- | --- |
| Событийный пересчёт и live-доставка для текущего 1win пути | Реализация покрыта тестами; runtime измерен отдельно | Callback/index/dispatcher и scanner → backend snapshot pipeline покрыты регрессиями. Read-only baseline 24 ноября запустил 1win и PM feeds; измеренная receipt→scanner-status latency — внутренняя метрика, не end-to-end UI latency. |
| Приоритет 5 — разделение `polyarb_v4.py` | Выполнено | Candidate builder (`arb_candidates.py`), PM CLOB/book/recovery (`pm_market_data.py`), Gamma catalog (`pm_catalog.py`) и ROI/stake calculations (`arb_calculations.py`) выделены с совместимым facade; commits отправлены в `main`. |
| Приоритет 6.1 — baseline полноты Gamma и покрытия PM↔1win | Частично выполнено, требуется повторяемое наблюдение | Снимок от 21 ноября 2026: 1 406 активных бинарных рынков, 338 поддерживаемых, 75 сопоставленных, 41 свежая полная пара (54,7% от сопоставленных; 12,1% от поддерживаемых). Это один момент времени, а исходные данные не находятся в текущей Sandbox-копии. Нужен новый read-only сбор с timestamp и сохранённой диагностикой. |
| Приоритет 6.2 — baseline свежести/устойчивости текущего 1win pipeline | Gamma-blocking снижено и измерено; WebSocket ping/pong fix добавлен, runtime-проверка ожидается | После background Gamma worker capture 12:15:59–12:30:59 MSK на `0da7d2a`: 245 snapshots/899,478 с, 0 ошибок чтения, connected 100%, feed errors 0; fresh pairs p50 1/p95 2/max 9; last price update age p50 0,7 с. Catalog complete только в 7,76% snapshots: 212 из 245 снимков соответствовали Gamma fetch с `SSLError` для 14/14 query streams; ещё были частичные ошибки `ReadTimeout`/`SSLError`. Старая карта сохранялась. Сравнение со вторым capture: медиана rolling receipt→status p95 упала 118,428→25,995 с (−78,1%), queue-wait p95 64,839→16,563 с (−74,5%), dispatcher-processing p95 66,280→8,899 с (−86,6%); все метрики локальные, не end-to-end, отдельные component p95 нельзя суммировать. В timestamped publisher log — 17 `BrokenPipeError` за 900 с (медианный интервал 54,6 с), client close code/reason `None`, backend peer-disconnect events не зарегистрированы. Uvicorn runtime defaults — ping interval/timeout 20/20 с; `websocket-client` автоматически отвечает на ping только при чтении control frames, а прежний publisher не вызывал `recv()`. Добавлен reader thread для ping/pong и peer-close detection; regression прошла, полный offline набор — 221 Python tests + 58 subtests и 5 Node suites, compileall/diff-check прошли. Capture после ping/pong изменения ещё не выполнен; WebSocket-причина остаётся гипотезой до runtime-проверки. Raw captures/reports лежат в игнорируемом `.work/`. |
| Приоритет 6.3 — пороги quality gate и пилот другого источника | Отложено | Не задавать пороги задним числом: сначала получить сопоставимые повторные baseline-снимки и согласовать критерии. Реализацию новой БК не начинать без отдельного разрешения; текущий фокус остаётся на 1win. BetBoom сюда не подключается, и его прежний путь не считается исправленным. |

### Следующее действие

После push reader/ping-pong изменения провести сопоставимый 15-минутный read-only capture на 1win-only. Проверить отсутствие ~55-секундных BrokenPipe reconnects и появление серверных close codes; сравнить latency components с окном на `0da7d2a`. Gamma TLS failures диагностировать отдельно от live-dispatch пути. Не считать quality gate пройденным и не назначать пороги до нескольких сопоставимых повторов; новую БК не подключать.
