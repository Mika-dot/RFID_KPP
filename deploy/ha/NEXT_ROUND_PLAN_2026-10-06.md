# План следующего раунда работ — RFID КПП «Периметр»

Дата фиксации: **06.10.2026**  
Статус документа: **рабочий план / backlog следующего раунда**  
Базовый принятый runtime: `f5fdb6aed1c3749b0ada51e28c5dec96ed2c59fa`, protocol2.

Этот документ фиксирует:
- что уже принято и считается рабочим;
- что реализовано, но ещё не прошло production-приёмку;
- что необходимо добавить в следующем раунде;
- какие проверки должны блокировать небезопасное автообновление;
- как построить отдельный контур анализа шин данных, дрейфа и расхождений поведения.

---

## 1. Границы системы

У системы два крупных контура.

### 1.1. Бизнес-контур «Периметр»

Пять production-блоков:

1. **RFID Reader** — UHF считыватель, durable SQLite spool, запись в `dbo.RFID_Tags`.
2. **RusGuard Sync** — события СКУД, запись в `dbo.RusGuardLogs`.
3. **RTSP + YOLO** — две камеры, детекции и переходы, запись в `dbo.ReelTransitions`.
4. **Aggregator** — сессии RFID, корреляция RFID/YOLO/RusGuard/1C/Warehouse, итог `dbo.KPP_ReelEvents`.
5. **Web Dashboard** — read-only представление итоговых событий.

### 1.2. Контур отказоустойчивости

Три узла:

1. `physical` — основной исполнитель, priority 1.
2. `perimetr` — первый резерв, priority 2.
3. `comparator` — второй резерв и штатный HA controller, priority 3 как executor.

Переключение выполняется жёсткой алгоритмикой через SQL lease/epoch/fencing.  
LLM/LM Studio не участвует в выборе active owner и применяется только для ограниченного ремонта уже выведенного из работы узла.

---

## 2. Что уже сделано и принято

### 2.1. HA

- [x] HA включён, legacy launcher не используется при HA ON.
- [x] Все три узла работают на одном принятом runtime `f5fdb6a...`.
- [x] SQL fencing protocol2 установлен и проверен.
- [x] Проверено автоматическое переключение `physical → perimetr → physical`.
- [x] Проверено автоматическое переключение `physical → comparator → physical`.
- [x] Проверена работа полного стека из пяти сервисов на обоих резервных узлах.
- [x] Проверена независимая верификация восстановившегося узла и automatic failback.
- [x] Проверено переключение controller lease с Comparator на Perimetr при недоступности Comparator.
- [x] Старый owner после смены epoch не может продолжать бизнес-записи в SQL.
- [x] Windows boot task и Linux systemd units подтверждены.
- [x] Passive reserve не запускает пять business-workers без действующего owner lease.

### 2.2. Бизнес-контур

- [x] RFID использует durable local spool и idempotent SQL delivery.
- [x] RusGuard имеет durable cursor и idempotent sync.
- [x] YOLO имеет local spool, stale-frame protection и grouped transitions.
- [x] Aggregator имеет durable sessions/cursor и atomic checkpoint.
- [x] Реализована строгая идентификация Warehouse: `TAG → IDS → SERIES`.
- [x] Реализованы `WAREHOUSE_ONLY`, recheck и `SUPERSEDED_BY_KPP`.
- [x] Реализован promotion физически считанного `UNKNOWN_RFID` при последующем подтверждении Warehouse.
- [x] Реализовано one-to-one сопоставление внешних событий, PassageGroup и fusion направления.
- [x] Существующие health/readiness endpoints `18101..18105` сохранены.

---

## 3. Что ещё НЕ принято или требует доработки

### P0 — обязательно закрыть до объявления полной production-приёмки

#### 3.1. Реальный RFID end-to-end

Текущий статус: **NOT PROVEN**.

Необходимо настоящей рабочей RFID-меткой доказать цепочку:

```text
UHF reader
→ RFID process
→ local spool SENT
→ dbo.RFID_Tags
→ Aggregator cursor
→ RFID session
→ reel identity
→ final direction
→ correlation with video/RusGuard/Warehouse/1C
→ dbo.KPP_ReelEvents
→ Web
```

