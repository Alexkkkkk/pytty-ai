# -*- coding: utf-8 -*-
"""Самодиагностика системы pytty-ai: «работает лучше часов».

Запускается при старте GUI и проверяет всё, без чего система деградирует
молча. Чинит тривиальное сама (битые JSON-конфиги, отсутствующие файлы),
о серьёзном докладывает в лог.

    import health_check as health
    ok, report = health.run_all(log=lambda m: ai_output.appendPlainText(m))
"""

import importlib
import json
import os

KB_FILES = ("u_boot_errors_kb.md", "tv_service_kb.md",
            "uart_repair_kb.md", "ufpi_sdmmc_kb.md", "learned_cases.md")
CONFIG_FILES = ("self_config.json", "evo_stats.json")


def _log(log, msg):
    if log:
        log(msg)


def check_modules(log):
    """Все ключевые модули импортируются и в разумном состоянии."""
    report, ok = [], True
    for name, probe in (
            ("boot_profiles", lambda m: len(m.PROFILES) >= 10),
            ("auto_fixer", lambda m: len(m.STRATEGIES) >= 7),
            ("self_evo", lambda m: hasattr(m.SelfEvoCore, "tick"))):
        try:
            m = importlib.import_module(name)
            good = probe(m)
            report.append("модуль %s: %s (%s)" % (
                name, "OK" if good else "ПОДОЗРИТЕЛЬНО",
                getattr(m, "__file__", "?")))
            ok = ok and good
        except ImportError as e:
            report.append("модуль %s: ОТСУТСТВУЕТ (%s)" % (name, e))
            ok = False
    return ok, report


def check_kb_files(log, base="."):
    """Базы знаний на месте. Отсутствующую learned_cases.md создаём."""
    report, ok = [], True
    for name in KB_FILES:
        path = os.path.join(base, name)
        if os.path.exists(path):
            size = os.path.getsize(path)
            status = "OK (%d КБ)" % (size // 1024) if size > 100 \
                else "ПОДОЗРИТЕЛЬНО МАЛ (%d байт)" % size
            if size <= 100:
                ok = False
        else:
            status = "ОТСУТСТВУЕТ"
            ok = False
            if name == "learned_cases.md":
                try:
                    with open(path, "w", encoding="utf-8") as f:
                        f.write("# Решённые кейсы (авто-чин + ручные)\n")
                    status = "СОЗДАН ПУСТОЙ"
                except OSError:
                    pass
        report.append("база %s: %s" % (name, status))
    return ok, report


def check_configs(log, base="."):
    """JSON-конфиги валидны; битые — пересоздаём из дефолтов."""
    report, ok = [], True
    defaults = {
        "self_config.json": {"idle_timeout": 20, "cooldown_scale": 1.0,
                             "min_cases_to_regulate": 5, "auto_update": True,
                             "auto_deploy": False, "update_interval_sec": 3600},
        "evo_stats.json": {"strategies": {}, "total_solved": 0,
                           "regulated_at": 0},
    }
    for name in CONFIG_FILES:
        path = os.path.join(base, name)
        try:
            with open(path, encoding="utf-8") as f:
                json.load(f)
            report.append("конфиг %s: OK" % name)
        except (OSError, ValueError):
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(defaults[name], f, ensure_ascii=False, indent=2)
                report.append("конфиг %s: БЫЛ БИТЫЙ — ВОССТАНОВЛЕН" % name)
            except OSError as e:
                report.append("конфиг %s: ОШИБКА ЗАПИСИ (%s)" % (name, e))
                ok = False
    return ok, report


def run_all(log=None, base="."):
    """Полный чекап. Возвращает (ok, report_text)."""
    results = [check_modules(log), check_kb_files(log, base),
               check_configs(log, base)]
    ok = all(r[0] for r in results)
    lines = ["=== самодиагностика pytty-ai ==="]
    for _, report in results:
        lines.extend("  " + r for r in report)
    lines.append("ИТОГ: %s" % ("ВСЁ В ПОРЯДКЕ" if ok else
                               "ЕСТЬ ПРОБЛЕМЫ — см. выше"))
    text = "\n".join(lines) + "\n"
    _log(log, text)
    return ok, text
