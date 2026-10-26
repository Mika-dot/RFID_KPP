# Восстановление и развёртывание Периметра

Инструкция для администратора, который восстанавливает систему без помощи автора.
Дата: 19.10.2026. [Проверка и поиск неисправности](OPERATIONS.md), [состояние при передаче](STATUS_2026-10-19.md).

Команды сверены с исходниками релиза `5bb19267248cb14f97edbdb3717bf94829c436bd`. Они не означают, что на объекте выполнено испытание полного уничтожения и восстановления всех машин.

## 1. Выбрать нужный сценарий

| Ситуация | Раздел |
|---|---|
| Не работает только сайт постоянного адреса или Grafana | 3 |
| Один HA-узел выключился, завис или не стартует | 4 |
| Узел доступен, но остался faulted после устранения причины | 5 |
| Оборвалась ручная установка программного релиза | 6 |
| Потеряна VM, Windows-машина или диск | 7 |
| Потерян ub22 | 8 |
| Не работает SQL Server или утрачена база | 9 |
| Нужна подготовка новой машины с нуля | 10 |
| Нужен управляемый переход на выбранный релиз | 11 |

Сначала выполнить разделы 2–3 [OPERATIONS.md](OPERATIONS.md). Если рабочий сайт доступен и один ведущий здоров, восстанавливать только неисправную машину. Рабочие узлы все одновременно не перезапускать.

## 2. Что должно находиться в резервной копии

Наличие этих копий на 19.10.2026 не подтверждено. Ответственный за сопровождение должен создать их, указать место хранения, дату и проверить чтение. Секретные файлы хранить в закрытом архиве вне Git.

| Объект | Что сохранить |
|---|---|
| SQL | Штатный полный backup рабочей базы, при принятой модели восстановления — цепочку журналов; настройки подключений и необходимые серверные объекты/login хранит DBA |
| Каждая Linux VM | Согласованный образ/backup VM; `/etc/perimeter`; фактические `state_dir`, `update_source`, `release_dir`, Python/Wine, модель и маски |
| Каждая Linux VM, автозапуск | `/etc/systemd/system/perimeter-guardian.service`, каталог `perimeter-guardian.service.d`, если существует; сведения `systemctl show` из OPERATIONS.md |
| Physical | Системный/дисковый backup, `D:\PerimeterHA`, фактическая production-папка с Python 64/32 bit, код/модель/маски/DLL; XML задачи `PerimeterGuardian` |
| Очереди | Фактические RFID/video SQLite, их WAL и SHM в согласованном backup; replica/fallback/metadata SQLite, business state, курсоры и конфигурация |
| ub22 | `/etc/perimeter/gateway.json`, `/etc/perimeter-resilience.env`, `/etc/perimeter-ha-monitor.token`, `/etc/mositlab-wallboard.env`, конфиги служб и drop-in |
| ub22, программы | `/opt/mositlab-wallboard`, `/usr/local/lib/perimeter-ha-monitor`, используемые каталоги `/opt/perimeter-resilience-releases` и пути из действующих units |
| ub22, Grafana | Конфигурация `/etc/grafana`, plugins/provisioning и согласованная копия хранилища Grafana; для стандартной SQLite — backup `/var/lib/grafana/grafana.db`; для внешней БД — её backup |
| ub22, откат экранов | `/var/lib/perimeter-ha-monitor/backups`, `/var/lib/perimeter-resilience-install` |
| Сеть и оборудование | IP/DNS/маршруты/firewall, параметры считывателя и камер, порядок камер и антенн, лицензии/дистрибутивы оборудования |

Пути в примерах не заменяют фактическую конфигурацию. В частности, Windows `node.json` может указывать на отдельный source и Python в старой production-папке. Сохранять оба дерева, если они используются.

