# RFID КПП «Периметр» — production runbook

> Канонический репозиторий production-системы КПП. Если вы продолжаете работу после паузы, **сначала прочитайте этот README целиком** и только потом меняйте код/мониторинг.

## 0. Коротко: что сейчас считается правильным состоянием

Дата фиксации: **2026-09-16**.

Рабочая production-папка на ПК КПП:

```text
D:\Desktop\RFID_KPP-main
```

Штатный запуск всей системы:

```bat
RUN_RFID_KPP_FINAL.cmd
```

Схема БД: **3.4.5**.

Фактически проверенный production release после восстановления мониторинга:

```text
3.4.5-warehouse-recheck+obs2
```

Последний полностью проверенный runtime SHA до финальной Git-уборки/документации:

```text
8ed4a8cc1b3718fe0afef2079cd9b08590ef0e3a
```

Важно: после слияния веток `main` может иметь более новый SHA только из-за README/manifest/security cleanup. Активные business/monitoring entrypoints должны оставаться теми же, пока явно не выпущен новый runtime-релиз.

---

# 1. Самое важное для следующего инженера / ChatGPT

## НЕ ДЕЛАТЬ

1. **Не придумывать новую схему мониторинга**, если уже существуют health-порты `18101–18105`.
2. **Не создавать заново Zabbix items/triggers и Sentry projects**, пока не доказано, что существующие удалены.
3. **Не возвращать временный `perimeter.master` UserParameter**. Он был диагностическим обходным путём и удалён после восстановления штатного observability.
4. **Не считать отсутствие health-портов новой архитектурой.** В этом проекте это означает, что monitoring/observability-код потерян или не запущен.
5. **Не откатывать текущую Warehouse identity-логику** ради переноса старого monitoring-кода.
6. **Не заменять целиком `KPP`, `RFID`, `YOLO`, `Web` файлами из старой ветки `perimeter-3.7.0-zabbix-sentry`.** Оттуда переносился только observability-контракт; старая ветка содержит другую business-логику.
7. **Не выводить и не просить пользователя присылать `deploy/config_v3.cmd` целиком.** Там production credentials.
8. **Не коммитить production passwords, RTSP credentials, SQL credentials, Sentry auth tokens и т.п.**
9. Перед изменением агрегатора обязательно сохранить:
   - nullable `Warehouse.Tag`;
   - identity priority `TAG → IDS → SERIES`;
   - `WAREHOUSE_ONLY`;
   - поздний recheck;
   - `SUPERSEDED_BY_KPP`;
   - promotion физически считанного `UNKNOWN_RFID` по `RfidReadCount>0`.

## Если проблема «Периметр красный в Grafana/Zabbix»

Сначала проверить локально:

```powershell
$Ports = 18101,18102,18103,18104,18105
foreach ($Port in $Ports) {
    curl.exe -s "http://127.0.0.1:$Port/health/ready"
}
```

Только после этого разбирать Zabbix/Grafana.

---

# 2. Архитектура

Система состоит из пяти production-процессов.

```text
RFID Reader
    ↓ raw reads
SQL dbo.RFID_Tags

RusGuard Sync
    ↓ SKUD events
SQL dbo.RusGuardLogs

RTSP + YOLO
    ↓ video transitions
SQL dbo.ReelTransitions

                ┌─ RFID_Tags
                ├─ RusGuardLogs
Aggregator  ←───┼─ ReelTransitions
                ├─ dbo.RfidTags       (1C task/registry)
                └─ dbo.Warehouse      (address warehouse fact)
                      ↓
               dbo.KPP_ReelEvents
                      ↓
                 Web Dashboard
```

Production entrypoints:

