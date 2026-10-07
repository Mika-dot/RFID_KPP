# RFID_KPP / «Периметр»: передача работ по HA другой нейронке

> **Последнее подтверждение завода — 06.10.2026 18:54 МСК:** на всех3 runtime
> `1c7930912ad84e8f205cf16fbdd715c76c9eb74e`, protocol2, physical epoch264.
> Все5 служб ready в7 стабильных снимках; резервы prepared/nonfaulted,
> maintenance=false, controller Comparator valid.
> [Подтверждение запуска](deploy/ha/DEPLOYED_STATUS_2026-10-06.md).
> Оба полных takeover/failback ранее приняты на f5. Повторный запуск не нужен.
> Код содержит установленные RFID evidence-hotfix и новую защиту updater;
> принятие нового runtime на узлах не подтверждено. Исходник без HA:
> `archive/perimeter-original-no-ha-2026-10-07`, exact `49da7e9ba61cfb843f7fa672e85193c2eae285f7`.
> [Уточнения приёмки от 07.10](deploy/ha/ACCEPTANCE_2026-10-07.md).
> Продолжение в отдельной ветке, без новой заводской установки:
> [Warehouse/направление/identity](deploy/ha/RECONCILIATION_CONTINUATION_2026-10-07.md).
> Разделы ниже сохраняют историю первоначальной передачи; их HA OFF/old SHA
> статусы не описывают текущие машины и не являются командами для нового включения.

Дата передачи: **6 октября 2026**. Время событий ниже, где оно известно, — **МСК (UTC+3)**.
Документ составлен по исходникам GitHub, выводам пользователя в текущем чате,
загруженному диагностическому логу и восстановленному контексту отдельной ветки разговора.

## 1. История первоначальной передачи до успешного ввода HA

**Трёхузловое резервирование не принято в эксплуатацию.** Код написан, опубликован
и проверен regression-тестами, но устойчивый активный physical с пятью готовыми
сервисами и двумя резервами не подтверждён. Первое включение и следующий hotfix
не довели систему до этого состояния.

В отдельной ветке разговора **05.10.2026 в 20:28 МСК** пользователь подтвердил
возврат старого запуска: `HA_DISABLED`, все пять legacy-сервисов `true`,
`LEGACY_RESTORED_HA_DISABLED`. Это последняя найденная отметка отката с точным временем.
Однако приведённые в текущем чате выводы установки `d898dbc...` не содержат времени:
их порядок относительно отката независимо не установлен. **Текущее состояние машин
на 06.10.2026 не проверено.** Не выводить его из порядка сообщений между чатами.
Нужно заново прочитать SQL enable/lease и фактические процессы, прежде чем что-либо запускать.

Последняя просьба пользователя: **сохранить всё необходимое в GitHub и этот файл
передачи**, а не проводить ещё одно включение. В рамках передачи машины не менялись.

| Что | Подтверждённая стадия | Что это не подтверждает |
|---|---|---|
| HA-агенты и конфигурация трёх узлов | Установлены; связь и пассивная подготовка проверялись | Работа полного стека на каждом узле |
| Первый cutover на `0c573bc...` | HA включился; lease переходил между узлами | Устойчивое healthy-состояние physical |
| Hotfix `d898dbc...` | Установлен на всех трёх узлах по выводу пользователя | Успешная эксплуатация; готовность двух VM не достигнута |
| Protocol 2, migration 004, диагностика | Подготовлены в `53ab6f7...`; CI прошёл | Установка на заводе или применение 004 к production SQL |
| Возврат legacy | В отдельном чате подтверждены HA OFF и пять healthy-сервисов | Текущее состояние Guardian и SHA всех машин |
| Финальное испытание HA | **Не выполнено / нет подтверждения** | Нельзя заявлять «горячий резерв работает» |

## 2. Где находится весь код

Репозиторий: <https://github.com/Mika-dot/RFID_KPP>.

**Рабочая ветка этой задачи: `feature/perimeter-ha-guardian`.**
На момент подготовки передачи она содержала 24 коммита поверх `main`; полный
HA-код, установщики, SQL-миграции, тесты и runbook уже опубликованы в ней.
Этот файл и ссылки из README добавляются отдельным коммитом документации.

