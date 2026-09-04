#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
export_dataset.py — конвертирует накопленные данные PuTTY-AI
(skills.json, learned_cases.md, u_boot_errors_kb.md) в обучающий
датасет формата ShareGPT для дообучения своей модели (LLaMA-Factory,
Unsloth, Axolotl).

Запуск:  python export_dataset.py
Результат: dataset.jsonl рядом со скриптом + статистика.

Формат каждой строки JSONL:
{"conversations": [
    {"from": "system",    "value": "<роль>"},
    {"from": "human",     "value": "<вопрос с ошибкой>"},
    {"from": "gpt",       "value": "<решение: команды + пояснение>"}
]}
"""

import json
import os
import random
import re
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "dataset.jsonl")

SYSTEM = ("Ты — эксперт по восстановлению embedded-устройств (TV-приставки, "
          "Smart-TV) через консоль U-Boot. Отвечай по-русски: называй причину "
          "ошибки и точные команды для исправления. Кратко, без воды.")

QUESTION_TEMPLATES = [
    "Устройство выдаёт ошибку:\n{t}\n\nЧто с этим делать?",
    "В консоли увидел:\n{t}\nКак исправить?",
    "При прошивке появляется:\n{t}\nЧто это значит и как решить?",
    "Помоги разобраться:\n{t}",
    "На экране терминала:\n{t}\nКоманды для восстановления?",
]


def load_skills():
    """skills.json -> список (trigger, solution, note, platform)."""
    path = os.path.join(BASE, "skills.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    rows = []
    for s in data if isinstance(data, list) else []:
        trig = str(s.get("trigger", "")).strip()
        sol = [str(c).strip() for c in s.get("solution", []) if str(c).strip()]
        note = str(s.get("note", "")).strip()
        if trig and sol:
            rows.append((trig, sol, note, s.get("platform", "uboot")))
    return rows


def load_learned_cases():
    """learned_cases.md -> список (problem, solution_commands)."""
    path = os.path.join(BASE, "learned_cases.md")
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return []
    cases = []
    # блоки «## Удачный случай ...» с ``` проблемой и ``` решением
    for m in re.finditer(
            r"## Удачный случай.*?(?=## Удачный случай|\Z)", text, re.S):
        block = m.group(0)
        chunks = re.findall(r"```(.*?)```", block, re.S)
        if len(chunks) >= 2:
            problem = chunks[0].strip()
            solution = [l.strip() for l in chunks[1].splitlines() if l.strip()]
            if problem and solution:
                cases.append((problem, solution))
    return cases


def load_kb_pairs():
    """u_boot_errors_kb.md -> пары (ошибка, решение) из таблиц/списков."""
    path = os.path.join(BASE, "u_boot_errors_kb.md")
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return []
    pairs = []
    # строки вида: `ошибка` -> причина | `ошибка` — причина
    for line in text.splitlines():
        line = line.strip()
        m = re.match(r"^[-*]?\s*`([^`]{8,120})`\s*(?:->|—)\s*(.+)$", line)
        if m:
            pairs.append((m.group(1).strip(), m.group(2).strip()))
    return pairs


def make_example(question, answer_lines, note=""):
    ans = "\n".join(answer_lines)
    if note:
        ans = ans + "\n\n# " + note
    return {"conversations": [
        {"from": "system", "value": SYSTEM},
        {"from": "human", "value": question},
        {"from": "gpt", "value": ans},
    ]}


def main():
    random.seed(42)
    examples = []
    n_skill = n_case = n_kb = 0

    # 1) навыки: по 3 варианта вопроса на каждый
    for trig, sol, note, _plat in load_skills():
        for _ in range(3):
            tpl = random.choice(QUESTION_TEMPLATES)
            examples.append(make_example(tpl.format(t=trig), sol, note))
        n_skill += 1

    # 2) удачные случаи
    for problem, solution in load_learned_cases():
        examples.append(make_example(
            "Устройство выдало такой вывод:\n" + problem, solution))
        n_case += 1

    # 3) знания из базы (ошибка -> что делать)
    for err, fix in load_kb_pairs():
        examples.append(make_example(
            "Объясни ошибку и как её исправить: " + err,
            [fix]))
        n_kb += 1

    random.shuffle(examples)
    with open(OUT, "w", encoding="utf-8") as f:
        for ex in examples:
            f.write(json.dumps(ex, ensure_ascii=False) + "\n")

    print("=" * 50)
    print("Датасет готов: %s" % OUT)
    print("  навыков использовано:   %d (x3 варианта)" % n_skill)
    print("  удачных случаев:        %d" % n_case)
    print("  знаний из базы:         %d" % n_kb)
    print("  ВСЕГО примеров:         %d" % len(examples))
    print("=" * 50)
    if len(examples) < 30:
        print("ВНИМАНИЕ: примеров мало (<30). Покрутите программу,")
        print("наберите навыков кнопкой «Обучиться на новой ошибке»,")
        print("затем запустите этот скрипт снова.")
    print("\nСледующий шаг: откройте colab_train_unsloth.ipynb,")
    print("загрузите dataset.jsonl и запустите все ячейки.")


if __name__ == "__main__":
    main()
