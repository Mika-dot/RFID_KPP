# Сверка плана с фактом — 08.10.2026

Считаются **33 пункта раздела 7 исходного плана**. Повторяющиеся критерии
приёмки из раздела 8 не увеличивают знаменатель. Дополнения пользователя
считаются отдельно: они уточняют часть тех же требований.

**ПО готово: 20; частично: 4; открыто из-за оборудования/времени: 8; отложено: 1.**

Дополнения 08.10: **9 критериев, 8 готовы программно, 1 частично**.

Готовность ПО означает реализацию и offline-проверку. Новая ветка не установлена
на оборудование; текущая работа завода из этой среды не проверена. TCP Grafana
и трёх Guardian недоступен. Main/прежний принятый runtime не менялись.

## По этапам

| Этап | Всего | ПО готово | Частично | Открыто | Отложено |
|---|---:|---:|---:|---:|---:|
| 1 | 4 | 0 | 0 | 4 | 0 |
| 2 | 4 | 0 | 2 | 2 | 0 |
| 3 | 2 | 2 | 0 | 0 | 0 |
| 4 | 10 | 8 | 2 | 0 | 0 |
| 5 | 8 | 7 | 0 | 1 | 0 |
| 6 | 5 | 3 | 0 | 1 | 1 |

## Проверка каждого пункта

| ID | Требование | Статус | Что ещё требуется |
|---|---|---|---|
| 1.1 | Настоящая RFID-метка | Открыто: оборудование/время | Физическая приёмка запрещена условиями этой итерации |
| 1.2 | RFID → spool → SQL → Aggregator | Открыто: оборудование/время | Offline adapter test есть; live business chain не доказана |
| 1.3 | Identity/direction/video/RusGuard/Warehouse/1C | Открыто: оборудование/время | Архивная проверка не заменяет новый физический проход |
| 1.4 | Зафиксировать acceptance journal | Открыто: оборудование/время | Offline журнал есть; полного live acceptance результата нет |
| 2.1 | Network isolation physical | Открыто: оборудование/время | Есть модель, реальная изоляция не выполнялась |
| 2.2 | Проверить takeover UHF reader | Открыто: оборудование/время | Настоящий TCP/device takeover не измерен |
| 2.3 | External hardware/network fencing | Частично | Verified interface/rollback реализованы; адаптер конкретного switch/relay не подключён |
| 2.4 | Измерить RTO | Частично | Readiness timing есть; физический business RTO/RPO не измерены |
| 3.1 | VIP/proxy/DNS | ПО готово | Gateway установить, адрес принять инфраструктурой; отказ Comparator отдельно |
| 3.2 | Проверить URL при physical/perimetr/comparator | ПО готово | Loopback проверка прошла; live проверка URL после установки |
| 4.1 | Change classifier | ПО готово | Production rollout не принят |
| 4.2 | Static mechanical gate | ПО готово | Применить через accepted staged rollout |
| 4.3 | Data-contract tests | ПО готово | Native SQL integration требует отдельной test DB |
| 4.4 | Golden Trace Replay | Частично | 15 моделей + 12 recorded fragments; recorded video отсутствует, expected — stable behavior reference, не независимая physical truth |
| 4.5 | Differential Shadow Run | Частично | Offline comparison на записанных inputs есть; длительный shadow полного pipeline на production tail не выполнен |
| 4.6 | HA simulation | ПО готово | Физический отказ отдельно |
| 4.7 | Reserve staging | ПО готово | Установка новой ветки на узлы не выполнена |
| 4.8 | Candidate quarantine | ПО готово | Production приёмка |
| 4.9 | Automatic rollback | ПО готово | Production приёмка |
| 4.10 | Docs-only commits не меняют runtime | ПО готово | Принять новое ПО; main пока не менять |
| 5.1 | Перечень bus metrics | ПО готово | Catalog явно показывает также not_instrumented и unavailable; наличие перечня не означает наличие каждого датчика |
| 5.2 | Cross-source residuals | ПО готово | Установка, реальная baseline |
| 5.3 | 1–2 недели baseline history | Открыто: оборудование/время | Observer установить и накопить реальные 1–2 недели, не заменить частыми synthetic samples |
| 5.4 | Anomaly/drift/divergence/degradation scores | ПО готово | Калибровка реальными наблюдениями |
| 5.5 | Change-point detection | ПО готово | CUSUM/persistence проверены offline, real thresholds не приняты |
| 5.6 | Short-horizon prediction | ПО готово | Линейный прогноз, не вероятность отказа; нужна реальная history |
| 5.7 | Grafana dashboard | ПО готово | Live Grafana недоступна, установка не выполнена |
| 5.8 | Warning/critical policy | ПО готово | Принять operating thresholds/notification endpoint при установке |
| 6.1 | Новые HA/business Zabbix items без дублей | Отложено | Ранее setup Zabbix был пропущен по решению пользователя; состояние существующих объектов не проверено live |
| 6.2 | Dashboard owner/epoch/reserves/RTO | ПО готово | Live install/verify; RTO readiness отдельно от physical business RTO |
| 6.3 | Реальный LLM repair на faulted reserve | Открыто: оборудование/время | Нужны установленный reserve, LM Studio и отдельно разрешённый реальный fault |
| 6.4 | LM Studio offline — HA работает | ПО готово | Native production scenario не принят |
| 6.5 | Invalid LLM output — команда не выполняется | ПО готово | Production приёмка ограниченного repair path |
| A1 | Обратный поиск ожидаемого прохода по складу | ПО готово | Найдены и ранжируются реальные session candidates; подтверждение/автоматическая business-запись требуют отдельной приёмки полного source context |
| A2 | Адаптивные окна, время суток/сезон/gradient descent | ПО готово | Confirmed/rejected factory labels и accepted holdout перед active |
| A3 | Постоянный независимый Web адрес | ПО готово | Установка/адрес; для отказа Comparator независимая инфраструктура |
| A4 | Локальные копии минимум три месяца без фото | ПО готово | Установка и backfill шести потоков; не полный SQL Server backup |
| A5 | Работа при полном обвале БД и последующая выгрузка | Частично | PENDING/replay есть; непрерывный capture без SQL authority не гарантирован, нужен SQL HA listener |
| A6 | Максимально визуальная схема всех цепочек и резервов | ПО готово | Live apply недоступен |
| A7 | Две страницы, переключение каждую минуту | ПО готово | Live apply и открыть playlist на display |
| A8 | Убрать верхнюю надпись | ПО готово | Удаляется top text panel; фактический тип live banner пока неизвестен |
| A9 | Сохранить информацию для продолжения в GitHub | ПО готово | Обновлять по каждому этапу |

