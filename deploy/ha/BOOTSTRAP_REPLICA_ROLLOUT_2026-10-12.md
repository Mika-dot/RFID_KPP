# Первый переход к реплицированному релизу: 12.10.2026

## Новое свидетельство с завода

Пользователь выполнил prepare/apply Comparator для `bc42ce0` и получил
`installed=true`. Следующая GET-диагностика показала **возврат на `1c793091`**,
epoch 3237, Comparator active/unhealthy. RFID dependencies: business_flow,
rfid_reader, rfid_tcp; self_heal_restarts=97, source age≈178033sec. YOLO и
RusGuard ready, Aggregator/Web degraded вслед за RFID. Physical и Perimetr
passive/faulted, Perimetr prepared. Это пользовательский runtime output,
а не собственное соединение агента.

Сам возврат версии подтверждён; механизм/причина отката ещё требуют
release record, audit/journal и ошибок SDK. Успешный пассивный installed receipt
не гарантирует завершения последующей активной probation.

## Пропущенный сценарий и исправление

Первый новый узел включал replication и сразу выходил из maintenance.
Обе старые версии не имеют `/replica/put`, поэтому первая запись нового
writer не может получить вторую durable ACK: `ReplicaQuorumUnavailable`.
Это независимо воспроизведено offline; factory rollback именно по этой
причине пока не заявляется. Прежний порядок немедленного запуска одного
нового узла против двух старых непригоден для начального bootstrap.

Новый порядок устанавливает два Linux replica receivers в operator maintenance
**до запуска их business workers**. Peer RPC принимает копии и в maintenance.
После проверки обоих exact SHA/qualification/preflight/replication/endpoints
maintenance снимается штатным API. Controller по-прежнему единственный
назначает lease и запускает writer. Quorum не отключается/не ослабляется,
SQL fault/lease вручную не редактируются, очереди не очищаются.

Обычный apply теперь отказывается до native stop, если ни один другой узел
не сообщает target SHA и replication_enabled. `--hold-maintenance` допускается
только с `--apply --recover-reserve` на пассивном Linux резерве. Возврат
`maintenance_held=true` означает установленный receiver без запуска workers,
не готовый business stack и не законченную probation.

## Диагностика отката и RFID

Из отдельного clean checkout **нового SHA из PR #9**, на Comparator:

```bash
sudo /opt/perimeter/venv/bin/python deploy/ha/diagnose_fault.py --release bc42ce08c98dae7902ba33bee4826b4315e33a1f
```

Фиксируется current/previous/rejected target, состояние трёх узлов,
подробности RFID dependencies, SDK mode/reader address, последние selected
audit events, ошибки RfidReader и selected systemd journal. Только GET,
локальное чтение файлов/journal; отсутствуют SQL calls, POST, процессы SDK,
ещё одно TCP-соединение к считывателю, рестарты и очистка latch. Private
environment загружается локально; secrets/RTSP credentials/IDs/raw tags
редактируются до вывода. После этого устраняем конкретный RFID отказ.

## Два пассивных receiver

Сначала на **пассивном Perimetr** (по последнему снимку он пассивен), затем
на **пассивном Comparator**, из clean checkout нового exact SHA:

```bash
PERIMETER_RELEASE=$(git rev-parse HEAD)
sudo /opt/perimeter/venv/bin/python deploy/ha/release_update.py --plan --release "$PERIMETER_RELEASE"
sudo /opt/perimeter/venv/bin/python deploy/ha/release_update.py --prepare --release "$PERIMETER_RELEASE"
sudo /opt/perimeter/venv/bin/python deploy/ha/release_update.py --apply --recover-reserve --hold-maintenance --release "$PERIMETER_RELEASE"
```

Не запускать apply на активном Comparator. Если он ещё ведущий, дождаться
штатной demotion неисправного owner и получить новую диагностику. Live owner
и lease race блокируются SQL maintenance gate; не обходить их через прямой
grant/UPDATE/kill. Native service restart относится только к пассивному узлу.
Если роль/конфигурация изменилась между prepare/apply, пересобрать receipt.

Оба результата должны иметь `installed=true`, `maintenance_held=true`,
`replica_receiver_ready=true`, `workers_started=false`. Maintenance
сохраняется на диске и при restart; candidate не исполняет активный trial.
До проверки обоих receivers не выполнять отдельный maintenance=false.

## Совместный выход из maintenance

На любой из двух Linux VM из checkout того же exact SHA:

```bash
sudo /opt/perimeter/venv/bin/python deploy/ha/release_update.py --release-reserves --release "$PERIMETER_RELEASE"
```

Helper сначала проверяет весь свежий protocol2 cluster/epoch и **оба**
receiver: exact target SHA, qualification=passed, preflight ok, replication
и fallback enabled, `/replica/stats` доступен и возвращает корректные counts.
Held node должен быть passive. Затем releases штатным API. После частичного
HTTP сбоя повторная команда пропускает уже выпущенный узел и не сбрасывает
его verify timer; каждый retry заново проверяет обе версии/endpoint.
Response `reserves_released=true` ещё не означает healthy owner.

Далее GET diagnosis: один healthy owner на новом SHA, пять ready services,
pending=false у owner после 180sec probation, второй резерв prepared/nonfaulted.
Если RFID по-прежнему не готов, сначала исправить его конкретную причину.
Не удалять persistent business latch ради зелёного индикатора.

После выполнения этих условий — физика с qualified handoff, ub22 Web/Observer/
dashboard и полный installation verifier из
[READY_TO_INSTALL_2026-10-09.md](READY_TO_INSTALL_2026-10-09.md).
Недельный тест и данные за сутки/пять дней начинаются только после готовности
всех четырёх машин; adaptive shadow, auto_update=false, без искусственных
отключений/проходов. Публикация нового helper не восстанавливает завод сама.

## Offline проверки

Реальный HTTP receiver/SQLite test показывает: два legacy peers дают quorum
failure с сохранённым PENDING; добавленный held receiver принимает запись,
обе копии durable, ACK count=2, workers не создаются. Installer tests проверяют
hold без запуска, отказ обычного первого apply, gate второго receiver и retry
после lost release response. Fault diagnosis проверена на секретах/URL/UUID/tag
и сохраняет только выбранные ошибочные строки. Старые Golden/protected бизнес
проверки сохраняются. Exact SHA и Linux/Windows CI — в PR #9.