Минимальный marker первого этапа:

`LIVE_RFID_SQL_AND_AGGREGATOR_PROVEN`

Отдельно должна быть доказана полная бизнес-цепочка:

`COIL_IDENTITY_DIRECTION_VIDEO_AND_WAREHOUSE_ACCEPTANCE_PROVEN`

Synthetic business rows для такой приёмки не использовать.

#### 3.2. Жёсткий отказ physical и физический доступ к RFID controller

Проверить не только kill worker, но и реальный отказ:

- network isolation physical;
- power/offline physical;
- зависший старый TCP-сеанс к RFID reader;
- возможность нового owner открыть UHF API/TCP;
- отсутствие параллельного владения физическим считывателем.

SQL fencing защищает БД, но не разрывает физический TCP сам по себе.

Если reader не допускает безопасный takeover, добавить внешний hardware/network fence:
- managed switch ACL / port shutdown;
- relay/power fence;
- другой детерминированный способ разорвать доступ старого owner.

#### 3.3. Постоянный Web endpoint

Сейчас известный production URL привязан к physical.

Нужно выбрать и принять один механизм:

- VIP/floating IP;
- reverse proxy;
- internal DNS/service discovery;
- отдельный постоянный proxy перед тремя nodes.

Пользователь не должен знать, какой executor сейчас active.

### P1 — обязательные эксплуатационные функции

#### 3.4. Production-qualified auto-update из GitHub

Механизм updater уже существует, но rollout `main → production` пока не считается принятым.

Автообновление нельзя включать по принципу «новый commit успешно скачался».  
Перед раскаткой candidate должен пройти **механический acceptance pipeline**, описанный в разделе 4.

#### 3.5. Расширенный HA/business monitoring

Подключить во внешний Zabbix/Grafana/Sentry без создания дублей существующих объектов:

- current owner;
- current epoch;
- active controller;
- lease age/renew jitter;
- prepared/faulted/quarantine по каждому node;
- failover/failback duration;
- worker restart count;
- repair attempts/outcomes;
- update candidate/staged/probation/quarantine;
- RFID business-flow status;
- source-to-source correlation drift;
- spool lag/PENDING;
- Warehouse-only / UNKNOWN / NeedRecheck ratios.

### P2 — развитие системы

#### 3.6. Формальная приёмка LLM repair

Код ремонта существует, но нужно отдельно испытать реальный сценарий:

```text
faulted reserve
→ deterministic repair
→ при необходимости LM Studio
→ разрешённое repair action
→ independent preflight
→ 60 sec verify
→ 120 sec stable
→ reserve returned to service
```

LLM не должен:
- выбирать owner;
- менять lease/epoch;
- выполнять произвольный shell;
- менять SQL;
- сам объявлять узел healthy.

#### 3.7. Зафиксировать фактические RTO/RPO

Измерять:
- detection time;
- fence time;
- new lease time;
- start five workers;
- all-ready time;
- full business-ready time;
- failback time.

Отдельно зафиксировать, что local SQLite spools не реплицированы между узлами, поэтому при физической гибели диска возможна потеря ещё не доставленного PENDING.

---

# 4. Безопасное автообновление GitHub: обязательный mechanical acceptance gate

## 4.1. Классификация изменения

Каждый candidate commit должен сначала классифицироваться:

1. `DOCS_ONLY`
2. `MONITORING_ONLY`
3. `BUSINESS_LOGIC`
4. `HA_GUARDIAN`
5. `DB_SCHEMA`
6. `DEPENDENCIES`
7. `MODEL_OR_DLL`

Для `DOCS_ONLY` не должен выполняться production runtime rollout вообще.

Чем опаснее класс изменения, тем больше обязательных gates.

## 4.2. Gate A — статическая и механическая целостность

Candidate должен автоматически проверить:

- required entrypoints существуют;
- Python AST parse/import;
- unit/regression suite;
- конфигурационная schema совместима;
- production secrets не попали в Git;
- SQL migration files валидны;
- protocol2 constants и epoch barrier не изменены случайно;
- 9 fencing triggers ожидаемы для текущей schema;
- DLL/model/masks/entrypoints существуют;
- Python dependency lock/requirements разрешимы;
- health ports и service names не конфликтуют;
- candidate может пройти passive preflight;
- нет изменения business DB destination на другой server/database.

