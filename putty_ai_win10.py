#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PuTTY-AI — SSH-клиент с ИИ-помощником.

Возможности:
  - подключение по SSH (пароль или ключ);
  - терминал с историей команд (стрелки вверх/вниз);
  - ИИ: объяснение вывода/ошибок, подбор команды по описанию,
    автодополнение по Tab;
  - ИИ работает через OpenAI-совместимый API (Ollama локально или OpenAI).

Запуск:  python putty_ai.py
"""

import sys
import os
import json
import socket
import urllib.request

try:
    import paramiko
except ImportError:
    paramiko = None

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QDialog, QWidget, QVBoxLayout, QHBoxLayout,
    QFormLayout, QLineEdit, QSpinBox, QComboBox, QCheckBox, QPushButton,
    QLabel, QPlainTextEdit, QToolBar, QDockWidget, QMessageBox, QFileDialog,
    QGroupBox
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt6.QtGui import QAction, QFont, QTextCursor


# ---------------------------------------------------------------------------
# Терминал
# ---------------------------------------------------------------------------
class Terminal(QPlainTextEdit):
    """Простой эмулятор терминала с локальным эхом и историей команд."""

    sendText = pyqtSignal(str)               # отправить данные на сервер
    autocompleteRequested = pyqtSignal(str)  # запрос автодополнения (Tab)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFont(QFont("Consolas", 11))
        self.setStyleSheet(
            "QPlainTextEdit { background: #1e1e1e; color: #d4d4d4; }")
        self.setReadOnly(False)
        self.buffer = ""       # текущая вводимая строка (локальное эхо)
        self.history = []
        self.hist_idx = 0
        self.connected = False

    def set_connected(self, flag: bool):
        self.connected = flag
        self.buffer = ""

    # ---- вывод от сервера ----
    def insert_remote(self, text: str):
        self.moveCursor(QTextCursor.MoveOperation.End)
        self.insertPlainText(text)
        self.moveCursor(QTextCursor.MoveOperation.End)
        sb = self.verticalScrollBar()
        sb.setValue(sb.maximum())

    def last_output(self, n=80):
        return "\n".join(self.toPlainText().splitlines()[-n:])

    # ---- локальное редактирование ----
    def _replace_buffer(self, new: str):
        """Стереть набранную строку с экрана и показать новую (история)."""
        self.moveCursor(QTextCursor.MoveOperation.End)
        tc = self.textCursor()
        for _ in range(len(self.buffer)):
            tc.deletePreviousChar()
        self.buffer = new
        self.insertPlainText(new)

    def insert_suggestion(self, rest: str):
        """Вставить автодополнение (и показать, и отправить на сервер)."""
        if not rest:
            return
        self.buffer += rest
        self.insertPlainText(rest)
        self.sendText.emit(rest)

    def keyPressEvent(self, e):
        key = e.key()
        mods = e.modifiers()

        # Ctrl+C / Ctrl+L
        if mods & Qt.KeyboardModifier.ControlModifier:
            if key == Qt.Key.Key_C:
                self.sendText.emit("\x03")
                self.buffer = ""
                return
            if key == Qt.Key.Key_L:
                self.clear()
                return

        # Enter
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self.buffer.strip():
                self.history.append(self.buffer)
            self.hist_idx = len(self.history)
            self.buffer = ""
            self.sendText.emit("\n")
            return

        # Backspace
        if key == Qt.Key.Key_Backspace:
            if self.buffer:
                self.buffer = self.buffer[:-1]
                self.sendText.emit("\x7f")
                self.moveCursor(QTextCursor.MoveOperation.End)
                self.textCursor().deletePreviousChar()
            return

        # История
        if key == Qt.Key.Key_Up:
            if self.hist_idx > 0:
                self.hist_idx -= 1
                self._replace_buffer(self.history[self.hist_idx])
            return
        if key == Qt.Key.Key_Down:
            if self.hist_idx < len(self.history) - 1:
                self.hist_idx += 1
                self._replace_buffer(self.history[self.hist_idx])
            elif self.hist_idx == len(self.history) - 1:
                self.hist_idx = len(self.history)
                self._replace_buffer("")
            return

        # Tab -> автодополнение через ИИ
        if key == Qt.Key.Key_Tab:
            if self.buffer.strip():
                self.autocompleteRequested.emit(self.buffer)
            return

        # Печатные символы -> локальное эхо + отправка
        text = e.text()
        if text and not (mods & Qt.KeyboardModifier.ControlModifier):
            self.buffer += text
            self.insertPlainText(text)
            self.sendText.emit(text)
            return

        super().keyPressEvent(e)


# ---------------------------------------------------------------------------
# SSH-поток
# ---------------------------------------------------------------------------
class SshWorker(QThread):
    dataReceived = pyqtSignal(str)
    connected = pyqtSignal()
    disconnected = pyqtSignal(str)   # пустая строка = штатное отключение

    def __init__(self, host, port, user, password, keyfile, parent=None):
        super().__init__(parent)
        self.host, self.port, self.user = host, port, user
        self.password, self.keyfile = password, keyfile
        self._stop = False
        self.client = None
        self.channel = None

    def run(self):
        try:
            self.client = paramiko.SSHClient()
            self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            kw = dict(hostname=self.host, port=self.port,
                      username=self.user, timeout=12,
                      look_for_keys=False, allow_agent=False)
            if self.keyfile:
                kw["key_filename"] = self.keyfile
                kw["passphrase"] = self.password or None
            else:
                kw["password"] = self.password
            self.client.connect(**kw)

            self.channel = self.client.invoke_shell(term="xterm",
                                                    width=140, height=34)
            self.channel.settimeout(0.2)
            # Эхо делаем локально -> отключаем эхо сервера
            self.channel.send("stty -echo\n")
            self.connected.emit()

            while not self._stop:
                try:
                    data = self.channel.recv(4096)
                except socket.timeout:
                    continue
                if not data:
                    break
                self.dataReceived.emit(data.decode("utf-8", errors="replace"))
        except Exception as ex:
            self.disconnected.emit(str(ex))
            return
        self.disconnected.emit("")

    def send(self, text: str):
        if self.channel:
            try:
                self.channel.send(text)
            except Exception:
                pass

    def stop(self):
        self._stop = True
        try:
            if self.channel:
                self.channel.close()
            if self.client:
                self.client.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# ИИ-поток (OpenAI-совместимый API)
# ---------------------------------------------------------------------------
class AiWorker(QThread):
    result = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, settings, messages, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.messages = messages

    def run(self):
        try:
            payload = {
                "model": self.settings["model"],
                "messages": self.messages,
                "temperature": 0.2,
                "stream": False,
            }
            headers = {"Content-Type": "application/json"}
            if self.settings.get("api_key"):
                headers["Authorization"] = \
                    "Bearer " + self.settings["api_key"]
            req = urllib.request.Request(
                self.settings["base_url"].rstrip("/") + "/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers=headers)
            with urllib.request.urlopen(req, timeout=90) as r:
                resp = json.loads(r.read().decode("utf-8"))
            text = resp["choices"][0]["message"]["content"].strip()
            self.result.emit(text)
        except Exception as ex:
            self.failed.emit(str(ex))


# ---------------------------------------------------------------------------
# Диалоги
# ---------------------------------------------------------------------------
class ConnectDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Подключение по SSH")
        self.setMinimumWidth(360)
        lay = QFormLayout(self)

        self.host = QLineEdit("127.0.0.1")
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(22)
        self.user = QLineEdit()
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.keyfile = QLineEdit()
        btn_browse = QPushButton("…")
        btn_browse.setFixedWidth(32)
        btn_browse.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.addWidget(self.keyfile)
        row.addWidget(btn_browse)

        lay.addRow("Хост:", self.host)
        lay.addRow("Порт:", self.port)
        lay.addRow("Пользователь:", self.user)
        lay.addRow("Пароль / passphrase:", self.password)
        lay.addRow("Ключ (необяз.):", row)

        btns = QHBoxLayout()
        ok = QPushButton("Подключиться")
        cancel = QPushButton("Отмена")
        ok.clicked.connect(self.accept)
        cancel.clicked.connect(self.reject)
        btns.addStretch(1)
        btns.addWidget(ok)
        btns.addWidget(cancel)
        lay.addRow(btns)

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(self, "Приватный ключ")
        if path:
            self.keyfile.setText(path)


class AiSettingsDialog(QDialog):
    """Настройки ИИ: по умолчанию локальная Ollama."""

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Настройки ИИ")
        self.setMinimumWidth(420)
        lay = QFormLayout(self)

        self.base = QLineEdit(settings["base_url"])
        self.key = QLineEdit(settings.get("api_key", ""))
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.model = QLineEdit(settings["model"])

        lay.addRow("API URL:", self.base)
        lay.addRow("API-ключ:", self.key)
        lay.addRow("Модель:", self.model)
        lay.addRow(QLabel(
            "Локально (Ollama): http://localhost:11434/v1, ключ пустой.\n"
            "OpenAI: https://api.openai.com/v1 + ваш ключ."))

        btns = QHBoxLayout()
        ok = QPushButton("Сохранить")
        cancel = QPushButton("Отмена")
        ok.clicked.connect(self.accept)
        cancel.clicked.connect(self.reject)
        btns.addStretch(1)
        btns.addWidget(ok)
        btns.addWidget(cancel)
        lay.addRow(btns)


# ---------------------------------------------------------------------------
# Главное окно
# ---------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PuTTY-AI — SSH-клиент с ИИ-помощником")
        self.resize(1000, 640)

        self.settings = {
            "base_url": "http://localhost:11434/v1",   # Ollama по умолчанию
            "api_key": "",
            "model": "qwen2.5-coder",
        }
        # --- база знаний по U-Boot (файл u_boot_errors_kb.md рядом с программой) ---
        self.kb_text = self._load_kb()

        self.ssh = None
        self._ai_busy = False
        self._ac_cache = {}
        self._agent_active = False
        self._agent_last_cmd = ""

        # --- терминал ---
        self.term = Terminal()
        self.term.insert_remote(
            "PuTTY-AI\nПодключитесь через меню «Подключение».\n"
            "ИИ-помощник: правая панель, автодополнение по Tab.\n\n")
        self.setCentralWidget(self.term)

        self.term.sendText.connect(self._send_to_server)
        self.term.autocompleteRequested.connect(self._autocomplete)

        # --- панель ИИ ---
        self._build_ai_panel()
        self._build_toolbar()

    def _load_kb(self):
        """Загружает базу знаний по ошибкам U-Boot (рядом с exe/скриптом)."""
        dirs = [os.path.dirname(os.path.abspath(sys.argv[0]))]
        if hasattr(sys, "_MEIPASS"):  # PyInstaller --onefile
            dirs.append(sys._MEIPASS)
        for d in dirs:
            path = os.path.join(d, "u_boot_errors_kb.md")
            try:
                with open(path, encoding="utf-8") as f:
                    return f.read()
            except OSError:
                continue
        return ""

    def _system_prompt(self):
        """Системный промпт с учётом профиля и базы знаний."""
        profile = self.profile_combo.currentData()
        kb = ""
        if self.kb_text:
            kb = ("\n\nБаза знаний по ошибкам U-Boot/прошивке (опирайся на неё, "
                  "но не цитируй дословно):\n" + self.kb_text)
        if profile == "uboot":
            return (
                "Ты — эксперт по загрузчику U-Boot и прошивке embedded-устройств "
                "(TV-приставки, роутеры; SoC Amlogic/MediaTek/M7332). Пользователь "
                "работает в консоли U-Boot через UART (PuTTY). Отвечай по-русски: "
                "1) причина ошибки, 2) точные команды для исправления, 3) чем "
                "проверить результат. Кратко, без воды." + kb)
        return (
            "Ты помощник в терминале Linux. Объясни по-русски кратко: "
            "есть ли в выводе ошибки, почему они возникли и как "
            "исправить. Если всё в порядке — скажи об этом одной фразой." + kb)

    def _cmd_prompt(self):
        if self.profile_combo.currentData() == "uboot":
            return ("Ты — эксперт по U-Boot. Пользователь описывает задачу по "
                    "прошивке/восстановлению устройства — верни ТОЛЬКО одну "
                    "команду U-Boot (или короткую последовательность через ; ) "
                    "без пояснений и без markdown.")
        return ("Ты помощник в терминале Linux. Пользователь описывает "
                "задачу — верни ТОЛЬКО одну команду bash без пояснений, "
                "без markdown и без кавычек вокруг команды.")

    def _ac_prompt(self):
        if self.profile_combo.currentData() == "uboot":
            return ("Дополни начало команды U-Boot. Ответь ТОЛЬКО продолжением "
                    "текста (без повтора введённого), либо пустой строкой, "
                    "если не уверен. Без пояснений и markdown.")
        return ("Дополни начало команды bash. Ответь ТОЛЬКО продолжением "
                "текста (без повтора введённого), либо пустой строкой, "
                "если не уверен. Без пояснений и markdown.")

    # ---------- Авто-исправление (агент) ----------
    def _agent_prompt(self):
        kb = ("\n\nБаза знаний (опирайся на неё):\n" + self.kb_text) \
            if self.kb_text else ""
        if self.profile_combo.currentData() == "uboot":
            return (
                "Ты — автоматический агент, управляющий консолью U-Boot "
                "(прошивка embedded-устройств: M7332, Amlogic, TV-приставки). "
                "Твоя цель — найти и исправить ошибку в выводе терминала. "
                "Правила:\n"
                "- Отвечай ТОЛЬКО командой U-Boot (можно несколько строк) для "
                "следующего шага, без пояснений, без markdown, без кавычек.\n"
                "- Если нужно подождать завершения долгой операции "
                "(например прошивки) — ответь WAIT.\n"
                "- Если проблема решена или дальше нужен человек — DONE.\n"
                "- Перед прошивкой обязательно проверь флешку: usb start, "
                "затем fatls usb 0:1 /.\n"
                "- Действуй пошагово: после каждой команды получишь её вывод."
                + kb)
        return (
            "Ты — автоматический агент в терминале Linux. Цель — исправить "
            "ошибку из вывода терминала. Отвечай ТОЛЬКО командой bash для "
            "следующего шага (без пояснений и markdown), WAIT — если нужно "
            "подождать, DONE — когда проблема решена.")

    def _auto_fix(self):
        if self._ai_busy:
            self.ai_output.appendPlainText("…ИИ занят, подождите…")
            return
        if self._agent_active:
            self._agent_finish("остановлен (повторный запуск)")
            return
        if not (self.ssh and self.ssh.isRunning()):
            QMessageBox.information(
                self, "Авто-исправление",
                "Нужно подключиться через «Подключиться»,\n"
                "чтобы программа могла сама выполнять команды в терминале.")
            return
        self._ai_busy = True
        self._agent_active = True
        self._agent_steps = 0
        self._agent_history = [
            {"role": "system", "content": self._agent_prompt()}]
        out = self.term.last_output(100)
        self._agent_history.append(
            {"role": "user", "content":
                "Текущий вывод терминала (в нём есть ошибка — исправь):\n"
                + out})
        self.ai_output.appendPlainText("— агент запущен, анализирую…\n")
        self.agent_status.setText("агент работает…")
        self._agent_request()

    def _agent_request(self):
        self.worker = AiWorker(self.settings, self._agent_history[-30:])
        self.worker.result.connect(self._agent_got)
        self.worker.failed.connect(self._agent_fail)
        self.worker.start()

    def _agent_fail(self, err):
        self.ai_output.appendPlainText("[агент: ошибка ИИ: %s]\n" % err)
        self._agent_finish("ошибка ИИ")

    def _agent_finish(self, msg):
        self._agent_active = False
        self._ai_busy = False
        self.agent_status.setText("готово: " + msg)
        self.ai_output.appendPlainText("— агент завершил работу (%s)\n" % msg)

    def _agent_got(self, text):
        if not self._agent_active:
            return
        cmd = text.strip().strip("`").strip()
        up = cmd.upper()
        if not cmd or up.startswith("DONE"):
            self._agent_finish("проблема решена или нужен человек")
            return
        if self._agent_steps >= self.agent_steps_spin.value():
            self._agent_finish("достигнут лимит шагов")
            return
        if up.startswith("WAIT"):
            self.ai_output.appendPlainText("…жду завершения операции…")
            self.agent_status.setText("ожидание… (шаг %d)" % self._agent_steps)
            QTimer.singleShot(self.agent_delay_spin.value() * 1000,
                              self._agent_observe)
            return
        if self.chk_confirm.isChecked():
            ret = QMessageBox.question(
                self, "Авто-исправление — шаг %d" % (self._agent_steps + 1),
                "Выполнить команду?\n\n" + cmd,
                QMessageBox.StandardButton.Yes |
                QMessageBox.StandardButton.No)
            if ret != QMessageBox.StandardButton.Yes:
                self._agent_finish("отменено пользователем")
                return
        self._agent_steps += 1
        self._agent_last_cmd = cmd
        self.ai_output.appendPlainText("> " + cmd)
        self.agent_status.setText("шаг %d: %s"
                                  % (self._agent_steps,
                                     cmd.splitlines()[0][:60]))
        self._type_command(cmd)
        QTimer.singleShot(self.agent_delay_spin.value() * 1000,
                          self._agent_observe)

    def _agent_observe(self):
        if not self._agent_active:
            return
        out = self.term.last_output(100)
        self._agent_history.append(
            {"role": "assistant", "content": self._agent_last_cmd})
        self._agent_history.append(
            {"role": "user", "content": "Вывод после команды:\n" + out})
        self._agent_request()

    def _type_command(self, cmd):
        """Ввести команду в терминал, как если бы её набрал пользователь."""
        if self.term.buffer.strip():
            self.term.insert_remote("\n")
            self._send_to_server("\n")
            self.term.buffer = ""
        for ch in cmd:
            self.term.buffer += ch
            self.term.insertPlainText(ch)
            self._send_to_server(ch)
        self.term.insert_remote("\n")
        self._send_to_server("\n")
        self.term.buffer = ""

    # ---------- UI ----------
    def _build_toolbar(self):
        tb = QToolBar("Главная")
        self.addToolBar(tb)

        act_connect = QAction("Подключиться", self)
        act_connect.triggered.connect(self.connect_ssh)
        act_disconnect = QAction("Отключиться", self)
        act_disconnect.triggered.connect(self.disconnect_ssh)
        act_ai = QAction("Настройки ИИ", self)
        act_ai.triggered.connect(self.edit_ai_settings)
        act_exit = QAction("Выход", self)
        act_exit.triggered.connect(self.close)

        tb.addAction(act_connect)
        tb.addAction(act_disconnect)
        tb.addSeparator()
        tb.addAction(act_ai)
        tb.addSeparator()
        tb.addAction(act_exit)

    def _build_ai_panel(self):
        panel = QWidget()
        lay = QVBoxLayout(panel)

        # -- профиль помощника --
        self.profile_combo = QComboBox()
        self.profile_combo.addItem("Профиль: Linux shell", "linux")
        self.profile_combo.addItem("Профиль: U-Boot / прошивка устройств", "uboot")
        lay.addWidget(self.profile_combo)

        # -- авто-исправление (агент) --
        g0 = QGroupBox("Авто-исправление (ИИ сам выполняет команды)")
        l0 = QVBoxLayout(g0)
        self.chk_confirm = QCheckBox(
            "Спрашивать подтверждение перед каждой командой")
        self.chk_confirm.setChecked(True)
        row0 = QHBoxLayout()
        row0.addWidget(QLabel("Макс. шагов:"))
        self.agent_steps_spin = QSpinBox()
        self.agent_steps_spin.setRange(2, 20)
        self.agent_steps_spin.setValue(8)
        row0.addWidget(self.agent_steps_spin)
        row0.addWidget(QLabel("Пауза, сек:"))
        self.agent_delay_spin = QSpinBox()
        self.agent_delay_spin.setRange(2, 120)
        self.agent_delay_spin.setValue(8)
        row0.addWidget(self.agent_delay_spin)
        row0.addStretch(1)
        btn_agent = QPushButton("Исправить ошибку автоматически")
        btn_agent.clicked.connect(self._auto_fix)
        self.agent_status = QLabel("агент не запущен")
        self.agent_status.setWordWrap(True)
        l0.addWidget(self.chk_confirm)
        l0.addLayout(row0)
        l0.addWidget(btn_agent)
        l0.addWidget(self.agent_status)
        lay.addWidget(g0)

        # -- объяснение вывода --
        g1 = QGroupBox("Объяснить вывод терминала")
        l1 = QVBoxLayout(g1)
        btn_explain = QPushButton("Объяснить / найти ошибки")
        btn_explain.clicked.connect(self.explain_output)
        l1.addWidget(btn_explain)

        # -- подбор команды --
        g2 = QGroupBox("Спросить ИИ: какую команду ввести?")
        l2 = QVBoxLayout(g2)
        self.ask_input = QLineEdit()
        self.ask_input.setPlaceholderText(
            "например: найти все файлы больше 100 МБ")
        btn_ask = QPushButton("Подобрать команду")
        btn_ask.clicked.connect(self.suggest_command)
        self.suggestion = QLabel("—")
        self.suggestion.setWordWrap(True)
        self.suggestion.setStyleSheet(
            "QLabel { color: #2e7d32; font-family: Consolas; }")
        btn_insert = QPushButton("Вставить в терминал")
        btn_insert.clicked.connect(self._insert_suggestion)
        l2.addWidget(self.ask_input)
        l2.addWidget(btn_ask)
        l2.addWidget(self.suggestion)
        l2.addWidget(btn_insert)

        # -- журнал ответов ИИ --
        g3 = QGroupBox("Ответы ИИ")
        l3 = QVBoxLayout(g3)
        self.ai_output = QPlainTextEdit()
        self.ai_output.setReadOnly(True)
        self.ai_output.setFont(QFont("Consolas", 10))
        l3.addWidget(self.ai_output)

        lay.addWidget(g1)
        lay.addWidget(g2)
        lay.addWidget(g3)

        dock = QDockWidget("ИИ-помощник")
        dock.setWidget(panel)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)

    # ---------- SSH ----------
    def connect_ssh(self):
        if paramiko is None:
            QMessageBox.critical(self, "Ошибка",
                                 "Не установлен пакет paramiko:\n"
                                 "pip install paramiko")
            return
        if self.ssh and self.ssh.isRunning():
            QMessageBox.information(self, "SSH", "Уже подключено.")
            return
        dlg = ConnectDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self.ssh = SshWorker(dlg.host.text().strip(), dlg.port.value(),
                             dlg.user.text().strip(),
                             dlg.password.text(), dlg.keyfile.text().strip())
        self.ssh.dataReceived.connect(self.term.insert_remote)
        self.ssh.connected.connect(lambda: self.term.set_connected(True))
        self.ssh.disconnected.connect(self._on_disconnected)
        self.ssh.start()

    def disconnect_ssh(self):
        if self.ssh:
            self.ssh.stop()
            self.ssh.wait(2000)
            self.ssh = None
        self.term.set_connected(False)
        self.term.insert_remote("\n[отключено]\n")

    def _on_disconnected(self, err):
        self.term.set_connected(False)
        if err:
            self.term.insert_remote(f"\n[ошибка SSH: {err}]\n")
        else:
            self.term.insert_remote("\n[соединение закрыто]\n")

    def _send_to_server(self, text):
        if self.ssh and self.ssh.isRunning():
            self.ssh.send(text)

    # ---------- ИИ ----------
    def edit_ai_settings(self):
        dlg = AiSettingsDialog(self.settings, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.settings["base_url"] = dlg.base.text().strip()
            self.settings["api_key"] = dlg.key.text().strip()
            self.settings["model"] = dlg.model.text().strip()

    def _ai_available(self):
        if not self.settings["base_url"]:
            QMessageBox.information(
                self, "ИИ", "Задайте URL ИИ в «Настройки ИИ».\n"
                            "Для локальной Ollama: http://localhost:11434/v1")
            return False
        if self._ai_busy:
            self.ai_output.appendPlainText("…ИИ занят, подождите…")
            return False
        return True

    def _ask_ai(self, messages, on_result):
        self._ai_busy = True
        self.worker = AiWorker(self.settings, messages)
        self.worker.result.connect(on_result)
        self.worker.result.connect(lambda _: setattr(self, "_ai_busy", False))
        self.worker.failed.connect(self._ai_error)
        self.worker.failed.connect(lambda _: setattr(self, "_ai_busy", False))
        self.worker.start()

    def _ai_error(self, err):
        self.ai_output.appendPlainText(f"[ошибка ИИ: {err}]\n")

    # -- объяснение вывода --
    def explain_output(self):
        if not self._ai_available():
            return
        output = self.term.last_output()
        messages = [
            {"role": "system", "content": self._system_prompt()},
            {"role": "user", "content": output},
        ]
        self.ai_output.appendPlainText("— анализ вывода…\n")
        self._ask_ai(messages, self._show_explanation)

    def _show_explanation(self, text):
        self.ai_output.appendPlainText(text + "\n")

    # -- подбор команды --
    def suggest_command(self):
        if not self._ai_available():
            return
        wish = self.ask_input.text().strip()
        if not wish:
            return
        context = self.term.last_output(20)
        messages = [
            {"role": "system", "content": self._cmd_prompt()},
            {"role": "user", "content":
                f"Контекст (последний вывод терминала):\n{context}\n\n"
                f"Задача: {wish}"},
        ]
        self.suggestion.setText("…думаю…")
        self._ask_ai(messages, self._show_suggestion)

    def _show_suggestion(self, text):
        cmd = text.splitlines()[0].strip().strip("`")
        self._pending_cmd = cmd
        self.suggestion.setText(cmd)

    def _insert_suggestion(self):
        cmd = getattr(self, "_pending_cmd", "")
        if not cmd:
            return
        # вставляем, только если терминал не занят своей строкой
        if self.term.buffer.strip():
            self.term.insert_remote("\n")
            self.term.buffer = ""
        for ch in cmd:
            self.term.buffer += ch
            self.term.insertPlainText(ch)
            self._send_to_server(ch)
        self.term.insert_remote("\n")
        self._send_to_server("\n")
        self.term.buffer = ""

    # -- автодополнение по Tab --
    def _autocomplete(self, buf):
        if not self._ai_available():
            return
        if buf in self._ac_cache:
            self.term.insert_suggestion(self._ac_cache[buf])
            return
        context = self.term.last_output(20)
        messages = [
            {"role": "system", "content": self._ac_prompt()},
            {"role": "user", "content":
                f"Контекст:\n{context}\n\nНачало команды: {buf}"},
        ]
        holder = {"buf": buf}

        def on_result(text):
            rest = text.splitlines()[0] if text else ""
            rest = rest.strip()
            # защита от повтора введённого текста
            if rest.startswith(holder["buf"]):
                rest = rest[len(holder["buf"]):]
            self._ac_cache[holder["buf"]] = rest
            self.term.insert_suggestion(rest)

        self._ask_ai(messages, on_result)

    # ---------- закрытие ----------
    def closeEvent(self, e):
        if self.ssh:
            self.ssh.stop()
            self.ssh.wait(2000)
        e.accept()


# ---------------------------------------------------------------------------
def main():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
