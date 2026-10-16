# Кандидат следующего этапа отказоустойчивости

Дата: 07.10.2026. Ветка `feature/ha-resilience-2026-10-07`, поверх PR7
(`d57c911ced3e8b94672eb05672b6fc26fdcab503`). **Статус: программная проверка;
на оборудование ещё не установлен.** Main не менялся. Последнее заводское
подтверждение остаётся 06.10 18:54 МСК, runtime `1c793091…`, epoch264.

Физические проходы, отключения узлов, synthetic RFID/1C/SQL записи, migrations,
grants и изменения заводского latch в этом раунде не выполнялись.

## Реализованные программные части

| Компонент | Поведение |
|---|---|
| Репликация входа RFID/YOLO | SQLite WAL/FULL journal; штатный SQL writer требует локальную и одну удалённую durable копию. UUID, исходное время, EPC+TID, sequence, число катушек и image bytes сохраняются. Новый owner получает pending копии и доставляет их обычными fenced writers. |
| Повторы и сбои репликации | Проверка SHA256/UUID, отказ при конфликте содержимого, ограниченные HTTP bodies, no redirects, cooldown недоступного peer. Потерянный ACK допускает повтор без новой бизнес-записи. SENT устанавливается после SQL commit; retention не удаляет PENDING. |
| Единый Web | `gateway.server` на отдельном сервере, пример port5051. Каждый запрос требует действующей SQL lease, соответствующих owner/epoch/protocol2 и свежей readiness. Смена epoch во время ответа даёт503. Basic auth, снимки и ответы Web передаются штатному backend. |
| Квалификация обновления | Семь классов изменений; AST/entrypoints/protocol; защищённые12 бизнес-проверок;6 проверок штатных SQL adapters;15 golden моделей; differential replay принятого runtime и кандидата;8 сценариев HA; candidate regression suite; preflight. Проверяющий код и corpus берутся из установленного runtime. |
| Защита destinations | Изменение бизнес-таблиц, SQL connection expressions/env defaults требует отдельного операторского этапа. Schema/dependencies/model/DLL не раскатываются автоматически. Дочерние проверки не получают производственные credentials/destinations. |
| Probation/rollback | Подтверждение кандидата после минимум180сек готовности и свежих метрик обеих очередей. Застой delivery120сек или отсутствие бизнес-метрик240сек запрещает подтверждение, запускает штатный unhealthy/demotion и последующий rollback/quarantine. |
| Behavior Observer | Отдельный read-only процесс: SQL aggregate counts, RFID/антенны/RSSI, видео, СКУД, Warehouse/1C, cursor/backlogs, HA/update/repair. Median/MAD, EWMA, CUSUM, устойчивость отклонения, residual/divergence, тенденция и линейный прогноз15мин/1ч/8ч. |
| История и cold start | SQLite history сохраняется между restart. Требуется минимум7 суток истории, не только много частых выборок. До этого `collecting_baseline`; отсутствующие источники не превращаются в нули. Гипотезы причины не объявляются диагнозом и не переключают HA. |
| Уведомления/панели | Durable outbox, стабильный event id, retries, dedup incident/recovery,3 последовательных плохих выборки перед уведомлением. URL существующего webhook задаёт оператор. Три managed Grafana панели добавляются в существующий dashboard без дублирования HA panels/datasource. |
| Измерение восстановления | Журнал фаз detection/fence/grant/all-ready и observed readiness RTO. Это время готовности служб, не доказательство физического чтения RFID. |
| Внешний hardware fence | Опциональный интерфейс фиксированных operator argv с verified receipt и durable SQL pending ledger. Неудачный fence блокирует новый owner, включая restart контроллера. Требует настоящего адаптера изоляции/возврата доступа и отдельной migration005; по умолчанию выключен. |

## Проверка без оборудования

Локально Python3.12: **498 tests:483 passed,15 skipped**. Пропуски требуют
SQL integration environment либо другой ОС. AST/compileall, protected checker,
golden/differential qualification, HA model и `git diff --check` прошли.
GitHub CI дополнительно запускает suite и qualifier на Ubuntu/Windows Python3.11.
Окончательный статус CI проверяется для опубликованного exact SHA.
Отдельный запуск candidate против исходного PR7 `d57c911…` прошёл:
15 моделей, ноль shadow differences; destinations сохранены.