Любой fail => candidate quarantine, rollout запрещён.

## 4.3. Gate B — data-contract tests

Проверять схемы и инварианты входных/выходных данных всех пяти блоков:

### RFID
- `ClientReadUuid` idempotency;
- monotonic source sequence;
- EPC+TID identity semantics;
- correct antenna/RSSI/source-time fields;
- PENDING не удаляется до durable SQL delivery;
- reconnect/approximate time semantics не ломаются.

### RusGuard
- monotonic `ExternalId2`;
- high-water mark;
- correct IN/OUT mapping;
- irrelevant rows не блокируют cursor.

### YOLO
- stale/frozen frame rejection;
- one track cannot generate duplicate transition;
- grouped transition count deterministic;
- direction normalization stable;
- spool → SQL idempotency.

### Aggregator
- cursor monotonicity;
- session close rules;
- late data forms separate session;
- OUTER→INNER = IN;
- INNER→OUTER = OUT;
- unknown RFID не становится reel без evidence;
- PassageGroup preserves reel count;
- one external event is not assigned to two independent groups;
- direction fusion deterministic;
- transaction rollback restores in-memory state;
- recheck does not extend source session outside its raw ID range.

### Warehouse/1C
- identity priority remains `TAG → IDS → SERIES`;
- ambiguous SERIES never auto-links;
- WAREHOUSE_ONLY can be superseded only by real physical event;
- persisted WarehouseId has priority;
- physical `UNKNOWN_RFID` promotion remains supported.

### HA
- exactly one SQL owner;
- epoch increases on ownership transfer;
- stale epoch cannot write;
- passive node workers remain stopped;
- LLM never enters election state machine.

## 4.4. Gate C — Golden Trace Replay

Создать versioned набор обезличенных «золотых трасс» реальных событий.

Каждая trace должна содержать согласованный набор:
- RFID raw reads;
- RusGuard events;
- YOLO transitions;
- 1C/RfidTags;
- Warehouse rows;
- ожидаемые final events.

Обязательные сценарии:
- обычный одиночный барабан IN;
- обычный одиночный барабан OUT;
- несколько барабанов на одном погрузчике;
- RFID без видео;
- видео без RFID;
- пропуск SKUD;
- late RFID row;
- reconnect reader;
- одинаковый EPC с разными TID;
- ambiguous SeriesNumber;
- Warehouse arrives later;
- Warehouse-only → real KPP supersession;
- conflicting direction sources;
- duplicate source delivery;
- SQL temporary outage;
- camera stale/frozen;
- reordered events.

Candidate запускается на trace **без подключения к реальному оборудованию**.

Сравнивать:
- exact identities;
- direction;
- reel count;
- source links;
- warnings;
- NeedRecheck;
- cursor;
- spool states;
- final event count.

Для ожидаемых изменений допускается явно versioned изменение golden result, а не молчаливое расхождение.

## 4.5. Gate D — Differential Shadow Run

Перед rollout candidate должен некоторое время работать в shadow/read-only режиме рядом со stable runtime.

Важно: shadow НЕ открывает второй RFID TCP и НЕ пишет production business tables.

Вход shadow:
- копия уже записанных raw source events;
- либо bounded read-only tail production SQL;
- либо recorded event bus.

Сравнивать stable vs candidate:
- число итоговых событий;
- identity;
- direction;
- correlation links;
- warnings;
- processing lag;
- exception rate;
- NeedRecheck;
- unmatched source rates.

Формировать `candidate_diff_score`.

Если расхождение не объяснено allowlisted изменением — rollout блокировать.

## 4.6. Gate E — HA simulation

До production activation прогонять deterministic simulation:

- owner failure;
- stale agent;
- lost controller;
- lost SQL;
- candidate startup failure;
- readiness timeout;
- rollback;
- quarantined candidate;
- failback.

Проверять:
- не возникает два owner;
- старый epoch не пишет;
- failed candidate не становится preferred;
- rollback возвращает прошлый release;
- controller продолжает работать без LM Studio.