| Назначение | Exact SHA |
|---|---|
| `main`, production baseline на момент передачи | `49da7e9ba61cfb843f7fa672e85193c2eae285f7` |
| Последний подготовленный **runtime** HA | `53ab6f7ce502dd3f8b1cf4ac5e9f74bd67721007` |
| Предыдущий runtime: protocol 2 | `79f79e7ad0836a86808d05b543f14fb9be84e940` |
| Реально установленный hotfix из присланного вывода | `d898dbc05feeb3157304d0c34cca344514b59b38` |
| Runtime первого включения | `0c573bc708f2906c240af64a23e0f6042006a669` |
| Инструмент первого включения | `fba98e41ea087f2f42b0d44d77fe9eab1d0eb4f9` |

Получить исходники для продолжения можно без доступа к production:

```bash
git clone --branch feature/perimeter-ha-guardian https://github.com/Mika-dot/RFID_KPP.git
cd RFID_KPP
git log -5 --oneline
```

Для разбора последнего runtime использовать exact SHA `53ab6f7...`; более новый
коммит с этим документом не означает новую установленную версию. `main` остаётся
production baseline. HA-ветка — источник незавершённой работы, а не подтверждённый
production release. Слияние в `main` — отдельное действие: агенты предназначены
для автообновления из `main`, а SQL migration автоматически не применяется.

## 3. Требования пользователя и существующие контракты

- Исполнитель выбирается строго: **physical (1) → perimetr (2) → comparator (3)**.
  Comparator — основной контроллер и последний исполнительный резерв; Perimetr —
  запасной контроллер. Текущее владение контроллером всегда подтверждается SQL lease.
- LLM не выбирает ведущего и не выдаёт произвольные shell-команды. Она используется
  для ремонта остановленного узла через ограниченный набор инструментов. Модели
  и LM Studio задаются существующей конфигурацией; планировались `gpt-oss-20b`
  и `qwen-vl-8b`. Потерявший lease контроллер должен отбросить запоздалый ответ модели.
- **Не требовать проведения контрольной RFID-метки**: пользователь прямо запретил
  затрагивать учёт 1С таким тестом. Проверенное чтение тестовой метки не заявлено.
- Не изменять данные или механизм обмена 1С ради readiness. Входы
  `dbo.Warehouse` / `dbo.RfidTags` не входят в HA output fencing.
- Сохранить действующую Warehouse/business-логику: nullable `Warehouse.Tag`,
  приоритет identity `TAG → IDS → SERIES`, запрет неоднозначного SERIES,
  сохранённый `WarehouseId`, `WAREHOUSE_ONLY`, поздний recheck 168 часов,
  `SUPERSEDED_BY_KPP`, promotion физического `UNKNOWN_RFID` по `RfidReadCount>0`.
- Существуют Zabbix, Grafana, Sentry и health-порты `18101–18105`.
  Не пересоздавать мониторинг и не возвращать экспериментальный `perimeter.master`.
- UHFAPI.dll — существующая Windows DLL. На Ubuntu используется Wine с 32-bit
  Windows Python и мостом; пассивная загрузка DLL не доказывает захват TCP-считывателя
  после отказа другого узла.
- Сохранять durable SQLite spools, исходные UUID/timestamps, business cursors,
  активные sessions и сохранённые fault latch. Сброс этих данных не является ремонтом HA.

Подробный production/business baseline: [README.md](README.md).
Подробный технический HA runbook: [deploy/ha/README.md](deploy/ha/README.md).
Старые разделы runbook про первое включение описывают исторические процедуры;
их предусловия сейчас необходимо заново проверить.

## 4. Узлы, пути и процессы

Адреса и пути ниже взяты из конфигурации/выводов предыдущих запусков. При следующем
подключении фактические `node.json`, release record и process identity — источник истины.

