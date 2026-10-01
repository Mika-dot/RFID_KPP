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
