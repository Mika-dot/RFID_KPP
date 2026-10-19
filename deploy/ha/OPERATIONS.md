# Инструкция дежурного: проверка и поиск неисправности

Версия инструкции: 19.10.2026. [Состояние при передаче](STATUS_2026-10-19.md).
Команды ниже читают состояние. Действия восстановления находятся в [RECOVERY.md](RECOVERY.md).

## 1. Где выполнять команды

| Надпись в инструкции | Где открыть консоль | Подключение из Linux |
|---|---|---|
| ub22 | `mkm@ub22:~$` | `ssh mkm@172.31.0.97` |
| Perimetr | `mkm@Perimetr:~$` | `ssh mkm@172.31.0.134` |
| Comparator | `mkm@Comparator:~$` | `ssh mkm@172.31.0.192` |
| Physical | Windows PowerShell, «Запуск от имени администратора» | Локальная консоль или установленный удалённый доступ |
| SQL-клиент | SSMS либо другой SQL-клиент, подключённый к рабочему серверу | Это не Bash и не PowerShell |

Копировать только содержимое блока кода, без надписей `mkm@...`, `PS ...>` и знаков `>` из чужого вывода. Пароль sudo вводится локально и не отображается. Отсутствующий файл `/etc/perimeter/gateway.json` на Perimetr — признак запуска инструкции не на той машине: этот файл нужен на ub22.

## 2. Первая проверка: сайт и мониторинг

Открыть в браузере:

- Сайт: http://172.31.0.97:5051/
- Мониторинг: http://172.31.0.97:3000/d/mositlab-perimeter-visual?kiosk

На схеме найти ведущего и оба резерва. Роль не закреплена навсегда за Physical. На резерве пять рабочих служб могут быть остановлены — это штатно.

**На ub22:**

```bash
systemctl is-active perimeter-gateway.service grafana-server.service
curl --noproxy '*' --connect-timeout 3 --max-time 10 -sS -o /dev/null -w 'Gateway live: %{http_code}\n' http://127.0.0.1:5051/health/live
curl --noproxy '*' --connect-timeout 3 --max-time 10 -sS -o /dev/null -w 'Gateway ready: %{http_code}\n' http://127.0.0.1:5051/health/ready
curl --noproxy '*' --connect-timeout 3 --max-time 10 -sS -o /dev/null -w 'Grafana: %{http_code}\n' http://127.0.0.1:3000/api/health
```

Ожидается: две строки `active`, затем HTTP 200 у трёх запросов. HTTP 000 означает, что ответа HTTP нет; это не статус приложения. `live=200, ready=503` означает: Gateway запущен, но не может подтвердить готового ведущего. Не объявлять неисправным Gateway только по этому результату.

## 3. Определить ведущего, версии, службы и состояние копий

**На ub22:**

```bash
python3 - <<'PY'
import json
import urllib.request
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
base = "http://127.0.0.1:19150/perimeter-ha"
with opener.open(base + "/status", timeout=10) as response:
    data = json.load(response)
if not isinstance(data.get("nodes"), dict) or len(data["nodes"]) != 3:
    raise SystemExit("Не получены состояния трёх узлов")
print("MONITOR_STALE:", data.get("stale"))
for name, node in data["nodes"].items():
    mirror = node.get("mirror") or {}
    print(json.dumps({
        "node": name, "role": node.get("role"),
        "reachable": node.get("reachable"), "fresh": node.get("snapshot_fresh"),
        "active": node.get("active"), "healthy": node.get("healthy"),
        "prepared": node.get("prepared"), "faulted": node.get("faulted"),
        "epoch": node.get("epoch"), "release": node.get("release"),
        "detail": node.get("detail"), "services": node.get("services"),
        "business": node.get("business"), "bus": node.get("bus"),
        "update": node.get("update"),
        "mirror_error": mirror.get("error"),
        "blocked_streams": mirror.get("blocked_streams"),
        "mirror_streams": mirror.get("streams"),
    }, ensure_ascii=False, indent=2))
with opener.open(base + "/queues", timeout=10) as response:
    print("QUEUES:", json.dumps(json.load(response), ensure_ascii=False, indent=2))
PY
```

Если команда сообщает timeout или недоступность `:19150`, перейти к разделу 5. Это ещё не доказывает остановку самого Периметра.

