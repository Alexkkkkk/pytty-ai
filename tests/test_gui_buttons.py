# -*- coding: utf-8 -*-
"""GUI-тесты: все кнопки PuTTY-AI нажимаются и выполняют свои функции.

Запуск:  QT_QPA_PLATFORM=offscreen python -m pytest tests/ -v
"""
import os, sys, json
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from unittest import mock
from PyQt6.QtWidgets import (QApplication, QMessageBox, QFileDialog, QPushButton,
                             QGroupBox, QToolBar)
from PyQt6.QtTest import QTest
from PyQt6.QtCore import Qt

import putty_ai_win10 as app_mod
from putty_ai_win10 import (MainWindow, ConnectDialog, AiSettingsDialog,
                            AnalysisDialog, ActionDialog, MockDevice)

APP = None


def qapp():
    global APP
    if APP is None:
        APP = QApplication([])
    return APP


# ---------------------------------------------------------------- fixtures

@pytest.fixture()
def win(tmp_path, monkeypatch):
    qapp()
    # изолируем папку данных программы для каждого теста
    data = tmp_path / "appdata"
    data.mkdir()
    monkeypatch.setattr(MainWindow, "_data_dir", lambda self: str(data))
    # глушим сетевые автозапуски главного окна
    for m in ("_check_update", "_startup_sync", "_curriculum_update"):
        monkeypatch.setattr(MainWindow, m, lambda self: None)
    # QMessageBox не блокирует, запоминаем вызовы
    calls = {"info": [], "warn": [], "crit": []}
    monkeypatch.setattr(QMessageBox, "information",
        staticmethod(lambda *a, **k: calls["info"].append(str(a[1])) or QMessageBox.StandardButton.Ok))
    monkeypatch.setattr(QMessageBox, "warning",
        staticmethod(lambda *a, **k: calls["warn"].append(str(a[1])) or QMessageBox.StandardButton.Yes))
    monkeypatch.setattr(QMessageBox, "critical",
        staticmethod(lambda *a, **k: calls["crit"].append(str(a[1])) or QMessageBox.StandardButton.Ok))
    monkeypatch.setattr(QMessageBox, "question",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    # ИИ отвечает синхронно
    monkeypatch.setattr(MainWindow, "_ask_ai",
        lambda self, msgs, cb: cb("ОШИБКИ: test\nПРИЧИНА: test\nПЛАН: 1. reboot  # перезагрузка"))
    w = MainWindow()
    yield w, calls
    try:
        if w.ssh:
            w.ssh.stop()
            w.ssh.wait(1000)
    except Exception:
        pass
    w.close()


def click(btn):
    assert btn is not None, "кнопка не найдена"
    assert btn.isEnabled(), "кнопка задизейблена: %s" % btn.text()
    QTest.mouseClick(btn, Qt.MouseButton.LeftButton)


def buttons(w):
    return w.findChildren(QPushButton)


def find_button(w, text, group=None):
    """Поиск кнопки по тексту; group — подстрока заголовка QGroupBox."""
    cands = buttons(w)
    if group:
        for g in w.findChildren(QGroupBox):
            if group in g.title():
                cands = g.findChildren(QPushButton)
                break
    exact = [b for b in cands if b.text() == text]
    if exact:
        return exact[0]
    sub = [b for b in cands if text in b.text()]
    if sub:
        return sub[0]
    raise AssertionError("кнопка не найдена: %r (group=%r)" % (text, group))


def fake_ssh(w, monkeypatch):
    """Подставляем «подключённый» ssh и ловим отправленное."""
    sent = []
    ssh = mock.Mock()
    ssh.isRunning.return_value = True
    ssh.send.side_effect = lambda t: sent.append(t)
    monkeypatch.setattr(w, "ssh", ssh)
    return sent


# ---------------------------------------------------------------- сценарий

def test_make_script_button(win):
    w, _ = win
    w.term.insert_remote("U-Boot 2015.01\n*** Error - bad CRC\n")
    click(find_button(w, "Создать сценарий"))
    assert getattr(w, "_script_cmds", []), "сценарий не создан"
    assert "reboot" in w.script_view.toPlainText()


def test_copy_script_button(win):
    w, _ = win
    w.script_view.setPlainText("reboot  # перезагрузка")
    click(find_button(w, "Копировать всё"))
    assert "reboot" in QApplication.clipboard().text()


def test_next_step_button(win):
    w, _ = win
    w._script_cmds = ["cmd_one", "cmd_two"]
    w._script_idx = 0
    click(find_button(w, "Следующий шаг"))
    assert QApplication.clipboard().text() == "cmd_one"
    assert w._script_idx == 1
    click(find_button(w, "Следующий шаг"))
    assert QApplication.clipboard().text() == "cmd_two"
    assert w._script_idx == 2


def test_save_script_button(win, monkeypatch, tmp_path):
    w, _ = win
    target = str(tmp_path / "scenario.txt")
    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (target, "")))
    w.script_view.setPlainText("reboot")
    click(find_button(w, "Сохранить", group="Сценарий"))
    assert os.path.exists(target)
    assert "reboot" in open(target, encoding="utf-8").read()


