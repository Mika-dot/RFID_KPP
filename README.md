# RFID КПП — запуск консолей

Рабочая папка на сервере/ПК:

```bat
D:\Desktop\RFID_KPP-main
```

Запускать нужно 5 отдельных окон `cmd`:

```text
1. RFID Reader
2. RusGuard Sync
3. RTSP YOLO
4. KPP Aggregator
5. Web Dashboard
---

## 1. RFID Reader

Окно читает RFID-антенну и пишет метки в `dbo.RFID_Tags`.

```bat
@echo off
title RFID Reader

cd /d "D:\Desktop\RFID_KPP-main\RFID_readers\Версия (БД)"
venv310_32\Scripts\activate

set RFID_DLL_PATH=D:\Desktop\RFID_KPP-main\RFID_readers\Версия (БД)\UHFAPI.dll
set RFID_READER_IP=172.31.128.170
set RFID_READER_PORT=8888

set RFID_DB_CONNECTION=Driver={ODBC Driver 18 for SQL Server};Server=<SQL_SERVER>;Database=<SQL_DATABASE>;Uid=<SQL_USER>;Pwd=<SQL_PASSWORD>;Encrypt=yes;TrustServerCertificate=yes;

set RFID_CONSOLE_OUTPUT=1
set RFID_BATCH_SIZE=1
set RFID_BATCH_FLUSH_SEC=0.5
set RFID_DEDUP_ENABLED=0
set RFID_RECONNECT_DELAY_SEC=5

python rfid_to_sql_v3_2.py
```

---

## 2. RusGuard Sync

Окно переносит события СКУД в `dbo.RusGuardLogs`.

```bat
@echo off
title RusGuard Sync

cd /d D:\Desktop\RFID_KPP-main\DB_RusGard
..\venv64\Scripts\activate

set SRC_SERVER=<RUSGUARD_SQL_SERVER>
set SRC_DATABASE=<RUSGUARD_DATABASE>
set SRC_USERNAME=<RUSGUARD_USER>
set SRC_PASSWORD=<RUSGUARD_PASSWORD>
set SRC_DRIVER=ODBC Driver 18 for SQL Server
set SRC_TRUST_CERT=yes

set DST_SERVER=<SQL_SERVER>
set DST_DATABASE=<SQL_DATABASE>
set DST_USERNAME=<SQL_USER>
set DST_PASSWORD=<SQL_PASSWORD>
set DST_DRIVER=ODBC Driver 18 for SQL Server
set DST_TRUST_CERT=yes

python db_sync.py
```

---

## 3. RTSP YOLO

Окно читает 2 камеры, детектит катушку/погрузчик/человека и пишет переходы в `dbo.ReelTransitions`.

```bat
@echo off
title RTSP YOLO CPU SAFE

cd /d D:\Desktop\RFID_KPP-main\RTSP
..\venv64\Scripts\activate

if not exist recordings mkdir recordings

set RFID_DB_CONNECTION=Driver={ODBC Driver 18 for SQL Server};Server=<SQL_SERVER>;Database=<SQL_DATABASE>;Uid=<SQL_USER>;Pwd=<SQL_PASSWORD>;Encrypt=yes;TrustServerCertificate=yes;
set RFID_DB_LOG_TABLE=ReelTransitions

set RFID_MODEL_PATH=runs\detect\rfid_forklift_reel2\weights\best.pt
set RFID_CONFIDENCE_THRESHOLD=0.25
set RFID_IOU_THRESHOLD=0.45
set RFID_ENABLE_DETECTION=True

set RFID_RTSP_0=rtsp://<CAMERA_USER>:<CAMERA_PASSWORD>@10.192.2.11:554/Streaming/Channels/101
set RFID_RTSP_1=rtsp://<CAMERA_USER>:<CAMERA_PASSWORD>@10.192.2.12:554/Streaming/Channels/101

set RFID_MASK_ENABLED=True
set RFID_MASK_0=D:\Desktop\RFID_KPP-main\RTSP\Mask_0.jpg
set RFID_MASK_1=D:\Desktop\RFID_KPP-main\RTSP\Mask_1.jpg