## 4.7. Gate F — staged rollout

Рекомендуемый порядок:

```text
candidate fetched
→ static/tests/contracts
→ golden replay
→ shadow differential
→ stage Comparator
→ passive preflight
→ stage Perimetr
→ passive preflight
→ optional controlled reserve canary for runtime-impacting changes
→ update physical only after two reserves validated
→ probation window
→ confirm
```

Для runtime-impacting changes controlled canary должен доказать полный стек на резерве.

Для docs-only rollout бизнес runtime не трогать.

## 4.8. Автоматический rollback

Rollback обязателен при:

- readiness fail;
- business invariant fail;
- candidate_diff_score above threshold;
- crash loop;
- spool growth without delivery;
- cursor stall;
- abnormal source mismatch;
- failover regression;
- SQL fencing mismatch.

Плохой SHA помещается в quarantine и не повторяется автоматически до нового commit/operator release.

---

# 5. Новый контур: анализ шин данных и дрейфа поведения

Цель — не ждать бинарной аварии, а видеть, что система **постепенно начинает вести себя не так, как обычно**.

Предлагаемое рабочее имя: **Behavior Observer / Data Bus Analytics**.

Он должен быть отдельным read-only observer и не входить в critical failover path.

## 5.1. Что считать «шинами»

### RFID bus

Метрики:
- reads/sec;
- unique EPC/sec;
- unique EPC+TID/sec;
- reads per tag;
- antenna distribution;
- antenna transition matrix;
- RSSI median/quantiles;
- inter-read gap;
- session duration;
- reconnect rate;
- approximate-time ratio;
- spool PENDING;
- spool delivery latency;
- SQL insert latency.

### Video bus

Метрики:
- fresh FPS per camera;
- stale frame rate;
- detection rate;
- class distribution;
- tracks/min;
- transitions/min;
- camera 0/1 asymmetry;
- transition time;
- grouped ReelCount;
- match delay;
- unmatched transitions;
- confidence distribution.

### RusGuard bus

Метрики:
- events/min;
- IN/OUT ratio;
- source delay;
- missing identity/card fields;
- gate/device distribution;
- sync lag;
- cursor velocity.

### Aggregator bus

Метрики:
- RFID sessions/min;
- PassageGroups/min;
- group reel count;
- UNKNOWN_RFID ratio;
- NeedRecheck ratio;
- video match ratio;
- SKUD match ratio;
- direction UNKNOWN/CONFLICT ratio;
- confidence distribution;
- source time deltas;
- processing latency;
- cursor lag.

### Warehouse / 1C bus

Метрики:
- warehouse rows/min;
- task rows/min;
- TAG/IDS/SERIES match proportions;
- ambiguous series count;
- Warehouse-only ratio;
- time from physical passage to warehouse evidence;
- superseded Warehouse-only ratio;
- unlinked warehouse rows.

### HA bus

Метрики:
- lease renew interval/jitter;
- controller renew interval/jitter;
- epoch changes/day;
- failover count;
- failover RTO;
- readiness flaps;
- process restarts;
- node quarantine count;
- repair action count;
- updater candidate failures.

---

## 5.2. Анализ не отдельных метрик, а связей между шинами

Главная ценность — отслеживать не только «RFID стало меньше», а **расхождение источников**.

Примеры residuals:

```text
RFID passage groups          ↔ YOLO transitions
RFID OUT                     ↔ Warehouse departures
RFID passage                 ↔ RusGuard gate events
physical RFID reel events    ↔ later Warehouse/1C identity
camera 0 transitions         ↔ camera 1 transitions
raw RFID volume              ↔ completed RFID sessions
spool produced               ↔ SQL delivered
final events                 ↔ Web-visible events
```

Для каждой связи хранить:
- expected ratio;
- expected time lag;
- expected variance;
- current residual;
- drift score;
- duration of abnormal state.

Пример:

```text
Обычно:
100 RFID passage groups
≈ 96..102 video transitions
≈ 94..101 warehouse confirmations later

Сейчас:
100 RFID groups
→ 78 video transitions
→ 97 warehouse confirmations

Вывод:
материальный поток, вероятно, продолжается,
а video bus постепенно расходится с остальными источниками.
```

