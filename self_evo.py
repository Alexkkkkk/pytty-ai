# -*- coding: utf-8 -*-
"""Саморазвитие, саморегулирование, самообновление, самодеплой pytty-ai.

Четыре контура:
1. САМОРАЗВИТИЕ (обучение на своей эффективности):
   - каждый решённый кейс фиксирует, какая стратегия (попытка №N) сработала;
   - по мере накопления статистики стратегии auto_fixer ПЕРЕУПОРЯДОЧИВАЮТСЯ
     по частоте успеха (bandit-принцип: чаще пробуем то, что чаще работает);
   - статистика по командам/профилям копится для будущего дообучения.
2. САМОРЕГУЛИРОВАНИЕ (ИИ правит собственный конфиг):
   - раз в N решённых кейсов ИИ анализирует статистику и предлагает правки
     self_config.json ТОЛЬКО по белому списку ключей (idle_timeout,
     cooldown_scale); изменения валидируются по типу/диапазону и применяются
     на лету; расширение белого списка — только вручную.
3. САМООБНОВЛЕНИЕ: git fetch + pull из origin/main (таймер), отчёт в лог.
4. САМОДЕПЛОЙ: бамп версии (version.txt) + запуск build_exe_win10.bat
   в subprocess после успешного pull, если в pull вошли изменения.

Использование (см. патч в putty_ai_win*.py):
    self._evo = selfev.SelfEvoCore(log=lambda m: self.ai_output.appendPlainText(m))
    QTimer(60_000).timeout.connect(lambda: self._evo.tick())
    # при успехе чинильщика:
    self._evo.note_success(self._fixer.attempt)
"""

import json
import os
import subprocess
import time

try:
    import auto_fixer as autofix
except ImportError:
    autofix = None

CONFIG_PATH = "self_config.json"
STATS_PATH = "evo_stats.json"
VERSION_PATH = "version.txt"

# Белый список саморегулируемых параметров: ключ -> (тип, мин, макс)
_CONFIG_WHITELIST = {
    "idle_timeout": (int, 5, 120),        # сек простоя до авто-чина
    "cooldown_scale": (float, 0.5, 2.0),  # множитель пауз между попытками
    "min_cases_to_regulate": (int, 3, 50),
}

DEFAULT_CONFIG = {
    "idle_timeout": 20,
    "cooldown_scale": 1.0,
    "min_cases_to_regulate": 5,
    "auto_update": True,       # git pull по таймеру
    "auto_deploy": False,      # пересборка exe после pull (включать осознанно)
    "update_interval_sec": 3600,
}


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except OSError:
        return False


