# Продолжать отсюда — RFID_KPP, 08.10.2026

**Следующий этап 09.10:** [единая инструкция установки на четыре машины](READY_TO_INSTALL_2026-10-09.md).
Подключены 42 из 43 wishlist instruments; physical business RTO остаётся
неизмеренным. Старые counts wishlist ниже относятся к предыдущей итерации.

**Актуализация 09.10:** сначала прочитать [AUDIT_AND_CONTINUE_2026-10-09.md](AUDIT_AND_CONTINUE_2026-10-09.md).
После прежней проверки обнаружены и исправлены 22 разных review-дефекта из
PR #5/#7–9. Старые head этих PR нельзя считать заменой итогового исправленного PR #9.
Точный новый SHA/CI находится в его описании; установленным runtime он не объявлен.

Сначала читать этот handoff, затем `ADAPTIVE_CORRELATION_AND_STORAGE_2026-10-08.md`
и прежний `RESILIENCE_CANDIDATE_2026-10-07.md`. Не начинать заново по старому чату.

## Повторная сверка

Сначала сверить [машиночитаемый audit](PLAN_AUDIT_2026-10-08.json) и
[таблицу всех пунктов](PLAN_AUDIT_2026-10-08.md). Исходные 33 checkbox-пункта:
20 SOFTWARE_READY, 4 PARTIAL, 8 BLOCKED, 1 DEFERRED. Это готовность ПО,
не заводская приёмка. Девять уточнений пользователя: 8 готовы программно,
1 частично. Раздел 8 исходного плана повторяет критерии и не увеличивает счётчик.

В этой итерации доделаны шесть потоков локального зеркала, проверка полного
backfill/свежести, выбор исправной копии, наблюдаемые repair/update/RTO в Grafana,
восстановление retrospective session candidates и recorded golden regression.
Observer `/catalog` различает observed/unavailable/not_instrumented:
73 определения инструментированных метрик, ещё 43 позиции wishlist не
инструментированы. Не считать их измеренными или закрывать весь wishlist.

## GitHub и исходный план

- Репозиторий: https://github.com/Mika-dot/RFID_KPP
- Main на момент проверки: `76778494851d03c5c3c5650b40e54986ddcfddd1`.
- PR #8 https://github.com/Mika-dot/RFID_KPP/pull/8 открыт, mergeable, не merged,
  head `1d59061d0f29e3eba8f877ed03cd63d347300d3f`. Пять review threads закрыты;
  три workflow, включая Linux/Windows HA, success. Код уже был залит в GitHub;
  наличие ветки не означает merge/main или установку.
- Новый кандидат `feature/adaptive-correlation-fallback-2026-10-08` поверх PR #8.
  Проверенный head PR #8 не менять незаметно.
- PR #9 https://github.com/Mika-dot/RFID_KPP/pull/9 — текущий handoff и код;
  точный последний head и CI записаны в его описании. База повторной сверки
  `4e005d769495f01f2e665133695827d33bf0fac5` уже прошла HA Linux/Windows,
  Warehouse и Observability CI. Не переносить её success на новый SHA без проверки.
- Исходный план draft PR #4 прочитан полностью:
  https://github.com/Mika-dot/RFID_KPP/blob/4735b48aa2e7b9b30d26732ac28e64a4337a6376/deploy/ha/NEXT_ROUND_PLAN_2026-10-06.md
  Его production Definition of Done ещё не выполнен.

## Соответствие плану