Работающую SQLite нельзя надёжно архивировать простым копированием одного `.sqlite` без WAL. Применять штатную согласованную резервную копию VM/VSS, SQLite backup API либо копирование после остановки соответствующего узла в согласованное окно. Отдельные SQLite backups не являются общей атомарной копией всего кластера. Если остановка нужна, сначала убедиться в готовности резервов и учитывать перерыв при переключении.

### Выгрузить описание задачи Windows

**Physical, PowerShell администратора; сохраняет XML локально, службу не останавливает:**

```powershell
$Backup = Join-Path "D:\PerimeterHA\private-backup" (Get-Date -Format "yyyyMMdd-HHmmss")
New-Item -ItemType Directory -Path $Backup -Force | Out-Null
Export-ScheduledTask -TaskName "PerimeterGuardian" |
    Set-Content -LiteralPath (Join-Path $Backup "PerimeterGuardian.xml") -Encoding UTF8
Write-Host "XML задачи сохранён в $Backup. Скопируйте каталог в закрытое резервное хранилище."
```

Это экспорт регистрации задачи, а не backup очередей, программ или SQL. Права на закрытый каталог и доступ к внешней резервной копии проверяет Windows-администратор.

Для чистой переустановки из исходников одного Git недостаточно: он не содержит действующих паролей и накопленных производственных данных. Состав резервного комплекта должен быть проверен до потери машин.

## 3. Восстановить сайт или мониторинг на ub22

Если по независимой диагностике HA есть здоровый ведущий, а не работают только сайт/Grafana на ub22:

**ub22, запуск отсутствующей службы:**

```bash
sudo systemctl start perimeter-gateway.service
sudo systemctl start grafana-server.service
sudo systemctl start mositlab-wallboard.service
sudo systemctl start perimeter-ha-monitor.service
```

`start` не перезапускает уже работающую службу. Если конкретная служба active, но зависла, после сохранения журнала перезапустить только её. Пример для Gateway:

```bash
sudo systemctl restart perimeter-gateway.service
```

Проверить HTTP из OPERATIONS.md. Успех: Gateway ready=200, Grafana health=200, источник `:19150` отвечает. Если ready=503 при живом Gateway, проверить ведущего и SQL; повторный restart Gateway не устраняет их отказ.

`Unit ... not found` означает потерю регистрации/установки службы. Восстановить её из закрытого backup, раздел 8. Не подменять отсутствующий конфиг случайным примером.

## 4. Восстановить один HA-узел

1. Найти текущего ведущего по OPERATIONS.md.
2. Сохранить журнал неисправной машины и проверить питание, сеть, свободное место, SQL, RFID и камеры.
3. Если другая машина уже ведёт работу, не запускать старый legacy launcher на отказавшей.
4. Запустить штатный Guardian на отказавшей машине.

**Perimetr или Comparator, на той VM, которая восстанавливается:**

```bash
sudo systemctl start perimeter-guardian.service
sudo systemctl status perimeter-guardian.service --no-pager -l
```

Если процесс завис и не реагирует, а другой здоровый ведущий уже подтверждён, сохранить журнал и выполнить один restart:

```bash
sudo systemctl restart perimeter-guardian.service
```

**Physical, PowerShell администратора:**

```powershell
$Task = Get-ScheduledTask -TaskName "PerimeterGuardian"
if ($Task.State -ne "Running") {
    Enable-ScheduledTask -TaskName "PerimeterGuardian" | Out-Null
    Start-ScheduledTask -TaskName "PerimeterGuardian"
}
Get-ScheduledTask -TaskName "PerimeterGuardian" | Select-Object TaskName,State
```

Для restart зависшего Windows Guardian, только когда другой здоровый ведущий уже работает:

```powershell
Stop-ScheduledTask -TaskName "PerimeterGuardian"
Start-ScheduledTask -TaskName "PerimeterGuardian"
```

После запуска дать время на подготовку и независимую проверку: обычно несколько минут; новый runtime дополнительно проходит испытание. Перечитать состояния. Ожидается prepared=true, faulted=false. Восстановленный узел может остаться резервом.

