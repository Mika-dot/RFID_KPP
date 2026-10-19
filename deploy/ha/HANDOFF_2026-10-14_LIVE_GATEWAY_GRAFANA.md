> Документ описывает события 14.10.2026. Текущие инструкции с 19.10: [эксплуатация](OPERATIONS.md), [восстановление](RECOVERY.md), [состояние](STATUS_2026-10-19.md). Указание читать этот файл первым ниже относится к исторической передаче.

# «Периметр» — фактическая передача на 14.10.2026: три HA-узла, ub22 Gateway, Grafana

> **Читать первым при возобновлении работы 15.10.2026 или позже.** Сеанс пользователя
> от 14.10 завершён по его просьбе. Не выполнять повторный rollout, не перезапускать
> существующие HA-узлы, не менять SQL lease/epoch, не сбрасывать faulted,
> не очищать очереди, не отключать fencing, не откатывать и не повышать runtime SHA
> без нового конкретного основания. Этот файл сохраняет **проверенные выводы
> команд пользователя**, а не непрерывное наблюдение за производством.
> Исторические статусы в старых README/HANDOFF не заменяют сведения ниже.

## 0. Репозиторий, версии и ограничения

- Репозиторий: https://github.com/Mika-dot/RFID_KPP
- Рабочая ветка для продолжения: `feature/adaptive-correlation-fallback-2026-10-08`.
- **Закреплённый целевой runtime для Physical, Perimetr, Comparator (НЕ менять):
  `7fc779e406c250b69b5fa34c6cab0e0d1d3bd5a6`.**
- Отдельный чистый checkout на ub22 также подтверждён как exact
  `7fc779e406c250b69b5fa34c6cab0e0d1d3bd5a6`.
- В рабочей ветке после runtime отдельно исправлена Grafana 13 RBAC-проверка:
  `deploy/ha/update_visual_wallboard.py` (commit `8ef6936f61ec972216e7d69bf6ebd642bbfd856c`)
  и соответствующий регрессионный тест (commit
  `5410a3b2a76d6430086e126d78a7a6c22c28d600`).
  **HEAD рабочей ветки не тождествен установленному runtime**;
  не делать `git pull` в установленные деревья. Код фикса Grafana не потребовал
  менять три HA-узла; на ub22 был применён временный API-адаптер при штатном update.
- Protocol fencing 2. Ведущего выбирает HA/SQL, а не Gateway и не Grafana.
- Непрерывный стабильный HA с двумя готовыми резервами **пока не подтверждён**.
- Пользователь явно отложил физический проход барабана и физическое отключение
  питания/сети; не подменять их симуляциями и не отмечать как выполненные.
- Не публиковать секреты, полные окружения, cookies, bearer tokens, SQL logins,
  RTSP credentials, реальные EPC/TID и содержимое частных журналов в Git.
  В этой передаче указаны только известные сетевые адреса, имена файлов,
  не секретные параметры и очищенные статусы.

## 1. Топология / сетевые адреса

| Узел | ОС / роль | IP | Контроль Guardian | Web сервиса |
|---|---|---|---|---|
| `physical` | Windows; основной приоритет 1, RFID SDK UHFAPI.dll Windows-only | `172.31.0.188` | `http://172.31.0.188:18200` | `http://172.31.0.188:5050` |
| `perimetr` | Ubuntu VM, приоритет 2 | `172.31.0.134` | `http://172.31.0.134:18200` | `http://172.31.0.134:5050` |
| `comparator` | Ubuntu VM, контроль/надзор, приоритет 3 | `172.31.0.192` | `http://172.31.0.192:18200` | `http://172.31.0.192:5050` |
| `ub22` | отдельный Linux хост вне триады HA; Gateway + Grafana + мониторинг | `172.31.0.97` | Gateway `0.0.0.0:5051` | Grafana `:3000` |

**Публичный постоянный URL сайта:** `http://172.31.0.97:5051/`.

**URL мониторинга:** `http://172.31.0.97:3000/`.

WebDashboard на пассивном узле может не слушать `:5050` **по проекту**:
Guardian стартует все пять workers лишь у действующего SQL owner, а при
demotion останавливает их. Поэтому отказ в подключении к пассивному Web
сам по себе **не свидетельствует о дефекте**. На активном Web порт должен
отвечать; HTTP **401 нормален**, если требуется Basic Auth.

