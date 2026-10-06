# Подключение внешнего мониторинга HA, 06.10.2026

## Актуальная точка продолжения: все три hotfix установлены

Последний вывод пользователя подтверждает HOTFIX_INSTALLED/RUNTIME_STAGED на
physical, Comparator и Perimetr для **1c7930912ad84e8f205cf16fbdd715c76c9eb74e**.
Все candidate tests прошли. Последний Perimetr --resume-after-stage остановился:
`All three exact hotfix agents required before resume: perimetr`.
Это остановка ДО release maintenance: текущая полная работоспособность не
подтверждена. Повторное staging/установка новой production версии не требуется.

В operator script исправлено ожидание асинхронного preflight: прежний wait_staged
принимал свежий Guardian со starting=false, а resume сразу требовал preflight.ok.
Это найденная гонка в коде, вероятное объяснение отказа; конкретный заводской
failed check в старом сообщении не напечатан. Теперь до180сек SELECT/GET-only
ожидание всех3 в одной итерации, точный release/protocol/identity/maintenance type,
свежий sample и preflight обязательны. Held inactive/workers={} и resource guard
сохранены. Несовпадение release, auth refusal, active held worker, HA off или
resource exhaustion не обходятся. При timeout maintenance этим запуском не
снимается; выводятся failed_checks без значений/секретов. Нет reset/forced owner,
SQL migration, synthetic read или изменения business latch.

**Следующий шаг на физике:** скачать новую operator-копию
repair_activation_runtime.py, проверить SHA256
`762cdb756550dcae6f4aa17112b0fe5781d3b1a88c7fc17e0d2b75c17386061b`, запустить
`--node physical --release 1c7930912ad84e8f205cf16fbdd715c76c9eb74e --resume-protocol2`.
SHA скрипта и target runtime release различаются намеренно: обновляется только
операторский файл, три уже установленных checkout не переставляются.
Нужны HA_ACTIVE_PHYSICAL_RESERVES_READY и HOTFIX_PROTOCOL2_RUNTIME_RESTORED.
Полный wait_cluster имеет прежний timeout600сек/7 stable samples; это восстановление
работы, не очередное fault-injection испытание. Бизнес-приёмка реальным проходом
отдельно не доказана. Main не двигать до текущего factory результата.

**Grafana:** приложенный screenshot содержит f5/epoch258 (старый runtime), таблица
узлов заполнена, обе string Stat карточки показывают НЕТ ДАННЫХ. В новом
finish_monitoring.py после генерации именно managed HA Stat задан выбор всех
полей (старый default был numeric-only), включая leader/business strings.
На ub22 скачать только новую копию finish_monitoring.py, SHA256
`9b9fef3d9a0010b39cf189b6baa79c168451455fcae301a02ab3fb404ef625e3`, запустить
только `--finish-grafana`. Использует уже установленный observer, сохраняет backup,
проверяет реальные4 backend queries. Никаких --configure-zabbix/--repair-passive,
повторного installer, передачи ключа или изменений прочих панелей.
Zabbix **пропущен по прямому указанию пользователя** и запуск HA не блокирует.

Проверки:407 local regression tests OK/skipped15 после operator wait fix,
26 runtime repair/epoch tests OK,35 monitoring tests OK после panel correction,
CLI/help/diff --check OK. Production runtime bytes не менялись.

Далее — исторические записи; READY237/снимки f5 не текущая готовность.


**Обновление18:23: Zabbix по прямому указанию пользователя ПРОПУЩЕН.
External observer и4 Grafana panels работают независимо. READY237 больше не
актуально: в18:20–18:23 unhealthy takeover239→241→243. Root cause — old temporal
RFID evidence/latch logic, не Grafana/Zabbix/TCP/SQL. Нужен production fix и
штатное обновление всех3; см. [RFID_EVIDENCE_FIX_2026-10-06.md](RFID_EVIDENCE_FIX_2026-10-06.md).
Мониторинг не переустанавливать, к Zabbix login не возвращаться как условию работы.**

## Подтверждённый результат, вывод получен 18:16 МСК

Пользователь запустил finish_monitoring.py из906102659389b5202b65ecec4bb3e54d186a6270,
checksum aeb386b950a5636ac7f22280c677060b2df361a6a04dfc3f3004c874ef934e3f совпал.
Получен GRAFANA_FOUR_HA_PANELS_LIVE_AUTOSTART_OBSERVER_OK: все4 HA panels сохранены,
каждый реальный backend query работает, внешний observer enabled/active.
Dashboard: http://172.31.0.97:3000/d/mositlab-director-wallboard.