| Параметр | physical | perimetr | comparator |
|---|---|---|---|
| ОС | Windows | Ubuntu | Ubuntu |
| IP | `172.31.0.188` | `172.31.0.134` | `172.31.0.192` |
| HA source | `D:\PerimeterHA\source` | `/opt/perimeter/source` | `/opt/perimeter/source` |
| node config | `D:\PerimeterHA\node.json` | `/etc/perimeter/node.json` | `/etc/perimeter/node.json` |
| state | `D:\PerimeterHA\state` | `/var/lib/perimeter` | `/var/lib/perimeter` |
| Агент | Scheduled Task `PerimeterGuardian`, SYSTEM | systemd `perimeter-guardian` | systemd `perimeter-guardian` |
| 64-bit Python | `D:\Desktop\RFID_KPP-main\venv64\Scripts\python.exe` | `/opt/perimeter/venv/bin/python` | `/opt/perimeter/venv/bin/python` |
| Guardian API | `http://172.31.0.188:18200` | `http://172.31.0.134:18200` | `http://172.31.0.192:18200` |
| Приоритет executor | 1 | 2 | 3 |

На physical старый рабочий каталог: `D:\Desktop\RFID_KPP-main`.
Один из использованных 32-bit Python: `RFID_readers\Версия (БД)\venv310_32\Scripts\python.exe`
внутри него. Точный интерпретатор сверять по `node.json`; не создавать новый наугад.
Новый Guardian может использовать старый venv. **Путь argv[0] к этому venv не делает
процесс legacy**: различать роль по полному entrypoint, node identity, PID и creation time.

На VM используются `/opt/perimeter/releases`, Wine prefix `/var/lib/perimeter/wine32`,
Windows Python `/opt/perimeter/python32/python.exe`, DLL
`/opt/perimeter/source/RFID_reader_v4/UHFAPI.dll`; фактические пути сверить с окружением.
Spools, заданные для VM: `/var/lib/perimeter/rfid_spool_v4.sqlite` и
`/var/lib/perimeter/video_spool_v3.sqlite`. Перенос root может поменять относительные
пути spools, поэтому hotfix обновляет source на месте.

В state искать `release.json`, `repair-verification.json`, `operator-maintenance.json`,
`upgrade-backups/`, worker logs `logs/<Service>.log` и HA audit JSONL.
Это локальное состояние; наличие исходника файла в Git не подтверждает его создание на узле.

| Сервис | Readiness port | HA entrypoint |
|---|---:|---|
| RfidReader | 18101 | `deploy/monitored_rfid_recovery_v2.py` |
| RusGuardSync | 18102 | `deploy/monitored_rusguard.py` |
| Yolo | 18103 | `deploy/monitored_yolo.py` |
| Aggregator | 18104 | `deploy/monitored_aggregator.py` |
| WebDashboard | 18105 | `web/kpp_reel_dashboard_v3_fixed.py` |

Web application: port `5050`. Readiness: `/health/ready` каждого сервиса.
HTTP 200 + `status=ok` — ready; HTTP 503/degraded — зависимость не готова;
отсутствие ответа может означать падение процесса. `/health` сам по себе не доказывает readiness.

Guardian `/status` требует Bearer token. Protocol 2 добавляет read-only `/diagnostics`
и изменяющий состояние `/maintenance`. Старый установленный агент может не иметь новых endpoints.

### Где лежат секреты — без их значений

Windows private bundle: `D:\PerimeterHA\transfer-private\environment.local.json`.
Windows helper: `deploy/ha/windows_tool.py`.
Linux environment: `/etc/perimeter/environment`; parser —
`deploy/ha/environment_tool.py:read_generated_environment`.
**Этот systemd environment нельзя `source` как Bash**: синтаксис escaping отличается.
Legacy config: локальный `deploy/config_v3.cmd`, отсутствующий в публичном Git.

Пароли, HA token, SQL/RTSP connection strings и private bundle в эту передачу не включены.
У пользователя есть локальные секреты; их нужно использовать на машине, не публиковать
целиком конфиг или вывод окружения. В старой истории Git уже встречались credentials;
не копировать их в новую документацию и не считать их актуальными.

## 5. Что произошло при первом cutover

### Пассивная подготовка

VM прошли предварительные обновления до `0c573bc...`. Были исправлены накопление
SQL-сокетов Guardian, перенос production environment, загрузка DLL через Wine,
чистая остановка cgroup/process tree и ограничения исходного Windows checkout.
Эти исправления находятся в HA-ветке. Пассивный preflight выполняет проверки
SQL/файлов/interpreters/SDK, но не запускает полный business stack и не подключается
к считывателю для inventory. `prepared=true` не означает `healthy=true`.