Это полезнее простого «камера online».

---

## 5.3. Baseline и drift

Первый этап сделать детерминированным, без LLM.

Для каждой метрики и residual:
- rolling median;
- MAD;
- EWMA;
- percentiles;
- hourly/shift/day-of-week baseline;
- rate normalized by traffic volume;
- CUSUM/change-point detector для устойчивого сдвига;
- slope/trend за 15 min / 1 h / 8 h / 24 h / 7 d.

Получать:

- `anomaly_score` — насколько сейчас необычно;
- `drift_score` — насколько поведение устойчиво сместилось;
- `divergence_score` — насколько источники перестали согласовываться;
- `degradation_velocity` — скорость ухудшения.

Не считать единичный выброс трендом.

---

## 5.4. Прогноз ожидаемой «реальности»

Создать простой expected-state model:

```text
Observed sources
RFID + Video + RusGuard + Warehouse/1C
        ↓
expected physical-flow state
        ↓
residuals per source
```

Система не обязана знать абсолютную истину, но может оценивать, какой источник начал расходиться с консенсусом остальных.

Примеры:
- RFID и Warehouse согласованы, Video падает → вероятная деградация video.
- Video и Warehouse показывают движение, RFID уменьшается → вероятная деградация reader/antennas.
- RFID и Video согласованы, Warehouse сильно отстаёт → проблема warehouse/1C integration.
- все sources резко падают одновременно → вероятно реально нет движения, а не отказ.
- source rate нормальный, но latency растёт → раннее предупреждение о будущей очереди/зависании.

---

## 5.5. Prediction horizon

Не пытаться сразу строить «нейросетевой предиктивный ремонт».

Первый production вариант:
- прогноз на 15 минут;
- 1 час;
- текущую смену.

Прогнозировать:
- spool backlog;
- cursor lag;
- match-rate degradation;
- camera stale probability;
- вероятность выхода residual за warning threshold;
- вероятность fail/readiness degradation по устойчивому тренду.

После накопления истории можно добавить ML/time-series model, но только после наличия качественного baseline.

---

## 5.6. Root-cause graph

Хранить причинно-ориентированную карту:

```text
RFID TCP
→ inventory loop
→ local spool
→ SQL writer
→ RFID_Tags
→ Aggregator cursor
→ sessionizer
→ identity
→ fusion
→ KPP_ReelEvents
→ Web
```

И аналогично для Video/RusGuard/Warehouse.

Если downstream metric деградирует, observer должен искать ближайший upstream divergence.

Пример:

```text
Web events ↓
KPP_ReelEvents ↓
Aggregator cursor lag ↑
RFID_Tags normal
spool normal

Вероятная зона: Aggregator, а не RFID reader.
```

---

## 5.7. Использование LLM в Behavior Observer

LLM допускается только как объясняющий слой:

```text
metrics + residuals + trend + topology
→ LLM
→ человекочитаемая гипотеза причины
```

LLM не должен:
- менять thresholds автоматически;
- отключать monitoring;
- переключать HA;
- править БД;
- выполнять repair без существующего ограниченного repair pipeline.

---

## 5.8. Выходы observer

Минимум:

1. Prometheus/Zabbix-compatible metrics.
2. Grafana dashboard:
   - bus health;
   - cross-source divergence;
   - drift;
   - trends;
   - HA behavior;
   - updater health.
3. Sentry events только для существенных software failures.
4. Daily/shift summary:
   - что изменилось;
   - какие residual растут;
   - какие источники расходятся;
   - прогноз ближайшей деградации.
5. Machine-readable anomaly journal для последующего анализа.

---

# 6. Предлагаемые новые компоненты

Не встраивать behavioural analytics внутрь Aggregator.

Предпочтительно:

```text
common/
  behavior_contracts.py

observer/
  behavior_observer.py
  baselines.py
  residuals.py
  drift.py
  predictor.py
  topology.py
  metrics.py

tests/
  golden_traces/
  test_behavior_*.py
  test_golden_replay_*.py
  test_update_acceptance_*.py
```