## Что доделано после повторной сверки

- Зеркало расширено до шести потоков: итоговые события, Warehouse/1C, raw RFID, video metadata и СКУД без фото/персональных полей.
- GET-only installation gate проверяет минимум 93 дня, все шесть свежих caught-up потоков, архив и резервный просмотр на каждом узле: `--require-local-copies`.
- Незавершённый backfill/ошибка копии не рисуются готовым резервом. Fresh mirror выбирается прежде более нового ошибочного или неполного.
- Grafana показывает фактические repair verification/update trial/quarantine и наблюдаемый readiness RTO. Это не готовность LLM и не физический RTO.
- Ретроспективный поиск восстанавливает session candidates штатным sessionizer, показывает направление/время/границы/score. Кандидаты не объявляются подтверждёнными проходами и не обучают модель.
- Добавлены 12 обезличенных фрагментов записанной CSV-истории к 15 детерминированным моделям: всего 27 golden traces. 328 read occurrences, 203 distinct source reads; повторяющиеся окна не считаются независимыми экспериментами.
- Expected recorded results получены из чистого pinned PR #8 (`1d59061...`). Это behavioral regression reference, не независимая разметка физической истины. Записанного video в этих фрагментах нет.
- Добавлен авторизованный `/catalog` Behavior Observer: измеренные, недоступные и неинструментированные метрики различаются; пропуски не заполняются нулями.

## Оставшаяся детализация

Каталог содержит 73 определений доступных адаптеров и 43 `not_instrumented` позиции из расширенного wishlist раздела 5.
Перечень метрик (пункт 5.1) составлен; наличие перечня не означает, что весь расширенный wishlist уже измеряется. См. `observer/catalog.py`.
Для всех production-сценариев требуются установка, реальные данные и отдельная приёмка. Поддержка fencing interface не означает готовый аппаратный адаптер; SQL HA listener/VIP/failure domains остаются инфраструктурой.

## Свидетельства

