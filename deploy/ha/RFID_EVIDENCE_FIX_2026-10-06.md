# Исправление ложного RFID failover, 06.10.2026

## Последний заводской вывод: 18:23 МСК

Zabbix пользователь разрешил пропустить. Grafana/observer установлены и работают;
не возвращаться к Zabbix login/новым проверкам как условию запуска системы.
Историческое READY237 в18:14 не является текущей готовностью. Вывод18:20 показал
physical faulted/perimetr unhealthy owner239. Вывод18:23: сначала Comparator
unhealthy owner241, затем Perimetr unhealthy owner243; физика восстановлена как
prepared/nonfaulted passive, Comparator faulted/prepared. Все3 остаются наf5.

Ведущий: transport reader_loop/SDK/TCP, database/delivery_writer/local_spool,
RusGuardSync, Yolo/cameras — ok. RfidReader business_flow unavailable с
rfid_business_flow_fault_latched; Aggregator/WebDashboard — peer_not_ready cascade.
Последний RFID возраст1087.5сек, последнее video1089.6сек, Warehouse1257.7сек.
Видео и склад старше RFID, но video_recent1 и Warehouse33 из общего окна1800сек
ошибочно считаются независимой активностью при RFID stall900сек. На физике после
последнего чтения18:02:39 начинается отказ около18:17:50 — соответствует900сек.
Это доказанный дефект временной логики; нет доказательства отказа БД/TCP/камер.
Восстановление физики через guarded passive repair подтвердилось, но repeated
restarts сами причину не устраняют. Repair perimetr409 может быть race/rate guard,
error body толькоRuntimeError; не обходить защиту/не сбрасывать rate/quarantine.

## Изменение production

- SQL counts video/Warehouse теперь требуют event timestamp ПОСЛЕ текущего RFID
  marker и в прежнем bounded window. Сам тот же/старый проход не доказательство
  silent RFID fault. Последнее видео — MAX(COALESCE(CapturedAt,Timestamp)), а не
  последняя вставка Id (backlog может приходить позже и иметь старый timestamp).
- Pure classifier также проверяет causal order, в том числе равные timestamp.
  Реальные multiple passages после RFID либо video+Warehouse после RFID всё ещё
  требуют unavailable/latch/recovery. Warehouse-only остаётся warning.
- Сохранённый флаг НЕ удаляется/не очищается при обновлении. Если SQL полная video
  history заканчивается ДО/на том же самом persisted RFID marker и новый SQL
  count после marker0, а original last_fault_detail принадлежит двум известным
  video-based причинам старого detector, основание этого historical latch
  опровергнуто. Только эта комбинация даёт degraded warning
  rfid_historical_activity_evidence_contradicted без clear_latch и новых restart.
  Persisted bytes/restart attempts остаются неизменными, audit metric latch=true.
- Не равнозначно age-out: любое позднейшее video в history сохраняет real latch
  даже за пределами окна; отсутствие истории, unknown/corrupt metadata,
  changed/backlog marker и inconsistent count/MAX snapshots остаются unavailable.
  Новый свежий реальный RFID read — по-прежнему единственное normal clear_latch.
- Readiness допускает только typed warning при latch=true и exact reason; все
  transport/storage/delivery/peers и свежий probe остаются обязательными. Grafana
  показывает предупреждение RFID-потока, а не выдуманное бизнес-"всё хорошо".
  Метрика business_flow_fault_detail раскрывает original reason без секретов.
  Recovery attempts прекращаются только для опровергнутого historical evidence.
- Cursor/spool/sessions/latch contents и SQL схемы не меняются. RFID аппаратные
  команды, writer fencing/epoch, приоритеты, детерминированные переключения,
  LLM boundary и бизнес-классификация катушек остаются прежними.

## Установка: три последовательных шага

Использовать pinned repair_activation_runtime.py из этого feature commit,
SHA256: dfbcac2844ab9c3bcc8c626bc109e1f89588a8d79a4c2250d7e53240ea8abfd0.
Порядок СТРОГО physical→Comparator→Perimetr. Последняя VM автоматически выпускает
maintenance и проверяет восстановленный cluster. Краткая пауза при последнем
staging ожидаема; это исправление, не очередное fault-injection испытание.

1. Physical Administrator PowerShell: existing Python
   D:\Desktop\RFID_KPP-main\venv64\Scripts\python.exe -B downloaded-tool
   --node physical --release exactSHA --stage.
2. Comparator sudo /opt/perimeter/venv/bin/python -B downloaded-tool
   --node comparator --release sameExactSHA --stage.
3. Perimetr sudo /opt/perimeter/venv/bin/python -B downloaded-tool
   --node perimetr --release sameExactSHA --stage --resume-after-stage.

В каждом шаге download pinned raw URL и checksum; candidate tests/checkout/backup
перед остановкой. Режим принимает именноf5 и прежние allowlisted bases, проверяет
root/release/history/clean tree. Guardian stopped tree подтверждается, rollout
ставит operator-maintenance/repair-verification, business latch не трогает.
После exact-staged physical+Comparator последний Perimetr staging передаёт
controller Comparator. --resume-after-stage разрешён только --stage/perimetr.
Все3 exactSHA/protocol2/preflight проверяются ДО release, held workers={} обязательны.
Проверяются девять прежних fencing triggers SELECT-only, миграция не запускается.
Owner не назначается руками; Guardian независимо подтверждает recovered60сек,
controller выбирает исполнителя по обычной логике. Финал wait_cluster требует
physical active, full5 healthy, оба prepared/nonfaulted reserves, Comparator valid
controller и7 stable samples. Это не новая квалификация RFID бизнес-прохода.

При успехе нужны HA_ACTIVE_PHYSICAL_RESERVES_READY и
HOTFIX_PROTOCOL2_RUNTIME_RESTORED; BUSINESS_READ_ACCEPTANCE_NOT_PROVEN.
Предупреждение RFID_BUSINESS_WARNING допустимо/видно. Новые данные должны прийти
реально, никто не вводит synthetic rows и не сбрасывает persistent fault.
При HOTFIX_FAILED следующий зависимый шаг не запускать; прислать output.
Уже поставленный exact step повторяемый; после последнего step resume можно
повторить той же командой (requires first two held exact agents; если уже released,
использовать отдельно --resume-protocol2 from physical без повторного staging).

## Проверки и точка остановки

GitHub Linux job402 tests success. Windows job обнаружил только прежний mock
os.geteuid в двух ub22 monitoring tests (этого атрибута на Windows нет); tests
исправлены с create=True, production temporal/rollout code не менялся.
402 local tests OK, skipped15 (platform/environment/real SQL dependent), full
unittest discover после установки только локальных CI dependencies psutil/OpenCV.
Новые regression10 tests: observed timings, same-time passage, real later evidence,
latched contradiction/no clear, no age-out, SQL cutoff+MAX, recovery loop preserving
state bytes/no reconnect, real dependency readiness. CLI/help/diff --check OK.
Новые candidate tests запускаются каждой машиной ДО остановки старого runtime.
Пока исправление ТОЛЬКО опубликовано в feature. Factory install/output не получен,
не утверждать восстановление/долговременную устойчивость по локальным tests.
Main не обновлялся, auto-main adoption отдельно не доказан. Grafana живые панели
не переустанавливать. Работы18:14/18:20/18:23 — история в RESUME_2026-10-06.md.