| Компонент | Файл |
|---|---|
| RFID Reader | `RFID_reader_v4/rfid_to_sql_v4.py` |
| RusGuard | `DB_RusGard/db_sync_v2.py` |
| YOLO/RTSP | `RTSP/RTSP_yolo_DB_v3.py` |
| Aggregator | `KPP/kpp_aggregator_v3_warehouse_v3.py` |
| Web | `web/kpp_reel_dashboard_v3_fixed.py` |
| Общий launcher | `RUN_RFID_KPP_FINAL.cmd` |
| SQL migration | `migrations/001_kpp_v3_reliability.sql` |

Monitoring adapters:

| Компонент | Adapter |
|---|---|
| RFID | `deploy/monitored_rfid.py` |
| RusGuard | `deploy/monitored_rusguard.py` |
| YOLO | `deploy/monitored_yolo.py` |
| Aggregator | `deploy/monitored_aggregator.py` |
| Общий health/Sentry | `common/observability.py` |
| Service runner | `deploy/run_service.py` |

Важно: adapters **оборачивают текущую production business-логику**, а не заменяют её старой логикой.

---

# 3. Warehouse / 1C identity — обязательный контракт

`dbo.Warehouse.Tag` **может быть NULL или пустым**.

Валидная Warehouse-строка должна иметь:

```text
Dt
Ids
SeriesNumber
```

`Tag` — дополнительный идентификатор, а не обязательный.

Сопоставление Warehouse с физической RFID-меткой выполняется строго:

```text
1. TAG
2. IDS
3. SERIES
```

## TAG

Если `Warehouse.Tag` задан, используется прямое совпадение физического RFID tag.

## IDS

Если Tag отсутствует/не дал связи:

```text
Warehouse.Ids
    ↔ dbo.RfidTags.Ids
    → dbo.RfidTags.Tag
    → физический RFID/KPP event
```

`Ids` в SQL Server — `uniqueidentifier`; нельзя бездумно применять к нему `LTRIM/RTRIM`.

## SERIES

Если TAG/IDS не дали однозначной связи, используется `SeriesNumber`.

SeriesNumber разрешается только если результат **однозначен**.

Неоднозначный SeriesNumber:

```text
AMBIGUOUS_SERIES
```

и **не должен автоматически склеиваться**.

## Уже сохранённый WarehouseId

Если реальное KPP-событие уже связано с `WarehouseId`, эта связь имеет приоритет. Нельзя «перехватить» Warehouse row более близким по времени другим событием.

---

# 4. Что делать, если Warehouse есть, а RFID нет

Warehouse считается независимым физическим подтверждением того, что катушка попала на адресный склад.

Если Warehouse row валидна, но подходящего KPP/RFID event нет:

```text
SessionCloseReason = WAREHOUSE_ONLY
```

Создаётся синтетическое событие.

`WAREHOUSE_ONLY` — это **не утверждение, что антенна прочитала RFID**. Это отдельный факт склада.

Такие события повторно проверяются в течение:

```text
168 часов / 7 суток
```

обычно:

```text
каждые ~60 секунд
batch 500
```

Durable cursor:

```text
LAST_WAREHOUSE_ID_V3_4_5_RECHECK
```

Если позднее появляется реальное физическое KPP/RFID событие:

1. реальный event обогащается Warehouse/1C;
2. синтетический `WAREHOUSE_ONLY` получает:

```text
SUPERSEDED_BY_KPP
```

3. синтетическое событие больше не считается реальной катушкой.

---

# 5. Важный hotfix физического RFID

Ранее возможна ситуация:

```text
RFID физически считан
RfidReadCount > 0
но IsReel = 0 / UNKNOWN_RFID
```

После прихода Warehouse/1C такое событие **нужно повышать до реальной катушки**, а не создавать ложный `WAREHOUSE_ONLY`.

Поэтому поиск реального события допускает:

```text
RfidReadCount > 0
```

при этом исключает синтетические `WAREHOUSE_ONLY`.

Это обязательная production-регрессия, её нельзя потерять.

---

# 6. Отчёт

Для отчёта используются два физических источника факта:

1. реальный KPP/RFID проход;
2. Warehouse row как независимое подтверждение адресного склада.

