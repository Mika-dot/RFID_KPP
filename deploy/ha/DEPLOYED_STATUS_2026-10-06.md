# Подтверждённый запуск «Периметра», 06.10.2026 18:54 МСК

## Результат

Пользователь прислал полный вывод физики и ub22. Работоспособность развёрнутого
HA кластера подтверждена на runtime release
**1c7930912ad84e8f205cf16fbdd715c76c9eb74e**, общем **epoch264**.
Operator scripts взяты из143054948f038f30789b31bda0a1a2c105037e56.

| Узел | Роль | active | healthy | prepared | faulted |
|---|---|---|---|---|---|
| physical | Основной исполнитель | true | true | true | false |
| perimetr | Горячий резерв | false | false | true | false |
| comparator | Готовый резерв и действующий контроллер | false | false | true | false |

healthy=false у пассивных резервов соответствует отсутствию активных workers;
критерий готовности резерва — prepared=true, inactive и nonfaulted.

## Подтверждение физики

Checksum resume-evidence.py совпал. Получены:
- HOTFIX_ALL_AGENTS_PREFLIGHT_OK;
- SQL_PROTOCOL2_EXISTING_VERIFIED_NO_MIGRATION;
- HOTFIX_RELEASED_FOR_INDEPENDENT_VERIFICATION для каждого из трёх узлов;
- HA_ACTIVE_PHYSICAL_RESERVES_READY;
- HOTFIX_PROTOCOL2_RUNTIME_RESTORED; BUSINESS_READ_ACCEPTANCE_NOT_PROVEN.

Восстановление после maintenance прошло штатно: первоначально нет owner и все
faulted, затем independent verification снимает fault, Comparator выдаёт lease
physical epoch264. При первом старте RfidReader/RusGuardSync уже готовы,
Yolo/Aggregator/WebDashboard ещё запускаются. Затем **все5 readiness=true** на
7 последовательных выборках с интервалом около10сек, без смены owner/epoch.
Comparator controller lease valid=true во всех выборках. Protocol2 и
operator_maintenance=false на всех3. Никаких forced grants, ручного снятия
quarantine, SQL migration, business-latch reset или synthetic RFID не применялось.

Вывод содержит RFID_BUSINESS_WARNING:
`rfid_historical_activity_evidence_contradicted`.
Это ожидаемое предупреждение об опровергнутом основании старого сохранённого
business fault; transport/storage/весь stack готовы. Сам сохранённый latch не
удаляли. Новый реальный RFID проход и точность классификации катушек ещё не
приняты; новое чтение должно поступить естественным способом. Не объявлять
бизнес-приёмку по readiness, но и не запускать из-за этого новый цикл staging.

## Подтверждение ub22 / Grafana

finish_monitoring.py: /tmp/perimeter-grafana-finish.LKQbqL/finish.py,
SHA2569b9fef3d9a0010b39cf189b6baa79c168451455fcae301a02ab3fb404ef625e3 совпал.
Запущен только --finish-grafana. Получены:
- GRAFANA_FOUR_HA_PANELS_LIVE_AUTOSTART_OBSERVER_OK;
- MONITORING_FINISH_ACTIONS_COMPLETED;
- FINAL_CLUSTER_STATE с тем же physical healthy и двумя prepared reserves,
  все3 exact1c/epoch264/faulted=false;
- FINAL_HA_READY.

Четыре managed HA панели сохранены, все4 live backend queries подтверждены;
external observer enabled/active. Исправлен выбор string полей в Stat для
leader/business (старый screenshot показывал numeric-only НЕТ ДАННЫХ).
Новый скриншот после исправления не предоставлен; backend/save результат доказан.
Dashboard: http://172.31.0.97:3000/d/mositlab-director-wallboard
Zabbix пропущен по прямому разрешению пользователя, новые HA items/triggers не
создавались. Zabbix login не условие работы системы. Уведомления/contact points
не настраивались этим шагом; подтверждены именно данные и панели Grafana.

## Автономная работа и дальнейшие действия

Ранее пользователь подтвердил Windows PerimeterGuardian: SYSTEM, Running,
enabled, boot trigger, ExecutionTimeLimit=PT0S. Обе Linux службы enabled/active,
User=perimeter, Restart=always, KillMode=control-group. Эти настройки данным
rollout не отменялись. Детерминированный controller, SQL fencing и guarded repair
действуют; LLM не принимает решение о переключении.

Система сейчас запущена и оперативно готова. **Не нужны новые команды установки,
повторный resume, ручной контроль терминалов или новые fault-injection тесты.**
Наблюдать эксплуатационные отклонения через Grafana. Обычный реальный проход
нужен лишь для отдельной бизнес-приёмки. Это подтверждение запуска и примерно
минуты стабильных readiness выборок, не доказательство недельной безотказности.

Main пока0b022bf7b0f4eb25f36aa8db5045bc3eacbb1358. Runtime1c установлен оператором
из feature. Автообновление main реализовано, но успешное принятие этого hotfix
именно через main не доказано; main/release metadata/auto-update здесь не меняли.
Решение о публикации production fix в main и проверка adoption — отдельный этап,
который не должен снова останавливать уже работающую систему без основания.
Документировать состояние, не выдавать прежние f5/epoch237 READY за текущий результат.
