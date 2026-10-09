# Периметр: установка на четыре машины, 09.10.2026

Итоговый кандидат — `feature/adaptive-correlation-fallback-2026-10-08`,
[PR #9](https://github.com/Mika-dot/RFID_KPP/pull/9). Точный опубликованный SHA
и Actions указаны в PR. Зафиксировать этот SHA; не подставлять старый main,
старые head #7/#8 или автоматически меняющийся head ветки.

Код и инструменты подготовлены offline. Установка на завод, физические
проходы, отключения и изменения производственного SQL здесь не выполнялись.
Старые factory receipts описывают прежнее состояние. Аудит веток и 22
предыдущих исправлений: [AUDIT_AND_CONTINUE_2026-10-09.md](AUDIT_AND_CONTINUE_2026-10-09.md).

## Закончено после audit

| Работа | Результат | Проверка при установке |
|---|---|---|
| Метрики поведения | Подключены 42 из 43 wishlist: SQL aggregates и runtime telemetry | `/catalog`: реальные observed/unavailable; physical business RTO не подставляется |
| Отдельная task DB | Event/task источники независимы и SELECT-only | Отказ task DB не скрывает RFID/Video/HA и не переключает task-запросы в чужую DB |
| Обновление узлов | Exact SHA prepare/apply, native supervisor, rollback и crash recovery | Installed oracle, tests, owner/epoch, preflight, probation |
| Сохранение данных | Spool/lock/cursor/UUID сохраняются | Относительные spool/lock закрепляются в actual installed root |
| Web на ub22 | Маршрутизация по двум свежим authenticated SQL lease witnesses | Deadline lease уменьшается по monotonic time; собственной election нет |
| Отказ Comparator | Gateway и Behavior можно разместить на отдельном ub22 | Адрес ub22:5051, readiness, authenticated metadata fallback |
| Hardware adapter | IF-MIB / Net-SNMP v3 authPriv, admin/oper readback, epoch до/после | Нужны реальные отдельные reader-access interfaces |
| Dashboard | Локальный gateway health, Behavior, визуальная схема, две страницы, playlist 1m | Existing Grafana objects, backend frames, receipt и экран |
| Общий финал | GET-only сверка трёх Guardian + Gateway + Observer | Exact SHA, пять workers, один owner, два резерва, шесть caught-up зеркал |
| Safe auto-update | Live preflight failure откладывает candidate с повторной попыткой того же SHA | После восстановления SQL/network/resources SHA не остаётся permanently rejected; code defects по-прежнему quarantined |

## Общие условия

Helper запускать из **отдельного clean checkout кандидата**, закреплённого
на SHA из PR. Не делать `git pull` в работающий runtime. Существующие
`node.json`, `release.json`, private environment, spool и stable launcher
сохраняются. Рабочий candidate создаётся отдельным worktree существующего
`update_source`. Нужен уже установленный HA protocol 2; для первой установки
остаются прежние initial-install tools.

SQL Server должен поддерживать `DATEDIFF_BIG` (2016+), используемый для
остатка lease; источник: [Microsoft Learn](https://learn.microsoft.com/en-us/sql/t-sql/functions/datediff-big-transact-sql).

`--plan` ничего не меняет; `--prepare` создаёт candidate/qualification,
не останавливая services; `--apply` штатно меняет runtime и роль. Если paths
отличаются, указать фактические `--config` и `--environment`.
Нельзя обходить qualifier, если actual installed oracle отверг candidate:
offline comparison с предыдущим PR head не заменяет этот gate.

Event mirror/Observer требуют локально provisioned `PERIMETER_OBSERVER_SQL`.
Для Warehouse/RfidTags в другой DB нужен `PERIMETER_OBSERVER_TASK_SQL`
либо existing `KPP_TASK_CONN_STR`; самостоятельному Observer предпочтителен
отдельный SELECT-only login. Connections и recipients сохраняются private,
в commands/receipts их значений нет.

## Comparator, затем Perimetr

На каждой Linux VM из checkout кандидата:

```bash
PERIMETER_RELEASE=$(git rev-parse HEAD)
sudo /opt/perimeter/venv/bin/python deploy/ha/release_update.py --plan --release "$PERIMETER_RELEASE"
sudo /opt/perimeter/venv/bin/python deploy/ha/release_update.py --prepare --release "$PERIMETER_RELEASE"
sudo /opt/perimeter/venv/bin/python deploy/ha/release_update.py --apply --release "$PERIMETER_RELEASE"
```

Defaults: `/etc/perimeter/node.json`, `/etc/perimeter/environment`.
Каждый prepare выполнять непосредственно перед своим apply: изменение
config/environment/release record делает прежний receipt недействительным.
Обычный apply разрешён только пассивному узлу. Если VM сейчас ведущая,
сначала обновить доступный пассивный резерв; для active apply требуется
`--handoff` и другой prepared Linux-резерв на exact target SHA. При отсутствии
такого резерва gate не обходить: порядок зависит от actual owner.

Passive `installed=true` может сопровождаться `update.pending=true`: это
готовый резерв, а не завершённая бизнес-проба. При переходе роли выполняются
ordinary Guardian checks и 180-second probation. Установщик не подтверждает
релиз и не снимает quarantine прямым SQL-запросом.

## Физика / Windows

Административный PowerShell, отдельный clean checkout кандидата:

```powershell
$PerimeterRelease = (git rev-parse HEAD).Trim()
$PerimeterNode = Get-Content D:\PerimeterHA\node.json -Raw -Encoding UTF8 | ConvertFrom-Json
& $PerimeterNode.python deploy\ha\release_update.py --plan --release $PerimeterRelease
if ($LASTEXITCODE -ne 0) { throw 'Plan failed' }
& $PerimeterNode.python deploy\ha\release_update.py --prepare --release $PerimeterRelease
if ($LASTEXITCODE -ne 0) { throw 'Prepare failed' }
& $PerimeterNode.python deploy\ha\release_update.py --apply --handoff --release $PerimeterRelease
if ($LASTEXITCODE -ne 0) { throw 'Apply failed: inspect receipt/journal' }
```

Defaults: `D:\PerimeterHA\node.json` и
`D:\PerimeterHA\transfer-private\environment.local.json`.
Оба Linux-резерва перед изменением физики должны быть prepared на target SHA.
Installer останавливает Scheduled Task `PerimeterGuardian`, получает
`guardian.lock`, убирает только registered owned children, ждёт одного
healthy ведущего на target SHA и окончания его probation. При неуспехе
возвращает прежние config, release и maintenance.

При прерывании остаётся private `<state_dir>/operator-release-install.json`.
Для возврата по нему: `<node-python> deploy/ha/release_update.py --recover
--release <SHA прерванной установки>`. Не удалять журнал вручную. Recovery
проверяет node/config/hash/current SHA; при неуспешном возврате журнал
сохраняется. Старый runtime после восстановления проходит обычные HA guards.

## ub22: Web и Behavior

Нужны clean checkout того же SHA и Python venv: stdlib для gateway, `pyodbc`
и системный ODBC Driver для Observer. Guardian, Wine/SDK и business workers
на ub22 не ставятся.

Configs: `gateway.ub22.example.json` → `/etc/perimeter/gateway.json`,
`behavior.example.json` → `/etc/perimeter/behavior.json`; сверить actual node
URLs. Публичный адрес — LAN IP/DNS **ub22** на 5051. Можно задать им
`public_url`. Configs без credentials должны читаться служебным пользователем;
private `/etc/perimeter-resilience.env` — root:root 0600.

Environment: existing `PERIMETER_HA_TOKEN`, те же `KPP_WEB_AUTH_USER` /
`KPP_WEB_AUTH_PASSWORD`, что у backend; для Observer — SELECT-only SQL
connections выше. Gateway `lease_source=guardians` не требует SQL-control
login. Existing notification endpoint сохраняется; новые адресаты не создаются.

```bash
PERIMETER_RELEASE=$(git rev-parse HEAD)
sudo /opt/perimeter/venv/bin/python deploy/ha/install_resilience.py --plan --root "$PWD" --python /opt/perimeter/venv/bin/python --gateway-config /etc/perimeter/gateway.json --behavior-config /etc/perimeter/behavior.json --env-file /etc/perimeter-resilience.env --release "$PERIMETER_RELEASE"
sudo /opt/perimeter/venv/bin/python deploy/ha/install_resilience.py --install --root "$PWD" --python /opt/perimeter/venv/bin/python --gateway-config /etc/perimeter/gateway.json --behavior-config /etc/perimeter/behavior.json --env-file /etc/perimeter-resilience.env --release "$PERIMETER_RELEASE"
```

Заменить пути venv/checkout, если на ub22 они другие. Installer проверяет
clean exact HEAD, сохраняет units и возвращает их при неготовности новых
services. Unit фиксирует SHA, HTTP публикует его. Новый monitor проверяет
gateway локально `127.0.0.1:5051`. При потере SQL lease/quorum live routing
отключается; остаётся authenticated cache с явным stale/caught_up.

## ub22: dashboard и финальная сверка

С прежним локально provisioned Grafana credential:

```bash
sudo python3 deploy/ha/update_visual_wallboard.py --plan
sudo python3 deploy/ha/update_visual_wallboard.py --apply --release "$PERIMETER_RELEASE"
sudo python3 deploy/ha/finish_behavior_monitoring.py --plan
sudo python3 deploy/ha/finish_behavior_monitoring.py --apply
sudo /opt/perimeter/venv/bin/python deploy/ha/verify_resilience_installation.py --nodes-config /etc/perimeter/gateway.json --release "$PERIMETER_RELEASE" --require-local-copies --gateway-url http://127.0.0.1:5051 --observer-url http://127.0.0.1:19153 --token-file /etc/perimeter-ha-monitor.token --output /var/lib/perimeter-ha-monitor/installation-receipt.json
```

Первый apply обновляет collector, визуальную страницу и playlist. Второй
добавляет managed Behavior panels к existing `mositlab-director-wallboard`,
сохраняя folder. Если banner не top text panel, указать фактический
`--banner-panel-id` после plan. Открыть
`/playlists/play/mositlab-perimeter-wallboard?kiosk`, осмотреть обе страницы;
actual URL/UID и backup находятся в JSON receipt. Backend frames не заменяют
осмотр экрана. Чужие concurrent edits не перезаписываются rollback.

Финальный verifier требует caught-up шесть потоков на всех трёх узлах,
retention >=93 дней, пять healthy workers одного owner, общий epoch и SHA
Guardian/Gateway/Observer. Большой backfill может занять больше установки:
до его завершения gate не зелёный. Passive `update_pending` сохраняется в
receipt, пока резерв не исполнит роль и не пройдёт свою бизнес-пробу.

## Что нельзя закончить одним upload

| Условие | Реальный остаток |
|---|---|
| Switch/relay ports | Adapter готов; нужны actual exclusive IF-MIB mappings и private authPriv credentials. Template намеренно невалиден до заполнения; общий uplink/trunk запрещён; mandatory hardware fence автоматически не включается |
| Полный отказ SQL authority | PENDING/replay/fallback реализованы. Для непрерывного capture нужен реально развёрнутый SQL HA listener/control storage и согласованные destinations; env placeholder его не создаёт |
| Factory Golden/Shadow | 27 traces и protected qualifier есть; нужны настоящий recorded video/история и длительное production shadow |
| Observer/adaptive baseline | После установки начинается 7–14 дней наблюдения и разметка confirmed/rejected пар; active требует profile v2/holdout |
| Physical RTO/RPO / business acceptance | Не измерены: реальные проходы и отключения исключены условиями этой итерации |
| Zabbix | Прежний deferred setup отдельно; existing items/recipients не менялись |

Исходные 33 статуса сохраняются: **20 SOFTWARE_READY / 4 PARTIAL / 8 BLOCKED /
1 DEFERRED**. Новые tools не закрывают физические/временные критерии. Main
не merged: его updater следит за main. Candidate ставится exact SHA с
`auto_update=false`; дальнейший release/merge — после принятого rollout.

## Границы метрик

SQL — последние пять минут server local time, max 20 000 rows/stream
(настраиваемый limit 1..50 000). Counts отдельно; при truncation ratios и
распределения из неполной выборки не выдаются. Missing source/column =
unavailable. Output — числа, fixed buckets/ranks; EPC, TID, карточки, ФИО,
имена устройств и фото в telemetry output не публикуются.

Durations — seconds; RSSI — исходная SQL шкала; rates — /second, tracks —
/minute. RFID gaps идут по source Id, отрицательные часы исключаются.
Processing latency = `CompletedAt-LastSeen`, без позднего warehouse recheck.
Warehouse ambiguity — recent empty-Tag rows с >1 distinct task Tag в series
time window; это диагноз данных, не изменение matcher. Runtime window —
300 sec, max 12 000 samples/name, 128 names; overflow делает метрику
unavailable. Worker restarts — launches после первого за жизнь Guardian,
reset не означает repair. Readiness RTO отдельно от physical business RTO.