### Как читать результат

| Поле | Норма | Что означает отклонение |
|---|---|---|
| `MONITOR_STALE` | false | Сведения наблюдателя устарели; по ним нельзя объявлять систему исправной |
| `reachable`, `fresh` | true | Агент недоступен либо ответ устарел |
| `active` | true/1 ровно у одного узла | Нет ведущего либо несогласованные сведения; проверить SQL и связь |
| `healthy` у ведущего | true/1 | Как минимум одна рабочая проверка не пройдена |
| `prepared` | true/1 на ведущем и готовых резервах | Узел пока не готов принять работу |
| `faulted` | false/0 | Узел исключён из выбора до восстановления |
| `epoch` | одинаковый в согласованном свежем снимке | Расхождение при переключении возможно; повторить после стабилизации |
| `services.*.ok` у ведущего | true для пяти служб | Найти службу и её зависимости |
| `rfid_pending`, `video_pending` | обычно 0, после нагрузки уменьшаются | Рост без доставки — проблема обработки или SQL |
| `cache_state` | ГОТОВА | ОШИБКА, ЗАПОЛНЕНИЕ и НЕТ ДАННЫХ требуют отдельного разбора |
| `state=СВЕЖИЕ` в очередях | Свежая телеметрия | Не подтверждает готовность копии |
| `release` | выбранный программный релиз | Старый SHA после изменения только Markdown допустим |
| `pending=true` | Может сохраняться на пассивном новом резерве | Его runtime ещё ожидает испытания ведущим; не сбрасывать вручную |

Ведущий не обязан быть Physical. Автоматический возврат роли на Physical отключён. Смену ведущего после ошибки или штатного обновления отличать от постоянного переключения между узлами.

## 4. Если не работает одна служба

Следующие запросы выполнять **локально на текущем ведущем**, найденном в разделе 3.

**Если ведущий — Perimetr или Comparator, на нём в Bash:**

```bash
for PORT in 18101 18102 18103 18104 18105; do
    printf '\nPORT %s\n' "$PORT"
    curl --noproxy '*' --connect-timeout 2 --max-time 8 -sS "http://127.0.0.1:$PORT/health/ready"
done
```

**Если ведущий — Physical, на нём в PowerShell:**

```powershell
foreach ($Port in 18101,18102,18103,18104,18105) {
    Write-Host "PORT $Port"
    curl.exe --noproxy "*" --connect-timeout 2 --max-time 8 -sS "http://127.0.0.1:$Port/health/ready"
}
```

| Порт / служба | Сначала проверить |
|---|---|
| 18101 RfidReader | Питание/сеть считывателя, reader_loop, доставку, SQL, local_spool |
| 18102 RusGuardSync | Доступ к исходной и целевой SQL-базам СКУД |
| 18103 Yolo | Камеры, сетевой видеопоток, модель, маски, свежесть кадров |
| 18104 Aggregator | SQL, курсор чтения, очередь повторного сопоставления, исключения в логе |
| 18105 WebDashboard | SQL рабочего сайта, порт 5050, параметры авторизации |

HTTP 401 при открытии корня рабочего сайта `:5050/` может означать запрос авторизации, а не поломку Web. Локальные `/health/ready` проверять отдельно. Недоступность `:5050` на резерве ожидаема.

`business_flow=degraded` с `rfid_stale_with_partial_activity_evidence` — предупреждение: чтения RFID давно не поступали при некоторой внешней активности. Это не доказательство прохода конкретного барабана. Сначала сопоставить фактические перевозки и последние события. `business_flow_latched=true`, неисправность SDK/reader_loop, ошибки доставки и рост pending — отдельные реальные признаки сбоя.

## 5. Если недоступен агент или наблюдатель

**На неисправной Linux VM:**

```bash
sudo systemctl status perimeter-guardian.service --no-pager -l
sudo systemctl show perimeter-guardian.service -p User -p MainPID -p ExecStart -p DropInPaths -p Restart -p UnitFileState
sudo journalctl -u perimeter-guardian.service --since "-20 min" -n 150 --no-pager
df -h / /var/lib/perimeter /opt/perimeter
free -h
```

Вывод `active` подтверждает процесс, но не все пять рабочих служб. Журналы могут содержать служебные данные; передавать их инженеру по внутреннему каналу.

