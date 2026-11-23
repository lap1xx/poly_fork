# ADR-001: Канонический backend и доменная модель сигнала

- **Статус:** Accepted for the current backend choice; Draft 2020-12 schema and replay instances are validated in CI; additive backend publication and the first dashboard list/card dual-read are implemented, broader client adoption remains pending.
- **Дата:** 2026-11-23
- **Контекст:** `polyarb_v4.py` — основной read-only сканер; `scanner_publisher.py` публикует полные snapshot-сообщения в локальный защищённый ingest `/ws/scanner`; `web_app.py` проверяет сигналы и обслуживает используемую локальную панель. README прямо называет `miniapp/` незавершённым прототипом, а `server/api_bridge.py` — необязательным мостом Mini App с отдельным монитором и отдельной схемой данных.

## Решение

1. Для текущего поддерживаемого desktop/local сценария каноническим backend остаётся **`web_app.py`**. Основной поток: `polyarb_v4.py` → `scanner_publisher.py` → loopback `/ws/scanner` в `web_app.py` → UI-сокет `/ws` → `templates/index.html` и `static/`.
2. Сканер является владельцем наблюдений и расчёта возможностей. Backend проверяет входной контракт, обогащает данные для представления и доставляет снимок; он не должен повторно рассчитывать ROI из другой модели или сливать ноги разных букмекеров.
3. **Канонический доменный объект — `Opportunity v1`**, описанный в `opportunity-v1.schema.json`. Он отделён от transport envelope. Один opportunity содержит ровно две противоположные ноги: одну prediction-market и одну bookmaker; для MVP источниками остаются Polymarket и один текущий источник котировки.
4. Веб-панель остаётся первым и единственным поддерживаемым UI на этом этапе. `miniapp/` не удаляется; его последующее подключение должно быть клиентом того же API/домена, а не вторым независимым сканером. До этой миграции Mini App и `server/api_bridge.py` считать экспериментальным контуром и не рекламировать как production путь.
5. Существующий WebSocket transport `scanner.snapshot` версии 1 и файловый fallback сохраняются без изменения. Backend аддитивно публикует `opportunities_v1` рядом с legacy `arbs`; миграция scanner transport требует отдельного ADR.

## Почему

Это соответствует актуальному README: текущий локальный запуск и поддерживаемая панель проходят через `bot_live.py`/`polyarb_v4.py` и `web_app.py`. Сканерный ingest уже имеет token, loopback-only проверку, `session_id`, sequence, время отправки и версию transport schema. В то же время Mini App описана как prototype, а `server/api_bridge.py` запускает отдельный `server.monitor`, использует иной набор конфигурации и имеет demo-поля в `/api/scan`. Выбирать последний в качестве основы означало бы сначала поддерживать и нормализовать второй источник данных.

## Границы `Opportunity v1`

- `id` стабильно идентифицирует конкретную пару рынок/источник/исходы; один event slug сам по себе недостаточен для отличения нескольких токенов/линий.
- `market` описывает Polymarket event/market, sport, рынок и фазу. `phase` принимает `live`, `prematch` или `unknown`; время старта само по себе не доказывает live.
- `legs` — ровно две противоположные стороны одного сопоставления. Каждая содержит источник, outcome, цену с явной единицей, плановую долю и валюту. У каждой ноги есть `observed_at`, `age_ms` и `timestamp_basis`. Когда поставщик не даёт время котировки, `timestamp_basis` должен быть `received_at`, а не фиктивным provider timestamp.
- PM price — вероятность от 0 до 1 и должна сопровождаться token ID. Bookmaker price — десятичный коэффициент больше 1. Нельзя сравнивать цены, пока их единицы не указаны.
- `calculation.roi_fraction` хранится долей: `0.04` означает 4%; отображение в процентах — обязанность UI. `currency` обязательна; для текущего scanner path плановая валюта — USDT.
- `mapping` сообщает, почему соответствие принято, каким режимом (ручное/автоматизированное) и, если рассчитывается, confidence. Неоднозначное соответствие не должно выпускать active opportunity.
- `status=active` допустим только когда обе ноги прошли инварианты, котировки не старше применимого TTL и расчёт завершён по валидным ценам/глубине. Сигнал, ставший несвежим, должен перестать отображаться как действующий; он может отдельно оставаться в истории/снимках.