Если это единственный доступный узел или он ещё ведущий, сначала восстановить готовность другого резерва. При вынужденном перезапуске единственного узла будет перерыв; это отдельное решение ответственного за эксплуатацию.

## 5. Узел остался faulted: штатная ручная проверка восстановления

Применять после устранения причины: питание, SQL, сеть, файлы, зависимости и диск уже исправны. Действие предназначено для **пассивного ошибочного узла**. Оно запускает штатную процедуру ремонта/проверки через Guardian, без ручного UPDATE SQL и без внешнего помощника.

В команде ниже выбран `comparator`. Для другой машины заменить только аргумент после `python3 -` на `perimetr` или `physical`. Команда выполняется **на ub22 от sudo**, а восстанавливает выбранный удалённый узел.

```bash
sudo python3 - comparator <<'PY'
import json
import shlex
import sys
import urllib.error
import urllib.request
from pathlib import Path

target = sys.argv[1]
cfg = json.loads(Path("/etc/perimeter/gateway.json").read_text())
nodes = {node["id"]: node for node in cfg["nodes"]}
if target not in nodes:
    raise SystemExit("Неизвестный узел")
token = None
for line in Path("/etc/perimeter-resilience.env").read_text().splitlines():
    for part in shlex.split(line, comments=True):
        if part.startswith("PERIMETER_HA_TOKEN="):
            token = part.split("=", 1)[1]
if not token:
    raise SystemExit("Токен не найден: восстановите частный environment ub22")
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
url = nodes[target]["url"].rstrip("/")
headers = {"Authorization": "Bearer " + token}
with opener.open(urllib.request.Request(url + "/status", headers=headers), timeout=8) as response:
    status = json.load(response)
age = status.get("sample_age")
if (status.get("node") != target or status.get("fencing_protocol") != 2
        or type(age) not in (int, float) or not 0 <= age <= 10):
    raise SystemExit("Нет свежего совместимого статуса")
if status.get("active") is not False or status.get("faulted") is not True:
    raise SystemExit("Команда предназначена только для пассивного faulted-узла")
if status.get("operator_maintenance"):
    raise SystemExit("Узел в обслуживании: сначала разберите журнал ручной установки")
body = json.dumps({"action": "restart_service", "service": "all"}).encode()
request = urllib.request.Request(
    url + "/repair", data=body,
    headers=dict(headers, **{"Content-Type": "application/json"}), method="POST")
try:
    with opener.open(request, timeout=40) as response:
        result = json.load(response)
    preflight = result.get("preflight") or {}
    print("NODE:", target, "REPAIR_REQUEST_ACCEPTED")
    print("PREFLIGHT_OK:", preflight.get("ok"))
    print("FAILED_CHECKS:", [key for key, value in (preflight.get("checks") or {}).items() if value is not True])
except urllib.error.HTTPError as exc:
    print("REPAIR_REFUSED_HTTP:", exc.code)
    raise SystemExit(2)
PY
```

Запрос меняет состояние процедуры ремонта выбранного резерва. Guardian повторно проверяет настоящую SQL-аренду и откажет, если этот узел уже владеет ей. Во время проверки локальные workers резерва остановлены. Ответ «принят» не означает «полностью восстановлен».

Ожидаемый итог после независимых проверок: prepared=true, faulted=false. Повторить чтение раздела 3 OPERATIONS.md через 1–3 минуты. При отказе не отправлять запрос в цикле: в коде есть ограничение частоты ремонта. Если preflight не прошёл, устранить перечисленную зависимость.

Если после общей аварии все узлы faulted, сначала восстановить SQL и общие зависимости, запустить штатные Guardians, затем выполнять эту процедуру по одному пассивному узлу. Выбор ведущего остаётся у контроллера. Если ни один узел не проходит preflight, требуется устранить зависимость; SQL-флаги вручную не обнулять.

## 6. Оборвалась ручная установка

Признак: в state_dir есть `operator-release-install.json`. Это частный журнал с данными восстановления; не публиковать целиком и не удалять его.