**На Physical в PowerShell администратора:**

```powershell
Get-ScheduledTask -TaskName "PerimeterGuardian" | Select-Object TaskName,State
Get-ScheduledTaskInfo -TaskName "PerimeterGuardian" | Select-Object LastRunTime,LastTaskResult,NextRunTime
(Get-ScheduledTask -TaskName "PerimeterGuardian").Actions | Select-Object Execute,Arguments,WorkingDirectory
Get-Volume | Select-Object DriveLetter,SizeRemaining,Size
```

Задача с долгоживущим Guardian обычно остаётся Running; сама эта надпись не подтверждает готовность RFID. Если задачи нет, перейти к восстановлению регистрации из резервной копии.

**На ub22:**

```bash
systemctl is-active perimeter-gateway.service grafana-server.service mositlab-wallboard.service perimeter-ha-monitor.service
sudo journalctl -u perimeter-gateway.service -u mositlab-wallboard.service -u perimeter-ha-monitor.service --since "-20 min" -n 150 --no-pager
```

Недоступность `perimeter-ha-monitor.service` относится к наблюдателю, а не автоматически к остановке ведущего. Наличие отдельной службы `perimeter-behavior.service` не считается подтверждённым условием эксплуатации.

### Независимая диагностика трёх узлов

Если ub22 не работает, выполнить **на Perimetr или Comparator**:

```bash
sudo python3 -u - <<'PY'
import json
import subprocess
from pathlib import Path
cfg = json.loads(Path("/etc/perimeter/node.json").read_text(encoding="utf-8-sig"))
record = json.loads((Path(cfg["state_dir"]) / "release.json").read_text(encoding="utf-8-sig"))
current = record["current"]
tool = Path(current["root"]) / "deploy/ha/diagnose_cluster.py"
command = [current.get("python") or cfg["python"], "-B", str(tool),
           "--nodes-config", "/etc/perimeter/node.json",
           "--environment", "/etc/perimeter/environment"]
raise SystemExit(subprocess.run(command, timeout=60).returncode)
PY
```

Читает существующие секреты локально, не печатает токен и не меняет роль узла. При отсутствии конфигурации/записи релиза не создавать их пустыми: нужен комплект восстановления.

## 6. Если сайт работает, а копии показывают ошибку

Для каждого узла в разделе 3 посмотреть `mirror_error`, `blocked_streams` и `mirror_streams`.

- `MirrorReadBlocked`, `events`, native 1222 в audit — чтение событий не прошло из-за ожидания SQL-блокировки. Остальные потоки могут синхронизироваться.
- `caught_up=false` — этот поток ещё заполняется; нельзя считать его полным.
- Рост `age_sec` — поток давно не завершал успешное чтение.
- Шесть обязательных потоков: events, warehouse, tasks, rfid, video, skud.
- Настройка хранения 93 дня не подтверждает наличие полной 93-дневной истории.
- Нулевые очереди доставки не подтверждают готовность зеркала: это разные механизмы.

Дежурный фиксирует узел, поток, время, возраст и ошибку. DBA проверяет блокировки именно на сервере/базе, используемых этим потоком. На 19.10.2026 в проверенном Comparator это SRV-SQL4 / 1CTgSend. Фактические назначения задаются частным окружением и могут измениться.

**SQL-клиент, рабочее соединение DBA:**

```sql
SELECT @@SERVERNAME AS sql_server, DB_NAME() AS current_database,
       IS_SRVROLEMEMBER('sysadmin') AS is_sysadmin,
       HAS_PERMS_BY_NAME(NULL, NULL, 'VIEW SERVER STATE') AS can_view_server_state,
       HAS_PERMS_BY_NAME(NULL, NULL, 'VIEW SERVER PERFORMANCE STATE') AS can_view_server_performance_state;

SELECT r.session_id, r.blocking_session_id, r.wait_type, r.wait_time,
       r.wait_resource, r.status, r.command,
       s.host_name, s.program_name, s.open_transaction_count
FROM sys.dm_exec_requests r
LEFT JOIN sys.dm_exec_sessions s ON s.session_id=r.session_id
WHERE r.session_id<>@@SPID AND r.database_id=DB_ID()
ORDER BY r.wait_time DESC;
```