## Безопасная миграция

Не переименовывать поля в publisher или JS в этом PR. Сначала добавить доменную схему, адаптер и offline fixtures. Одновременно проверить, что один и тот же opportunity проходит:

1. сериализацию publisher в `scanner.snapshot` v1;
2. текущую validation/filtering в `/ws/scanner`;
3. адаптацию к `Opportunity v1` в backend;
4. передачу существующему UI без изменения поведения.

Сначала включать адаптер в тестах; в runtime добавлять его только после успешного replay и полного прогона текущего offline набора. Старые поля должны оставаться доступными UI на переходный период. Contract/replay и полный offline-набор пройдены; backend добавляет нормализованный snapshot аддитивно, а UI сохраняет legacy-поведение.

## Не входит в это решение

- Публичный/облачный deployment, многопользовательская авторизация, платежи или хранение учётных данных площадок.
- Автоматическая постановка ставок; проект остаётся read-only монитором с ручным действием пользователя.
- Расширение bookmaker coverage или ослабление правил сопоставления.
- Переписывание Mini App/удаление `server/`.
- Обещания суб-100ms доставки: сначала измерить существующую цепочку и определить, какая часть задержки находится под контролем проекта.

## Последствия и следующие шаги

- Снижается риск поддерживать две конкурирующие модели сигнала и два несогласованных backend-контура.
- Python-нормализатор и контрактная replay-fixture уже добавлены; TypeScript-клиент и миграция UI/transport остаются отдельными будущими шагами.
- JSON Schema валидируется `jsonschema` только в dev/CI-зависимостях; replay проверяет как допустимый экземпляр, так и отказ для PM-цены выше 1. Браузерный dual-read adapter принимает только v1-контракт с `mapping.status=accepted` и совпадающими slug/token; при несовпадении карточка остаётся на legacy fallback.
- Первый шаг dual-read реализован для отображения списка/карточек: market, две ноги, расчётный ROI, возраст котировок и неактивный статус читаются из `opportunities_v1`. Сортировка/фильтры и UI-only метаданные остаются на `arbs`; детали, калькулятор, ссылки/действия, закреплённые снимки и журнал также остаются legacy. Headless Chromium parity test запускает реальную страницу и production UI JS с replay-парой, нормализованной через production `Opportunity` adapter; WS-снимок инъецируется управляемым shim, а backend transport отдельно покрыт `test_event_pipeline.py`. CI устанавливает Chromium и гоняет этот regression до расширения миграции.
- Семантика legacy `bank`/`total_bank`/`stakes`/`roi`/`profit`, PM ask/target fields и provider-specific links сведена в [legacy field contract](legacy-field-semantics.md); описания соответствующих canonical schema fields уточнены без смены версии/структуры. Не выводить legacy `bets[].o` из PM probability и bookmaker odds: для PM это fee-adjusted effective multiplier, для bookmaker — decimal odds.

## Подтверждено владельцем

- `web_app.py` остаётся каноническим backend текущего локального приложения.

## Остаётся до расчётной/UI migration

- PM refined VWAP/size теперь публикуется аддитивно и имеет multi-level-book regression. Если после пяти итераций refinement ещё меняет implied PM price, candidate отклоняется: последняя пересчитанная stake allocation не считается подтверждённой тем quote, который она сама подразумевает. Нельзя переносить расчёт на Opportunity v1, пока replay/regression не подтвердит согласованность quote и allocation на стабилизировавшихся кандидатах.
- Opportunity v1 теперь переносит legacy PM/bookmaker URL отдельно в соответствующую leg; сами action-кнопки остаются legacy. Перед их миграцией добавить source-bound link action tests. PM direct/fallback open paths уже используют общий exact HTTPS origin/path validator, покрытый `tests/test_provider_links.cjs`.
- План подключения Mini App к общей модели и API; до этого она остаётся вне текущего поддерживаемого deployment target.