class SelfEvoCore:
    def __init__(self, log=lambda m: None, llm_call=None,
                 apply_config=None, repo_dir="."):
        """llm_call(messages, on_done) — тот же контракт, что у auto_fixer.
        apply_config(dict) — применить валидированный конфиг к живым объектам."""
        self.log = log
        self.llm_call = llm_call
        self.apply_config = apply_config
        self.repo_dir = repo_dir

        self.config = dict(DEFAULT_CONFIG)
        self.config.update(_read_json(CONFIG_PATH, {}))
        self._validate_config(self.config)

        self.stats = _read_json(STATS_PATH, {"strategies": {}, "total_solved": 0,
                                             "regulated_at": 0})
        self._last_update_check = 0.0
        self._busy = False

        # 1. САМОРАЗВИТИЕ: перестановка стратегий по накопленному успеху
        self._reorder_strategies()

    # ---------------- публичный API ----------------

    def note_success(self, attempt_no, strategy_idx=None):
        """Чинильщик решил кейс. attempt_no — номер попытки (с 1)."""
        idx = strategy_idx if strategy_idx is not None \
            else min(max(attempt_no - 1, 0), 6)
        s = self.stats["strategies"].setdefault(
            str(idx), {"tries": 0, "solved": 0})
        s["tries"] += 1
        s["solved"] += 1
        self.stats["total_solved"] += 1
        _write_json(STATS_PATH, self.stats)

    def note_strategy_tried(self, idx):
        s = self.stats["strategies"].setdefault(
            str(idx), {"tries": 0, "solved": 0})
        s["tries"] += 1
        _write_json(STATS_PATH, self.stats)

    def tick(self):
        """Раз в минуту из GUI-таймера: самообновление + саморегулирование."""
        now = time.time()
        if (self.config.get("auto_update")
                and now - self._last_update_check
                >= self.config.get("update_interval_sec", 3600)):
            self._last_update_check = now
            self._self_update()
        if (self.stats["total_solved"] - self.stats.get("regulated_at", 0)
                >= self.config.get("min_cases_to_regulate", 5)):
            self._self_regulate()

    # ---------------- 1. саморазвитие ----------------

    def _reorder_strategies(self):
        """Стратегии с лучшей долей успеха встают раньше (beta-разведка)."""
        if not autofix or not self.stats.get("strategies"):
            return
        rating = []
        for i, strat in enumerate(autofix.STRATEGIES):
            st = self.stats["strategies"].get(str(i), {"tries": 0, "solved": 0})
            # Байесова оценка с приором (правило Лапласа), чтобы новые не глохли
            score = (st["solved"] + 1) / (st["tries"] + 2)
            rating.append((score, i, strat))
        rating.sort(reverse=True)
        new_order = [s for _, _, s in rating]
        if new_order != autofix.STRATEGIES:
            autofix.STRATEGIES[:] = new_order
            self.log("[эво]: стратегии перестроены по успеху: %s\n"
                     % ", ".join("#%d" % i for _, i, _ in rating))

    # ---------------- 2. саморегулирование ----------------

    def _self_regulate(self):
        if self._busy or not self.llm_call:
            return
        self._busy = True
        msgs = [
            {"role": "system", "content":
                "Ты — мета-регулятор сервисного агента. По статистике ниже "
                "предложи корректировки конфига. Ответь СТРОГО одним JSON "
                "объектом: {\"changes\":{\"<ключ>\":<значение>}, "
                "\"reason\":\"кратко\"}. Разрешённые ключи и диапазоны: "
                + json.dumps({k: [t.__name__, lo, hi] for k, (t, lo, hi)
                              in _CONFIG_WHITELIST.items()})
                + ". Если менять нечего — пустой changes."},
            {"role": "user", "content":
                "[СТАТИСТИКА СТРАТЕГИЙ]\n%s\n\n[ТЕКУЩИЙ КОНФИГ]\n%s\n\n"
                "Решено кейсов: %d" % (
                    json.dumps(self.stats["strategies"], ensure_ascii=False),
                    json.dumps(self.config, ensure_ascii=False),
                    self.stats["total_solved"])},
        ]
        def _done(text):
            self._busy = False
            self._apply_llm_config(text)
        self.log("[эво]: саморегулирование — анализ статистики...\n")
        self.llm_call(msgs, _done)

    def _apply_llm_config(self, text):
        try:
            m = text[text.find("{"):text.rfind("}") + 1]
            proposal = json.loads(m)
            changes = proposal.get("changes", {})
        except (ValueError, AttributeError):
            self.log("[эво]: ответ регулятора не разобран, конфиг без изменений\n")
            return
        applied = {}
        for key, val in changes.items():
            rule = _CONFIG_WHITELIST.get(key)
            if not rule:
                continue                      # не из белого списка — отказ
            typ, lo, hi = rule
            try:
                val = typ(val)
            except (TypeError, ValueError):
                continue
            if not (lo <= val <= hi):
                continue                      # вне диапазона — отказ
            self.config[key] = val
            applied[key] = val
        if applied:
            _write_json(CONFIG_PATH, self.config)
            self.stats["regulated_at"] = self.stats["total_solved"]
            _write_json(STATS_PATH, self.stats)
            if self.apply_config:
                self.apply_config(self.config)
            self.log("[эво]: конфиг обновлён ИИ: %s (%s)\n"
                     % (json.dumps(applied, ensure_ascii=False),
                        proposal.get("reason", "")[:150]))
        else:
            self.log("[эво]: регулятор не предложил валидных изменений\n")

    def _validate_config(self, cfg):
        for key, (typ, lo, hi) in _CONFIG_WHITELIST.items():
            if key in cfg:
                try:
                    cfg[key] = typ(cfg[key])
                    cfg[key] = min(max(cfg[key], lo), hi)
                except (TypeError, ValueError):
                    cfg[key] = DEFAULT_CONFIG[key]

    # ---------------- 3. самообновление ----------------

    def _self_update(self):
        def run(args):
            try:
                return subprocess.run(
                    ["git"] + args, cwd=self.repo_dir,
                    capture_output=True, text=True, timeout=60)
            except (OSError, subprocess.TimeoutExpired) as e:
                class R: stdout = ""; stderr = str(e)
                return R()
        run(["fetch", "origin"])
        status = run(["status", "-sb"])
        behind = "behind" in (status.stdout or "")
        if not behind:
            self.log("[эво]: обновлений нет\n")
            return
        self.log("[эво]: найдены обновления на GitHub, применяю git pull...\n")
        pull = run(["pull", "--ff-only", "origin", "main"])
        if pull.returncode != 0:
            self.log("[эво]: pull не удался: %s\n" % (pull.stderr or "")[:200])
            return
        self.log("[эво]: код обновлён ✔\n")
        self._bump_version()
        if self.config.get("auto_deploy"):
            self._self_deploy()

    # ---------------- 4. самодеплой ----------------

    def _bump_version(self):
        try:
            with open(VERSION_PATH, encoding="utf-8") as f:
                parts = f.read().strip().split(".")
            parts = (parts + ["0"])[:3]
            parts[2] = str(int(parts[2]) + 1)
            ver = ".".join(parts)
            with open(VERSION_PATH, "w", encoding="utf-8") as f:
                f.write(ver + "\n")
            self.log("[эво]: версия повышена до %s\n" % ver)
        except (OSError, ValueError):
            pass

    def _self_deploy(self):
        bat = ("build_exe_win10.bat" if os.path.exists("build_exe_win10.bat")
               else "build_exe_win7.bat")
        if not os.path.exists(bat):
            self.log("[эво]: скрипт сборки %s не найден\n" % bat)
            return
        self.log("[эво]: самодеплой — запускаю %s...\n" % bat)
        try:
            subprocess.Popen([bat], cwd=self.repo_dir, shell=True,
                             creationflags=getattr(
                                 subprocess, "CREATE_NO_WINDOW", 0))
            self.log("[эво]: сборка запущена в фоне, exe обновится по завершении\n")
        except OSError as e:
            self.log("[эво]: деплой не запустился: %s\n" % e)

    # ---------------- статистика для интерфейса ----------------

    def report(self):
        lines = ["Решено кейсов: %d" % self.stats["total_solved"]]
        for i in sorted(self.stats["strategies"], key=int):
            st = self.stats["strategies"][i]
            rate = 100.0 * st["solved"] / max(st["tries"], 1)
            lines.append("Стратегия %s: %d/%d (%.0f%%)" %
                         (i, st["solved"], st["tries"], rate))
        lines.append("Конфиг: %s" % json.dumps(self.config, ensure_ascii=False))
        return "\n".join(lines)