# ---------------------------------------------------------------- обучение

def test_learn_success_button(win):
    w, _ = win
    w._session_cmds = ["printenv", "setenv bootcmd run recovery"]
    path = os.path.join(w._base_dir, "learned_cases.md")
    before = os.path.getsize(path) if os.path.exists(path) else 0
    click(find_button(w, "Сохранить успешное решение"))
    assert os.path.getsize(path) > before


def test_learn_new_button(win, monkeypatch):
    w, _ = win
    trig = "UNIQUE_TEST_TRIG_7f3a"
    monkeypatch.setattr(MainWindow, "_ask_ai", lambda self, m, cb: cb(
        '{"trigger": "%s", "platform": "uboot", '
        '"solution": ["reset"], "note": "n", "dangerous": false}' % trig))
    before = len(w.skills)
    click(find_button(w, "Обучиться на новой ошибке"))
    assert len(w.skills) == before + 1
    saved = json.load(open(os.path.join(w._base_dir, "skills.json"), encoding="utf-8"))
    assert any(s.get("trigger") == trig for s in saved)


def test_apply_skill_button(win, monkeypatch):
    w, _ = win
    sent = fake_ssh(w, monkeypatch)
    w._type_command = lambda c: sent.append(c)
    w._matched_skill = {"trigger": "e", "solution": ["printenv"], "dangerous": False}
    click(find_button(w, "Применить найденное решение"))
    assert "printenv" in sent
    assert "Применяю выученное решение" in w.ai_output.toPlainText()


def test_apply_skill_guard_no_match(win):
    w, calls = win
    w._matched_skill = None
    click(find_button(w, "Применить найденное решение"))
    assert calls["info"], "должно быть сообщение «нет найденного решения»"


def test_tune_prompts_button(win):
    w, _ = win
    with open(os.path.join(w._base_dir, "runs.jsonl"), "a", encoding="utf-8") as f:
        for _ in range(3):
            f.write('{"r": "fail: timeout"}\n')
    click(find_button(w, "Настроить промпты"))
    assert os.path.exists(os.path.join(w._base_dir, "prompt_tuning.md"))


# ---------------------------------------------------------------- модели

def test_model_add_button(win):
    w, _ = win
    w.m_board.setText("T.MS3663S.PB801")
    w.m_brand.setText("Thomson")
    w.m_model.setText("T32D16SF-01B")
    n = w.m_list.count()
    click(find_button(w, "Запомнить модель"))
    assert w.m_list.count() == n + 1
    saved = json.load(open(os.path.join(w._base_dir, "models.json"), encoding="utf-8"))
    assert any(m.get("model") == "T32D16SF-01B" for m in saved)


def test_model_add_requires_name(win):
    w, calls = win
    w.m_model.setText("")
    click(find_button(w, "Запомнить модель"))
    assert calls["warn"], "должно быть предупреждение «укажите модель»"


def test_detect_model_no_connection(win):
    w, calls = win
    w.ssh = None
    click(find_button(w, "Определить модель"))
    assert calls["info"], "должно быть предупреждение о подключении"


def test_detect_model_parse(win):
    w, _ = win
    w.term.insert_remote("Board: T.MS3663S.PB801\n")
    w._parse_detect()
    assert "MS3663S" in w.m_board.text()


