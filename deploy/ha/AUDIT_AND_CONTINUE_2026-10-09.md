# Периметр — сверка всех веток и продолжение, 09.10.2026

## Вывод

Работа не сводилась к установке: после последнего отчёта оставались 23 открытых
review-замечания к PR #5/#7–9, описывающих 22 разных дефекта. Они исправлены в
итоговой ветке `feature/adaptive-correlation-fallback-2026-10-08` (PR #9).
Старые head PR #7 и #8 сохранены для истории и не содержат всех этих исправлений.
Итоговый PR #9 направлен в main как общий кандидат, чтобы не выпускать сначала
промежуточную версию с известными дефектами. Main и заводские узлы не изменялись.
Точный опубликованный SHA и результаты GitHub Actions находятся в описании PR #9.

Код готов к следующему этапу квалифицированной установки. «Все работы выполнены»
и «заводская приёмка завершена» не заявляются. Установка на три машины, Grafana
и приёмка на реальной истории ещё требуются.

## Источники и ограничения сверки

Использованы доступная история задач 06–08.10, исходный план целиком, handoff,
код всех веток, Git ancestry, все девять PR, review threads и Actions. Поиск
истории чатов через Personal Context дважды вернул ошибку: полного повторного
чтения всех чатов не было. Непросмотренные сообщения не используются как доказательство.
В репозитории нет отдельных GitHub issues; задачи ведутся в PR и Markdown.
GitHub branches endpoint вернул девять веток без следующей страницы; git refs
подтвердили тот же список. Ветки не удалялись и не перезаписывались force push.

Сохраняются условия пользователя: не проводить катушки, не отключать питание
или сеть завода, не менять производственный SQL в этой offline-итерации.
Машины и текущий runtime не инспектированы live; прежние factory receipts
описывают прошлое состояние, а не состояние 09.10.

## Все ветки перед этой итерацией

| Ветка | Head | Состояние |
|---|---|---|
| `main` | `76778494851d03c5c3c5650b40e54986ddcfddd1` | Содержит прежний HA, release guard и исправление снимков; PR #5/#6 merged |
| `archive/perimeter-original-no-ha-2026-10-07` | `49da7e9ba61cfb843f7fa672e85193c2eae285f7` | Оригинал без HA сохранён; 61 коммит main после него |
| `docs/next-round-plan-2026-10-06` | `4735b48aa2e7b9b30d26732ac28e64a4337a6376` | Draft PR #4; содержание плана уже в кандидате, отличие — завершающая пустая строка |
| `feature/perimeter-ha-guardian` | `ccf39a9d3c5d86cb5629c6d0fe317a6a90be8729` | Предок main; прежний PR #3 merged |
| `fix/perimeter-release-guard-2026-10-07` | `97935f3b3cd2adeaa114e1d7ba2f2a6091a24d8d` | Предок main; PR #5 merged |
| `fix/web-video-snapshots-2026-10-07` | `c15edc8b234f5cce7df3177a1b47ec5aa61c0674` | Предок main; PR #6 merged |
| `fix/warehouse-evidence-consistency-2026-10-07` | `d57c911ced3e8b94672eb05672b6fc26fdcab503` | PR #7 открыт; один коммит сверх main, полностью унаследован PR #9 |
| `feature/ha-resilience-2026-10-07` | `1d59061d0f29e3eba8f877ed03cd63d347300d3f` | PR #8 открыт; пять коммитов сверх main, полностью унаследован PR #9 |
| `feature/adaptive-correlation-fallback-2026-10-08` | `d700becc9889e99786403d290c41e3e513a6cf99` | Начало этой итерации; семь коммитов сверх main. Исправления 09.10 поверх этого SHA |

PR #1/#2/#3/#5/#6 merged; #4 draft; #7/#8/#9 были открыты. Программные
изменения #7/#8 входят в общий кандидат #9; отдельный rollout старых head
не нужен. Архивную ветку без HA сохранять и после приёмки.

## Исправлено в этой итерации

| № | Дефект | Итог |
|---|---|---|
| 1 | Направление в Web-фильтрах/сводке/графике расходилось с проекцией списка | Общий effective-direction SQL учитывает складской fallback и сохраняет явный conflict |
| 2 | Исторический UNKNOWN/NO_DATA превращался в OUT без provenance | Startup repair сохраняет `WAREHOUSE_DIRECTION_INFERRED`; это вывод по складу |
| 3 | CSV-аудит разделял запись по физическим строкам | Ограниченный потоковый парсер логических CSV-записей, включая quoted multiline/chunk boundaries |
| 4 | Node ID с дефисом останавливал весь Behavior Observer | Валидатор метрик принимает тот же алфавит node IDs |
| 5 | Аппаратный fence не выполнялся без готового нового owner | Durable pending fences выполняются после demotion и повторяются при пустой lease |
| 6 | Behavior installer терял старую numeric Grafana folder | Сохраняется folderUid или legacy folderId |
| 7 | Незавершённое зеркало выглядело свежим | `caught_up=false` явно означает stale на recent endpoint |
| 8 | Replica становилась SENT до сохранения fallback archive | Digest проверяется; archive commit происходит первым; при ошибке replica остаётся PENDING |
| 9 | Adaptive baseline всегда равнялся 24 часам | Baseline берётся из фактического legacy window и привязывается к принятому профилю |
| 10 | Replay связанной строки удалял adaptive provenance | Старые adaptive flags/window сохраняются под row lock; self-selected link не становится training data |
| 11 | Mirror читал Warehouse/1C из event DB вместо task DB | Отдельное SELECT-only task connection, корректное закрытие обоих соединений |
| 12 | Holdout использовал transport, которого нет у рабочего matcher | Qualification проверяет UNKNOWN-путь; профиль v1 нельзя включать без новой квалификации |
| 13 | При adaptive delay > legacy window identity lookup не доходил до события | Envelope задач и pure identity resolver расширяются согласованно; ambiguity guards остаются |
| 14 | Backend Web падал после успешной проверки Guardian, cache не открывался | GET/HEAD fallback также при refused/reset/timeout/incomplete-read upstream; authentication сохраняется |
| 15 | Пустой event cursor искал несуществующий EventId=0 | Нулевой cursor считается начальным; пустая БД может начать заполняться позже |
| 16 | Protected checker проверял helper, но не проводку deployed watchdog | Проверяет AST call contract обоих watchdog adapters и causal SQL, даже при зелёных candidate-owned tests |
| 17 | Backlog Warehouse с новым Id и старым Dt подавлял сигнал потерянного чтения | Watchdog использует MAX(Dt), а не timestamp последнего Id |
| 18 | Docs-only SHA повторно проверялся и засорял конечный HA log | Уже наблюдавшийся docs SHA пропускается после fetch; сохранённый runtime target не меняется |
| 19 | Первая установка Grafana рисовала пустые string stat panels | String-capable selector задаётся самим make_panels, повторный install не ломает его |
| 20 | Пустые link keys создавали ложную общую passage group | Missing keys учитываются отдельно и не входят в групповые/duplicate metrics |
| 21 | Монитор мог показать зелёным несовместимый fencing protocol | Authenticated status требует целочисленный protocol 2, иначе critical |
| 22 | Агрегированный CSV-аудит публиковал примеры отдельных складских строк | Персональные примеры/row IDs/task IDs удалены из нового результата; остаются агрегаты |

В PR #9 замечание про отдельную task DB было помечено outdated, но проблема
сохранялась в текущем коде и тоже исправлена. Два замечания о startup inference
в #7/#8 относятся к одному дефекту. Не закрывать review threads старых PR
как исправленные в их неизменённых head: исправления находятся в итоговом #9.

## Проверки

- Полный локальный набор: **573 tests**, **0 failures/errors**, **15 skipped**.
  558 выполнены. Среда Python 3.12; CI дополнительно проверяет Python 3.11
  на Linux и Windows. Native SQL/Windows/systemd/PID skips не равны PASS.
- Qualification относительно чистого pinned PR #8 `1d59061...`: **passed**;
  6 adapter contracts, 27 golden traces, 8 HA model scenarios,
  0 shadow differences; 120 Python-файлов разобраны.
- Trace SHA-256: `a7ee0f8c63b6ff352989efdbd19e3c82454e0dde9f8b64c9769280bef7282096`.
- Compileall и `git diff --check` прошли; plan audit counts/evidence проверены.
- Protected business checker: 15 checks, включая production watchdog wiring.
- Для исходного `d700bec...` все три Actions были success. Для нового SHA
  success не наследуется: проверить новые runs в PR #9 перед установкой.

Golden corpus остаётся поведенческим эталоном, не доказательством физического
RFID-прохода. Проверка относительно PR #8 не является квалификацией текущего
установленного заводского runtime: его точный SHA надо прочитать с машин.

## План с фактом

Счётчик исходных 33 пунктов сохраняется: **20 SOFTWARE_READY, 4 PARTIAL,
8 BLOCKED, 1 DEFERRED**. Девять дополнений пользователя: **8 SOFTWARE_READY,
1 PARTIAL**. Счётчики пересекаются; процент из их суммы не вычисляется.
Поиск обнаруженных дефектов и их исправление не закрывают production-критерии.
Подробная таблица каждого пункта: [PLAN_AUDIT_2026-10-08.md](PLAN_AUDIT_2026-10-08.md).

| Область | ПО | Что осталось |
|---|---|---|
| Три Guardian, пять workers, replication, fencing/rollback | Реализовано с исправлениями 09.10 | Staged install, read-only проверка всех узлов; конкретный hardware adapter |
| Сверка КПП/склад и adaptive windows | Shadow/retrospective/active gate реализованы | Реальные confirmed/rejected пары, профиль v2 и соответствующий holdout |
| Локальные копии без фото | Шесть потоков, минимум 93 дня, fresh/backfill guard | Установка и полный backfill всех узлов; mirror не заменяет SQL backup |
| Постоянный Web | Gateway и authenticated cache реализованы | Установка/адрес; независимый Web/VIP для отказа самого Comparator |
| Новая Grafana | Визуальная цепочка, 15 workers, две страницы, playlist 1m, banner removal | Live apply, receipt и фактический просмотр обеих страниц |
| Observer | Метрики, residual/drift/forecast/history реализованы | 1–2 недели factory baseline, thresholds; 43 wishlist metrics пока not_instrumented |
| Safe auto-update | Installed qualifier, model/replay/shadow/quarantine/probation | Реальный staged rollout/rollback acceptance; долгое production shadow |
| Полный отказ общей SQL authority | PENDING/replay; fail-closed fencing | SQL HA listener/control storage, независимые failure domains |
| Factory business acceptance/отключения/LLM repair | Offline модели имеются | Физическая приёмка отдельно; ограничения пользователя сохраняются |

## Следующая точка продолжения

1. Прочитать exact head/CI в [PR #9](https://github.com/Mika-dot/RFID_KPP/pull/9).
   Не ставить вместо него старый head #7/#8 или текущий main.
2. Получить GET-only inventory physical/perimetr/comparator: SHA, owner/epoch,
   пять workers, очереди, paths/config. Сохранить receipt. Не обнулять spool,
   mirror, UUID, cursor, secrets или business latch.
3. По существующим staged tools квалифицировать и установить общий exact SHA
   сначала на резерв, затем проверить все узлы. Merge в main является отдельным
   release-действием: production updater следит за main, поэтому audit не сделал merge.
4. Начать с shadow; active не включать без реальной разметки и profile v2.
   `--legacy-window-hours` при обучении должен совпадать с рабочим
   `KPP_TASK_MATCH_WINDOW_HOURS`. Для отдельной task DB задать private
   `PERIMETER_OBSERVER_TASK_SQL` с SELECT-only правами.
5. Проверить свежий backfill шести потоков и архивы на каждом узле через
   `verify_resilience_installation.py --nodes-config <nodes.json> --release <exact-SHA>
   --require-local-copies --output <receipt.json>`; проверить gateway.
6. На Grafana-хосте из полного clean checkout выполнить
   `update_visual_wallboard.py --plan`, затем `--apply --release <exact-SHA>`.
   Проверить backend frames, receipt, обе страницы, playlist и верхнюю надпись.
7. Собрать baseline/long shadow; отдельно решить SQL HA/hardware fence/Web
   failure domains. Не объявлять их выполненными из unit tests.
8. Записать установленный SHA, receipts и оставшиеся ограничения в GitHub.
   Только после установки и приёмки привести связанные PR/ветки в порядок;
   оригинал без резервирования оставить в архиве.
