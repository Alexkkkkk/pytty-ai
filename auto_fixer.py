# -*- coding: utf-8 -*-
"""Фоновый авто-чинильщик pytty-ai (ядро, без привязки к Qt).

Пока пользователь бездействует, ядро:
  1. Смотрит последний вывод терминала — похож ли на проблему.
  2. Спрашивает ИИ план исправления (JSON: шаги с ожидаемым выводом).
  3. Выполняет шаги через send_cmd, проверяет expect в новом выводе.
  4. При успехе дописывает кейс в learned_cases.md («решён»).
  5. При неудаче переспрашивает ИИ с текстом ошибки (до max_attempts),
     потом сдаётся и уходит в cooldown.

Подключение из главного окна (см. патч в putty_ai_win*.py):
    self._fixer = autofix.AutoFixerCore(
        llm_call=self._fixer_llm,            # fn(messages, on_done)
        send_cmd=self._fixer_send,           # fn(text) -> отправка в порт
        get_recent=self._fixer_recent,       # fn() -> str (последние строки)
        is_connected=self._fixer_connected,  # fn() -> bool
        log=lambda m: self.ai_output.appendPlainText(m),
        append_kb=self._fixer_append_kb,     # fn(text) -> запись в БД
        ask_confirm=self._fixer_confirm,     # fn(cmd)->bool или None
    )
    QTimer(3с).timeout -> self._fixer.tick()

Импорт/простой:  import auto_fixer as autofix
"""

import re
import time

try:
    import boot_profiles as bootprof
except ImportError:
    bootprof = None

# ---------- активность пользователя (хуки из Terminal) ----------
_last_output_ts = 0.0
_last_user_ts = 0.0


def note_output():
    global _last_output_ts
    _last_output_ts = time.time()


def note_user():
    global _last_user_ts
    _last_user_ts = time.time()


def idle_seconds():
    return time.time() - max(_last_output_ts, _last_user_ts)


# ---------- детектор «проблемного» вывода ----------
_PROBLEM_PATTERNS = [
    r"error", r"fail", r"panic", r"exception", r"unknown command",
    r"not found", r"denied", r"invalid", r"corrupt", r"bad signature",
    r"bootloop", r"watchdog", r"assert", r"cannot", r"timeout",
    r"no such file", r"permission", r"refused", r"unreachable",
    r"0x[0-9a-f]{8}.*abort", r"data abort", r"prefetch abort",
    r"kernel panic", r"init:.*critical",
]
_IGNORE_PATTERNS = [
    r"^\s*$", r"^\S+>$",                     # чистый prompt — не проблема
    r"usage:", r"syntax",                     # подсказка по команде — норма
]
_RX = [re.compile(p, re.I) for p in _PROBLEM_PATTERNS]
_RX_IGN = [re.compile(p, re.I) for p in _IGNORE_PATTERNS]

DANGEROUS = ("wipe", "erase", "format", "factory", "delete", "rm ",
             "mmc write", "amlmmc write", "store write", "saveenv",
             "usb_partial_upgrade", "spi_wrc", "swuu", "dd ")


def _looks_like_problem(text):
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return False
    tail = "\n".join(lines[-6:])          # последние строки — самое важное
    ign = sum(1 for r in _RX_IGN if r.search(tail))
    hits = sum(1 for r in _RX if r.search(tail))
    return hits >= 1 and ign < hits


# ---------- промпт для ИИ ----------
_SYS = (
    "Ты — автоматический сервисный агент embedded-устройства (ТВ/приставка/роутер), "
    "работающий через UART-консоль загрузчика. Проанализируй вывод терминала и "
    "составь план исправления. Ответь СТРОГО одним JSON-объектом без markdown:\n"
    '{"problem":"краткое описание","steps":[{"cmd":"команда","expect":"фрагмент '
    'вывода подтверждающего успех","timeout_sec":8}],"summary":"что проверить в '
    'конце"}\n'
    "Правила: команды — ТОЛЬКО из разрешённого набора текущего профиля загрузчика; "
    "безопасные проверки (printenv, help, mmc part, read-only) — до любых записей; "
    "один шаг = одна команда; expect — короткий уникальный фрагмент; "
    "если проблема неисправима через консоль — steps пустой, в summary объясни."
)

_USER_TMPL = (
    "[ТЕКУЩИЙ ВЫВОД ТЕРМИНАЛА]\n%s\n\n"
    "[ПРОФИЛЬ ЗАГРУЗЧИКА]\n%s\n\n"
    "Предыдущие попытки не помогли: %s\n"
    "Составь план исправления (JSON)."
)