### Gateway ub22: установленные пути/параметры

- `/opt/perimeter-resilience-releases/7fc779e406c250b69b5fa34c6cab0e0d1d3bd5a6` —
  отдельный clean exact checkout; вывод пользователя:
  `EXACT RELEASE VERIFIED`, затем `{"services_ready":["gateway"],"release":"7fc779..."}`.
- `/etc/perimeter/gateway.json` — создан из
  `deploy/ha/gateway.ub22.example.json`: `listen=0.0.0.0`,
  `port=5051`, `lease_source=guardians`, три node URL из таблицы выше;
  фактически создан при установке, изменения после не подтверждались.
- `/etc/perimeter-resilience.env` — приватный root-файл окружения с
  `PERIMETER_HA_TOKEN`. Значение **не хранить в репозитории**.
  При установке использован существовавший локально cluster credential из
  первого доступного файла:
  `/etc/perimeter-ha-monitor.token` либо
  `/home/mkm/.perimeter-ha/observer.token`;
  какой именно выбран, пользователь не показывал.
- `perimeter-gateway.service`: системный сервис, `perimeter-observer`,
  модуль `python3 -m gateway.server`; установлен через
  `deploy/ha/install_resilience.py --install` с SHA выше.
- Gateway использует **двух свежих авторизованных свидетельств** SQL lease
  с портов `:18200`. Не владеет выбором, не продлевает аренду.
  `/health/live=200` означает процесс жив, `/health/ready=200` —
  валидный healthy owner/epoch, `503` — fail-closed при отсутствии
  подтверждённого активного Web; последний результат может меняться при failover.
- Дополнительный доступ к устаревшей metadata-копии требует
  `KPP_WEB_AUTH_USER` и `KPP_WEB_AUTH_PASSWORD` в окружении Gateway;
  наличие этих параметров на ub22 **не проверено**; не заявлять stale fallback
  действующим без проверки. `perimeter-behavior.service` (19153)
  **не устанавливался этим запуском** и не подтверждён.

## 2. Что фактически сделано и проверено 14.10

### A. ub22: независимый Gateway — УСТАНОВЛЕН

Сначала на ub22 не существовало `perimeter-gateway.service`,
`/etc/perimeter/gateway.json` и `/etc/perimeter-resilience.env`,
`:5051` не слушал. После установки через exact pinned SHA:

- `install_resilience.py --plan` вернул `{"mode":"plan","services":["gateway"],"changes_applied":false}`.
- `--install` вернул `{"services_ready":["gateway"],"release":"7fc779e406c250b69b5fa34c6cab0e0d1d3bd5a6"}`.
- `systemctl is-active perimeter-gateway.service` → `active`.
- `GET /health/live` → `HTTP 200`; `GET /health/ready` → `HTTP 200` после установки.
- В промежуточных проверках `ready` периодически был `503` во время потери
  здорового owner/failover; этот факт **не скрывать**.
- Позже 4 подряд синхронных снимка при perimetr owner (epoch 5442):
  `Gateway /health/ready=200` во всех четырёх.

### B. ub22: Grafana — ОБНОВЛЕНА И ПРИНЯТА ПО API

- Существующая Grafana `13.2.0`, `http://172.31.0.97:3000`.
- `Business Charts / ECharts` panel `volkovlabs-echarts-panel` v`7.2.5`;
  развёрнут через `grafana cli --homepath /usr/share/grafana
  --pluginsDir /var/lib/grafana/plugins plugins install volkovlabs-echarts-panel`.
  `/var/lib/grafana/plugins/volkovlabs-echarts-panel/plugin.json`
  показывает `grafanaDependency >=12.3.0`.
- `journalctl`: `logger=plugins.registration ... Plugin registered
  pluginId=volkovlabs-echarts-panel`.
- **Подводный камень:** на Grafana 13 служебный токен wallboard может получать
  `/api/plugins=[]` (RBAC/filter), хотя плагин зарегистрирован.
  `GET /api/plugins/volkovlabs-echarts-panel/settings` подтвердил
  `id=volkovlabs-echarts-panel,type=panel`. В ветке это исправлено
  на direct settings check. На хосте применили исходный pinned
  `update()` через **однократный адаптер только для API-list plugins**,
  а не изменение HA runtime.
