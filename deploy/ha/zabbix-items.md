Дополнить существующие хосты, не пересоздавая их и пять штатных проверок.

| Ключ | HTTP agent / dependent item | Назначение |
|---|---|---|
| perimeter.ha.json | GET http://{HOST.CONN}:18200/health/ready, accept 200,503 | Общий JSON агента |
| perimeter.ha.active | $.active, boolean → 0/1 | Ведущий узел |
| perimeter.ha.healthy | $.healthy, boolean → 0/1 | Полная readiness активного стека |
| perimeter.ha.prepared | $.prepared, boolean → 0/1 | Готовность резерва |
| perimeter.ha.faulted | $.faulted, boolean → 0/1 | Карантин после сбоя |
| perimeter.ha.epoch | $.epoch | Поколение ведущего |

Частота 5 секунд. Добавить триггеры «агент недоступен», «ведущий degraded»,
«резерв не подготовлен», «узел в ремонте»; для резервов expected standby не
считать отказом пяти остановленных процессов. Не возвращать perimeter.master.
Grafana получает эти же поля из действующего Zabbix datasource либо /metrics
через Prometheus. Действующие Sentry проекты 21–25 остаются; события HA идут в 24.

RFID operational readiness и качество бизнес-потока проверять отдельно.
Для ведущего узла добавить dependent item из его полного agent JSON:
`$.services.RfidReader.detail.dependencies.business_flow.status` и строку причины
`$.services.RfidReader.detail.dependencies.business_flow.detail`.
`degraded`/`rfid_stale_with_partial_activity_evidence` — предупреждение об отсутствии
RFID при неполной активности; на RFID health также есть `warnings.business_flow`.
При исправных технических зависимостях /health/ready в этом случае HTTP200, чтобы
не запускать бессмысленные HA переключения. Это НЕ доказательство успешного чтения.
`unavailable`, latched fault, stale probe, ошибка транспорта/SQL по-прежнему HTTP503
и обычная детерминированная реакция HA. Срабатывание бизнес-предупреждения требует
отдельного уведомления оператору, даже если `perimeter.ha.healthy=1`.
Этот файл — инструкция; фактическая установка item/trigger в Zabbix не выполнена.