NULL в проверке разрешения не означает наличие права. Требования DMV зависят от версии SQL Server. Пустая таблица без подходящих прав не доказывает отсутствие других сеансов; с правами — описывает только момент выборки. Эти запросы не завершают сеансы и не меняют SQL isolation.

## 7. Если не приходит автообновление

Сначала сопоставить версию с изменениями: после коммита только Markdown работающий runtime остаётся прежним. Обычный программный релиз проходит тесты и подготовку, это занимает несколько минут.

На Linux VM можно прочитать историю, не выводя окружение:

```bash
sudo python3 - <<'PY'
import json
from pathlib import Path
cfg = json.loads(Path("/etc/perimeter/node.json").read_text(encoding="utf-8-sig"))
state = Path(cfg["state_dir"])
record = json.loads((state / "release.json").read_text(encoding="utf-8-sig"))
print("node:", cfg["node_id"], "auto_update:", cfg.get("auto_update"))
print("current:", record["current"]["sha"], "pending:", record.get("pending"))
items = []
for name in ("audit.previous.jsonl", "audit.jsonl"):
    path = state / name
    if not path.exists():
        continue
    with path.open("rb") as file:
        offset = max(0, path.stat().st_size - 12 * 1024 * 1024)
        file.seek(offset)
        if offset:
            file.readline()
        for line in file:
            try:
                item = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if str(item.get("kind", "")).startswith(("update_", "candidate_")):
                items.append({key: item[key] for key in ("time", "kind", "sha", "reason", "error") if key in item})
for item in items[-20:]:
    print(json.dumps(item, ensure_ascii=False))
PY
```

| Сообщение | Действие |
|---|---|
| `CandidateTestsFailed` | Инженер воспроизводит тесты из отдельного checkout под сервисным пользователем с очищенным окружением |
| `CandidateRequiresInfrastructureQualification` | Нужна отдельная процедура для SQL/зависимостей/модели, не штатный автоматический rollout |
| `GitUpdateFailed` | Проверить доступ GitHub и права сервисного пользователя на source/release directories |
| `CandidatePreflightFailed` | Проверить зависимости, диск, пути, SQL; это не основание удалять очереди |
| `operator-release-install.json` существует | Незавершённая ручная установка; использовать восстановление по журналу из RECOVERY.md |
| Старый отклонённый SHA | Исправление публикуется новым коммитом; список rejected вручную не очищается |

`trial_started=true` при `pending=false` может остаться от завершённой проверки и само по себе не означает сбой. Проверенный релиз 5bb19267 уже содержит исправление привилегированного systemd-теста.

## 8. Если пустая или некорректная Grafana

На ub22 проверить службы и `:19150`, затем выполнить аудит из существующего программного релиза. Его проверенный файл можно получить отдельно:

```bash
TASK_AUDIT=$(mktemp /tmp/perimeter-wallboard-audit.XXXXXX.py)
curl -fL --retry 2 --connect-timeout 10 --max-time 60 \
  https://raw.githubusercontent.com/Mika-dot/RFID_KPP/5bb19267248cb14f97edbdb3717bf94829c436bd/deploy/ha/audit_wallboard_live.py \
  -o "$TASK_AUDIT"
sudo python3 -B "$TASK_AUDIT" --timeout 10 --max-queries 130
```

Ожидается: обе страницы загружены, ECharts найден, playlist правильный, запросы панелей успешны. Успешный запрос ещё не проверяет смысл статуса проекта и видимость текста на телевизоре. Для ТВ использовать playlist URL, а не одиночный dashboard. Не переустанавливать Grafana при исправных процессах и источнике данных.

## 9. Что записать в заявке на обслуживание

Дата/время МСК; машина; что не работает; текущий ведущий и epoch; версия; ошибочная служба и зависимость; размеры/изменение очередей; ошибка/возраст зеркала; первые сообщения об ошибке. Указать, какие действия уже выполнены.

Не прикладывать пароли, токены, полные environment-файлы, RTSP-адреса с паролем или сырые RFID-идентификаторы в публичный GitHub. Частные логи хранить в эксплуатационном архиве.

После восстановления: один здоровый ведущий, два подготовленных резерва, пять ready-служб у ведущего, Gateway ready=200. Копии оценить отдельно. Ошибку зеркала не закрывать только потому, что сайт заработал.