| Пункт | Состояние | Остаётся |
|---|---|---|
| HA трёх узлов, пять workers, SQL epoch | Старый принятый runtime описан планом; кандидат сохраняет контракт | Новую ветку установить/проверить отдельно |
| Настоящая RFID business acceptance | NOT PROVEN | Реальная метка, identity/direction/video/СКУД/склад |
| Hard failure / UHF takeover | Fence/rollback ПО в PR #8 | Настоящий fence adapter, физическая приёмка/RTO |
| Постоянный Web | Gateway PR #8 + authenticated local cache здесь | Установка, адрес/DNS/VIP, отказ самого Comparator |
| Safe Git auto-update | Classifier/installed qualifier/golden/differential/model/probation/quarantine/rollback в PR #8 | Production staging и приёмка на реальных трассах |
| Недоставленные входы | Durable quorum/UUID в PR #8 | Failure domains и предел RPO до ACK |
| Behavior Observer | Read-only residual/drift/forecast/history ПО в PR #8 | Установка, 1–2 недели заводской baseline history |
| Grafana/Zabbix HA/business | Существующий мониторинг; новая visual page/installer подготовлены | Живая Grafana недоступна; apply на ub22 |
| LLM repair acceptance | Ограниченный path сохранён | Настоящий faulted reserve и LM Studio offline |
| Два окна КПП → склад | Shadow/retrospective search/accepted active profile реализованы | Реальная разметка/calibration/holdout/precision-recall |
| ≥3 месяца локальных копий | Metadata journal и SELECT-only mirror, минимум 93 дня | Установка/заполнение узлов; полный SQL backup/recovery отдельно |
| Полный обвал общей БД | PENDING сохраняется; fencing остаётся fail-closed | SQL HA authority/listener для непрерывного захвата |
| Смена двух экранов 1m, убрать надпись | Playlist/kiosk/top text banner removal подготовлены | Live apply и проверка фактической надписи |
| Сохранение прогресса GitHub | Код и этот handoff в новой ветке | Обновлять SHA/CI/installed status по каждому этапу |

## Ограничения

Разрешены разработка, offline-проверки и публикация GitHub. Физические проходы,
power/network failures, production SQL-записи и установка бизнес-runtime не
выполнялись; требуется отдельный согласованный этап. Пользователь отдельно
авторизовал обновление существующей Grafana, но TCP к `172.31.0.97:3000` здесь
недоступен и callable Grafana/SSH нет. Подготовленные файлы не называть live.

Не разрешать offline лидерам писать без SQL lease. Не превращать Warehouse/raw
кандидаты в доказанный RFID-проход. Не обучать на собственных adaptive links.
Не переиспользовать IP physical без network fencing. Не называть зеркало SQL backup.

## Продолжение

1. Проверить head/CI/review новой ветки и PR #8.
2. На ub22 выполнить `update_visual_wallboard.py --plan`, затем
   `--apply --release <exact SHA>` из чистого checkout. Проверить receipt,
   backend frames, обе страницы и фактический banner. Display открыть на playlist.
3. Runtime выкатывать отдельным staged-процессом; начать с local copies и shadow.
   Сохранить существующие paths spools/секретов.
4. Дождаться backfill всех шести потоков (`events/warehouse/tasks/rfid/video/skud`),
   проверить архивы/возраст/очереди всех узлов, gateway и честный offline cache.
   Выполнить GET-only `verify_resilience_installation.py --nodes-config <nodes.json>
   --release <exact SHA> --require-local-copies --output <receipt.json>`.
   Без флага local copies явно остаются `not_requested`, а не подтверждаются.
5. Собрать confirmed/rejected пары; принять профиль по chronological holdout.
6. Закрыть SQL HA, независимый Web/VIP, реальный hardware fencing.
7. Физическую приёмку и RTO/RPO — по отдельному согласованному этапу.
8. Результаты/SHA/CI/installed-not-installed записывать в GitHub.

## Последняя проверка

Полный локальный набор: 549 тестов, ошибок нет, 15 skip (настоящий Windows/systemd,
SQL integration и PID namespace). Qualification против PR #8 прошла: 6 adapter contracts, 27 golden
traces, 8 HA model scenarios, 0 shadow differences. Compileall/diff-check прошли;
синтетические SVG визуально проверены. Итог SHA/CI — в описании нового PR.
Production-статус из таблицы от CI не меняется.

27 traces = 15 моделей + 12 обезличенных CSV-фрагментов 06–07.10.2026,
328 read occurrences / 203 distinct source reads. `tools/build_recorded_traces.py`
сохраняет источник/хеши/границы и связи TAG→IDS→SERIES без ФИО/card fields.
Expected получены из чистого pinned PR #8, а не физической разметки. Recorded
video отсутствует. Не называть корпус полной business acceptance и не заменять
им длительный shadow или 1–2 недели реальной baseline.

Новые entrypoints: `deploy/ha/update_visual_wallboard.py`,
`deploy/ha/tune_adaptive_windows.py`, `observer/mirror.py`,
`common/adaptive_windows.py`, `common/fallback_store.py`, `common/metadata_mirror.py`.
Установщик Grafana требует весь checkout, не standalone-файл.
Также добавлены `common/retrospective.py`, `observer/catalog.py`,
`tools/build_recorded_traces.py`, `tools/render_plan_audit.py`.