### Первое включение

`activate_initial.py` из `fba98e41...` проверил Comparator и передал контроллер
с Perimetr. Первая попытка на physical завершилась `INITIAL_HA_FAILED URLError`.
После локальной правки обращения на `http://127.0.0.1:18200` cutover дошёл до:

```text
LINK_OK physical
LINK_OK perimetr
LINK_OK comparator
CUTOVER_PRECHECK_OK
LEGACY_TREE_STOPPED 704 taskkill_exit=0
LEGACY_TREE_STOPPED 6504 taskkill_exit=0
LEGACY_TREE_STOPPED 7364 taskkill_exit=0
LEGACY_TREE_STOPPED 10180 taskkill_exit=0
LEGACY_TREE_STOPPED 10236 taskkill_exit=0
HA_ENABLED
```

Далее owner переходил `physical / epoch 2 → perimetr / epoch 4 → comparator / epoch 6`.
`healthy_samples` всё время оставался 0. В финальном отчёте:

- physical и perimetr: passive, prepared, **faulted**, `services={}`;
- comparator: active, **healthy=false**, lease/controller valid;
- RFID и YOLO на comparator отвечали ok;
- RusGuard: source/destination database unavailable;
- Aggregator: недоступен; Web: aggregator/database unavailable;
- итог: `PHYSICAL_STEP_FAILED HA is enabled but full physical operation was not confirmed`.

Старые writers были остановлены; при этой ошибке HA оставался включённым.
Историческое сообщение `KEEP_GUARDIANS_RUNNING_DO_NOT_START_LEGACY` относится
к этому моменту. Более поздний подтверждённый откат описан отдельно ниже.

## 6. Причины, найденные в диагностике, и hotfix d898dbc

Из загруженного файла диагностики от 05.10.2026 установлены три разных симптома:

1. SQL error **15664**: нельзя повторно задать readonly SESSION_CONTEXT
   `perimeter_node` на переиспользованном ODBC connection.
2. Windows WebDashboard: **UnicodeEncodeError**, вывод Unicode через cp1251 pipe.
3. SQL error **51001**: `Perimeter write fenced: no current lease`, в том числе
   в Aggregator/RusGuard; активный стек не удерживал рабочую lease.

Hotfix `d898dbc05feeb3157304d0c34cca344514b59b38` исправил первые два механизма:

- `guardian/fencing.py` отключает `pyodbc.pooling` до первого worker connection;
  readonly node/epoch и проверка SQL lease сохраняются;
- `guardian/processes.py` задаёт workers `PYTHONIOENCODING=utf-8`, `PYTHONUTF8=1`;
- добавлены stopped in-place installer `repair_activation_runtime.py` и regression-тесты.

Установщик этого **старого** релиза имел SHA256:
`1ff8a682113c56bbd8add7661a3c51801f67f3a1da1040ddb640c33c23f58d9c`.
Пользователь скачал pinned файл, проверил hash и запустил на всех трёх узлах.
На каждом прошли 9 candidate tests, quarantine/stop/install. Это подтверждает
установку именно `d898dbc...` во время тех запусков.

| Узел | Результат после установки | Backup |
|---|---|---|
| physical | `HOTFIX_NODE_READY physical`: passive, prepared=true, faulted=false | `D:\PerimeterHA\state\upgrade-backups\82b5ef2de99b4b4f85f8bb8580a23ca1` |
| comparator | Временно active; затем faulted и `HOTFIX_FAILED` | `/var/lib/perimeter/upgrade-backups/5ceebe536ef44ecfb98da03923409743` |
| perimetr | Временно active; затем faulted и `HOTFIX_FAILED` | `/var/lib/perimeter/upgrade-backups/b3ea60832db24b0895e192d81b5aec7e` |

На двух VM RusGuardSync и Yolo иногда были true, но RfidReader, Aggregator и
WebDashboard в присланных `HOTFIX_WAIT` оставались false. Perimetr не дошёл до
проверки полного кластера, несмотря на переданный `--wait-cluster`.
**Hotfix исправил код, но не восстановил всю работу.** Passive readiness physical
не доказывает запуск его пяти сервисов. В этих выводах нет новых worker tracebacks:
по одним boolean нельзя установить, осталась ли ошибка 15664 либо появилась другая.

