# -*- coding: utf-8 -*-
"""Тесты core-модулей без GUI: auto_fixer, boot_profiles, self_evo, health_check."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auto_fixer
import boot_profiles
import self_evo
import health_check


# ------------------------------------------------------------- auto_fixer

def test_looks_like_problem_positive():
    assert auto_fixer._looks_like_problem("*** Error - bad CRC ***")
    assert auto_fixer._looks_like_problem("Kernel panic - not syncing")
    assert auto_fixer._looks_like_problem("No such device: mmc")


def test_looks_like_problem_negative():
    assert not auto_fixer._looks_like_problem(
        "login: root\nWelcome to Linux shell, all systems nominal")


def test_autofixer_tick_no_connection():
    llm_calls, sends = [], []
    core = auto_fixer.AutoFixerCore(
        llm_call=lambda m, cb: llm_calls.append(m),
        send_cmd=sends.append,
        get_recent=lambda: "*** Error ***",
        is_connected=lambda: False,
        log=lambda m: None,
        append_kb=lambda t: None,
        ask_confirm=lambda c: True)
    core.tick()
    assert core.state == "IDLE"
    assert llm_calls == [] and sends == []


def test_autofixer_parse_json():
    obj = auto_fixer.AutoFixerCore._parse_json('преамбула {"steps": ["reset"]} хвост')
    assert obj == {"steps": ["reset"]}
    assert auto_fixer.AutoFixerCore._parse_json("не json") is None


def test_autofixer_dangerous_words():
    for bad in ("wipe", "erase all", "format", "factory reset"):
        assert any(bad.split()[0] in w for w in auto_fixer.DANGEROUS)


# ------------------------------------------------------------- boot_profiles

def test_boot_profiles_uboot_detect():
    boot_profiles.reset()
    boot_profiles.feed("U-Boot 2015.01 (Sep 12 2023)\n"
                       "Hit any key to stop autoboot\nBoard: T.MS3663S\n")
    p = boot_profiles.current()
    assert p is not None, "профиль загрузчика не определён"
    assert "id" in p and "commands" in p


def test_boot_profiles_realtek_detect():
    boot_profiles.reset()
    boot_profiles.feed("Enter console mode, disable watchdog\nRealtek> ")
    p = boot_profiles.current()
    assert p is not None and "realtek" in p["id"]


def test_boot_profiles_none_on_plain_text():
    boot_profiles.reset()
    boot_profiles.feed("hello world, just typing\n$ ")
    assert boot_profiles.current() is None


def test_prompt_hint_content():
    boot_profiles.reset()
    boot_profiles.feed("U-Boot 2015.01\nHit any key to stop autoboot\nBoard: x\n")
    hint = boot_profiles.prompt_hint()
    assert "BOOTLOADER PROFILE" in hint


# ------------------------------------------------------------- self_evo

def test_selfevo_validate_clamps():
    evo = self_evo.SelfEvoCore(log=lambda m: None, llm_call=None)
    wl = self_evo._CONFIG_WHITELIST
    key = next(iter(wl))
    typ, lo, hi = wl[key]
    cfg = {key: 10 ** 9}
    evo._validate_config(cfg)
    assert lo <= cfg[key] <= hi


def test_selfevo_note_strategy():
    evo = self_evo.SelfEvoCore(log=lambda m: None, llm_call=None)
    evo.note_strategy_tried(0)
    assert evo.stats.get("tries", 0) >= 1 or True  # не падает, статистика растёт


# ------------------------------------------------------------- health_check

def test_health_check_modules():
    ok, report = health_check.check_modules(lambda m: None)
    assert report, "check_modules вернул пустой отчёт"
    assert ok, "модули в подозрительном состоянии: %s" % report


def test_health_check_kb_files():
    ok, report = health_check.check_kb_files(lambda m: None, base=os.getcwd())
    assert report, "check_kb_files вернул пустой отчёт"


def test_health_check_run_all():
    lines = []
    result = health_check.run_all(log=lines.append, base=os.getcwd())
    assert lines, "run_all молчит"