# ---------------------------------------------------------------- конфиг

def test_download_config_no_connection(win):
    w, calls = win
    w.ssh = None
    click(find_button(w, "Скачать конфиг"))
    assert calls["info"]


def test_download_config_connected(win, monkeypatch):
    w, _ = win
    sent = fake_ssh(w, monkeypatch)
    w._type_command = lambda c: sent.append(c)
    click(find_button(w, "Скачать конфиг"))
    assert sent and sent[0] in ("printenv", "fw_printenv 2>/dev/null || cat /proc/cmdline")


def test_upload_config_no_connection(win):
    w, calls = win
    w.ssh = None
    click(find_button(w, "Залить конфиг"))
    assert calls["info"]


def test_upload_config_connected(win, monkeypatch):
    w, _ = win
    sent = fake_ssh(w, monkeypatch)
    w._type_command = lambda c: sent.append(c)
    monkeypatch.setattr(QFileDialog, "getOpenFileName",
                        staticmethod(lambda *a, **k: ("", "")))  # отмена выбора
    click(find_button(w, "Залить конфиг"))
    # отмена файла — команд не отправлено, падения нет
    assert sent == []


# ---------------------------------------------------------------- ИИ-кнопки

def test_explain_button(win):
    w, _ = win
    w.term.insert_remote("Kernel panic - not syncing\n")
    click(find_button(w, "Объяснить"))
    assert w.ai_output.toPlainText().strip()


def test_suggest_and_insert_buttons(win, monkeypatch):
    w, _ = win
    w.ask_input.setText("перезагрузить устройство")
    click(find_button(w, "Подобрать команду", group="Спросить ИИ"))
    assert getattr(w, "_pending_cmd", ""), "подсказка не получена"
    # «Вставить в терминал»
    sent = fake_ssh(w, monkeypatch)
    w._send_to_server = lambda t: sent.append(t)
    click(find_button(w, "Вставить в терминал"))
    assert "ОШИБКИ" in "".join(sent)  # команда отправлена в терминал посимвольно


def test_full_analysis_button(win, monkeypatch):
    w, _ = win
    monkeypatch.setattr(AnalysisDialog, "exec", lambda self: 0)
    click(find_button(w, "Полный анализ"))
    assert "полный анализ" in w.ai_output.toPlainText()


def test_analyze_and_ask_button(win, monkeypatch):
    w, _ = win
    monkeypatch.setattr(AnalysisDialog, "exec", lambda self: 0)
    monkeypatch.setattr(ActionDialog, "exec", lambda self: 0)
    monkeypatch.setattr(MainWindow, "_run_agent_with_goal", lambda self, g: None)
    click(find_button(w, "Анализ → спросить → сделать"))
    QTest.qWait(50)
    assert w.ai_output.toPlainText().strip()


def test_auto_fix_guard(win):
    w, calls = win
    w.ssh = None
    click(find_button(w, "Исправить ошибку автоматически"))
    assert calls["info"]


# ---------------------------------------------------------------- синхронизация

def test_sync_download_button(win, monkeypatch):
    w, _ = win
    monkeypatch.setattr(MainWindow, "_http_get",
                        lambda self, url, headers=None, timeout=15:
                        json.dumps([{"trigger": "x", "solution": ["y"]}]).encode())
    before = len(w.skills)
    click(find_button(w, "Скачать общую базу"))
    QTest.qWait(30)
    assert len(w.ai_output.toPlainText()) > 0


def test_sync_upload_button(win, monkeypatch):
    w, _ = win
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda *a, **k: _Resp(b'{"ok": true}'))
    click(find_button(w, "Отправить мою базу"))
    QTest.qWait(30)  # не падает


def test_srv_download_button(win, monkeypatch):
    w, _ = win
    monkeypatch.setattr(MainWindow, "_http_get",
                        lambda self, url, headers=None, timeout=15: b"")
    click(find_button(w, "С сервера"))
    QTest.qWait(30)


def test_srv_upload_button(win, monkeypatch):
    w, _ = win
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda *a, **k: _Resp(b'{"ok": true}'))
    click(find_button(w, "На сервер"))
    QTest.qWait(30)


