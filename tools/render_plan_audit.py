"""Render a counted audit with explicit evidence and missing work, without services."""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LABELS = {"SOFTWARE_READY": "ПО готово", "PARTIAL": "Частично", "BLOCKED": "Открыто: оборудование/время", "DEFERRED": "Отложено"}


def validate(audit):
    plan = (ROOT / audit["source_plan"]).read_text(encoding="utf-8")
    section = plan.split("# 7.", 1)[1].split("# 8.", 1)[0]
    count = len(re.findall(r"^- \[ \]", section, re.M))
    if len(audit["tasks"]) != count:
        raise ValueError("OriginalPlanTaskCountMismatch")
    for group in ("tasks", "additions"):
        ids = [row["id"] for row in audit[group]]
        if len(ids) != len(set(ids)):
            raise ValueError("DuplicateAuditTask")
        for row in audit[group]:
            if row["status"] not in LABELS or not row.get("remaining"):
                raise ValueError("InvalidAuditTaskStatus")
            for path in row["evidence"]:
                resolved = (ROOT / path).resolve()
                if not resolved.is_relative_to(ROOT) or not resolved.is_file():
                    raise ValueError("AuditEvidenceMissing:" + path)
    return audit


def render(audit):
    validate(audit)
    counts = Counter(row["status"] for row in audit["tasks"])
    additions = Counter(row["status"] for row in audit["additions"])
    lines = ["# Сверка плана с фактом — 08.10.2026", "",
        "Считаются **33 пункта раздела 7 исходного плана**. Повторяющиеся критерии",
        "приёмки из раздела 8 не увеличивают знаменатель. Дополнения пользователя",
        "считаются отдельно: они уточняют часть тех же требований.", "",
        f"**ПО готово: {counts['SOFTWARE_READY']}; частично: {counts['PARTIAL']}; открыто из-за оборудования/времени: {counts['BLOCKED']}; отложено: {counts['DEFERRED']}.**",
        "", f"Дополнения 08.10: **{len(audit['additions'])} критериев, {additions['SOFTWARE_READY']} готовы программно, {additions['PARTIAL']} частично**.", "",
        "Готовность ПО означает реализацию и offline-проверку. Новая ветка не установлена",
        "на оборудование; текущая работа завода из этой среды не проверена. TCP Grafana",
        "и трёх Guardian недоступен. Main/прежний принятый runtime не менялись.", "",
        "## По этапам", "", "| Этап | Всего | ПО готово | Частично | Открыто | Отложено |", "|---|---:|---:|---:|---:|---:|"]
    for stage in range(1, 7):
        rows = [row for row in audit["tasks"] if row["stage"] == stage]
        count = Counter(row["status"] for row in rows)
        lines.append(f"| {stage} | {len(rows)} | {count['SOFTWARE_READY']} | {count['PARTIAL']} | {count['BLOCKED']} | {count['DEFERRED']} |")
    lines += ["", "## Проверка каждого пункта", "", "| ID | Требование | Статус | Что ещё требуется |", "|---|---|---|---|"]
    for row in audit["tasks"] + audit["additions"]:
        remaining = row["remaining"].replace("|", "/")
        lines.append(f"| {row['id']} | {row['requirement']} | {LABELS[row['status']]} | {remaining} |")
    lines += ["", "## Что доделано после повторной сверки", "",
        "- Зеркало расширено до шести потоков: итоговые события, Warehouse/1C, raw RFID, video metadata и СКУД без фото/персональных полей.",
        "- GET-only installation gate проверяет минимум 93 дня, все шесть свежих caught-up потоков, архив и резервный просмотр на каждом узле: `--require-local-copies`.",
        "- Незавершённый backfill/ошибка копии не рисуются готовым резервом. Fresh mirror выбирается прежде более нового ошибочного или неполного.",
        "- Grafana показывает фактические repair verification/update trial/quarantine и наблюдаемый readiness RTO. Это не готовность LLM и не физический RTO.",
        "- Ретроспективный поиск восстанавливает session candidates штатным sessionizer, показывает направление/время/границы/score. Кандидаты не объявляются подтверждёнными проходами и не обучают модель.",
        "- Добавлены 12 обезличенных фрагментов записанной CSV-истории к 15 детерминированным моделям: всего 27 golden traces. 328 read occurrences, 203 distinct source reads; повторяющиеся окна не считаются независимыми экспериментами.",
        "- Expected recorded results получены из чистого pinned PR #8 (`1d59061...`). Это behavioral regression reference, не независимая разметка физической истины. Записанного video в этих фрагментах нет.",
        "- Добавлен авторизованный `/catalog` Behavior Observer: измеренные, недоступные и неинструментированные метрики различаются; пропуски не заполняются нулями.", "",
        "## Оставшаяся детализация", "",
        f"Каталог содержит {audit['metric_catalog']['instrumented_definitions']} определений доступных адаптеров и {audit['metric_catalog']['not_instrumented_definitions']} `not_instrumented` позиции из расширенного wishlist раздела 5.",
        "Перечень метрик (пункт 5.1) составлен; наличие перечня не означает, что весь расширенный wishlist уже измеряется. См. `observer/catalog.py`.",
        "Для всех production-сценариев требуются установка, реальные данные и отдельная приёмка. Поддержка fencing interface не означает готовый аппаратный адаптер; SQL HA listener/VIP/failure domains остаются инфраструктурой.", "",
        "## Свидетельства", ""]
    for row in audit["tasks"] + audit["additions"]:
        if row["evidence"]:
            refs = ", ".join(f"[{path}](../../{path})" for path in row["evidence"])
            lines.append(f"- {row['id']}: {refs}")
    lines += ["", "Машиночитаемый источник: [PLAN_AUDIT_2026-10-08.json](PLAN_AUDIT_2026-10-08.json).",
              "Продолжение/точный head/CI: [PR #9](https://github.com/Mika-dot/RFID_KPP/pull/9) и [handoff](CONTINUE_HERE_2026-10-08.md).", ""]
    return "\n".join(lines)


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--input", type=Path, default=ROOT / "deploy/ha/PLAN_AUDIT_2026-10-08.json")
    p.add_argument("--output", type=Path, default=ROOT / "deploy/ha/PLAN_AUDIT_2026-10-08.md")
    a = p.parse_args(argv)
    a.output.write_text(render(json.loads(a.input.read_text(encoding="utf-8"))), encoding="utf-8")
    print("PLAN_AUDIT_COUNTS_AND_EVIDENCE_VERIFIED")


if __name__ == "__main__":
    main()