## 7. Следующее исправление: protocol 2 и controller handoff

### Дефект старой SQL-схемы

В migration 003 output triggers читали `KPP_HA_Lease` с `UPDLOCK,HOLDLOCK`.
Блокировка удерживалась до конца **business transaction**. При долгой обработке
Aggregator она мешает контроллеру продлевать lease: обычный цикл около 2 секунд,
TTL около 15 секунд. Затем writer получает 51001 и стек демотируется.
Это подтверждённый дефект исходников и механизм, согласующийся с ранними логами.
**Не доказано, что он единственная причина неуспеха после d898dbc**: свежих
tracebacks того запуска не получено.

### Изменения в 79f79e7, включённые в 53ab6f7

- `guardian/sql.py`: `FENCING_PROTOCOL=2`, transaction-owned app lock
  `Perimeter.HA.Epoch`. Writer берёт Shared gate; смена owner/epoch и восстановление
  истёкшей lease — Exclusive gate. Продление ещё действующей той же lease меняет
  только ExpiresAt без Exclusive gate. Ошибка захвата lock закрывает доступ.
- `migrations/004_perimeter_ha_epoch_barrier.sql`: все девять output triggers
  получают Shared gate и читают lease с `READCOMMITTEDLOCK`. Сохраняются проверки
  enabled/owner/epoch/expiry/readonly SESSION_CONTEXT. Migration 003 остаётся
  для bootstrap/истории, но её старые trigger definitions нельзя восстанавливать
  поверх protocol 2 без согласованного отката всего контракта.
- `guardian/node.py`: authenticated **GET `/diagnostics`** читает status,
  workers/running/exit codes и очищенные хвосты логов (до 8 KiB на сервис), в том
  числе на активном узле. Не переводит узел в maintenance и не останавливает его.
- Persistent `operator-maintenance.json` удерживает staged агент вне candidacy,
  старта workers и активации обновления. POST `/maintenance` требует точный
  текущий release; включение pause запрещено действующему локальному owner.
  Снятие pause сбрасывает проверку восстановления, а не объявляет узел исправным.
- `guardian/update.py`: после protocol 2 несовместимый auto-update кандидат
  отклоняется. Автообновление не выполняет migration 004.
- Установщик дополнен staging, атомарной заменой девяти триггеров, сохранением
  definitions, реальной SQL lock compatibility проверкой и диагностикой при ошибке.
- SQL integration test держит долгую writer transaction: lease renewal должна
  проходить, а epoch transfer ждать её завершения.

Финальный `53ab6f7ce502dd3f8b1cf4ac5e9f74bd67721007` исправляет ещё одну гонку:
быстрый restart Perimetr мог вернуть ему controller lease до takeover Comparator.
Теперь Perimetr staging требует уже staged physical и Comparator и держит
Perimetr остановленным до подтверждения **valid Comparator controller lease**.
Это относится и к idempotent retry. Установщик сам lease контроллера не продлевает.

**Нет полученных выводов, что этот последний rollout выполнен, что migration 004
применена к production или что появился `HA_ACTIVE_PHYSICAL_RESERVES_READY`.**

## 8. Контракт последнего установщика — не инструкция немедленно запускать

Файл: [deploy/ha/repair_activation_runtime.py](deploy/ha/repair_activation_runtime.py).
Pinned runtime: `53ab6f7ce502dd3f8b1cf4ac5e9f74bd67721007`.
SHA256 файла именно на этом runtime:
`124c0edff26c33578dba770f8032e4335fe00c8761100c6351710725b55403ed`.

В этой версии обязательны exact `--release`, ожидаемый `--node` и один режим:
`--stage`, `--finalize` или `--diagnose`. Старый вызов без режима и `--wait-cluster`
больше не соответствует CLI. Это другой installer, несмотря на одинаковый путь.

Уже предложенный порядок для **включённого HA** был:

1. physical `--stage` из Administrator PowerShell;
2. comparator `--stage` через sudo;
3. perimetr `--stage` через sudo;
4. physical `--finalize`.