- Существующая визуальная база: Infinity datasource
  `uid=wallboard-api`, тип `yesoreyeram-infinity-datasource`,
  allowed host `http://127.0.0.1:19150`. Исходный обзорный
  dashboard `uid=mositlab-director-wallboard`, `canSave=true`.
  Источник collector/backend `:19150`; в журналах Grafana были
  успешные Infinity query.
- Итоговый вывод пользователя (проверка записей и backend query):

```json
{
  "applied": true,
  "removed_banner_ids": [],
  "banner_detection": "no_top_text_panel_detected; kiosk hides Grafana header",
  "interval": "1m",
  "refresh": "5s",
  "visual_panels": 9,
  "overview_url": "/d/mositlab-director-wallboard?kiosk",
  "perimeter_url": "/d/mositlab-perimeter-visual?kiosk",
  "rotation_url": "/playlists/play/mositlab-perimeter-wallboard?kiosk",
  "backend_queries_verified": true,
  "backup": "/var/lib/perimeter-ha-monitor/backups/visual-t0t422hv",
  "versions": {"mositlab-director-wallboard": 19, "mositlab-perimeter-visual": 1}
}
```

- Пользователь получил `GRAFANA_UPDATE_SUCCESS`.
- **Телевизор:** для автопереключения по 1 минуте нужно открыть
  `http://172.31.0.97:3000/playlists/play/mositlab-perimeter-wallboard?kiosk`,
  а **не** одиночный dashboard URL. Порядок: обзор MOSITLAB →
  визуальная схема Периметр (9 панелей) → цикл; refresh данных 5 s.
  Пользовательский телевизор/браузер **не проверяли физически**; playlist
  сохранён и подтверждён API.
- Прямая схема: `http://172.31.0.97:3000/d/mositlab-perimeter-visual`.
- Резервная копия/rollback receipt — **только на ub22**:
  `/var/lib/perimeter-ha-monitor/backups/visual-t0t422hv`.
  Не коммитить его содержимое, не терять папку.
- Не путать этот экран с отдельным dashboard `zmk-production-kpi`:
  обновлён **MOSITLAB wallboard**, не производственная плашка ZMK.
- При перезапуске Grafana `:3000` на короткое время не отвечал, затем
  `grafana-server.service active/running`, порт `:3000` снова слушал.
  Нерелевантные ошибки Zabbix/Sentry в journal не доказывают отказ wallboard.

### C. Реальный HA: наблюдения менялись — НЕ ОБЪЯВЛЯТЬ ГОТОВЫМ

1. Снимок epoch `5421`: физика `active=false,healthy=false,faulted=true`,
   Perimetr такой же; Comparator `active=true,healthy=true,faulted=false`.
   Все 3 наблюдали валидный Comparator SQL lease; независимый `ObservedLease`
   вернул `owner=comparator,epoch=5421,valid=true,source=authenticated_guardian_sql_witnesses`.
2. Позже `Router.resolve()` ответил `owner=perimetr,epoch=5426`.
   Запрос к **пассивному** Comparator :5050 не прошёл; он не должен
   трактоваться как отказ активного Web.
3. Следующие **четыре последовательных измерения** epoch `5442`:
   `perimetr active=true,healthy=true,faulted=false,WebDashboard.ok=true`,
   `ACTIVE_WEB perimetr=HTTP 401` (ожидаемая авторизация),
   `Gateway ready=200`; Physical и Comparator `active=false,
   healthy=false,faulted=true`. На ведущем `web_port` probe status=ok,
   detail=`http_401`.
4. **САМЫЙ ПОЗДНИЙ предоставленный снимок (после этих четырёх):**

```json
{"node":"physical",   "active":false,"prepared":true,"faulted":true,"failed_preflight":[],"repair_verification_required":false,"update_pending":true}
{"node":"perimetr",    "active":false,"prepared":true,"faulted":true,"failed_preflight":[],"repair_verification_required":false,"update_pending":true}
{"node":"comparator",  "active":false,"prepared":true,"faulted":true,"failed_preflight":[],"repair_verification_required":false,"update_pending":true}
```

   **Здесь нет ведущего.** Это точечный GET-снимок, не доказательство
   постоянного отсутствия owner. Фактическая причина роста epoch,
   повторного faulting и `update_pending=true` на трёх узлах **не установлена**.
   Все preflight checks в последнем снимке были `true`, но этого
   недостаточно для снятия SQL faulted: реализация Guardian требует
   проверенной repair-процедуры и подтверждения, а не ручной смены флага.