Observer читает уже существующие SQL/runtime metrics и не имеет write-доступа к business tables.

---

# 7. План следующего раунда по порядку

## Этап 1 — закрыть business acceptance
- [ ] Настоящая RFID-метка.
- [ ] RFID → spool → SQL → Aggregator.
- [ ] Identity/direction/video/RusGuard/Warehouse/1C.
- [ ] Зафиксировать acceptance journal.

## Этап 2 — проверить hard failure
- [ ] Network isolation physical.
- [ ] Проверить takeover UHF reader.
- [ ] При необходимости реализовать external hardware/network fencing.
- [ ] Измерить RTO.

## Этап 3 — единый Web endpoint
- [ ] VIP/proxy/DNS.
- [ ] Проверить URL при `physical`, `perimetr`, `comparator`.

## Этап 4 — квалифицировать auto-update
- [ ] Change classifier.
- [ ] Static mechanical gate.
- [ ] Data-contract tests.
- [ ] Golden Trace Replay.
- [ ] Differential Shadow Run.
- [ ] HA simulation.
- [ ] Reserve staging.
- [ ] Candidate quarantine.
- [ ] Automatic rollback.
- [ ] Docs-only commits не должны менять runtime.

## Этап 5 — Behavior Observer
- [ ] Снять перечень всех bus metrics.
- [ ] Добавить cross-source residuals.
- [ ] Собрать 1–2 недели baseline history.
- [ ] Добавить anomaly/drift/divergence/degradation scores.
- [ ] Добавить change-point detection.
- [ ] Добавить short-horizon prediction.
- [ ] Сделать Grafana dashboard.
- [ ] Добавить warning/critical policy.

## Этап 6 — monitoring + repair acceptance
- [ ] Новые HA/business Zabbix items без дублей.
- [ ] Dashboard owner/epoch/reserves/RTO.
- [ ] Проверить реальный LLM repair на faulted reserve.
- [ ] Проверить LM Studio offline — HA продолжает работать.
- [ ] Проверить invalid LLM output — неизвестная команда не выполняется.

---

# 8. Definition of Done следующего раунда

Следующий раунд считается закрытым только если одновременно:

- [ ] Пройдена настоящая business RFID acceptance.
- [ ] Пройден hard physical/network failure.
- [ ] UHF takeover доказан.
- [ ] Один постоянный Web endpoint работает независимо от owner.
- [ ] Auto-update имеет mechanical + logical + replay + shadow gates.
- [ ] Bad candidate автоматически rollback/quarantine.
- [ ] Docs-only commit не перезапускает production.
- [ ] Behavior Observer показывает baseline и cross-bus residuals.
- [ ] Есть drift history и минимум один проверенный synthetic/replayed drift scenario.
- [ ] В Grafana видны business, HA и divergence metrics.
- [ ] LLM repair отдельно принят, либо явно остаётся advisory/experimental.
- [ ] Зафиксированы реальные RTO и ограничения RPO.

---

# 9. Неизменяемые правила

1. **LLM никогда не принимает решение о HA owner.**
2. **SQL lease/epoch/fencing остаётся единственным источником права на business write.**
3. **Unknown RFID не становится reel без evidence.**
4. **Не ослаблять Warehouse identity rules.**
5. **Не запускать второй активный RFID reader только ради shadow test.**
6. **Golden/shadow tests не должны писать synthetic rows в production tables.**
7. **Не выкатывать candidate при необъяснённом behavioral divergence.**
8. **Observer остаётся read-only и не входит в critical control loop.**
9. **Не создавать дубли существующих Sentry/Zabbix объектов.**
10. **Любое обновление должно быть обратимо до предыдущего принятого runtime.**

---

## Краткий целевой результат

После следующего раунда система должна не только автоматически переживать отказ узла, но и:

1. механически доказывать корректность нового кода до автообновления;
2. сравнивать новый runtime со стабильным на реальных трассах;
3. видеть постепенные отклонения между RFID, видео, СКУД, Warehouse/1C и HA;
4. оценивать скорость деградации и краткосрочный риск;
5. автоматически блокировать/откатывать подозрительный update;
6. оставлять переключение и safety-critical решения полностью детерминированными.