### Linux VM

Команда ниже **меняет состояние**: восстанавливает предыдущую локальную конфигурацию и регистрацию supervisor из журнала, останавливает/запускает локальный Guardian. Выполнять на пострадавшей VM после проверки другого здорового ведущего. Если здорового ведущего нет — выполнять в согласованном аварийном окне.

```bash
sudo python3 -u - <<'PY'
import json
import subprocess
from pathlib import Path
config = Path("/etc/perimeter/node.json")
cfg = json.loads(config.read_text(encoding="utf-8-sig"))
state = Path(cfg["state_dir"])
journal = json.loads((state / "operator-release-install.json").read_text(encoding="utf-8"))
target = journal["release"]
tool = Path(cfg["release_dir"]) / target / "deploy/ha/release_update.py"
if not tool.is_file():
    raise SystemExit("Нет исходников целевого релиза. Восстановите точный checkout из резервной копии; не удаляйте журнал.")
command = [cfg["python"], "-B", str(tool), "--recover",
           "--release", target, "--config", str(config),
           "--environment", "/etc/perimeter/environment"]
raise SystemExit(subprocess.run(command).returncode)
PY
```

Успех: `restored: true`. Далее штатная проверка ролей, служб и Gateway. Отказ `InstallJournalDoesNotMatchCurrentNode` означает несовпадение состояния/журнала; не обходить его.

### Physical

Та же операция выполняется в PowerShell администратора:

```powershell
$ErrorActionPreference = "Stop"
$Config = "D:\PerimeterHA\node.json"
$Node = Get-Content -LiteralPath $Config -Raw -Encoding UTF8 | ConvertFrom-Json
$JournalPath = Join-Path $Node.state_dir "operator-release-install.json"
$Journal = Get-Content -LiteralPath $JournalPath -Raw -Encoding UTF8 | ConvertFrom-Json
$Target = [string]$Journal.release
$Tool = Join-Path (Join-Path $Node.release_dir $Target) "deploy\ha\release_update.py"
if (!(Test-Path -LiteralPath $Tool)) {
    throw "Нет исходников целевого релиза. Восстановите точный checkout, журнал сохраните."
}
& $Node.python -B $Tool --recover --release $Target --config $Config --environment "D:\PerimeterHA\transfer-private\environment.local.json"
if ($LASTEXITCODE -ne 0) { throw "Восстановление отказало; сохраните вывод и журнал." }
```

## 7. Потеря машины или диска: восстановление из резервной копии

Предпочтительный путь — проверенная согласованная копия VM/системного диска. Она сохраняет Python, Wine, права, пути и регистрацию запуска.

1. Отключить старую неисправную машину либо отсоединить её от производственной сети.
2. Восстанавливаемую копию сначала держать без подключения к производственной сети. Две машины с одним node_id одновременно не запускать.
3. Восстановить полный образ, имена дисков/буквы, IP и hostname. На Linux проверить пользователя perimeter; на Windows — задачу SYSTEM.
4. До подключения к сети остановить/отключить штатный Guardian на восстанавливаемой машине. На Linux: `sudo systemctl disable --now perimeter-guardian.service`. На Windows: `Stop-ScheduledTask -TaskName PerimeterGuardian`, затем `Disable-ScheduledTask -TaskName PerimeterGuardian`.
5. Вернуть более свежие частные настройки/данные только из согласованного комплекта именно этого узла. Не копировать state другого узла поверх собственного.
6. Проверить node_id, три node URL и пути из node.json. `release.json.current.root` и `current.python` должны существовать. Вернуть именно эти каталоги релиза. Старый source HEAD сам по себе не определяет исполняемый runtime.
7. Проверить clock/timezone и доступность SQL, камер, считывателя. Для Windows сохранить правильные 64- и 32-битные Python/SDK; для VM — Wine32 и Python32.
8. Подключить только восстановленную машину к производственной сети; убедиться, что старая с тем же IP/node_id изолирована.
9. Linux: `sudo systemctl daemon-reload`, затем `sudo systemctl enable --now perimeter-guardian.service`. Windows: `Enable-ScheduledTask -TaskName PerimeterGuardian`, затем `Start-ScheduledTask -TaskName PerimeterGuardian`.
10. Проверить состояние по OPERATIONS.md. Если SQL помнит fault узла, выполнить раздел 5 после исправления зависимостей.
11. Отметить время backup и возможный разрыв данных между ним и отказом. Отсутствие дублей/потерь нельзя объявлять только по зелёному health.