## 3. Не завершено / не принимать без свидетельств

1. **P0: стабилизировать HA.** Получить устойчиво одного healthy владельца,
   валидный SQL lease/epoch, все 5 рабочих служб ready и два
   `prepared=true,faulted=false` standby. Найти причины
   `update_pending=true` и повторных fault/failover. Не очищать SQL
   faulted вручную, не отключать `protocol2`, не инициировать новый rollout.
2. **P0: Web Gateway при отказах.** Отслеживать `/health/ready` в момент
   переходов и отсутствие ошибочной маршрутизации; периодические 503
   до восстановления healthy owner допустимы по fail-closed дизайну.
   Стойкое 503 при confirmed healthy active Web — дефект.
3. **P1: длительное наблюдение, резервное восстановление и end-to-end.**
   Убедиться, что в нормальном состоянии работающего владельца остаются
   два готовых резерва, все бизнес-сервисы и данные/очереди/зеркала корректны.
   Нельзя считать весь первоначальный план закрытым лишь по Grafana.
4. **Не проверено:** реальный проход RFID-барабана, физическое отключение
   питания/сети и недельный acceptance; пользователь отложил их.
   `physical_business_acceptance=not_measured` по принятой политике проверки.
5. **Установка Behavior Observer `:19153` и полный SQL backup**
   в этой сессии не подтверждены. Не принимать как факт наличие
   полноразмерной копии SQL только по локальному bounded fallback/mirror.
6. **Привязка телевизора к playlist URL на самом устройстве** не проверена.
   API сохранил playlist; нужно проверить реальный kiosk экран при следующей смене.

## 4. С чего безопасно начать следующий сеанс

- **Никакого повторного развёртывания `7fc779e...` на всех машинах без
  обоснования.** Не `git pull` в runtime. Создать/использовать отдельное
  checkout инструментария и верификаторов; release SHA сохранить закреплённым.
- Выполнить **GET-only** снимок одновременно: три
  `http://<ip>:18200/status` с существующим
  `PERIMETER_HA_TOKEN` (не печатать), поля `node,active,healthy,prepared,
  faulted,epoch,release_sha,fencing_protocol,observed_lease,update.pending,
  repair.verification_required,preflight.checks,services`.
  Готовый инструмент: `deploy/ha/diagnose_cluster.py` или более глубокий
  `deploy/ha/verify_factory_acceptance.py --snapshot`, см.
  `FACTORY_READ_ONLY_ACCEPTANCE.md`; они не должны менять SQL/lease.
- Обязательно исследовать **`update.pending=true`**: проверять update/probation/
  operator pin/repair и статус контроллера Comparator, причину faulting,
  без циклической переустановки. Связать epoch с журналом переключений.
- Проверить `perimeter-gateway.service active`, `:5051/health/live=200`,
  `:5051/health/ready=200` в **устойчивом состоянии**. На активном
  `<owner>:5050/` HTTP 200/401 допустим; на standby `:5050` может быть
  закрыт.
- Grafana **уже применена**; заново не запускать установку plugin/playlist.
  Проверить страницу и реальные панели в браузере; при ошибке сначала читать
  `/var/lib/perimeter-ha-monitor/backups/visual-t0t422hv/receipt.json`
  и действующие объекты Grafana. При необходимости используется guarded
  обновление в отдельном checkout с RBAC-фиксом.
- Точные имена рабочих процессов: `RfidReader`, `RusGuardSync`, `Yolo`,
  `Aggregator`, `WebDashboard`. Health порты:
  `18101`, `18102`, `18103`, `18104`, `18105` соответственно.
  На активном `WebDashboard` конфиг `KPP_WEB_HOST=0.0.0.0`,
  `KPP_WEB_PORT=5050` (на Perimetr 14.10 подтверждены grep). Эти
  health endpoints локальны у владельца; другой узел не обязан иметь открытый
  `:5050`. Наличие `prepared=true` у faulted узла — не готовый резерв.