Идентификация человека/1C может идти через:

```text
Tag
Ids
SeriesNumber
```

Человеку в отчёте в первую очередь нужен `SeriesNumber`; `Ids` — внутренняя связь 1C; `Tag` — физическая RFID identity.

Общая логика сборки отчёта вынесена в:

```text
common/warehouse_report.py
```

Web не должен иметь отдельную противоречащую реализацию отчёта.

---

# 7. Runtime / SQL

Основные таблицы:

```text
dbo.RFID_Tags
dbo.RusGuardLogs
dbo.ReelTransitions
dbo.RfidTags
dbo.Warehouse
dbo.KPP_ReelEvents
dbo.KPP_RuntimeState
dbo.KPP_ActiveRfidSessions
dbo.KPP_ProcessingErrors
```

Schema version:

```text
KPP_SCHEMA_VERSION = 3.4.5
```

RFID cursor:

```text
LAST_RFID_ID_V3
```

Warehouse cursor:

```text
LAST_WAREHOUSE_ID_V3_4_5_RECHECK
```

Spool files:

```text
runtime\rfid_spool_v4.sqlite
runtime\video_spool_v3.sqlite
```

Pending spool должен быть способен пережить временную недоступность SQL; записи нельзя удалять до durable delivery.

---

# 8. Штатный запуск

Production запускается **только** через:

```bat
D:\Desktop\RFID_KPP-main\RUN_RFID_KPP_FINAL.cmd
```

Launcher:

1. определяет существующий 64-bit Python;
2. определяет 32-bit Python для vendor RFID DLL;
3. устанавливает только отсутствующие Python dependencies;
4. применяет idempotent SQL migration;
5. выполняет precheck;
6. запускает пять service wrappers;
7. ждёт Web `http://127.0.0.1:5050`.

Не надо вручную запускать старые v2/v2.4/v3.2 scripts из старого README/истории.

---

# 9. Production config / секреты

Файл:

```text
deploy/config_v3.cmd
```

является **локальным production-файлом** и не должен храниться в Git.

Для нового развёртывания:

```text
deploy/config_v3.example.cmd
    ↓ copy
local deploy/config_v3.cmd
    ↓ fill secrets locally
```

Важно при обновлении существующего ПК:

```text
НЕ ПЕРЕТИРАТЬ deploy/config_v3.cmd
```

В истории репозитория production credentials ранее уже попадали в commits. Поэтому считать исторически опубликованные SQL/RusGuard/RTSP credentials скомпрометированными и **ротировать их**. Простое удаление файла из текущего `main` не удаляет секреты из старой Git history.

---

# 10. Monitoring — это уже настроено

## 10.1 Zabbix

На production ПК установлен classic Zabbix Agent.

```text
Windows service: Zabbix Agent
Agent port:      10050
Host:            DESKTOP-OFF5KSM
Server:          ub22 / 172.31.0.97
```

Существующая Zabbix-схема ожидает service health endpoints:

| Service | Port |
|---|---:|
| Perimeter.RfidReader | 18101 |
| Perimeter.RusGuardSync | 18102 |
| Perimeter.Yolo | 18103 |
| Perimeter.Aggregator | 18104 |
| Perimeter.WebDashboard | 18105 |

Endpoints каждого сервиса:

```text
/health
/health/ready
```

`/health` = процесс/health server жив.

`/health/ready` = сервис реально готов с учётом обязательных dependencies.

Ожидаемая семантика:

```text
200 + status=ok        → READY
503 + status=degraded  → LIVE, но dependency degraded
нет ответа             → процесс/health layer не работает
```

### Важно

Порты `18101–18105` **не legacy-мусор**. Это штатный контракт уже созданного Zabbix monitoring.

Временный эксперимент:

```text
perimeter.master
C:\ProgramData\RFID_KPP\monitoring
zabbix_agentd.d\perimeter.conf
```

был удалён. Его **не восстанавливать**, если нет отдельного решения менять всю monitoring architecture.