### Если утрачена только регистрация Windows-задачи

Код, конфиг и данные уже должны быть восстановлены по прежним путям. Убедиться, что Guardian не запущен другим способом. Выбрать сохранённый XML задачи:

```powershell
$ErrorActionPreference = "Stop"
$XmlPath = Read-Host "Полный путь к сохранённому PerimeterGuardian.xml"
if (!(Test-Path -LiteralPath $XmlPath)) { throw "XML не найден" }
Register-ScheduledTask -TaskName "PerimeterGuardian" -Xml (Get-Content -LiteralPath $XmlPath -Raw) -Force | Out-Null
Disable-ScheduledTask -TaskName "PerimeterGuardian" | Out-Null
(Get-ScheduledTask -TaskName "PerimeterGuardian").Actions | Select-Object Execute,Arguments,WorkingDirectory
```

Сверить Execute/Arguments/WorkingDirectory с восстановленными файлами, затем включить и запустить задачу как в шаге 9. Восстановление XML не устанавливает Python и не восстанавливает отсутствующие программы.

## 8. Потерян ub22

1. Восстановить OS/backup ub22, прежний IP 172.31.0.97 и сетевые правила.
2. Восстановить Grafana и её хранилище из согласованной копии, плагины, provisioning и частные environment-файлы.
3. Вернуть wallboard `/opt/mositlab-wallboard`, observer `/usr/local/lib/perimeter-ha-monitor`, их units/drop-in и используемые runtime directories.
4. Сверить пути ExecStart и WorkingDirectory через `systemctl show`. Файлы по абсолютным путям должны существовать.
5. Выполнить `sudo systemctl daemon-reload`; включить и запустить четыре службы из раздела 3.
6. Проверить сайт, backend и Grafana, выполнить аудит из OPERATIONS.md. HA-узлы ради восстановления ub22 не переустанавливать.

Если утрачена только регистрация Gateway, а его конфиг, частный environment и исходники сохранены, инженер может создать unit штатным установщиком.

**ub22, отдельный root-owned checkout, не рабочее дерево HA:**

```bash
sudo bash <<'BASH'
set -euo pipefail
TASK_REL="5bb19267248cb14f97edbdb3717bf94829c436bd"
TASK_ROOT="/opt/perimeter-resilience-releases/$TASK_REL"
if [ ! -d "$TASK_ROOT/.git" ] && [ ! -f "$TASK_ROOT/.git" ]; then
    install -d /opt/perimeter-resilience-releases
    git clone --no-checkout https://github.com/Mika-dot/RFID_KPP.git "$TASK_ROOT"
    git -C "$TASK_ROOT" checkout --detach "$TASK_REL"
fi
test "$(git -C "$TASK_ROOT" rev-parse HEAD)" = "$TASK_REL"
test -z "$(git -C "$TASK_ROOT" status --porcelain)"
test -f /etc/perimeter/gateway.json
test -f /etc/perimeter-resilience.env
python3 -B "$TASK_ROOT/deploy/ha/install_resilience.py" --install \
    --root "$TASK_ROOT" --python /usr/bin/python3 \
    --gateway-config /etc/perimeter/gateway.json \
    --env-file /etc/perimeter-resilience.env --release "$TASK_REL"
BASH
```