set RFID_SAVE_IMAGE_ON_TRANSITION=True
set RFID_IMAGE_QUALITY=70
set RFID_IMAGE_MAX_WIDTH=320
set RFID_IMAGE_MAX_HEIGHT=240

set RFID_LOG_DETECTIONS=True
set RFID_LOG_THROTTLE_MS=1500
set RFID_CSV_LOG_PATH=recordings\detections_log.csv

set RFID_REEL_TRACKING_ENABLED=True
set RFID_REEL_CLASS_NAME=cable_reel
set RFID_TRANSITION_WINDOW_SEC=30.0
set RFID_REEL_DISAPPEAR_SEC=5.0
set RFID_REEL_NEARBY_THRESHOLD_PX=300
set RFID_REEL_TRANSITION_LOG_PATH=recordings\reel_transitions.csv
set RFID_TRACK_MERGE_DISTANCE_PX=100
set RFID_EVENT_COOLDOWN_SEC=2.0

set RFID_WINDOW_WIDTH=320
set RFID_WINDOW_HEIGHT=240

python RTSP_yolo_DB_v2.py
```

---

## 4. KPP Aggregator v2.4

Главное окно логики. Склеивает RFID + видео + СКУД + 1С + Warehouse и пишет итог в `dbo.KPP_ReelEvents`.

Новая логика:

```text
1. 1С-метки ищутся в окне ±24 часа.
2. `Warehouse.Ids` и `Warehouse.SeriesNumber` обязательны; `Warehouse.Tag` может быть `NULL`/пустым.
3. Связь склада с КПП ищется строго по приоритету: `Tag`, затем `Ids -> RfidTags.Tag`, затем однозначный `SeriesNumber -> RfidTags.Tag`.
4. Пустые `Tag` никогда не сравниваются друг с другом. Неоднозначный `SeriesNumber` получает статус `AMBIGUOUS_SERIES` и автоматически не склеивается.
5. Если КПП-событие есть — оно дополняется складом и методом связи `TAG`/`IDS`/`SERIES`.
6. Если катушка появилась на складе, но КПП её не увидел — создаётся событие `WAREHOUSE_ONLY` в `KPP_ReelEvents`.
7. `WAREHOUSE_ONLY` повторно сверяется с поздними данными 1С/RFID в течение 7 суток; при нахождении КПП реальное событие обогащается, а синтетическая строка помечается `SUPERSEDED_BY_KPP` и больше не считается катушкой.
```

Production-запуск выполняется через `RUN_RFID_KPP_FINAL.cmd`; он применяет миграцию схемы `3.4.5` и запускает `kpp_aggregator_v3_warehouse_v3.py` вместе с `kpp_reel_dashboard_v3_fixed.py`.

```bat
@echo off
title KPP Aggregator v2.4 Warehouse

cd /d D:\Desktop\RFID_KPP-main\KPP
..\venv64\Scripts\activate

set KPP_CONN_STR=DRIVER={ODBC Driver 18 for SQL Server};SERVER=<SQL_SERVER>;DATABASE=<SQL_DATABASE>;UID=<SQL_USER>;PWD=<SQL_PASSWORD>;Encrypt=yes;TrustServerCertificate=yes;
set KPP_TASK_CONN_STR=DRIVER={ODBC Driver 18 for SQL Server};SERVER=<SQL_SERVER>;DATABASE=<SQL_DATABASE>;UID=<SQL_USER>;PWD=<SQL_PASSWORD>;Encrypt=yes;TrustServerCertificate=yes;

set KPP_RFID_TABLE=dbo.RFID_Tags
set KPP_VIDEO_TABLE=dbo.ReelTransitions
set KPP_SKUD_TABLE=dbo.RusGuardLogs
set KPP_TASK_TABLE=dbo.RfidTags
set KPP_EVENT_TABLE=dbo.KPP_ReelEvents
set KPP_STATE_TABLE=dbo.KPP_RuntimeState

set KPP_TASK_ID_COL=Id
set KPP_TASK_DT_COL=Dt
set KPP_TASK_TAG_COL=Tag
set KPP_TASK_DOCIDS_COL=Ids