## 10.2 Readiness dependencies

### RFID Reader / 18101

Ожидаются:

```text
database
rfid_reader
rfid_tcp
```

Проверяется не «были ли сегодня проходы», а реальная связь/SDK/read-loop.

**Отсутствие RFID reads само по себе не авария.** Машин может просто не быть.

### RusGuard / 18102

```text
source_database
destination_database
sync_loop
```

### YOLO / 18103

```text
database
model
camera_0
camera_1
pipeline
```

Камера считается здоровой по реально свежим кадрам, а не только по открытому TCP-порту.

### Aggregator / 18104

```text
database
pipeline
rfid_reader
yolo
rusguard
```

Aggregator peer-health зависит от heartbeat остальных процессов.

### Web / 18105

```text
database
aggregator
web_port
```

Web application port:

```text
5050
```

## 10.3 Почему сразу после запуска может быть degraded

Нормально, если первые несколько секунд после старта:

```text
YOLO        LIVE_BUT_NOT_READY
Aggregator  LIVE_BUT_NOT_READY
Web         LIVE_BUT_NOT_READY
```

Пока первый camera/probe/peer heartbeat не прошёл.

Через несколько heartbeat cycles все dependencies должны перейти в `ok`.

---

# 11. Sentry — тоже уже настроено

Sentry endpoint:

```text
sentry.mositlab.ru
```

Organization:

```text
mositlab
```

Team:

```text
perimetr
```

Существующие проекты:

| Service | Sentry project ID |
|---|---:|
| Perimeter.RfidReader | 21 |
| Perimeter.RusGuardSync | 22 |
| Perimeter.Yolo | 23 |
| Perimeter.Aggregator | 24 |
| Perimeter.WebDashboard | 25 |

Sentry integration находится в:

```text
common/observability.py
```

Не создавать новые проекты только потому, что после обновления события пропали. Сначала проверить, что текущий service запускается через `deploy/run_service.py` и нужный monitored adapter.

---

# 12. Быстрая проверка всей системы

PowerShell на production ПК:

```powershell
$Ports = 18101,18102,18103,18104,18105

foreach ($Port in $Ports) {
    Write-Host ""
    Write-Host "========== $Port =========="
    curl.exe -s "http://127.0.0.1:$Port/health/ready"
}

Invoke-WebRequest http://127.0.0.1:5050 -UseBasicParsing -TimeoutSec 10

Get-NetTCPConnection -State Listen |
    Where-Object { $_.LocalPort -in @(5050,10050,18101,18102,18103,18104,18105) } |
    Sort-Object LocalPort

Get-Service "Zabbix Agent"
```

Нормальный результат:

```text
18101 status=ok
18102 status=ok
18103 status=ok
18104 status=ok
18105 status=ok
5050  HTTP 200
10050 LISTEN
Zabbix Agent Running / Automatic
```

Если один service degraded, смотреть `dependencies` в JSON **до любых изменений архитектуры**.

---

# 13. Как диагностировать по слоям

## Все процессы не стартовали

Смотреть:

```text
RUN_RFID_KPP_FINAL.cmd
deploy/precheck_v3.py
deploy/resolve_python.cmd
deploy/ensure_dependencies.cmd
```

## RFID красный

Смотреть readiness `18101`:

```text
rfid_reader
rfid_tcp
database
```

Затем локальный spool:

```text
runtime/rfid_spool_v4.sqlite
```

## YOLO красный

Смотреть `18103`:

```text
camera_0
camera_1
model
pipeline
database
```

Не считать `reels=0` неисправностью.

## Aggregator красный

Смотреть `18104` dependencies. Если `yolo=unavailable`, сначала чинить YOLO, а не Aggregator.

## Web красный

Сначала:

```text
http://127.0.0.1:5050
18105 /health/ready
```

Если `aggregator=unavailable`, это каскадная проблема upstream.

---

# 14. Git / release discipline