- 1.2: [guardian/adapter_checks.py](../../guardian/adapter_checks.py)
- 1.3: [deploy/ha/ACCEPTANCE_2026-10-07.md](../../deploy/ha/ACCEPTANCE_2026-10-07.md)
- 1.4: [deploy/ha/evidence/reconciliation_export_2026-10-07.json](../../deploy/ha/evidence/reconciliation_export_2026-10-07.json)
- 2.1: [guardian/simulation.py](../../guardian/simulation.py)
- 2.2: [guardian/hardware_fence.py](../../guardian/hardware_fence.py)
- 2.3: [guardian/hardware_fence.py](../../guardian/hardware_fence.py), [tests/test_ha_qualification.py](../../tests/test_ha_qualification.py)
- 2.4: [guardian/recovery.py](../../guardian/recovery.py), [guardian/telemetry.py](../../guardian/telemetry.py)
- 3.1: [gateway/server.py](../../gateway/server.py), [deploy/ha/install_resilience.py](../../deploy/ha/install_resilience.py)
- 3.2: [tests/test_ha_resilience.py](../../tests/test_ha_resilience.py), [tests/test_gateway_fallback.py](../../tests/test_gateway_fallback.py)
- 4.1: [guardian/qualification.py](../../guardian/qualification.py), [tests/test_ha_qualification.py](../../tests/test_ha_qualification.py)
- 4.2: [guardian/qualification.py](../../guardian/qualification.py), [guardian/release_contract.py](../../guardian/release_contract.py)
- 4.3: [guardian/adapter_checks.py](../../guardian/adapter_checks.py), [tests/test_ha_release_contract.py](../../tests/test_ha_release_contract.py)
- 4.4: [guardian/golden_traces.json](../../guardian/golden_traces.json), [tools/build_recorded_traces.py](../../tools/build_recorded_traces.py)
- 4.5: [guardian/replay.py](../../guardian/replay.py), [guardian/qualification.py](../../guardian/qualification.py)
- 4.6: [guardian/simulation.py](../../guardian/simulation.py), [tests/test_ha_qualification.py](../../tests/test_ha_qualification.py)
- 4.7: [guardian/update.py](../../guardian/update.py), [deploy/ha/start_staged_rollout.py](../../deploy/ha/start_staged_rollout.py)
- 4.8: [guardian/update.py](../../guardian/update.py), [guardian/probation.py](../../guardian/probation.py)
- 4.9: [guardian/update.py](../../guardian/update.py), [tests/test_ha_release_contract.py](../../tests/test_ha_release_contract.py)
- 4.10: [guardian/qualification.py](../../guardian/qualification.py), [guardian/update.py](../../guardian/update.py)
- 5.1: [observer/catalog.py](../../observer/catalog.py), [observer/collector.py](../../observer/collector.py)
- 5.2: [observer/behavior.py](../../observer/behavior.py)
- 5.3: [observer/behavior.py](../../observer/behavior.py)
- 5.4: [observer/behavior.py](../../observer/behavior.py), [tests/test_ha_resilience.py](../../tests/test_ha_resilience.py)
- 5.5: [observer/behavior.py](../../observer/behavior.py)
- 5.6: [observer/behavior.py](../../observer/behavior.py)
- 5.7: [deploy/ha/finish_behavior_monitoring.py](../../deploy/ha/finish_behavior_monitoring.py), [deploy/ha/update_visual_wallboard.py](../../deploy/ha/update_visual_wallboard.py)
- 5.8: [observer/service.py](../../observer/service.py), [observer/notifications.py](../../observer/notifications.py)
- 6.1: [deploy/ha/finish_monitoring.py](../../deploy/ha/finish_monitoring.py), [deploy/ha/zabbix-items.md](../../deploy/ha/zabbix-items.md)
- 6.2: [deploy/ha/install_monitoring.py](../../deploy/ha/install_monitoring.py), [deploy/ha/grafana_visual.py](../../deploy/ha/grafana_visual.py)
- 6.3: [guardian/repair.py](../../guardian/repair.py)
- 6.4: [guardian/simulation.py](../../guardian/simulation.py), [guardian/repair.py](../../guardian/repair.py), [tests/test_ha_repair.py](../../tests/test_ha_repair.py)
- 6.5: [guardian/repair.py](../../guardian/repair.py), [tests/test_ha_repair.py](../../tests/test_ha_repair.py)
- A1: [KPP/kpp_aggregator_v3_warehouse.py](../../KPP/kpp_aggregator_v3_warehouse.py), [common/retrospective.py](../../common/retrospective.py)
- A2: [common/adaptive_windows.py](../../common/adaptive_windows.py), [deploy/ha/tune_adaptive_windows.py](../../deploy/ha/tune_adaptive_windows.py)
- A3: [gateway/server.py](../../gateway/server.py)
- A4: [common/fallback_store.py](../../common/fallback_store.py), [common/metadata_mirror.py](../../common/metadata_mirror.py), [observer/mirror.py](../../observer/mirror.py)
- A5: [common/replicated_ingest.py](../../common/replicated_ingest.py)
- A6: [deploy/ha/grafana_visual.py](../../deploy/ha/grafana_visual.py), [deploy/ha/perimeter_topology.js](../../deploy/ha/perimeter_topology.js)
- A7: [deploy/ha/grafana/playlist.json](../../deploy/ha/grafana/playlist.json)
- A8: [deploy/ha/grafana_visual.py](../../deploy/ha/grafana_visual.py)
- A9: [deploy/ha/CONTINUE_HERE_2026-10-08.md](../../deploy/ha/CONTINUE_HERE_2026-10-08.md), [deploy/ha/PLAN_AUDIT_2026-10-08.json](../../deploy/ha/PLAN_AUDIT_2026-10-08.json)

Машиночитаемый источник: [PLAN_AUDIT_2026-10-08.json](PLAN_AUDIT_2026-10-08.json).
Продолжение/точный head/CI: [PR #9](https://github.com/Mika-dot/RFID_KPP/pull/9) и [handoff](CONTINUE_HERE_2026-10-08.md).