Это установка/перезапуск только Gateway. Требует root-only environment (владелец root, режим 0600), готового ведущего и существующих конфигов. При отказе готовности установщик возвращает прежние units; сохранить сообщение. Здесь выбран известный релиз; для будущего утверждённого релиза заменить SHA согласованно во всём блоке.

Установщики `install_monitoring.py` и `update_visual_wallboard.py` требуют **уже существующие** MOSITLAB wallboard, Grafana, Infinity datasource и частные настройки. Они не являются установкой всей инфраструктуры мониторинга на пустой OS. Поэтому для полного ub22 обязателен его собственный backup; при его отсутствии восстановление всего общего wallboard — отдельная задача администратора мониторинга.

## 9. Не работает SQL Server

SQL — общая зависимость. Переключение между Physical/Perimetr/Comparator не заменяет восстановление SQL.

1. DBA проверяет доступность самого SQL Server, диски, службы, базу, аутентификацию и блокировки.
2. Если база цела — устранить недоступность, не восстанавливать старый backup поверх исправной базы.
3. Если база утрачена — DBA выбирает точку восстановления и восстанавливает штатный backup/логи. Старые экземпляры базы и узлы, способные продолжить запись в них, изолировать.
4. DBA проверяет таблицы HA и девять fencing triggers protocol 2; восстановление старой схемы без них не готово к запуску текущих writers.
5. После согласованного возврата SQL запустить/проверить штатные Guardians. Роль выдаёт контроллер; вручную выбирать owner и epoch в SQL нельзя.
6. Проверить доставку pending, курсоры, последние события и работу сайта. Если восстановленная SQL-база старее локального metadata cursor, возможен `MirrorSourceHistoryChanged`: нужен разбор истории, а не удаление metadata.sqlite.
7. Сверить период потери/повтора данных по backup и фактическим очередям. SQL backup и реплика очередей решают разные задачи.

Fencing triggers находятся в migrations 003/004, но не запускать эти миграции повторно вслепую на работающей системе. DDL-восстановление выполняет DBA после сравнения фактической схемы с выбранным релизом.

## 10. Подготовка новой машины с нуля

Этот раздел — для замены потерянной машины. Для работающего узла использовать разделы 3–6. Отсутствие резервной копии очередей означает возможную потерю недоставленных данных; новый Git checkout их не воссоздаёт.

### Linux Perimetr или Comparator

Нужны Ubuntu amd64 22.04/24.04, root, GitHub, пакеты Microsoft/Python, SQL ODBC18 и закрытый комплект настроек прежнего узла. Пример подготовки кода известного релиза:

```bash
sudo bash <<'BASH'
set -euo pipefail
TASK_REL="5bb19267248cb14f97edbdb3717bf94829c436bd"
test ! -e /etc/perimeter/node.json
test ! -e /var/lib/perimeter/release.json
test ! -e /opt/perimeter/source
id perimeter >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/perimeter --shell /usr/sbin/nologin perimeter
install -d -o perimeter -g perimeter /opt/perimeter
runuser -u perimeter -- git clone --no-checkout https://github.com/Mika-dot/RFID_KPP.git /opt/perimeter/source
runuser -u perimeter -- git -C /opt/perimeter/source checkout --detach "$TASK_REL"
bash /opt/perimeter/source/deploy/ha/install-ubuntu.sh
BASH
```

Установщик ставит зависимости, Wine32 и шаблоны, но Guardian автоматически не запускает. На 19.10 старый установщик без предварительного checkout сам клонирует историческую ветку; поэтому в блоке source заранее закреплён на нужном SHA.

Далее, по порядку:

1. Восстановить настоящие `/etc/perimeter/node.json` и `/etc/perimeter/environment` из закрытого комплекта именно Perimetr либо Comparator вместо шаблонов.
2. Восстановить state_dir, используемые release/source paths, модель, маски и DLL. Проверить абсолютные пути, владельца perimeter и права частных файлов.
3. Вернуть согласованный systemd unit и drop-in. Старый drop-in не должен ссылаться на отсутствующий recovery-tools каталог.
4. Проверить node_id и URL, CPU-настройку YOLO, Wine/Python32, SQL-подключения. Не присваивать обоим VM один node_id.
5. Пока узел остановлен, выполнить локальный doctor:

```bash
sudo /opt/perimeter/venv/bin/python -B /opt/perimeter/source/deploy/ha/environment_tool.py doctor --node /etc/perimeter/node.json --environment /etc/perimeter/environment
```

Ожидается `ok=true`. Если восстановленная конфигурация указывает на другой Python/root, использовать фактический исправный путь из комплекта, не затирать конфиг ради примера. Doctor не заменяет проверку takeover.

6. Убедиться, что старая машина изолирована, и запустить Guardian по разделу 7. SQL schema/lease вручную не инициализировать заново в существующем кластере.
7. Проверить готовность и копии по OPERATIONS.md. Для потерянного state оформить фактический разрыв данных и выполнить производственную сверку.

### Windows Physical

Нужны Windows, прежние буквы/пути, Git, Python 64 bit для служб, совместимый 32-bit Python и SDK для считывателя, SQL ODBC, драйверы/GPU-настройки, модель и маски.

1. Восстановить production-папку и `D:\PerimeterHA` из backup. Для Python надёжнее восстановленный образ; перенесённое окружение с другой машины может требовать переустановки по прежним путям.
2. Проверить все пути `node.json` и `release.json`, приватный `transfer-private/environment.local.json`. Не создавать новый HA token для одного восстановленного узла.
3. Восстановить задачу из XML по разделу 7; проверить principal SYSTEM, действие, триггер старта и рабочую папку.
4. Если XML утрачен, но узел восстановлен и задача не запущена, штатный `deploy/ha/install-windows.ps1` может заново зарегистрировать задачу. Его запускать из checkout, совпадающего с `node.json.root`, с действующими `-Config`, `-Bundle`, `-Root` (последний — production-папка с Python). `Root` и отдельный HA checkout должны различаться.
5. На полностью пустой машине сначала воссоздать эти пути/окружения и взять действующий закрытый bundle. `windows_tool.py prepare` предназначен только для отсутствующего node.json и отдельного source checkout; не перезаписывает действующий конфиг. Полная установка Windows/Python/драйверов не автоматизирована этим PowerShell-скриптом.
6. Для проверки остановленного узла выполнить из восстановленного HA checkout:

```powershell
$ErrorActionPreference = "Stop"
$Config = "D:\PerimeterHA\node.json"
$Node = Get-Content -LiteralPath $Config -Raw -Encoding UTF8 | ConvertFrom-Json
$Tool = Join-Path $Node.root "deploy\ha\windows_tool.py"
& $Node.python -B $Tool doctor --config $Config --bundle "D:\PerimeterHA\transfer-private\environment.local.json"
if ($LASTEXITCODE -ne 0) { throw "Doctor не пройден; Guardian пока не запускать." }
```

7. Включить задачу и запустить по разделу 4. Не запускать параллельно legacy CMD. Проверить роль, пять служб только у ведущего, готовность резервов и копий.

## 11. Управляемая установка точного релиза

Для обычного изменения кода используется main auto-update. Ручной rollout нужен инженеру для восстановления/контролируемого перехода и требует существующих конфигов, SQL и доступных агентов. Это не установка пустого кластера и не лечение выключенного SQL.

Штатная последовательность рассчитана на исправного ведущего Perimetr:

1. Comparator: `recover_bus_rollout.py --phase hold --release SHA`.
2. Physical: та же команда, `--phase hold`.
3. Comparator: `--phase release`; дождаться `PAIR_READY`.
4. Perimetr: `--phase finish`; дождаться `CORE_HA_ACCEPTED`.

Каждая команда выполняется из отдельного **чистого checkout того же полного SHA** с root/администратором, существующим Python, `--config` и `--environment` своей машины. Пример вызова на Linux из подготовленного каталога:

```bash
sudo /opt/perimeter/venv/bin/python -B deploy/ha/recover_bus_rollout.py --phase verify --release 5bb19267248cb14f97edbdb3717bf94829c436bd --config /etc/perimeter/node.json --environment /etc/perimeter/environment
```

Этот пример — verify из exact checkout, не команда новой установки. Верификатор ручного rollout требует в том числе exact operator pin; после штатного auto-update pin может не совпадать с новым SHA, поэтому отказ этого специализированного verifier не равен отказу рабочего контура. Для обычной эксплуатации использовать OPERATIONS.md.

Если ведущий Physical или Comparator, не запускать hold на нём: процедура откажет до изменения. Не подставлять другие node_id и не останавливать ведущего ради обхода guard. Инженер выбирает согласованную последовательность с достаточными готовыми приёмниками либо сохраняет штатное auto-update.

`HELD_NODE_INSTALLED` означает только установленный удержанный резерв; `PAIR_READY` — готовность пары; `CORE_HA_ACCEPTED` — техническую приёмку указанной процедуры. Копии SQL и реальный проход оцениваются отдельно.

### Резервы застряли на разных незавершённых релизах

Признаки: здоровый ведущий подготовил новый SHA (`update_staged`), оба
резерва имеют `pending=true`, `trial_started=false`, но разные старые SHA.
Обычный updater не заменяет незавершённое испытание, а контроллер ждёт
двух резервов на новом SHA перед плановой передачей роли. Ожидание само
по себе не разрешает такое состояние.

Использовать последовательность hold/hold/release/finish выше из нового
чистого exact checkout. Фаза hold явно допускает подготовку замены
неиспытанного резерва: перед подготовкой штатный SQL-проверяемый endpoint
удерживает его в maintenance. Требуются совпадение локального и живого SHA,
`trial_started=false` в обоих источниках, независимая квалификация текущего
релиза, свежий успешный preflight с ports_free/sdk_load и здоровый другой
ведущий. Удержание штатно выставляет SQL Faulted=1 и `prepared=false`,
исключая резерв из выборов; они не считаются отказом подготовки под hold.

Опция `release_update.py --prepare --replace-untried-reserve` доступна
только при уже установленном maintenance hold; она сама не останавливает
службы и не изменяет pending. Кандидат заново проходит квалификацию и тесты,
а установка сохраняет обычные проверки receipt, владения, native shutdown,
Guardian lock, журнал восстановления и испытание нового релиза. Уже
запускавшееся или неопределённое испытание этим путём заменить нельзя.

Не сбрасывать pending/trial_started вручную и не подтверждать старый релиз
без испытания. Ошибка SQL-зеркала остаётся отдельным открытым результатом
проверки; успешная установка версии её не устраняет.

## 12. Приёмка после восстановления

1. Один здоровый ведущий, одинаковый свежий epoch/lease у доступных агентов; пять служб ведущего готовы.
2. Оба резерва prepared=true, faulted=false. Резерв может ожидать испытания нового runtime: pending не сбрасывать вручную.
3. Gateway ready=200; сайт открывается с авторизацией.
4. Очереди не растут без доставки; после накопившейся нагрузки уменьшаются.
5. Все шесть потоков копий отдельно проверены, ошибки/задержки не замаскированы свежестью мониторинга.
6. Grafana получает данные, playlist работает.
7. Если требуется подтвердить производственный учёт — выполнить реальный проход по [FACTORY_READ_ONLY_ACCEPTANCE.md](FACTORY_READ_ONLY_ACCEPTANCE.md) и сверку с ожидаемым барабаном/направлением/складом.
8. Записать время отказа, восстановленную версию, источник backup, возможный разрыв данных и оставшиеся замечания.

Наличие одной здоровой машины подтверждает работу, но не полную готовность резервирования и копий. На 19.10.2026 периодические SQL-тайм-ауты зеркала остаются открытой задачей сопровождения.