Каноническая ветка:

```text
main
```

После текущей уборки исторические feature/fix ветки не являются источником production truth.

Для любой новой задачи:

1. ветка от `main`;
2. минимальный diff;
3. CI;
4. проверить Warehouse regression;
5. проверить observability contract;
6. PR в `main`;
7. merge только после проверки;
8. production deployment — exact SHA;
9. записать SHA и release в этот README/manifest.

Старые ветки использовать только как исторический reference, не как базу для нового production.

---

# 15. CI

Обязательные regression-наборы:

```text
Warehouse identity/report/aggregator/deployment tests
Observability contract tests
py_compile production modules
```

Особенно важно сохранить тесты на:

- nullable Warehouse.Tag;
- TAG → IDS → SERIES;
- ambiguous series;
- persisted WarehouseId priority;
- WAREHOUSE_ONLY late recheck;
- UNKNOWN_RFID physical promotion;
- health ports 18101–18105;
- monitored adapters;
- normal sibling-import behavior в `run_service.py`.

---

# 16. Что произошло 2026-09-16 и почему это записано здесь

При обновлении Warehouse business-логики был развёрнут корректный `3.4.5`, но в той ветке отсутствовал старый observability layer. В результате:

```text
business logic = OK
Web 5050       = OK
18101-18105    = отсутствовали
Zabbix/Grafana = показывали ложные проблемы
Sentry         = потерял runtime integration
```

Ошибка диагностики заключалась в том, что отсутствие `18101–18105` сначала было принято за «устаревшую схему мониторинга». Это было неверно.

Правильное решение:

```text
оставить текущий 3.4.5 Warehouse/business code
+
вернуть существующий observability contract
+
не пересоздавать Zabbix/Sentry infrastructure
```

В итоге monitoring восстановлен как adapters вокруг текущих production-файлов.

Это ключевая причина существования данного README: **не повторять эту ошибку при следующем изменении.**

---

# 17. Проверенное состояние на момент фиксации

После production установки release `3.4.5-warehouse-recheck+obs2` было фактически подтверждено:

```text
RFID Reader :18101 status=ok
RusGuard    :18102 status=ok
YOLO        :18103 status=ok
Aggregator  :18104 status=ok
Web         :18105 status=ok

Web :5050 HTTP 200
Zabbix Agent Running / Automatic
Sentry HTTPS reachable
Warehouse cursor == Warehouse MAX Id
Warehouse lag == 0
RFID spool pending == 0
Video spool pending == 0
```

YOLO readiness фактически подтвердил:

```text
camera_0 = ok
camera_1 = ok
database = ok
model = ok
pipeline = ok
```

Aggregator:

```text
database = ok
pipeline = ok
rfid_reader = ok
rusguard = ok
yolo = ok
```

Web:

```text
aggregator = ok
database = ok
web_port = ok
```

Это baseline, от которого надо отталкиваться при следующих изменениях.

---

# 18. Минимальная памятка для будущего AI/инженера

Перед ответом на вопрос по этому проекту:

1. считать `main` канонической веткой;
2. прочитать этот README;
3. проверить текущий SHA/CI, если вопрос про код;
4. не смешивать старую `3.7.x` business-ветку с production `3.4.5`;
5. помнить: `Tag` Warehouse необязателен;
6. помнить: `Ids` и `SeriesNumber` обязательны для Warehouse identity;
7. помнить приоритет `TAG → IDS → SERIES`;
8. помнить `WAREHOUSE_ONLY` + 168h late recheck;
9. помнить promotion физического `UNKNOWN_RFID`;
10. помнить, что Zabbix/Sentry **уже существуют**;
11. monitoring contract = `18101–18105`;
12. не создавать `perimeter.master`, если явно не проводится миграция monitoring architecture;
13. никогда не публиковать `deploy/config_v3.cmd`.

Если всё это соблюдено — изменения обычно можно делать точечно, без перестройки всей системы.