Golden corpus — **15 детерминированных моделей**, а не выдуманная live-приёмка:
IN/OUT, группа катушек, missing video/SKUD, unknown RFID, одинаковый EPC с разным
TID, duplicate reads, late/reordered data, duration boundary, nullable Warehouse
tag, warehouse-only и конфликт источников. Candidate/stable читают одни inputs
в отдельных процессах; ODBC заменён recording adapter. Дополнительно проверены
настоящие loopback HTTP маршруты proxy/replica, потеря исходной journal-копии,
недоступный peer, lost ACK, persistent drift, alerts и installation tools.

Повторяемые команды из checkout:

```bash
python -m unittest discover -s tests -v
python -m compileall -q guardian deploy common gateway observer RFID_reader_v4 RTSP
python guardian/release_contract.py .
python guardian/qualification.py --candidate . --stable . --report qualification.json
python guardian/simulation.py --root . --output ha-model.json
```

`--traces` допускает дополнительные записанные inputs. Изменённый ожидаемый
результат требует versioned golden result; shadow allowlist разрешает только
точное сочетание trace/field/old/new. Нельзя заменять installed corpus файлом
из автоматически проверяемого candidate.

## Следующий этап: установка с пользователем

1. Закрепить опубликованный exact SHA. Сохранить текущие release/config/queues.
   `prepare_resilience_config.py` создаёт **новый** JSON, требует действующих
   абсолютных spool paths и сохраняет бизнес-настройки. Не переносить пустую
   очередь вместо прежней. В существующей конфигурации replication отключена
   до её явного включения на **всех трёх** новых agents.
2. Установить одинаковый candidate на три узла через прежний безопасный rollout
   с независимой проверкой/возвратом. Не делать принудительный grant/reset latch.
   Наличие ветки/PR само по себе ничего на заводе не обновляет.
3. На ub22 подготовить exact checkout/venv, JSON gateway/behavior и private0600
   environment с прежним cluster token, правильным SQL destination и выделенным
   read-only observer account. `install_resilience.py --plan` ничего не меняет;
   `--install` устанавливает новые systemd units, проверяет readiness и восстанавливает
   прежние units при неудаче. Config ports должны соответствовать выбранному адресу.
4. Обновить установленный wallboard adapter его pinned `install_monitoring.py`,
   сохранив backup и существующие unit/env/routes. Применить только новые managed
   панели через `finish_behavior_monitoring.py --apply`. Zabbix setup не возобновлять
   без отдельного решения; ранее он был пропущен пользователем.
5. `verify_resilience_installation.py` делает только GET: exact releases, общий
   epoch, один healthy executor/пять services, два prepared/nonfaulted reserves,
   replica endpoints, gateway и observer. Он не создаёт проход и не вызывает
   переключение. Зафиксировать свежие результаты и pending adoption/probation.
6. После проверки установленной версии привести ветки/PR/main в порядок и
   обновить итоговую `.md` приёмки с фактическими данными оборудования.

## Границы гарантий

Кворум защищает **уже подтверждённую** запись при потере одной из её двух
независимых копий. До удалённого ACK запись остаётся локальной; нулевой RPO для
всех физически наблюдённых чтений не заявляется. При недоступности обоих peers
новые записи остаются PENDING. Общий гипервизор/питание требует независимых
failure domains; две VM на одном хосте этого не обеспечивают.

Репликация SQL Server, backup/restore инфраструктуры, дублирование считывателя/
камер и резервирование самого сервера gateway не разворачиваются одним Python
commit. ODBC может использовать подготовленный SQL HA listener; его создание —
отдельная инфраструктурная установка. При отсутствии SQL программа прекращает
исполнение/запись безопасно; непрерывную доступность общего SQL это не создаёт.

Модель TCP/power/LLM не заменяет прошивку считывателя или внешнее fence-устройство.
Реальный hardware adapter не подключён. Физические испытания по условиям работы
не требуются; результат этого раунда называется программной проверкой кандидата.
Предыдущие архивные расхождения/5 NeedRecheck не исправляются искусственным
переписыванием истории. См. [RECONCILIATION_CONTINUATION_2026-10-07.md](RECONCILIATION_CONTINUATION_2026-10-07.md).