set KPP_TASK_LOAD_MODE=LOOKBACK
set KPP_TASK_LOOKBACK_HOURS=24
set KPP_TASK_MATCH_WARN_DELTA_HOURS=24
set KPP_PENDING_RECHECK_HOURS=24
set KPP_RECHECK_DELAY_SEC=300
set KPP_MAX_RECHECK_COUNT=288

set KPP_WAREHOUSE_ENABLED=1
set KPP_WAREHOUSE_TABLE=dbo.Warehouse
set KPP_WAREHOUSE_LOAD_MODE=LOOKBACK
set KPP_WAREHOUSE_LOOKBACK_HOURS=24
set KPP_WAREHOUSE_MATCH_WINDOW_HOURS=24
set KPP_WAREHOUSE_ONLY_GRACE_MINUTES=10

set KPP_FULL_REBUILD_ON_START=0
set KPP_CONTINUE_LIVE_AFTER_REBUILD=1

set KPP_POLL_INTERVAL_SEC=3
set KPP_TASK_RELOAD_INTERVAL_SEC=30
set KPP_ENRICH_INTERVAL_SEC=60
set KPP_STATUS_INTERVAL_SEC=30

set KPP_RFID_DISAPPEAR_TIMEOUT_SEC=35
set KPP_SESSION_MAX_DURATION_SEC=900
set KPP_PRINT_REPORTS=1

python kpp_1_reliable_v2.4_full_rebuild.py
```

---

## 5. Web Dashboard v2.7

Веб-панель: журнал событий, карточки, отчёт XLSX, помощник-скрепка через LM Studio.

Адреса после запуска:

```text
http://127.0.0.1:5050
http://172.31.64.110:5050
```

```bat
@echo off
title RFID KPP Web v2.7

cd /d D:\Desktop\RFID_KPP-main\web
..\venv64\Scripts\activate

set KPP_WEB_HOST=0.0.0.0
set KPP_WEB_PORT=5050
set KPP_WEB_DEBUG=0
set KPP_WEB_REFRESH_SEC=10
set KPP_WEB_DEFAULT_LIMIT=200
set KPP_WEB_MAX_LIMIT=1000
set KPP_WEB_SUMMARY_HOURS=24
set KPP_WEB_CHART_DAYS=3

set KPP_WEB_DB_CONNECTION=DRIVER={ODBC Driver 18 for SQL Server};SERVER=<SQL_SERVER>;DATABASE=<SQL_DATABASE>;UID=<SQL_USER>;PWD=<SQL_PASSWORD>;Encrypt=yes;TrustServerCertificate=yes;
set KPP_TASK_TABLE=dbo.RfidTags
set KPP_WAREHOUSE_TABLE=dbo.Warehouse

set KPP_AI_BASE_URL=http://spider-nest.mkm.lan:49572
set KPP_AI_FALLBACK_BASE_URL=http://172.31.0.153:49572
set KPP_AI_MODEL=openai/gpt-oss-20b

python kpp_reel_dashboard_v2.7_ru.py
```

---

## Проверка Warehouse в KPP_ReelEvents

```sql
SELECT TOP 50
    EventId,
    SourceTag,
    Task1CId,
    Task1CDocIds,
    TaskMatchType,
    FirstSeen,
    LastSeen,
    SessionCloseReason,
    FinalDirection,
    ConfidencePct,
    ConsensusCode,
    TransportMode,
    WarningFlags,
    EvidenceJson
FROM dbo.KPP_ReelEvents
WHERE 
    SessionCloseReason = 'WAREHOUSE_ONLY'
    OR TaskMatchType LIKE '%WAREHOUSE%'
    OR EvidenceJson LIKE '%warehouse%'
ORDER BY EventId DESC;
```

## Проверка отчёта

В web нажать:

```text
Скачать отчёт → выбрать дату от / до → предпросмотр → Скачать XLSX
```

В отчёт попадают только подтверждённые события:

```text
- есть RFID;
- есть связь с 1С;
- есть Warehouse;
- событие прошло через КПП, не просто мусорная RFID-метка.
```

## Проверка помощника

В web нажать скрепку справа снизу и спросить, например:

```text
Покажи проблемные события за сегодня
```

или в карточке события:

```text
Почему система решила, что это выезд?
```
