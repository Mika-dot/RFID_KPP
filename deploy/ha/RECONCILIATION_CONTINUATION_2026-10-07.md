# Продолжение работ: направление, Warehouse и запись 1С

Дата: 07.10.2026. База ветки — `main` exact
`76778494851d03c5c3c5650b40e54986ddcfddd1` (PR6, снимки Web).
Рабочая ветка: `fix/warehouse-evidence-consistency-2026-10-07`.
Код подготовлен для PR; установка на узлах и новое состояние production не заявляются.
Последний подтверждённый заводской runtime остаётся `1c793091...` от 06.10 18:54 МСК.

## Откуда продолжена работа

Исходник без HA уже сохранён в `archive/perimeter-original-no-ha-2026-10-07`
от `49da7e9ba61cfb843f7fa672e85193c2eae285f7`. PR5 перенёс RFID hotfix и
защиту updater в main; PR6 добавил снимки YOLO в Web. Эти работы не повторялись.
Основной предыдущий статус: [ACCEPTANCE_2026-10-07.md](ACCEPTANCE_2026-10-07.md).

## Повторная проверка предоставленных данных

Прочитаны все четыре части `ReelTransitions_202610070935` и текущие события/
video links из `БД(4).zip`. Содержимое video CSV — 2 194 669 648 байт;
CRC32 `7b4c2eff` проверен по всему ZIP member. SHA256 источников и результаты:
[reconciliation_export_2026-10-07.json](evidence/reconciliation_export_2026-10-07.json).

| Проверка | Результат |
|---|---:|
| Текущие события | 61 607 |
| Исходные видео | 17 730 |
| Различные ссылки на видео из events + links | 6 540 |
| Ссылки без исходного видео | 0 |
| Сравнения сохранённого UUID события с исходным видео | 1 202 |
| Несовпадения UUID | 0 |
| RFID-катушки после 06.10 18:54 | 8 |
| Из них NeedRecheck=1 в снимке | 5 |
| Несовпадения текущего video direction у этих восьми | 0 |
| Катушки IN с сохранённым WarehouseId | 232 |
| Из них также имеют старый OUT_CONFIRMED_BY_WAREHOUSE | 172 |
| Известные IN/OUT, изменённые новой read-only проекцией | 0 |

232 — все связки `IN + WarehouseId`; 172 — их подмножество с противоречивым
OUT-флагом. Это расхождение семантики связанного события, а не доказательство
ошибки физического направления: история калибровки камер и точная семантика
момента Warehouse.Dt в архиве не заданы.

В 17 706 видео бинарные данные уже превратились в текст с заменёнными байтами.
UUID, даты, count и хвостовые поля восстановлены только при однозначном разборе;
картинки из такого CSV не восстанавливаются. Это не доказывает повреждение
production VARBINARY. Реальный HTTP кадр остаётся непроверенным.

## Исправление направления

Общая политика вынесена в `common/warehouse_direction.py`:

- Известные IN/OUT и их ConfidencePct сохраняются.
- Warehouse показывает отдельный факт OUT. Для IN ставится
  `WAREHOUSE_DIRECTION_CONFLICT`, неверный `OUT_CONFIRMED_BY_WAREHOUSE` убирается
  из нового результата обработки/отображения.
- `UNKNOWN + ConsensusCode=CONFLICT` остаётся UNKNOWN; склад не разрешает
  конфликт голосов RFID/видео/СКУД и не повышает уверенность до 100%.
- Для отсутствующего направления без явного конфликта сохранён прежний
  Warehouse fallback OUT/100, помеченный `WAREHOUSE_DIRECTION_INFERRED`.
- WAREHOUSE_ONLY сохраняет прежнюю отдельную семантику и не становится RFID
  чтением, не получает чужое видео.

Политика используется при Warehouse enrichment, upsert с прямым Warehouse
evidence и построении Web отчёта. List/detail endpoints проецируют старые
противоречивые флаги при чтении; карточка и HTML отчёт показывают расхождение.
Web не пишет исправления в SQL. SQL schema остаётся 3.4.5; миграций нет.
Массовое переписывание исторических IN в OUT не добавлялось.
Существующий startup fallback теперь исключает явный ConsensusCode=CONFLICT.