- Отдельно проверить operational acceptance из
  `deploy/ha/FACTORY_READ_ONLY_ACCEPTANCE.md` и задачи
  `deploy/ha/PLAN_AUDIT_2026-10-08.md`. Не смешивать offline CI, состояние
  GitHub и подтверждение работы на заводе.

## 4a. Проверка ВСЕХ проектов и панелей MOSITLAB wallboard

- Подготовлен самостоятельный **read-only** аудитор
  [`audit_wallboard_live.py`](audit_wallboard_live.py), тесты:
  [`test_wallboard_audit_readonly.py`](../../tests/test_wallboard_audit_readonly.py).
- Он читает **Grafana 3000** (dashboard uid `mositlab-director-wallboard` и
  `mositlab-perimeter-visual`), проверяет `playlist`, ECharts, Infinity
  datasource, количество и расположение панелей, **POST только к**
  `/api/ds/query` (это чтение данных), фиксирует ошибки/no-values.
  Дополнительно проверяет **локальный `19150/projects`**, статусы
  всех возвращаемых проектов и подтверждённые endpoints `/attention`,
  `/perimeter-ha/...`, age мониторинга, видимый inventory дашбордов.
- Аудит **не выполнялся на реальном ub22** на момент записи; код и mock-тесты
  в GitHub не являются подтверждением актуальных данных производства.
  Не выдавать старый `backend_queries_verified=true` за корректность всех
  проектов, особенно Zabbix/Sentry, где ранее отмечались ошибки.
- Запуск на **ub22**, не на трёх HA-узлах:

```bash
AUDIT_DIR=$(mktemp -d)
chmod 700 "$AUDIT_DIR"
curl -fL --retry 2 --connect-timeout 10 --max-time 60 \
  https://raw.githubusercontent.com/Mika-dot/RFID_KPP/53ac54bebc85dd4df3d84e8c4e2aaf78def34fd2/deploy/ha/audit_wallboard_live.py \
  -o "$AUDIT_DIR/audit_wallboard_live.py"
python3 -m py_compile "$AUDIT_DIR/audit_wallboard_live.py"
sudo -n python3 "$AUDIT_DIR/audit_wallboard_live.py" \
  --timeout 10 --max-queries 130 \
  --report "/root/mositlab-wallboard-audit-$(date +%Y%m%d-%H%M%S).json"
```

- Скрипт не выводит bearer token, реальные RFID теги, сырые бизнес-данные;
  отдельный JSON-отчёт сохраняется root-only (`0600`) и не отправляется в Git.
  Отправить оператору/следующему агенту **текстовый вывод** с `PROJECT`,
  `SOURCE`, `PANEL`, `FINDINGS` и `TOTALS` для фактической оценки.
- Даже успешный API-аудит **не равен визуальному QA**: для проверки обрезания
  текста и компоновки на реальном телевизоре нужен снимок экрана обеих страниц
  при фактическом разрешении и масштабировании браузера.

## 5. Команды для read-only сверки при продолжении

На **ub22** (root читает приватное окружение; секрет не печатается):

```bash
sudo systemctl is-active perimeter-gateway.service grafana-server.service
curl --noproxy '*' -sS -o /dev/null -w 'gateway live=%{http_code}\n' http://127.0.0.1:5051/health/live
curl --noproxy '*' -sS -o /dev/null -w 'gateway ready=%{http_code}\n' http://127.0.0.1:5051/health/ready
curl --noproxy '*' -sS -o /dev/null -w 'grafana=%{http_code}\n' http://127.0.0.1:3000/api/health
```

Следующий оператор/агент должен **начать с последнего P0-снимка** и
при необходимости расшифровать `update.pending` и fault history, а не
продолжать устаревшую инструкцию `READY_TO_INSTALL_2026-10-09.md`.

## 6. Временная и доказательная оговорка

Документ составлен по командам, реально выполненным пользователем на
`mkm@ub22` и `mkm@Perimetr` в разговоре 14.10.2026, и по исходникам
репозитория. Значения epoch и active/faulted **волатильны**, особенно при
частых аварийных переключениях. GitHub не даёт прямого доступа к
производственным IP, поэтому утверждать актуальную готовность после последнего
снимка нельзя. Физических тестов в рамках передачи не было.