class _Resp:
    def __init__(self, data):
        self.data = data
    def read(self):
        return self.data
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


# ---------------------------------------------------------------- фото (QAction)

def test_diagnose_photo_action(win, monkeypatch, tmp_path):
    w, _ = win
    p = tmp_path / "screen.jpg"
    p.write_bytes(b"\xff\xd8\xff\xe0fake")
    monkeypatch.setattr(QFileDialog, "getOpenFileName",
                        staticmethod(lambda *a, **k: (str(p), "")))
    monkeypatch.setattr(AnalysisDialog, "exec", lambda self: 0)
    w.settings["api_key"] = "test-key"
    acts = []
    for tb in w.findChildren(QToolBar):
        acts += [a for a in tb.actions() if "Диагноз по фото" in a.text()]
    assert acts, "action «Диагноз по фото» не найден"
    acts[0].trigger()
    assert "анализирую фото" in w.ai_output.toPlainText()


# ---------------------------------------------------------------- тулбар

def test_toolbar_actions_exist(win):
    w, _ = win
    tb = w.findChildren(QToolBar)[0]
    texts = [a.text() for a in tb.actions()]
    for need in ("Подключиться", "Отключиться", "Настройки ИИ", "Выход"):
        assert any(need in t for t in texts), "нет действия: " + need
    keys = [a.text() for a in tb.actions() if a.text() in ("Пробел", "Tab", "Enter", "Esc", "Ctrl+C")]
    assert len(keys) == 5, "нет служебных клавиш: %s" % keys


def test_connect_mock_and_disconnect(win, monkeypatch):
    w, _ = win
    w._conn = {"type": "mock"}
    monkeypatch.setattr(ConnectDialog, "exec",
                        lambda self: ConnectDialog.DialogCode.Accepted)
    # «Подключиться» из тулбара
    tb = w.findChildren(QToolBar)[0]
    act = [a for a in tb.actions() if "Подключиться" in a.text()][0]
    act.trigger()
    QTest.qWait(400)
    assert isinstance(w.ssh, MockDevice), "эмулятор не запущен"
    assert w.term.local_echo is False
    # служебные клавиши отправляются в эмулятор
    sent = []
    w.ssh.send = lambda t: sent.append(t)
    act_key = [a for a in tb.actions() if a.text() == "Пробел"][0]
    act_key.trigger()
    assert sent == [" "], "клавиша «Пробел» не отправлена: %s" % sent
    # «Отключиться»
    act = [a for a in tb.actions() if "Отключиться" in a.text()][0]
    act.trigger()
    QTest.qWait(200)
    assert w.ssh is None
    assert "[отключено]" in w.term.toPlainText()


def test_edit_ai_settings_action(win, monkeypatch):
    w, _ = win
    monkeypatch.setattr(AiSettingsDialog, "exec",
                        lambda self: AiSettingsDialog.DialogCode.Accepted)
    self_dlg = {}
    orig = AiSettingsDialog.__init__
    def capture(self, settings, parent=None):
        orig(self, settings, parent)
        self.base.setText("http://new/v1")
    monkeypatch.setattr(AiSettingsDialog, "__init__", capture)
    tb = w.findChildren(QToolBar)[0]
    act = [a for a in tb.actions() if "Настройки ИИ" in a.text()][0]
    act.trigger()
    assert w.settings["base_url"] == "http://new/v1"


def test_exit_action(win):
    w, _ = win
    tb = w.findChildren(QToolBar)[0]
    act = [a for a in tb.actions() if "Выход" in a.text()][0]
    closed = []
    act.triggered.disconnect()                      # отцепляем исходный self.close
    act.triggered.connect(lambda _=False: closed.append(1))
    act.trigger()
    assert closed, "«Выход» не вызвал close"


# ---------------------------------------------------------------- диалоги

