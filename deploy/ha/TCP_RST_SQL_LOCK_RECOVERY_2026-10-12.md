# Подтверждённые отказы считывателя и SQL-зеркала: 12.10.2026

## Заводские свидетельства

Пользователь установил `7ae0283fc237f3a1a2e6806898445f9c18b32a95` на оба
Linux-узла в maintenance, затем `--release-reserves` подтвердил оба receivers.
Диагностика 11:16 МСК: epoch3328, Perimetr active/unhealthy на новом SHA,
Comparator passive/prepared/nonfaulted. Обе verification завершились.
Qualification passed, rejected=false, active probation ещё pending.
Физика остаётся на старом `7677849`; ub22 не обновлён в этом rollout.

Новый пользовательский capture 11:35:24–11:35:44 МСК показал пять пар:
Comparator172.31.0.192 отправляет SYN на reader172.31.128.170:8888,
в ответ немедленно приходит ACK|RST с reader IP. Соединение TCP активно
отвергается. Это согласуется с TCPConnect code=1, но не доказывает конкретную
причину: закрытый порт, single-client ограничение, фильтр с REJECT или
несоответствие endpoint. Отказ не объявляется неисправностью Wine или quorum.
Perimetr за свои25sec не поймал пакетов; без свежей роли это не вывод о сети.

SELECT TOP(0) прошёл для всех шести mirror streams. Точное чтение с сохранённым
watermark воспроизвело events ProgrammingError/42000/native1222:
«Превышено время ожидания запроса на блокировку». Это блокировка SQL,
не доказанная нехватка колонки или права SELECT.

12:15 МСК пользователь сообщил, что связь RFID восстановили. Новый свежий
SDK/business/owner/принятая probation snapshot ещё не получен; причину и
способ аппаратного восстановления пользователь пока не описал.

Из среды агента заводская LAN недоступна; это выводы пользователя,
а не выполненный агентом live poll. Позднейший owner/SHA может измениться
из-за обычной probation; его нужно получать заново.

## Исправления поведения при отказах

- SQL native1222/42000 распознаётся отдельно. Заблокированный SELECT/anchor/fetch
  не обновляет watermark или synced_at. Остальные streams синхронизируются;
  затем MirrorReadBlocked сообщает их имена. Следующая обычная попытка через
  существующий60sec interval повторяет тот же cursor. Другие SQL errors и
  MirrorSourceHistoryChanged остаются ошибками. NOLOCK, сброс истории,
  изменение схемы/изоляции или SQL grants не добавляются.
- Guardian публикует blocked_streams и mirror_sync_blocked/native_code1222.
  Recent events остаются stale при неполной синхронизации.
- В repo UHFAPI.dll экспорт TCPConnect RVA0x1010 возвращает1 после неудачного
  connect, оставляя созданный socket в глобальной переменной0x1000f014.
  TCPDisconnect удаляет critical section, инициализированную только на success.
  Поэтому прямой cleanup в основном процессе не добавляется. Wine bridge
  после nonzero connect завершает изолированный SDK process group и ждёт
  подтверждения остановки. Следующий TCPConnect запускает новый bridge;
  shutdown/stop не запускает его. Старый stdout reader привязан к старой
  queue и не может испортить новый handshake. SQL writer/spools в parent
  не перезапускаются. Исходный code сохраняется; success остаётся подключённым.
  Native Windows adapter этим изменением не меняется.
  Repo DLL SHA256:51bb1ceb7afe3c1249f7fc330c375119b9be1b7e3990212d1fa95c22f6880de9.
  Это устраняет обнаруженную утечку retry path; настройку считывателя и RST
  этим исправлением восстановленными не объявляем.

## Следующий шаг на оборудовании

Из clean checkout нового проверенного SHA на Comparator, **без prepare/apply**:

```bash
sudo /opt/perimeter/venv/bin/python deploy/ha/diagnose_sql_locks.py
sudo /opt/perimeter/venv/bin/python deploy/ha/diagnose_fault.py --release 7ae0283fc237f3a1a2e6806898445f9c18b32a95
```

SQL helper делает bounded SELECT: состояние row versioning, requests/blockers,
открытые sessions и сгруппированные locks событий. Не извлекает SQL text,
business rows или login credentials; нет KILL/ALTER/UPDATE/DBCC, reader socket,
POST, restart или grant. Если отсутствуют DMV permissions, возвращает ошибку
как неизвестное состояние, не выдаёт разрешения самостоятельно. GET diagnosis
одновременно фиксирует актуальные версии, роли и probation.

На физическом Windows проверить только существующие connections:

```powershell
Get-NetTCPConnection -RemoteAddress 172.31.128.170 -RemotePort 8888 -ErrorAction SilentlyContinue |
  Select-Object State,LocalAddress,LocalPort,RemoteAddress,RemotePort,OwningProcess,
    @{Name='Process';Expression={(Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue).ProcessName}}
```

Далее нужен model/utility считывателя: проверить TCP server mode/port8888 и
существующего клиента. Не запускать параллельный SDK/Test-NetConnection,
не делать factory reset/слепой power cycle. Конкретное действие определяем
по модели и фактическому клиенту. Блокирующую SQL transaction тоже сначала
идентифицируем; сессии/транзакции не убиваются наугад.

После восстановления TCP и SQL чтения: qualified update обоих Linux reserves,
один healthy new owner/пять ready services/pending=false у owner, физика,
ub22 Web/Observer/dashboard, полный installation verifier. Новое исправление
не применяется автоматически и не обходит активную lease/probation.
Недельное наблюдение начинается только после готовности всех четырёх машин.

## Проверки и источники

Регрессии: SQL1222 на anchor/fetch сохраняет cursor/cache, другие streams
работают, повтор после снятия блокировки использует прежний cursor; error207
не скрывается. Guardian сообщает blocked stream и stale. Wine retries retire
failed process before spawning another; success не отключается, cleanup не
запускает новый bridge, старый EOF не загрязняет новую queue. Real subprocess/
localhost TCP test даёт три genuine refusals и завершённых процессов, затем
успешное соединение после listen. Это не native vendor/Wine acceptance.
SQL helper tested SELECT-only/parameterization/secret redaction/no implicit grants.
Полный suite и installed-oracle qualification фиксируются отдельно.

- [Microsoft: SQL Server1222](https://learn.microsoft.com/en-us/sql/relational-databases/errors-events/mssqlserver-1222-database-engine-error)
- [Microsoft: диагностика блокировок](https://learn.microsoft.com/en-us/troubleshoot/sql/database-engine/performance/understand-resolve-blocking)

Локально:637 tests,0 failures/errors,15 native/environment skips;622 executed.
Installed oracle7ae0283:27 Golden traces,6 adapter contracts,8 HA models,
0 shadow differences, qualification passed. Compile/diff checks passed.
Exact SHA и Linux/Windows CI публикуются в PR#9.


## Перепроверка замечаний ревью после2fcf016

До следующего apply исправлены четыре сценария:

1. Passive preflight может автоматически начать verification только для
   действительно нового untried candidate с exact qualification passed.
   Стабильный runtime-faulted релиз остаётся в карантине до конкретной repair
   action; DLL load/свободные порты не доказывают исправление workload.
2. Warehouse/RfidTags обновляются не только по новым ID: bounded cyclic
   SELECT в пределах93days повторно получает старые IDs. Refresh cursor
   записывается атомарно вместе с payload, отдельно от ingest watermark.
   SQL1222 сохраняет refresh cursor и старый payload. Полный refresh является
   циклическим, не мгновенным; position/completed_at видны в mirror stats.
3. Peer replica cursors durable в SQLite; passive узлы тоже догоняют peers.
   Новый GET /replica/pending сначала приносит недоставленные inputs, затем
   идёт retained SENT history. После restart курсор не начинается с0.
   Ошибка archive/invalid page не продвигает cursor; replay SQL-spools остаётся
   только у владельца с повторной проверкой lease/epoch. Legacy endpoint
   без pending-first поддерживается через прежний history path.
4. Hardware rollback читает реально зафиксированный epoch после SQL error
   вместо предположения assigned_epoch+1. Если старый lease остаётся valid,
   emergency DENY допускается только для faulted target и live controller;
   epoch/authority проверяются до и после SNMP. Это не emergency UNFENCE.
   Exclusive mapping/authPriv/readback остаются обязательными. Hardware
   fencing не включается и на заводе не испытывается этим изменением.

Регрессии:650 local tests,0 failures/errors,15 environment skips (635 executed).
Новые проверки: unchanged runtime quarantine, repaired registry row below
high-water mark, durable cyclic refresh, HTTP pending-first за125SENT rows,
restart/empty/archive error replica cursor, rollback not committed/lost ack
actual epoch и emergency refusal при смене authority/healthy target.
Installed oracle2fcf016:27 Golden traces,6 adapter contracts,8 HA scenarios,
0 business shadow differences; exact CI и SHA публикуются в PR#9.

Дополнительная integration check: emergency adapter получает controller ID/lease token
действующего Guardian только через private subprocess environment. Дочерний
SqlStore использует тот же token для controller_owned; не создаёт новый claim.
Token не попадает в argv/stdout. Эта связка проверена отдельным тестом.