Получены HA_ONE_HEALTHY_LEADER_TWO_READY_RESERVES_CONFIRMED и FINAL_HA_READY.
Epoch237 общий, все3 наf5, protocol2/maintenance=false/faulted=false.
Physical active/healthy/prepared, все5 workers running и dependencies ok,
uptime2735сек (около45мин). Оба VM passive/prepared, preflight.ok=true, workers={}.
Resource restart не требуется. Passive repair skipped на всех3: восстановление
произошло самостоятельно до запуска, инструмент не останавливал/не ремонтировал
работающие процессы. Последующие25сек stable samples и финальный snapshot готовы.
Историческую деградацию17:20 не считать текущим состоянием.

RFID CONNECTED,241 reads/db deliveries, db_errors0, spool_pending0;
business_flow_latched=false, reader/TCP/database/writer и обе cameras ok.
Это подтверждает реальную работу служб и приём RFID. Business acceptance точности
учёта катушек не завершена: Aggregator log содержит неподтверждённые как катушка
события и recheck533. Не подменять её health/счётчиком чтений; искусственный проход
не создавался. Main adoption не подтверждён: release_sha=f5 и main_sha=null.

ZABBIX_FINISH_ACTION_INCOMPLETE RuntimeError после ZabbixApiRejected_user_login
code=-32602. Сервер отклонил местный вход; error data намеренно не раскрыты,
не угадывать пароль, block/role/версию причины. HA items/triggers этим запуском
НЕ созданы. Это отдельная незавершённая настройка, не отказ кластера или Grafana.
Остался только повтор прямого Zabbix этапа на ub22:

    printf '%s  %s\n' aeb386b950a5636ac7f22280c677060b2df361a6a04dfc3f3004c874ef934e3f /tmp/perimeter-finish.3e8Xh0/finish.py | sha256sum --check -
    sudo python3 -B /tmp/perimeter-finish.3e8Xh0/finish.py --configure-zabbix

Использовать именно Zabbix username, пароль скрыт/не сохраняется. Enter=Admin
только если такой login пользователя; '-' честно оставляет настройку pending.
Нужны ZABBIX_DIRECT_HA_ITEMS_AND_TRIGGERS_CONFIGURED и ZABBIX_HA_LIVE_HISTORY_CONFIRMED.
Повторный полный installer/token-transfer/recovery/failover не требуется.

## Историческая остановка: частичная установка и деградация, 17:20 МСК

Пользователь подтвердил HA_OBSERVER_TOKEN_TRANSFERRED physical→ub22. Повторно
передавать ключ не требуется. На ub22 запущен install_monitoring.py из ae4cf476;
checksum совпал, observer и wallboard adapter установлены. Получен marker
EXISTING_WALLBOARD_AND_HA_ROUTE_CONFIRMED, затем MONITORING_ACTION_INCOMPLETE
HTTP_403. Существующий wallboard сохранён. Grafana HA panels и Zabbix items этим
запуском НЕ созданы: прежний installer делал Zabbix writes раньше сохранения panels.

Свежий наблюдатель (timestamp1791296415): все три узла доступны по точным IP,
на f5fdb6aed1c3749b0ada51e28c5dec96ed2c59fa, epoch230. Physical active=true,
healthy=false/prepared=false/faulted=false; не готовы RfidReader, Aggregator,
WebDashboard. Perimetr и Comparator passive/prepared=true/faulted=true. Готовых
резервов0, severity2. Исторический успех15:38 не подтверждает текущее состояние.
Конкретная причина текущего отказа RFID ещё НЕ установлена; latch/SQL/transport
нельзя объявлять причиной без diagnostics. Принятие main0b022bf не подтверждено.

Причина HTTP403 в конфигураторе доказана исходниками официального grafana-zabbix:
pkg/datasource/guardrails.go разрешает read API methods, не item.create/update
и trigger.create/update; resource_handler.go отвергает их HTTP403. Это ошибка
маршрута установщика, не доказательство неверного пароля/недостаточных прав.
Нельзя снимать guardrails, менять Grafana роли или извлекать datasource passwords.
install_monitoring.py теперь не отправляет Zabbix writes через этот proxy; новая
установка завершает observer/Grafana и явно оставляет прямой Zabbix setup pending.
На текущем ub22 повторять --install не нужно: observer уже работает.