`--stage` проверяет checkout/release state/candidate tests, на physical — заранее
настроенный SQL login с ALTER на всех девяти таблицах. Затем quarantines/fences
узел через контроллер, подтверждает native stop, сохраняет source/state backup,
обновляет source на месте, пишет protocol minimum и persistent pause, поднимает
агент. `RUNTIME_STAGED` означает только завершённую установку с pause.
Допустимые прежние release для обновления: `0c573bc...`, `d898dbc...`, `79f79e7...`;
release record должен совпадать с checkout, без отдельного pending/previous trial.

`--finalize` требует свежие одноимённые агенты на всех трёх узлах, один exact SHA,
protocol 2, pause и пустые workers. После истечения старой executor lease
заменяет девять триггеров одной admin SQL transaction с backup definitions;
DDL ошибка откатывает транзакцию. Проверка `SQL_EPOCH_BARRIER_CHECK_OK` использует
реальные соединения, сохраняет ExpiresAt (`SET ExpiresAt=ExpiresAt`) и не вставляет
бизнес-данные. Снятие pause запускает независимую preflight verification.
Retry после committed migration умеет продолжить unpause/проверку.

Финальная проверка ждёт до 600 секунд: **семь подряд** хороших измерений через
10 секунд (не менее 60 секунд), одна valid physical lease с неизменным epoch,
все пять physical-сервисов ok, свежие passive prepared / not-faulted VM и valid
Comparator controller. Только тогда печатается `HA_ACTIVE_PHYSICAL_RESERVES_READY`.

**Ограничение текущей стадии:** quarantine/staging и первая migration требуют
`HA enabled`. Установщик **не реализует переход от восстановленного legacy / HA OFF**
к protocol 2. После отката нельзя просто выполнить эту последовательность:
потребуется отдельно подготовить и проверить процедуру для текущего HA OFF,
сохраняющую рабочие legacy services до согласованного cutover. Старый
`activate_initial.py` также привязан к `0c573bc...`; не использовать его поверх
`d898dbc...`/protocol 2 путём удаления проверок.

`--diagnose` читает диагностику до mutating стадий. Но endpoint `/diagnostics`
нужен protocol 2 agent: у старого runtime возможен 404. В этом случае читать
старые `/status`, native worker logs и process exit codes; не выкатывать новый
agent только ради получения отчёта без разбора текущего режима.

## 9. Что известно об откате и чего нет в Git

Отдельная ветка разговора подтверждает local rollback helper:
`D:\PerimeterHA\return-legacy-tonight.py`. Перед успешным выполнением пользователь
добавил фильтр имени процесса перед чтением `p.cmdline()`:

```python
if p.name().lower() not in ('python.exe', 'pythonw.exe'):
    continue
args = p.cmdline()
```

После этого в 20:28 МСК подтверждено:

```text
HA_DISABLED
LEGACY_TREE_STOPPED 2372 taskkill_exit=0
RfidReader=True RusGuardSync=True Yolo=True Aggregator=True WebDashboard=True
LEGACY_RESTORED_HA_DISABLED
```

Строка с пятью сервисами выше — нормализованная запись результата, не побайтовая
копия оригинального JSON. Пользователь получил указание оставить открытыми
пять окон сервисов. Из восстановленного контекста известно, что helper останавливал
Windows Guardian process tree/задачу и запускал legacy `RUN_*_V3.cmd`.

**Полный текст helper, pinned URL/hash и точные действия отката на обеих VM
не доступны в переданных исходниках.** В опубликованном дереве runtime `53ab6f7...`
этого файла нет. Здесь он не восстановлен по догадке. Следующему инженеру нужно
прочитать существующий локальный файл и сохранить очищенный исходник/точный backup,
прежде чем повторять или модифицировать rollback. Указание stop из старого лога VM
не доказывает, что она была остановлена именно во время этого отката.

Наличие сообщения `HA_DISABLED` не доказывает, что agents остановлены или больше
не обновляются. Их task/systemd, workers, current release и SQL controller lease
после rollback отдельно не зафиксированы. Нельзя запускать legacy при HA ON либо
неизвестной SQL lease; перед любым новым cutover нужно подтвердить режим и
отсутствие конкурирующих writers, а не отключать fencing для обхода ошибок.

## 10. Проверки, которые действительно выполнены