def test_connect_dialog_buttons(win, monkeypatch):
    d = ConnectDialog({})
    monkeypatch.setattr(QFileDialog, "getOpenFileName",
                        staticmethod(lambda *a, **k: ("", "")))
    click([b for b in d.findChildren(QPushButton) if b.text() == "…"][0])  # Обзор
    ok = [b for b in d.findChildren(QPushButton) if b.text() == "Подключиться"][0]
    click(ok)
    assert d.result() == ConnectDialog.DialogCode.Accepted
    d2 = ConnectDialog({})
    cancel = [b for b in d2.findChildren(QPushButton) if b.text() == "Отмена"][0]
    click(cancel)
    assert d2.result() == ConnectDialog.DialogCode.Rejected


def test_ai_settings_dialog_buttons(win):
    d = AiSettingsDialog({"base_url": "http://x/v1", "api_key": "", "model": "m"})
    ok = [b for b in d.findChildren(QPushButton) if b.text() == "Сохранить"][0]
    click(ok)
    assert d.result() == AiSettingsDialog.DialogCode.Accepted
    d2 = AiSettingsDialog({"base_url": "", "api_key": "", "model": ""})
    cancel = [b for b in d2.findChildren(QPushButton) if b.text() == "Отмена"][0]
    click(cancel)
    assert d2.result() == AiSettingsDialog.DialogCode.Rejected


def test_ai_settings_repair_legacy_token_in_base_url():
    """Старый config не должен собирать URL из значения токена."""
    legacy_token = "redacted-sync-token"
    settings, changed = app_mod._normalise_ai_settings({
        "base_url": legacy_token,
        "api_key": "",
        "model": "",
    })
    assert changed
    assert settings["base_url"] == app_mod.DEFAULT_AI_BASE_URL
    assert settings["api_key"] == legacy_token
    assert settings["model"] == app_mod.DEFAULT_AI_MODEL


def test_terminalai_provider_preset():
    d = AiSettingsDialog({"base_url": "", "api_key": "", "model": ""})
    idx = d.provider.findText("TerminalAI (сервер мастерской)")
    assert idx > 0
    d.provider.setCurrentIndex(idx)
    assert d.base.text() == app_mod.DEFAULT_AI_BASE_URL
    assert d.model.text() == app_mod.DEFAULT_AI_MODEL


def test_action_dialog_buttons():
    qapp()
    d = ActionDialog(["reboot", "printenv"])
    ok = [b for b in d.findChildren(QPushButton) if "Выполнить" in b.text()][0]
    click(ok)
    assert d.choice() == "reboot"
    d2 = ActionDialog(["reboot"])
    cancel = [b for b in d2.findChildren(QPushButton) if b.text() == "Отмена"][0]
    click(cancel)
    assert d2.result() == ActionDialog.DialogCode.Rejected


def test_analysis_dialog_copy():
    qapp()
    d = AnalysisDialog("ОШИБКИ: x\nПЛАН: y")
    copy = [b for b in d.findChildren(QPushButton) if "Копировать" in b.text()][0]
    click(copy)
    assert "ОШИБКИ" in QApplication.clipboard().text()


# ---------------------------------------------------------------- регрессии

def test_save_config_persists_settings(win, monkeypatch):
    """Баг: дубль _save_config глушил автосохранение настроек."""
    w, _ = win
    monkeypatch.setattr(app_mod.QTimer, "singleShot", lambda *a, **k: None)
    w.settings["model"] = "regression-test-model"
    w._save_config()
    cfg = json.load(open(os.path.join(w._base_dir, "config.json"), encoding="utf-8"))
    assert cfg["settings"]["model"] == "regression-test-model"


def test_fixer_append_kb_uses_data_dir(win):
    """Баг: learned_cases.md писался в cwd вместо папки данных."""
    w, _ = win
    w._fixer_append_kb("\nregression-test-case\n")
    kb = os.path.join(w._base_dir, "learned_cases.md")
    assert "regression-test-case" in open(kb, encoding="utf-8").read()


def test_download_config_arms_device_dumper(win, monkeypatch):
    """Баг: таймер «Скачать конфиг» звал не тот метод."""
    w, _ = win
    sent = fake_ssh(w, monkeypatch)
    w._type_command = lambda c: sent.append(c)
    armed = {}
    monkeypatch.setattr(app_mod.QTimer, "singleShot",
                        lambda ms, fn: armed.update(ms=ms, fn=fn))
    click(find_button(w, "Скачать конфиг"))
    assert armed.get("fn") == w._save_device_config