class AutoFixerCore:
    def __init__(self, llm_call, send_cmd, get_recent, is_connected,
                 log, append_kb, ask_confirm=None,
                 idle_timeout=20, max_attempts=3, cooldown_sec=900):
        self.llm_call = llm_call
        self.send_cmd = send_cmd
        self.get_recent = get_recent
        self.is_connected = is_connected
        self.log = log
        self.append_kb = append_kb
        self.ask_confirm = ask_confirm
        self.idle_timeout = idle_timeout
        self.max_attempts = max_attempts
        self.cooldown_sec = cooldown_sec

        self.state = "IDLE"            # IDLE ASKING RUNNING VERIFY COOLDOWN
        self.attempt = 0
        self.steps = []
        self.step_idx = 0
        self.problem_text = ""
        self.wait_until = 0.0
        self.step_snapshot = ""
        self.fail_history = []
        self.cooldown_until = 0.0

    # ---- главный цикл, вызывается из QTimer ----
    def tick(self):
        if self.state == "COOLDOWN":
            if time.time() >= self.cooldown_until:
                self.state = "IDLE"
            return
        if not self.is_connected():
            self._reset()
            return
        if self.state == "IDLE":
            if idle_seconds() < self.idle_timeout:
                return
            recent = self.get_recent()
            if not _looks_like_problem(recent):
                return
            self.problem_text = recent
            self.attempt = 0
            self.fail_history = []
            self._ask()
        elif self.state == "ASKING":
            return  # ждём ответа llm_call -> _on_plan
        elif self.state == "RUNNING":
            self._run_tick()
        elif self.state == "VERIFY":
            return  # ждём llm_call -> _on_verify

    # ---- спросить ИИ план ----
    def _ask(self):
        profile_hint = bootprof.prompt_hint() if bootprof else "(нет)"
        prev = "; ".join(self.fail_history) if self.fail_history else "нет"
        msgs = [
            {"role": "system", "content": _SYS},
            {"role": "user", "content": _USER_TMPL % (
                self.problem_text, profile_hint, prev)},
        ]
        self.state = "ASKING"
        self.log("[авто-чин]: анализирую проблему, запрашиваю план...\n")
        self.llm_call(msgs, self._on_plan)

    def _on_plan(self, text):
        plan = self._parse_json(text)
        if not plan or not plan.get("steps"):
            self._give_up("ИИ не дал исполняемых шагов: %s" %
                          (plan and plan.get("summary")) or text[:200])
            return
        self.steps = plan["steps"]
        self.step_idx = 0
        self.state = "RUNNING"
        self.log("[авто-чин]: план из %d шага(ов): %s\n" %
                 (len(self.steps), plan.get("problem", "?")))
        self._start_step()

    # ---- выполнение шагов ----
    def _start_step(self):
        st = self.steps[self.step_idx]
        cmd = st.get("cmd", "").strip()
        if not cmd:
            self._step_done(False, "пустая команда в плане")
            return
        if any(d in cmd.lower() for d in DANGEROUS):
            if self.ask_confirm is None or not self.ask_confirm(cmd):
                self._step_done(False, "опасная команда отклонена: " + cmd)
                return
        self.step_snapshot = self.get_recent()
        self.send_cmd(cmd)
        self.wait_until = time.time() + float(st.get("timeout_sec", 8))
        self.log("[авто-чин]: шаг %d/%d: %s\n" %
                 (self.step_idx + 1, len(self.steps), cmd))

    def _run_tick(self):
        if time.time() < self.wait_until:
            return
        st = self.steps[self.step_idx]
        expect = st.get("expect", "")
        new_out = self.get_recent()
        ok = (expect in new_out) if expect else True
        self._step_done(ok, "ожидал '%s'" % expect if expect else "без проверки")

    def _step_done(self, ok, why):
        st = self.steps[self.step_idx]
        cmd = st.get("cmd", "")
        if ok:
            self.log("[авто-чин]: шаг %d ОК (%s)\n" %
                     (self.step_idx + 1, why))
            self.step_idx += 1
            if self.step_idx >= len(self.steps):
                self._verify()
            else:
                self._start_step()
        else:
            self.fail_history.append("шаг '%s': %s" % (cmd, why))
            self.log("[авто-чин]: шаг %d НЕ УДАЛСЯ (%s)\n" %
                     (self.step_idx + 1, why))
            self.attempt += 1
            if self.attempt >= self.max_attempts:
                self._give_up("исчерпаны попытки")
            else:
                self._ask()

    # ---- верификация и запись в БД ----
    def _verify(self):
        msgs = [
            {"role": "system", "content":
                "Ты — верификатор. По текущему выводу терминала определи одним "
                "словом, решена ли исходная проблема. Ответь строго: РЕШЕНО или "
                "НЕ РЕШЕНО и одну строку почему."},
            {"role": "user", "content":
                "[ИСХОДНАЯ ПРОБЛЕМА]\n%s\n\n[ТЕКУЩИЙ ВЫВОД]\n%s\n\n[ВЫПОЛНЕННЫЕ "
                "КОМАНДЫ]\n%s" % (
                    self.problem_text, self.get_recent(),
                    "\n".join(s.get("cmd", "") for s in self.steps))},
        ]
        self.state = "VERIFY"
        self.llm_call(msgs, self._on_verify)

    def _on_verify(self, text):
        if "РЕШЕНО" in text.upper() and "НЕ РЕШЕНО" not in text.upper():
            self.log("[авто-чин]: проблема РЕШЕНА ✔\n")
            cmds = " ; ".join(s.get("cmd", "") for s in self.steps)
            self.append_kb(
                "\n## Решённый кейс (авто-чин)\n\n"
                "**Проблема:** %s\n\n**Лог:**\n```\n%s\n```\n\n"
                "**Решение:** %s\n\n**Результат:** %s\n" % (
                    self.problem_text.strip()[:500],
                    self.problem_text.strip()[-1200:],
                    cmds, text.strip()[:300]))
            self._reset()
        else:
            self.fail_history.append("верификация: " + text.strip()[:200])
            self.log("[авто-чин]: верификация: %s\n" % text.strip()[:200])
            self.attempt += 1
            if self.attempt >= self.max_attempts:
                self._give_up("верификация не пройдена")
            else:
                self._ask()

    # ---- утилиты ----
    def _give_up(self, why):
        self.log("[авто-чин]: сдаюсь (%s). Пауза %d мин.\n" %
                 (why, self.cooldown_sec // 60))
        self.state = "COOLDOWN"
        self.cooldown_until = time.time() + self.cooldown_sec

    def _reset(self):
        self.state = "IDLE"
        self.attempt = 0
        self.steps = []
        self.step_idx = 0
        self.fail_history = []

    @staticmethod
    def _parse_json(text):
        try:
            import json
            m = re.search(r"\{.*\}", text, re.S)
            return json.loads(m.group(0)) if m else None
        except Exception:
            return None