| Проверка | Результат и границы |
|---|---|
| Native candidate tests hotfix d898dbc | На каждой машине 9 tests, OK; установка прошла; это не full-stack readiness |
| Локальные focused checks последнего runtime | 80 всего: **74 passed, 6 skipped** (real SQL integration); partial source snapshot |
| Совместимость синтаксиса | Изменённые Python-модули проверены для Python 3.10 |
| GitHub HA regression на 53ab6f7 | **success**, проверено 06.10.2026 |
| GitHub Warehouse regression на 53ab6f7 | **success**, проверено 06.10.2026 |
| GitHub Observability regression на 53ab6f7 | **success**, проверено 06.10.2026 |
| Real SQL Server integration protocol 2 | **Не выполнена в локальном окружении**; SQL Server отсутствовал |
| Последний native staging candidate suite | В коде предусмотрено 20 runtime tests; полученного native результата этой версии нет |
| Physical active + два подготовленных пассивных резерва на final runtime | **Не подтверждено** |
| Реальный RFID takeover, RTO, длительная устойчивость | **Не подтверждено** |

CI подтверждение exact runtime:

- [Perimeter HA regression](https://github.com/Mika-dot/RFID_KPP/actions/runs/37350045646)
- [Warehouse regression](https://github.com/Mika-dot/RFID_KPP/actions/runs/37350045591)
- [Observability regression](https://github.com/Mika-dot/RFID_KPP/actions/runs/37350045550)

Не называть 74 локальных проверки полным suite репозитория: часть файлов была
только снимками для расследования; локально не было psutil/native Wine/SQL Server.
GitHub success — отдельное подтверждение CI, тоже не production integration.
HA workflow запускает полный unittest discovery и compileall на Ubuntu и Windows;
на Windows дополнительно разбирает PowerShell scripts. В CI нет заводского
считывателя, камер и production SQL, поэтому зелёный workflow не закрывает эти испытания.
При продолжении запустить нормальный suite из полного checkout с dependencies:

```bash
python -m unittest discover -s tests -v
```

`tests/test_ha_sql_integration.py` — opt-in destructive fixture: создаёт и удаляет
тестовые таблицы. Только **новая отдельная** SQL база с именем `*_ha_test` без
существующих Perimeter tables, через локально заданный `PERIMETER_HA_TEST_SQL`.
Никогда не подставлять production connection. Эти проверки должны подтвердить
renewal при долгой transaction и запрет старого node/epoch.

## 11. Ключевые исходники для продолжения

| Файл / группа | Для чего читать |
|---|---|
| `guardian/policy.py`, `controller.py`, `sql.py` | Приоритеты, controller token, lease renewal, epoch transitions |
| `guardian/node.py` | Prepared/healthy/faulted, independent recovery, pause и диагностика |
| `guardian/fencing.py` | Worker pooling и readonly session identity |
| `guardian/processes.py` | Native workers, Unicode pipes, process identity, worker logs |
| `guardian/probes.py` | Пассивный preflight и пять реальных readiness checks |
| `guardian/repair.py` | Allowlist AI tools, redaction, сохранение fault/recovery evidence |
| `guardian/update.py`, `boot.py` | SHA staging, probation/rollback, протокол SQL, main ancestry |
| `guardian/wine_proxy.py`, `wine_worker.py` | UHFAPI.dll bridge, native/Wine process lifecycle |
| `migrations/003_perimeter_ha.sql`, `004_perimeter_ha_epoch_barrier.sql` | Старые и новые fencing triggers; не смешивать контракты |
| `deploy/ha/repair_activation_runtime.py` | Последний HA-ON installer; stage/finalize/diagnose |
| `deploy/ha/activate_initial.py` | Исторический cutover exact 0c573bc; не повторять на другом runtime |
| `deploy/ha/upgrade_initial_vm.py`, `upgrade_initial_windows.py` | Исторические inactive checkout upgrade, CRLF/cache backups |
| `deploy/ha/environment_tool.py`, `windows_tool.py` | Production environment import без публикации секретов |
| `tests/test_ha_runtime_*.py` | Регрессии pooling, Unicode, остановки/staging/epoch barrier |
| `tests/test_ha_sql_integration.py`, `test_ha_update.py` | Real SQL long-transaction и compatibility guard |
| `common/observability.py`, `deploy/monitored_*.py` | Readiness dependencies и existing business-flow latch |

Полное сравнение работы с production baseline:
<https://github.com/Mika-dot/RFID_KPP/compare/49da7e9ba61cfb843f7fa672e85193c2eae285f7...53ab6f7ce502dd3f8b1cf4ac5e9f74bd67721007>.
Часть больших изменений исторических CMD/Python файлов относится к нормализации
и уборке; смотреть смысл diff, не считать количество строк размером новой HA-логики.

SQL HA control tables: `KPP_HA_Lease`, `KPP_HA_Controller`, `KPP_HA_NodeState`.
Fenced output tables: `RFID_Tags`, `RusGuardLogs`, `ReelTransitions`,
`KPP_ReelEvents`, `KPP_RuntimeState`, `KPP_ActiveRfidSessions`,
`KPP_ProcessingErrors`, `KPP_EventVideoLinks`, `KPP_EventSkudLinks`.
Их nine enabled triggers — необходимое предусловие, а не доказательство новой
версии definitions: прочитать именно содержимое `HA_<Table>` и app-lock contract.

## 12. Что следующей нейронке делать сначала

1. Прочитать этот файл и checkout HA-ветки. Разделить current machine facts,
   присланные исторические logs и подготовленный код; ничего не включать по одному README.
2. Сначала read-only собрать текущее SQL `Enabled`, owner/epoch/expiry, controller
   owner/expiry; `/status` всех доступных agents; Git SHA и `release.json` на каждом
   узле; Scheduled Task/systemd; process tree, entrypoint и creation time;
   готовность пяти legacy/HA-сервисов. Сохранять результаты с временем и node identity.
3. Прочитать локальный rollback helper и actual node/environment config без
   публикации secrets. Подтвердить, где лежат durable spools, pending count,
   business cursors и fault latch. Не сбрасывать их.
4. Для неуспешного d898dbc нужны **новые очищенные tracebacks/exit codes** прежде
   всего RfidReader/Aggregator/Web. Отсутствующий Aggregator и недоступная Web DB
   могут быть следствием, а не отдельной первопричиной. Если доступна read-only
   diagnostics — использовать её; иначе logs и readiness dependencies.
5. Проверить branch tests и real SQL protocol 2 на отдельной test DB. При необходимости
   исправить найденные native ошибки новым commit, сохранив business contracts.
6. Если актуален legacy / HA OFF, подготовить отдельный согласованный переход
   для **этого** режима: текущий final installer HA ON не покрывает его. Сначала
   сделать процедуру конкретной, тестируемой и с сохранением rollback; не
   обходить её guard и не запускать legacy одновременно с включённым HA.
7. Новый factory cutover, migration и merge в main выполнять как отдельную
   задачу после этой передачи. После нового запуска фиксировать exact installed
   SHA, trigger protocol, все пять services и обе reserves. Один `prepared=true`
   или `RUNTIME_STAGED` не завершает работу.

Остаются непроверенными: полная работа physical на final runtime, обе VM с реальным
RFID/Wine takeover и YOLO, деградация/восстановление зависимостей, смена controller,
поведение без LM Studio/SQL, runtime update rollback, delivery pending spools,
измеренный RTO и длительная устойчивость. Общие SQL, считыватель, камеры и
гипервизор/питание VM остаются общими точками отказа. Межузловой репликации pending
SQLite нет; zero RPO не подтверждён.

## 13. Границы этой передачи

На 06.10 сверены опубликованные runtime исходники: все последние изменённые
HA-модули, installer, migration 004 и regression tests находятся в GitHub на
`53ab6f7...`. Локальный каталог расследования не был полным git checkout;
добавлять его как новый `investigation/` в production репозиторий не требуется.
Единственный отличающийся старый снимок Windows initial-upgrade helper уступает
более новому опубликованному исходнику; он не перезаписывает GitHub.

В Git сохранены исходники, test definitions, runbook и эта передача. Полный private
bundle и необработанные логи с секретами не опубликованы. Полный local rollback
helper, свежие machine states и последние post-hotfix tracebacks пока отсутствуют.
Это явные пробелы, которые следующая нейронка должна закрыть фактами.

**Статус передачи: код и контекст сохранены; задача восстановления и приёмки HA
остаётся незавершённой.**
