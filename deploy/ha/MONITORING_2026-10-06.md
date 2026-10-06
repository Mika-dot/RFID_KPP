# Подключение внешнего мониторинга HA, 06.10.2026

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