Следующий единственный запуск на ub22: скачанный pinned finish_monitoring.py:

    sudo python3 -B finish.py --finish-grafana --diagnose --repair-passive --configure-zabbix

Инструмент импортирует уже установленный observer /usr/local/lib/perimeter-ha-monitor/monitor.py,
не перезапускает daemon/wallboard и использует существующий root credential.
1. Сохраняет4 HA panels в director dashboard, сохраняет прочие panels/options,
   проверяет все4 backend queries и enabled/active external observer. Dashboard
   backup0700/0600 в /var/lib/perimeter-ha-monitor/backups/grafana-<uuid>.
2. GET /diagnostics всех3 agents: status, preflight, resources, workers, readiness,
   business dependencies/metrics и короткие redacted logs. Секреты дополнительно
   удаляются локально; operator-maintenance/cursors/spools/latch не меняются.
3. Не более одного штатного restart_service/all repair request на каждый узел,
   только при fresh protocol2 passive/faulted/prepared, preflight.ok=true,
   maintenance=false, resources.restart_required=false. Повторные status/diagnostics
   должны иметь ту же epoch/release и workers={}. Guardian SQL owner/rate guards
   дополнительно проверяют состояние при запросе. Active owner не ремонтируется.
   Fault снимает только Guardian после собственного recovery verification.
   До105сек ожидается один healthy/prepared/nonfaulted owner и оба prepared/nonfaulted
   reserves на общей epoch; подтверждение требует25сек стабильности. Это штатное
   восстановление, не повторное испытание отказа. При failure выводятся причины;
   SQL fault flags не сбрасываются принудительно и бизнес-отказ не маскируется.
4. Локально запрашивает Zabbix login (Enter=Admin) и скрытый пароль Zabbix,
   отдельно от Linux sudo/SSH. '-' пропускает только этот этап и явно оставляет pending;
   финальное состояние кластера всё равно выводится. Пароль/сессия не сохраняются.
   Используется прямой http://127.0.0.1/api_jsonrpc.php, version6.x, затем local
   user.login. Только managed HA items/triggers существующего host10539; collision
   отказывает, backup перед managed updates. Проверяет свежую Zabbix историю.
   Новых actions/recipients не создаёт. Вход недостаточной роли честно отказывает.

Markers: GRAFANA_FOUR_HA_PANELS_LIVE_AUTOSTART_OBSERVER_OK означает живые4 queries;
ZABBIX_DIRECT_HA_ITEMS_AND_TRIGGERS_CONFIGURED и ZABBIX_HA_LIVE_HISTORY_CONFIRMED
означают установленный Zabbix setup/сбор. FINAL_CLUSTER_STATE и FINAL_HA_READY
либо FINAL_HA_NOT_READY отдельно отражают текущую готовность системы. Успех
мониторинга не подменяет успех production. Ошибка Grafana/Zabbix не блокирует
независимую диагностику и безопасную попытку восстановления. Полный успех exit0
требует завершённых requested actions и готового кластера; pending/failure exit1.

Проверено:35 monitoring tests, включая passive-only guard, active repair запрет,
workers/epoch recheck, failover consistency, local direct API credential scope,
redaction, skip-login final state и независимую диагностику после Grafana failure.
На заводских узлах новый инструмент пока НЕ запущен. Настоящий RFID-проход
пользователем отложен и не имитируется. Production code/main не менялись.

## Предыдущая подготовка и исторические данные (до 17:20)

Последняя заводская квалификация стека: f5, physical189 active/healthy, оба
резерва prepared/nonfaulted, 15:38 МСК. PR#3 объединён в main0b022bf в16:03.
Принятие main заводскими агентами ещё не подтверждено.

Вывод ub22 в16:45: Grafana dashboard mositlab-director-wallboard version16,
canSave=true. Infinity wallboard-api, allowedHosts19150/19151; Zabbix datasource
bfvr5vy0tr8cgb, существующий physical host DESKTOP-OFF5KSM id10539.
Physical18200/health/ready отвечает503: агент доступен, но readiness false;
первый inspector не сохранил body503, конкретная роль/fault/reason неизвестны.
Проверка VM по именам вернула URLError, причина не доказана. В deployed preparation
windows_tool.py точные адреса: Perimetr172.31.0.134, Comparator172.31.0.192.
Не считать все узлы healthy по историческому выводу после этого снимка.