Пример 71746: IN, ConfidencePct=70, WarehouseId=4411,
VideoEventId=920649 сохраняются. Отдельный складской факт OUT отображается
с предупреждением; это событие не объявлено эталоном физического направления.

## Исправление подстановки записи 1С

Ранее `event_candidate.task or identity.primary_task` мог подставить задачу 1С
с другой меткой: direct TAG указывает на A, а primary IDS task — на B.
То же происходило в Web отчёте и синтетическом Warehouse-only результате.

`IdentityResolution.task_for_tag` возвращает задачу только при совпадении её
полной нормализованной метки с фактически выбранной меткой. Priority
TAG → IDS → SERIES, nullable Warehouse.Tag, запрет неоднозначного SERIES,
приоритет сохранённого WarehouseId и promotion UNKNOWN_RFID сохранены.
Для существующей связи с неразрешённой меткой primary task другого tag не
подставляется и метод не объявляется ложным MATCH_IDS.

Существующие исторические Task1CId/WarehouseId не очищаются и не переназначаются.
Этот воспроизведённый путь ошибки не объявлен причиной всех прежних 119/36
несовпадений. В этом раунде полный source registry/raw экспорт не прочитан:
получение первого 500-MiB RAR тома вернуло HTTP403; более старые августовские
CSV не использовались как замена октябрьскому снимку.

## Проверки и защита обновлений

Локально Python3.12: **454 tests — 439 passed, 15 skipped**; compileall и
`git diff --check` прошли. Пропуски — существующие проверки, требующие SQL
integration environment или другой платформы. SQL Server/завод не подключались.

Защищённый installed checker выполняет **12** бизнес-проверок вместо 6:
старые RFID causal/latch правила и новые direction/task-tag правила.
Проверены candidates с зелёным собственным suite, которые насильно ставят OUT
или возвращают primary task другой метки: они отвергаются до preflight и
quarantine сохраняется для exact SHA.

Runtime contract — business_generation2 с двумя Warehouse capabilities.
Минимум в release.json повышается после принятия релиза; rollback неудачного
trial остаётся разрешён. Старый установленный checker получает новые правила
только после принятия этого кода. Это защита конкретных чистых функций;
полная проверка SQL adapters, golden replay и differential shadow всех пяти
блоков остаются отдельными этапами.

Новый `tools/audit_reconciliation_export.py` повторяет сверку без SQL, камер,
spools и считывателя. Он проверяет весь ZIP CRC, missing volumes, дубли IDs,
неоднозначный recovery и совпадение video refs/UUID. Пример запуска:

```bash
python tools/audit_reconciliation_export.py \
  --events-csv /exports/KPP_ReelEvents_202610070935.csv \
  --video /exports/ReelTransitions_202610070935.zip \
  --video-links-csv /exports/KPP_EventVideoLinks_202610070935.csv
```

`.z01/.z02/.z03` должны лежать рядом с `.zip`. В результате нет картинок,
RFID tag и персональных полей. Исходные данные не добавлены в Git.

## Что остаётся открытым

1. Принятие нового exact runtime на трёх узлах по новым наблюдениям. Ветка PR
   сама по себе не является production deployment; main в этом раунде не меняется.
2. Семантика физического направления/камер/Warehouse.Dt и проверка старых
   identity по полным source exports. Нельзя автоматически переназначать историю.
3. Завершение пяти NeedRecheck в более свежем снимке; текущий архив не содержит
   будущего результата, искусственная финализация не выполнялась.
4. Постоянный Web endpoint, уведомления/business metrics, полная квалификация
   auto-update/replay/shadow и отдельный read-only BehaviorObserver/drift.
5. Bounded repair/RTO/RPO acceptance по доступным наблюдениям. Power-off/network
   isolation и UHF takeover недоступны; fault injection и synthetic RFID/1C
   записи запрещены условиями текущей работы.

Команды на заводе, SQL grants/migrations, reset latch, изменения monitoring
infrastructure, repair или переключение owner в этом раунде не выполнялись.