Подготовлен standalone install_monitoring.py; публикуется ТОЛЬКО в feature,
без очередного main rollout и без изменений production HA/SQL/worker processes.
Настройка внешнего мониторинга пока НЕ выполнена на ub22: нужен --install output.

Последовательность: transfer_monitoring_token.ps1 на physical штатным PowerShell;
потом sudo python3 -B downloaded-install_monitoring.py --install на ub22.
PowerShell читает только ha_token из известного private bundle, передаёт stdin
SSH mkm@172.31.0.97, не печатает и не помещает token в argv/clipboard.
Исходный private bundle не изменяет. Один HA token переносится в приватный файл
пользователя mkm, затем root /etc/perimeter-ha-monitor.token mode0600.
Остальные credentials, SQL/RTSP/web passwords не переносятся.

Установщик использует прежний GRAFANA_TOKEN только к127.0.0.1:3000, отключает
HTTP redirects/proxies; HA token только GET /status к трём известным IP18200.
Server-private credential доступен runtime через systemd LoadCredential под nobody;
не помещается в Grafana datasource, Zabbix item или dashboard. После успешной
установки промежуточный /home/mkm/.perimeter-ha/observer.token удаляется.

Изменения:
- perimeter-ha-monitor.service: enabled, Restart=always, loopback19152, опрос5сек
  с bounded timeouts, public health503 JSON + authenticated snapshots, safe service
  readiness/preflight/release SHA, RFID business warning отдельно. Данные >25сек
  становятся critical, неизвестный business status не зеленеет.
- Небольшой systemd drop-in mositlab-wallboard.service запускает прежний
  /opt/mositlab-wallboard/wallboard.py через adapter; исходный код, EnvironmentFile,
  все прежние routes сохраняются. Добавляет только /perimeter-ha/status,summary,nodes
  на существующий allowed19150. Collector кратко перезапускается; проверяется прежний
  набор projects и новая route. При неудаче adapter drop-in откатывается и прежний
  launcher запускается снова. Нужен stdlib HTTPServer/ThreadingHTTPServer и прежний
  ExecStart /usr/bin/python3 /opt/mositlab-wallboard/wallboard.py.
- В существующий Zabbix host10539 добавляются perimeter.ha.cluster.json HTTP master
  на loopback19152,22 numeric dependent items,3 triggers: observer nodata45сек,
  critical HA/business failure, warning unavailable reserve/business advisory.
  Создаются только новые managed keys; existing key/trigger collision отказывает.
  Новые notification actions/recipients не создаются. Пять прежних проверок сохранены;
  их роль-зависимые expressions этим установщиком не изменяются.
- В начало существующего director dashboard добавляются4 native stat/table panels:
  HA status, leader, business warning,3nodes/details/epoch/release. Прежние panels/
  ECharts options/queries сохранены, сдвигаются на12 grid rows только первый раз.
  Повторный запуск не дублирует panels/items/triggers. Datasource settings и secret
  values не меняются: queries используют existing19150 allowed endpoint.
- Backups dashboard/datasource/prior managed item/trigger/unit/script в
  /var/lib/perimeter-ha-monitor/backups/<id>, directory0700. Backup не экспортировать:
  dashboard/datasource могут содержать внутренние metadata. Safe errors вместо raw
  exception bodies/token/env/argv.

Успех требует всех markers: EXISTING_WALLBOARD_AND_HA_ROUTE_CONFIRMED,
ZABBIX_HA_ITEMS_AND_TRIGGERS_CONFIGURED, GRAFANA_HA_BACKEND_DATA_CONFIRMED,
ZABBIX_HA_HISTORY_CONFIRMED, MONITORING_INSTALLED_AUTOSTART_GRAFANA_ZABBIX_OK.
Последний CLUSTER_OPERATIONAL_STATE — реальный снимок, может показывать failure;
успешная установка мониторинга НЕ означает автоматический repair всей системы.
До этих markers установка не подтверждена. При частичном отказе managed additions
могут сохраниться для retry; monitor/SQL/HA workers не останавливаются.

Проверки:24 monitoring unit tests OK, включая503 body/fault/epoch, expected standby,
two/no leader, failed/stale/auth snapshots, business warning отдельно, credential
destination/redirect restrictions, dashboard preservation/idempotence, original
wallboard routes и unavailable-observer critical fallback. Factory/API install
ещё не выполнен. Реальное RFID чтение не имитируется и отдельно не принято.
