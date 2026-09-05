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
import ast
import json
import math
import time
import socket
import shutil
import subprocess
import datetime
import importlib.util
import urllib.request

try:
    import paramiko
except ImportError:
    paramiko = None

try:
    import serial
except ImportError:
    serial = None

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QDialog, QWidget, QVBoxLayout, QHBoxLayout,
    QFormLayout, QLineEdit, QSpinBox, QComboBox, QCheckBox, QPushButton,
    QLabel, QPlainTextEdit, QTextEdit, QToolBar, QDockWidget, QMessageBox,
    QFileDialog, QGroupBox, QListWidget, QListWidgetItem
)
from PyQt6.QtCore import Qt, QThread, QTimer, QUrl, QEventLoop, pyqtSignal
from PyQt6.QtGui import (
    QAction, QFont, QIcon, QKeySequence, QShortcut, QDesktopServices,
    QTextCursor, QGuiApplication, QColor
)


def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        return True
    except OSError:
        return False


APP_VERSION = "1.0.0"


# ---------------------------------------------------------------------------
# Безопасность: команды, запрещённые для автоматического выполнения
# ---------------------------------------------------------------------------
# Полное стирание / форматирование флешек — НИКОГДА не выполняем автоматически
DANGEROUS_CMDS = (
    "mmc erase", "mmc format", "mmc rescan 1", "eraseall", "erase all",
    "nand erase", "sf erase", "sf probe 0; sf erase",
    "gpt write", "fdisk", "recovery_wipe", "recovery-wipe",
    "wipe", "factory_reset", "factory reset",
)
# Опасная запись во флеш — только с двойным предупреждением
RISKY_CMDS = (
    "mmc write", "nand write", "sf write", "fatwrite", "sparse_write",
    "usb2spi", "bin2emmc", "ursa_upgrade", "avbab disable-verity",
)


LAZY_LADDER = (
    "\n\nЛЕСТНИЦА ЛЕНИВОСТИ (останавливайся на первой сработавшей ступени):\n"
    "1. Устройство уже работает или грузится? → ответь DONE, ничего не делай.\n"
    "2. Проблема уже решена в базе знаний/навыках? → повтори то решение.\n"
    "3. Хватает частичного исправления (partial upgrade, ustar)? → не делай "
    "полное.\n"
    "4. Чинится одной командой? → одна команда, не больше.\n"
    "5. Только тогда — полный сценарий восстановления.\n"
    "Багфикс = корневая причина, а не симптом. Удаление лишнего важнее "
    "добавления нового.\n"
    "НИКОГДА не экономь на безопасности: стирание флеш (mmc erase и т.п.) "
    "программа заблокирует — не пытайся обойти.\n"
    "ПОСЛЕ КАЖДОГО ФИКСА — ПРОВЕРКА (как lint после правки): одна короткая "
    "команда-доказательство, что проблема исчезла. Подтверждено → DONE.\n"
    "После каждой команды оцени: проблема решена? → сразу DONE, не делай "
    "лишних шагов.")


def _cmd_safety(cmd):
    """('blocked'|'risky'|'ok', совпавший шаблон) для строки команды."""
    low = " ".join(cmd.lower().split())   # нормализация пробелов/табов
    for p in DANGEROUS_CMDS:
        if p in low:
            return "blocked", p
    for p in RISKY_CMDS:
        if p in low:
            return "risky", p
    return "ok", ""


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
            "QPlainTextEdit { background: #0d1117; color: #7ee787; "
            "border: 1px solid #30363d; border-radius: 8px; "
            "padding: 6px; selection-background-color: #1f6feb; "
            "selection-color: #ffffff; }")
        self.setReadOnly(False)
        self.buffer = ""       # текущая вводимая строка (локальное эхо)
        self.history = []
        self.hist_idx = 0
        self.connected = False
        self.local_echo = True   # False для Serial (эхо даёт само устройство)
        self.enter_seq = "\n"    # "\r" для Serial/U-Boot

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
        self.buffer = new
        if not self.local_echo:
            return
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
            if self.local_echo:
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
class SerialWorker(QThread):
    """Работа с COM-портом (UART) через pyserial — как PuTTY serial."""

    dataReceived = pyqtSignal(str)
    connected = pyqtSignal()
    disconnected = pyqtSignal(str)

    def __init__(self, port, baud, parent=None):
        super().__init__(parent)
        self.port_name, self.baud = port, baud
        self._stop = False
        self.ser = None

    def run(self):
        try:
            self.ser = serial.Serial(self.port_name, self.baud, timeout=0.2)
            self.connected.emit()
            while not self._stop:
                try:
                    data = self.ser.read(4096)
                except Exception:
                    break
                if data:
                    self.dataReceived.emit(
                        data.decode("utf-8", errors="replace"))
        except Exception as ex:
            self.disconnected.emit(str(ex))
            return
        self.disconnected.emit("")

    def send(self, text):
        if self.ser:
            try:
                self.ser.write(text.encode("utf-8", errors="ignore"))
            except Exception:
                pass

    def stop(self):
        self._stop = True
        try:
            if self.ser:
                self.ser.close()
        except Exception:
            pass


class AiWorker(QThread):
    result = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, settings, messages, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.messages = messages

    def _attempt(self, base_url, api_key, model):
        payload = {"model": model, "messages": self.messages,
                   "temperature": 0.2, "stream": False}
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = "Bearer " + api_key
        req = urllib.request.Request(
            base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers)
        with urllib.request.urlopen(req, timeout=90) as r:
            resp = json.loads(r.read().decode("utf-8"))
        return resp["choices"][0]["message"]["content"].strip()

    def run(self):
        # 3 попытки с нарастающей паузой + опциональный запасной провайдер
        delays = (0, 2, 5)
        last_err = ""
        for i in range(3):
            if i:
                self.msleep(int(delays[i] * 1000))
            try:
                self.result.emit(self._attempt(self.settings["base_url"],
                                               self.settings.get("api_key"),
                                               self.settings["model"]))
                return
            except Exception as ex:
                last_err = str(ex)
        fb_url = self.settings.get("base_url2")
        if fb_url:
            try:
                self.result.emit(self._attempt(
                    fb_url, self.settings.get("api_key2"),
                    self.settings.get("model2") or self.settings["model"]))
                return
            except Exception as ex:
                last_err = str(ex)
        self.failed.emit(last_err)


# ---------------------------------------------------------------------------
# Диалоги
# ---------------------------------------------------------------------------
class ConnectDialog(QDialog):
    def __init__(self, conn=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Подключение")
        self.setMinimumWidth(380)
        lay = QFormLayout(self)

        self.conn_type = QComboBox()
        self.conn_type.addItem("SSH (сервер)", "ssh")
        self.conn_type.addItem("Serial (COM-порт / UART)", "serial")
        self.conn_type.addItem("Эмулятор (тренировка без железа)", "mock")
        lay.addRow("Тип:", self.conn_type)

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

        # --- Serial (COM-порт) ---
        self.port_name = QComboBox()
        self.port_name.setEditable(True)
        try:
            from serial.tools import list_ports
            for p in list_ports.comports():
                self.port_name.addItem(p.device)
        except Exception:
            pass
        self.baud = QComboBox()
        self.baud.setEditable(True)
        for b in ("115200", "57600", "38400", "19200", "9600"):
            self.baud.addItem(b)
        row_s = QHBoxLayout()
        row_s.addWidget(self.port_name)
        row_s.addWidget(QLabel("Скорость:"))
        row_s.addWidget(self.baud)
        lay.addRow("Порт:", row_s)

        # подстановка сохранённого подключения
        if conn:
            try:
                self.host.setText(str(conn.get("host", "127.0.0.1")))
                self.port.setValue(int(conn.get("port", 22)))
                self.user.setText(str(conn.get("user", "")))
                if conn.get("type") == "serial":
                    self.conn_type.setCurrentIndex(1)
                elif conn.get("type") == "mock":
                    self.conn_type.setCurrentIndex(2)
                if conn.get("port_name"):
                    self.port_name.setCurrentText(str(conn.get("port_name")))
                if conn.get("baud"):
                    self.baud.setCurrentText(str(conn.get("baud")))
            except (AttributeError, TypeError, ValueError):
                pass

        self._ssh_widgets = [self.host, self.port, self.user,
                             self.password, self.keyfile, btn_browse]
        self._serial_widgets = [self.port_name, self.baud]
        self.conn_type.currentIndexChanged.connect(self._toggle_type)
        self._toggle_type(0)

        btns = QHBoxLayout()
        ok = QPushButton("Подключиться")
        cancel = QPushButton("Отмена")
        ok.clicked.connect(self.accept)
        cancel.clicked.connect(self.reject)
        btns.addStretch(1)
        btns.addWidget(ok)
        btns.addWidget(cancel)
        lay.addRow(btns)

    def _toggle_type(self, _):
        ssh = self.conn_type.currentData() == "ssh"
        for w in self._ssh_widgets:
            w.setEnabled(ssh)
        for w in self._serial_widgets:
            w.setEnabled(not ssh)

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(self, "Приватный ключ")
        if path:
            self.keyfile.setText(path)


PROVIDERS = [
    ("Ollama (локально, бесплатно)",
     "http://localhost:11434/v1", "qwen2.5-coder"),
    ("Groq (очень быстро, есть бесплатный лимит)",
     "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
    ("llamafile (1 файл, офлайн, работает без установки)",
     "http://localhost:8080/v1", "llamafile"),
    ("OpenAI", "https://api.openai.com/v1", "gpt-4o-mini"),
    ("Свой сервер (OpenAI-совместимый)", None, None),
]


class AnalysisDialog(QDialog):
    """Окно с полным отчётом анализа устройства."""

    def __init__(self, text, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Полный анализ устройства")
        self.resize(580, 520)
        lay = QVBoxLayout(self)
        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setFont(QFont("Consolas", 10))
        view.setPlainText(text)
        lay.addWidget(view)
        row = QHBoxLayout()
        btn_copy = QPushButton("Копировать отчёт")
        btn_copy.clicked.connect(
            lambda: QGuiApplication.clipboard().setText(text))
        btn_ok = QPushButton("Закрыть")
        btn_ok.clicked.connect(self.accept)
        row.addStretch(1)
        row.addWidget(btn_copy)
        row.addWidget(btn_ok)
        lay.addLayout(row)


class AiSettingsDialog(QDialog):
    """Настройки ИИ: пресеты провайдеров + ручной ввод."""

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Настройки ИИ")
        self.setMinimumWidth(460)
        lay = QFormLayout(self)

        self.provider = QComboBox()
        self.provider.addItem("— выберите провайдера (подставит URL и модель) —")
        for name, _, _ in PROVIDERS:
            self.provider.addItem(name)
        lay.addRow("Провайдер:", self.provider)
        self.provider.currentIndexChanged.connect(self._apply_preset)

        self.base = QLineEdit(settings["base_url"])
        self.key = QLineEdit(settings.get("api_key", ""))
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.model = QLineEdit(settings["model"])

        lay.addRow("API URL:", self.base)
        lay.addRow("API-ключ:", self.key)
        lay.addRow("Модель:", self.model)
        self.base2 = QLineEdit(settings.get("base_url2", ""))
        self.key2 = QLineEdit(settings.get("api_key2", ""))
        self.key2.setEchoMode(QLineEdit.EchoMode.Password)
        self.model2 = QLineEdit(settings.get("model2", ""))
        lay.addRow("Запасной URL:", self.base2)
        lay.addRow("Запасной ключ:", self.key2)
        lay.addRow("Запасная модель:", self.model2)
        self.llamafile_path = QLineEdit(settings.get("llamafile_path", ""))
        btn_lf = QPushButton("…")
        btn_lf.setFixedWidth(32)
        btn_lf.clicked.connect(self._browse_llamafile)
        row_lf = QHBoxLayout()
        row_lf.addWidget(self.llamafile_path)
        row_lf.addWidget(btn_lf)
        lay.addRow("llamafile.exe:", row_lf)
        lay.addRow(QLabel(
            "Ollama (Win10+): http://localhost:11434/v1, ключ пустой.\n"
            "Groq: ключ с https://console.groq.com/keys (регистрация бесплатно).\n"
            "  Модели Groq: llama-3.3-70b-versatile (умная),\n"
            "  llama-3.1-8b-instant (самая быстрая), qwen-qwq-32b (рассуждения).\n"
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

    def _browse_llamafile(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "llamafile.exe", "", "Исполняемые файлы (*.exe)")
        if path:
            self.llamafile_path.setText(path)

    def _apply_preset(self, idx):
        if idx <= 0:
            return
        url, model = PROVIDERS[idx - 1][1], PROVIDERS[idx - 1][2]
        if url:
            self.base.setText(url)
        if model:
            self.model.setText(model)


def _cosine(a, b):
    """Косинусная близость двух векторов (семантический поиск навыков)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return dot / (na * nb)


class MockDevice(QThread):
    """Имитатор консоли U-Boot M7332 для офлайн-тренировки агента."""

    dataReceived = pyqtSignal(str)
    connected = pyqtSignal()
    disconnected = pyqtSignal(str)

    BOOT_BAD = ("\nMStar/SigmaStar U-Boot 2011.12\n"
                "** Bad signature on 0:1: expected 0x5840, got 0x0000 **\n"
                "** partition 0 not valid on device 0 **\n"
                "** unable to use usb 0:0 for fatload **\n"
                "reading upgrade_image.pkg\n"
                '** unable to read "upgrade_image.pkg" from usb 0:1 **\n'
                "jump_to_console start!!\n"
                "<< M7332 >># ")
    BOOT_GOOD = ("\nMStar/SigmaStar U-Boot 2011.12\n"
                 "USB: scanning bus... 1 storage device(s) found\n"
                 "booting kernel...\nOK\n<< M7332 >># ")

    def __init__(self, parent=None):
        super().__init__(parent)
        self._stop = False
        self.usb_ready = False
        self._buf = ""   # буфер ввода до Enter (как настоящий UART)

    def run(self):
        self.connected.emit()
        self.msleep(400)
        self.dataReceived.emit(self.BOOT_BAD)
        while not self._stop:
            self.msleep(100)
        self.disconnected.emit("")

    def send(self, text):
        """Накапливаем символы до перевода строки — как настоящий UART."""
        self._buf += text
        while True:
            pos = -1
            sep = None
            for i, ch in enumerate(self._buf):
                if ch in ("\r", "\n"):
                    pos, sep = i, ch
                    break
            if pos < 0:
                return
            line = self._buf[:pos]
            self._buf = self._buf[pos + 1:]
            self._handle_line(line.strip())

    def _handle_line(self, t):
        if not t:
            return
        low = t.lower()
        if low == "usb start":
            self.usb_ready = True
            self.dataReceived.emit(
                "\nUSB: scanning bus for devices... 1 storage device(s) "
                "found\n<< M7332 >># ")
        elif low.startswith("fatls"):
            if self.usb_ready:
                self.dataReceived.emit(
                    "\n  upgrade_image.pkg      81234567 bytes\n"
                    "1 file(s) read\n<< M7332 >># ")
            else:
                self.dataReceived.emit("\n** No boot device **\n"
                                       "<< M7332 >># ")
        elif "ustar" in low or "upgrade_to_emmc" in low:
            if self.usb_ready:
                self.dataReceived.emit("\nreading upgrade_image.pkg ...\n")
                for pct in (25, 50, 75, 100):
                    self.msleep(250)
                    self.dataReceived.emit("writing emmc ... %d%%\n" % pct)
                self.dataReceived.emit("upgrade ok\n")
                self.msleep(200)
                self.dataReceived.emit(self.BOOT_GOOD)
            else:
                self.dataReceived.emit("\n** No boot device **\n"
                                       "<< M7332 >># ")
        elif low == "reset":
            self.dataReceived.emit("\nresetting...\n")
            self.msleep(200)
            self.dataReceived.emit(self.BOOT_GOOD)
        elif low == "help":
            self.dataReceived.emit(
                "\nusb start|stop|tree|part, fatls, ustar, "
                "usb_super_upgrade_to_emmc, reset\n<< M7332 >># ")
        else:
            self.dataReceived.emit("\nUnknown command '%s'\n<< M7332 >># "
                                   % t)

    def stop(self):
        self._stop = True


# ---------------------------------------------------------------------------
# Главное окно
# ---------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PuTTY-AI v%s — SSH-клиент с ИИ-помощником"
                            % APP_VERSION)
        self.resize(1000, 640)

        # --- папка данных пользователя нужна всем остальным —
        self._base_dir = self._data_dir()

        # --- настройки: из config.json (если есть) или по умолчанию ---
        self.config = self._load_config()
        self.settings = self.config.get("settings", {
            "base_url": "http://localhost:11434/v1",   # Ollama по умолчанию
            "api_key": "",
            "model": "qwen2.5-coder",
        })
        self._conn = self.config.get("conn", {})

        # --- самообучение: навыки, правила, самопереписываемый код ---
        self._seed_data_files()
        self.skills = _load_json(os.path.join(self._base_dir, "skills.json"), [])
        self.extra_rules = _load_json(
            os.path.join(self._base_dir, "learned_rules.json"),
            {"dangerous": [], "risky": []})
        self._output_hook = None
        self._matched_skill = None
        self._announced = set()
        self._last_skill_check = 0.0
        self._last_hook_msg = ""
        self._load_user_patches()

        # --- умное ожидание (expect), верификатор, рефлексия, лог сессии ---
        self._expecting = False
        self._expect_deadline = 0.0
        self._wait_count = 0
        self._cmd_counts = {}
        self._verifying = False
        self._reflect_used = 0
        self._session_log = None
        self._boot_armed = True
        self._start_session_log()
        QTimer.singleShot(2000, self._check_update)

        # --- семантическая память, ватчдог, бригада ---
        self._vec_cache = {}
        self._out_vec = (0.0, None)
        self._err_history = []
        self._wd_fired = set()
        self._last_verify_score = None
        QTimer.singleShot(3000, self._curriculum_update)
        # --- база знаний по U-Boot (файл u_boot_errors_kb.md рядом с программой) ---
        self.kb_text = self._load_kb()

        self.ssh = None
        self._ai_busy = False
        self._ac_cache = {}
        self._agent_active = False
        self._agent_last_cmd = ""
        self._session_cmds = []   # команды, выполненные в этой сессии

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

    def _data_dir(self):
        """Папка данных пользователя: %APPDATA%\\PuTTY-AI на Windows
        (рядом с exe писать запрещено в защищённых папках)."""
        if os.name == "nt":
            root = os.environ.get("APPDATA") or os.path.expanduser("~")
            d = os.path.join(root, "PuTTY-AI")
        else:
            d = os.path.dirname(os.path.abspath(sys.argv[0]))
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            d = os.path.dirname(os.path.abspath(sys.argv[0]))
        return d

    def _seed_data_files(self):
        """Первый запуск exe: копируем встроенные данные в папку
        пользователя, чтобы программа могла их дополнять."""
        sources = []
        if hasattr(sys, "_MEIPASS"):
            sources.append(sys._MEIPASS)
        sources.append(os.path.dirname(os.path.abspath(sys.argv[0])))
        for name in ("skills.json", "learned_rules.json", "user_patches.py",
                     "learned_cases.md", "prompt_tuning.md"):
            dst = os.path.join(self._base_dir, name)
            if os.path.exists(dst):
                continue
            for src_dir in sources:
                src = os.path.join(src_dir, name)
                if os.path.exists(src):
                    try:
                        shutil.copyfile(src, dst)
                    except OSError:
                        pass
                    break

    def _load_config(self):
        """Читает config.json (настройки ИИ + последнее подключение)."""
        dirs = [self._base_dir,
                os.path.dirname(os.path.abspath(sys.argv[0]))]
        if hasattr(sys, "_MEIPASS"):
            dirs.append(sys._MEIPASS)
        for d in dirs:
            try:
                with open(os.path.join(d, "config.json"),
                          encoding="utf-8") as f:
                    return json.load(f)
            except (OSError, ValueError):
                continue
        return {}

    def _save_config(self):
        """Сохраняет настройки ИИ и параметры подключения в config.json."""
        try:
            path = os.path.join(self._base_dir, "config.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"settings": self.settings, "conn": self._conn},
                          f, ensure_ascii=False, indent=1)
        except OSError:
            pass

    def _load_kb(self):
        """Загружает базу знаний (папка пользователя, рядом с exe, в exe)."""
        dirs = [self._base_dir,
                os.path.dirname(os.path.abspath(sys.argv[0]))]
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

    # ---------- Сценарий восстановления ----------
    def _make_script(self):
        if not self._ai_available():
            return
        kb = ("\n\nБаза знаний:\n" + self.kb_text) if self.kb_text else ""
        if self.profile_combo.currentData() == "uboot":
            sys = ("Ты — эксперт по восстановлению устройств через консоль "
                   "U-Boot (прошивка embedded/TV). Составь ПОЛНЫЙ сценарий "
                   "восстановления по текущему выводу терминала — от диагностики "
                   "до завершающей команды. Формат ответа строго: каждый шаг с "
                   "новой строки в виде `команда  # короткий комментарий` "
                   "(комментарий через # обязателен). Без нумерации, без "
                   "markdown, без пояснений между шагами, без пустых строк." + kb)
        else:
            sys = ("Ты — эксперт по восстановлению Linux-систем. Составь ПОЛНЫЙ "
                   "сценарий исправления по выводу терминала — от диагностики до "
                   "проверки результата. Формат: каждый шаг новой строкой в виде "
                   "`команда  # комментарий`. Без нумерации и markdown." + kb)
        out = self.term.last_output(120)
        messages = [{"role": "system", "content": sys},
                    {"role": "user", "content": "Вывод терминала:\n" + out}]
        self.ai_output.appendPlainText("— составляю сценарий восстановления…\n")
        self._ask_ai(messages, self._show_script)

    def _show_script(self, text):
        self._script_cmds = []
        lines = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("```"):
                continue
            if line.startswith("#"):
                lines.append(line)
                continue
            cmd = line.split("#", 1)[0].strip()
            if cmd:
                self._script_cmds.append(cmd)
            lines.append(line)
        self.script_view.setPlainText("\n".join(lines))
        self._script_idx = 0
        self.ai_output.appendPlainText(
            "— сценарий готов: %d команд(ы), используйте «Скопировать "
            "следующий шаг»\n" % len(self._script_cmds))

    def _copy_script(self):
        text = self.script_view.toPlainText()
        if text:
            QGuiApplication.clipboard().setText(text)

    def _copy_next_step(self):
        cmds = getattr(self, "_script_cmds", [])
        if not cmds:
            self.ai_output.appendPlainText("— сначала создайте сценарий\n")
            return
        i = getattr(self, "_script_idx", 0)
        if i >= len(cmds):
            self.ai_output.appendPlainText("— все шаги уже скопированы\n")
            return
        QGuiApplication.clipboard().setText(cmds[i])
        self._script_idx = i + 1
        self._highlight_step(self._script_idx - 1)
        self.ai_output.appendPlainText("шаг %d/%d скопирован: %s\n"
                                       % (i + 1, len(cmds), cmds[i]))
        lvl, _ = self._safety(cmds[i])
        if lvl == "blocked":
            self.ai_output.appendPlainText(
                "⛔ ВНИМАНИЕ: это команда ПОЛНОГО стирания флешки!\n")
        elif lvl == "risky":
            self.ai_output.appendPlainText(
                "⚠ ВНИМАНИЕ: команда записывает во флеш.\n")

    # ---------- Обучение: сохранение удачных решений ----------
    def _learn_success(self):
        cmds = getattr(self, "_session_cmds", [])
        if not cmds:
            QMessageBox.information(
                self, "Обучение",
                "В этой сессии ещё не выполнялось команд — нечего сохранять.")
            return
        problem = self.term.last_output(25).strip()
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        lines = ["", "## Удачный случай (%s)" % stamp,
                 "", "Проблема (фрагмент вывода терминала):", "",
                 "```", problem, "```", "",
                 "Решение (команды по порядку):", "", "```"]
        lines.extend(cmds)
        lines.extend(["```", ""])
        entry = "\n".join(lines)
        saved = None
        dirs = [self._base_dir]
        if hasattr(sys, "_MEIPASS"):
            dirs.append(sys._MEIPASS)
        for d in dirs:
            p = os.path.join(d, "learned_cases.md")
            try:
                with open(p, "a", encoding="utf-8") as f:
                    f.write(entry)
                saved = p
                break
            except OSError:
                continue
        if saved:
            self.kb_text = self._load_kb()
            self.ai_output.appendPlainText(
                "— решение сохранено в базу знаний: %s\n" % saved)
            QMessageBox.information(
                self, "Обучение",
                "Удачное решение записано в базу знаний.\n"
                "В следующий раз ИИ сразу будет знать это решение.")
        else:
            self.ai_output.appendPlainText(
                "[ошибка: не удалось записать learned_cases.md]\n")

    # ---------- Самообучение: навыки, правила, самопереписывание ----------
    def _load_user_patches(self):
        """Загружает user_patches.py с валидацией (паттерн mue-x):
        ast.parse, только безопасные конструкции верхнего уровня,
        бэкап перед применением, откат при поломке."""
        self._output_hook = None   # сброс: сломанный файл не оставляет старый хук
        path = os.path.join(self._base_dir, "user_patches.py")
        bak = os.path.join(self._base_dir, "user_patches.bak")
        try:
            with open(path, encoding="utf-8") as f:
                source = f.read()
        except OSError:
            return
        tree = None
        try:
            tree = ast.parse(source)
        except SyntaxError:
            pass
        if tree is not None:
            for node in tree.body:
                if isinstance(node, ast.Expr) and \
                        isinstance(node.value, ast.Constant):
                    continue  # docstring
                if not isinstance(node, (ast.Import, ast.ImportFrom,
                                         ast.FunctionDef, ast.ClassDef,
                                         ast.Assign)):
                    tree = None  # опасная конструкция верхнего уровня
                    break
        if tree is None:
            # откат из бэкапа
            try:
                with open(bak, encoding="utf-8") as f:
                    source = f.read()
                tree = ast.parse(source)
                shutil.copyfile(bak, path)
                self.ai_output.appendPlainText(
                    "[user_patches: обнаружена ошибка — выполнен откат "
                    "из бэкапа]\n")
            except (OSError, SyntaxError):
                self.ai_output.appendPlainText(
                    "[user_patches: ошибка в коде, бэкап не найден — "
                    "хук отключён]\n")
                return
        try:
            shutil.copyfile(path, bak)  # свежий бэкап рабочей версии
        except OSError:
            pass
        try:
            mod = ast.Module(body=tree.body, type_ignores=[])
            g = {"__name__": "user_patches"}
            exec(compile(mod, path, "exec"), g)
            hook = g.get("on_output")
            if callable(hook):
                self._output_hook = hook
        except Exception as ex:
            self._output_hook = None
            self.ai_output.appendPlainText(
                "[user_patches: сбой загрузки (%s) — хук отключён]\n" % ex)

    def _safety(self, cmd):
        """Базовые правила + правила, выученные программой самостоятельно."""
        level, pat = _cmd_safety(cmd)
        if level == "ok":
            low = cmd.lower()
            for p in self.extra_rules.get("dangerous", []):
                if p.lower() in low:
                    return "blocked", p
            for p in self.extra_rules.get("risky", []):
                if p.lower() in low:
                    return "risky", p
        return level, pat

    @staticmethod
    def _skill_score(skill, out):
        """Доля строк триггера, найденных в выводе (0.0–1.0)."""
        trig = str(skill.get("trigger", "")).strip()
        lines = [l.strip() for l in trig.splitlines() if l.strip()]
        if len(trig) < 8 or not lines:
            return 0.0
        hits = sum(1 for l in lines if l in out)
        return hits / float(len(lines))

    def _on_term_output(self, text):
        """Вывод терминала: показать + хук самопереписанного кода + навыки."""
        self.term.insert_remote(text)
        self._log_io("OUT", text)
        self._maybe_expect_done()
        self._try_boot_catch(text)
        self._watchdog_check(self.term.last_output(20))
        if self._expecting and "**" in text and self.chk_crew.isChecked():
            self._agent_history.append(
                {"role": "user", "content":
                    "КРИТИК: команда выдала ошибку:\n" + text[-400:] +
                    "\nИсправь подход."})
        if self._output_hook:
            try:
                msg = self._output_hook(self.term.last_output(60))
                if isinstance(msg, str) and msg and msg != self._last_hook_msg:
                    self._last_hook_msg = msg
                    self.ai_output.appendPlainText("🧩 user_patches: "
                                                   + msg + "\n")
            except Exception:
                pass
        now = time.time()
        if now - self._last_skill_check < 3:
            return
        self._last_skill_check = now
        if self.chk_semantic.isChecked():
            hit, _sc = self._find_best_skill(self.term.last_output(60))
            if hit is not None:
                key = str(hit.get("trigger", "")).splitlines()[0][:60]
                if key and key not in self._announced:
                    self._announced.add(key)
                    self._matched_skill = hit
                    hit["hits"] = int(hit.get("hits", 0)) + 1
                    _save_json(os.path.join(self._base_dir, "skills.json"),
                               self.skills)
                    self.ai_output.appendPlainText(
                        "🎯 Найдено известное решение: %s\n"
                        "   Нажмите «Применить найденное решение»\n"
                        % hit.get("note", key))
                    self._curriculum_update()
                    return
        out = self.term.last_output(60)
        for s in self.skills:
            trig = str(s.get("trigger", "")).strip()
            key = trig.splitlines()[0][:60] if trig else ""
            if self._skill_score(s, out) >= 0.5 and key not in self._announced:
                self._announced.add(key)
                self._matched_skill = s
                s["hits"] = int(s.get("hits", 0)) + 1
                _save_json(os.path.join(self._base_dir, "skills.json"),
                           self.skills)
                self.ai_output.appendPlainText(
                    "🎯 Найдено известное решение: %s\n"
                    "   Нажмите «Применить найденное решение»\n"
                    % s.get("note", trig))
                self.skill_status.setText(
                    "навыков: %d | 🎯 найдено совпадение" % len(self.skills))

    def _apply_skill(self):
        s = getattr(self, "_matched_skill", None)
        if not s:
            QMessageBox.information(
                self, "Навыки",
                "Пока нет найденного решения.\nПоявится само, когда в выводе "
                "терминала встретится известная ошибка.")
            return
        cmds = [str(c).strip() for c in s.get("solution", []) if str(c).strip()]
        if not cmds:
            return
        for c in cmds:
            level, pat = self._safety(c)
            if level == "blocked" and not self.chk_danger.isChecked():
                self.ai_output.appendPlainText(
                    "⛔ Навык содержит запрещённую команду (%s) — прервано.\n"
                    "Включите «Разрешить опасные команды» для выполнения.\n"
                    % pat)
                return
        self.ai_output.appendPlainText(
            "🎯 Применяю выученное решение (%d шагов)…\n" % len(cmds))
        self._type_skill(cmds, 0)

    def _type_skill(self, cmds, i):
        if i >= len(cmds):
            self.ai_output.appendPlainText("— навык выполнен\n")
            return
        self._type_command(cmds[i])
        QTimer.singleShot(3000, lambda: self._type_skill(cmds, i + 1))

    def _learn_new(self):
        """ИИ анализирует новую ошибку и переписывает правила программы."""
        if not self._ai_available():
            return
        kb = ("\n\nБаза знаний:\n" + self.kb_text) if self.kb_text else ""
        sys = ("Ты — движок самообучения программы. По выводу терминала и "
               "выполненным командам создай запись навыка. Ответь СТРОГО одним "
               "JSON-объектом (без markdown и пояснений):\n"
               '{"trigger": "<1-2 строки уникального фрагмента ошибки>", '
               '"platform": "uboot|linux", '
               '"solution": ["команда1", "команда2"], '
               '"note": "<что это было и почему помогло>", '
               '"dangerous": true/false}\n'
               "dangerous=true только если решение стирает/пишет во флеш." + kb)
        user = ("Вывод терминала:\n" + self.term.last_output(100) +
                "\n\nВыполненные команды:\n" +
                "\n".join(getattr(self, "_session_cmds", [])) +
                "\n\nПрофиль: " + str(self.profile_combo.currentData()))
        self.ai_output.appendPlainText("🧠 обучаюсь на новой ошибке…\n")
        self._ask_ai([{"role": "system", "content": sys},
                      {"role": "user", "content": user}], self._apply_learning)

    def _apply_learning(self, text):
        import re as _re
        m = _re.search(r"\{.*\}", text, _re.S)
        if not m:
            self.ai_output.appendPlainText(
                "[обучение: ИИ вернул не JSON, запись не создана]\n")
            return
        try:
            data = json.loads(m.group(0))
        except ValueError:
            self.ai_output.appendPlainText(
                "[обучение: битый JSON, запись не создана]\n")
            return
        trig = str(data.get("trigger", "")).strip()
        sol = [str(c).strip() for c in data.get("solution", []) if str(c).strip()]
        if not trig or not sol:
            self.ai_output.appendPlainText(
                "[обучение: пустой trigger/solution]\n")
            return
        try:
            if self.chk_semantic.isChecked():
                data["vec"] = self._embed(trig)
        except Exception:
            pass
        data["hits"] = 0
        data.setdefault("platform", "uboot")
        data.setdefault("note", trig[:120])
        # mem0-паттерн: похожий навык уже есть? → обновить, а не дублировать
        first_line = trig.splitlines()[0][:60] if trig else ""
        for old in self.skills:
            if first_line and first_line in str(old.get("trigger", "")):
                old["solution"] = sol
                old["note"] = data.get("note", old.get("note", ""))
                old["hits"] = int(old.get("hits", 0)) + 1
                old["dangerous"] = bool(data.get("dangerous"))
                _save_json(os.path.join(self._base_dir, "skills.json"),
                           self.skills)
                self.kb_text = self._load_kb()
                self.skill_status.setText("навыков: %d" % len(self.skills))
                self.ai_output.appendPlainText(
                    "🧠 Навык обновлён (без дубля): %s\n" % old.get("note"))
                return
        self.skills.append(data)
        _save_json(os.path.join(self._base_dir, "skills.json"), self.skills)
        rule_note = ""
        if data.get("dangerous"):
            p = trig.splitlines()[0][:60]
            self.extra_rules.setdefault("dangerous", []).append(p)
            _save_json(os.path.join(self._base_dir, "learned_rules.json"),
                       self.extra_rules)
            rule_note = "\n⛔ Программа переписала свои правила: теперь блокирует «%s»" % p
        self.kb_text = self._load_kb()
        self.skill_status.setText("навыков: %d" % len(self.skills))
        self.ai_output.appendPlainText("🧠 Выучен новый навык: %s%s\n"
                                       % (data["note"], rule_note))

    # ---------- Сессионный лог (запись всего обмена) ----------
    def _start_session_log(self):
        try:
            stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            path = os.path.join(self._base_dir,
                                "session_%s.log" % stamp)
            self._session_log = open(path, "a", encoding="utf-8")
        except OSError:
            self._session_log = None

    def _log_io(self, direction, text):
        if self._session_log:
            try:
                stamp = datetime.datetime.now().strftime("%H:%M:%S")
                self._session_log.write("[%s %s] %s\n"
                                        % (stamp, direction, text))
                self._session_log.flush()
            except OSError:
                self._session_log = None

    # ---------- Умное ожидание (expect-паттерн) ----------
    PROMPT_PATTERNS = (">>#", ">> #", "/ #", "# ", "$ ")

    def _start_expect_wait(self):
        """После команды ждём возврата приглашения консоли, не фикс. паузу."""
        self._expecting = True
        delay = self.agent_delay_spin.value()
        self._expect_deadline = time.time() + max(30, delay * 6)
        self._maybe_expect_done()   # промпт уже на экране?
        if self._expecting:
            self._expect_poll()

    def _expect_poll(self):
        """Проверка дедлайна (промпт ловится в _on_term_output)."""
        if not self._expecting:
            return
        if time.time() >= self._expect_deadline:
            self._expecting = False
            self._agent_observe()
        else:
            QTimer.singleShot(500, self._expect_poll)

    def _maybe_expect_done(self):
        if not self._expecting:
            return
        tail = self.term.last_output(3).rstrip()
        for pat in self.PROMPT_PATTERNS:
            if tail.endswith(pat):
                self._expecting = False
                self._agent_observe()
                return

    # ---------- Верификатор результата (OpenHands-паттерн) ----------
    def _verify_result(self):
        """После DONE: независимая оценка 1–10. <7 → продолжаем исправлять."""
        self._verifying = True
        sys = ("Ты — независимый верификатор. Оцени вывод терминала: "
               "действительно ли решена исходная проблема? Ответь СТРОГО в "
               "формате: число от 1 до 10, пробел, одна короткая фраза "
               "причины. Пример: «9 Ошибок нет, устройство загрузилось».")
        out = self.term.last_output(100)
        self.worker = AiWorker(self.settings,
                               [{"role": "system", "content": sys},
                                {"role": "user", "content": out}])
        self.worker.result.connect(self._verify_got)
        self.worker.failed.connect(self._verify_fail)
        self.worker.start()

    def _verify_fail(self, err):
        self._verifying = False
        self.ai_output.appendPlainText("[верификатор недоступен: %s]\n" % err)
        self._agent_finish("верификатор недоступен, остановлено")

    def _verify_got(self, text):
        self._verifying = False
        token = (text.split() or ["0"])[0]
        try:
            score = int("".join(ch for ch in token if ch.isdigit()) or "0")
        except ValueError:
            score = 0
        reason = " ".join(text.split()[1:])[:120]
        self._last_verify_score = score
        self.ai_output.appendPlainText("✔ Верификатор: %d/10 — %s\n"
                                       % (score, reason))
        if score >= 7:
            self._agent_finish("подтверждено верификатором (%d/10)" % score)
        else:
            self._agent_history.append(
                {"role": "user", "content":
                    "Верификатор дал %d/10 (%s). Проблема НЕ решена — "
                    "продолжай исправление другим способом."
                    % (score, reason)})
            self._reflect_used = 0
            self._agent_request()

    # ---------- Рефлексия (Reflexion-паттерн) ----------
    def _reflect_and_retry(self, why):
        """Агент застрял: разбор ошибки + повторная попытка с уроком."""
        if self._reflect_used >= 2:
            self._agent_finish("застрял: %s (рефлексия исчерпана)" % why)
            return
        self._reflect_used += 1
        sys = ("Ты — механизм самокритики агента. Агент застрял: %s. "
               "Проанализируй историю и выпиши короткий урок: что пошло не "
               "так и какой НОВЫЙ подход попробовать. Ответь одним абзацем, "
               "без команд." % why)
        hist = "\n".join(
            m.get("content", "") if isinstance(m.get("content"), str)
            else str(m.get("content")) for m in self._agent_history[-12:])
        self.worker = AiWorker(self.settings,
                               [{"role": "system", "content": sys},
                                {"role": "user", "content": hist}])
        self.worker.result.connect(self._reflection_got)
        self.worker.failed.connect(
            lambda e: self._agent_finish("рефлексия не удалась: %s" % e))
        self.worker.start()

    def _reflection_got(self, lesson):
        self.ai_output.appendPlainText("🪞 Рефлексия: %s\n" % lesson[:300])
        self._agent_history.append(
            {"role": "user", "content":
                "Урок от самокритики: %s\nИсправь подход и продолжай "
                "командой." % lesson})
        self._agent_request()

    # ---------- Диагноз по фотографии (vision) ----------
    def _diagnose_photo(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Фото экрана с ошибкой", "",
            "Изображения (*.jpg *.jpeg *.png *.bmp)")
        if not path:
            return
        try:
            import base64
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
        except OSError as ex:
            QMessageBox.warning(self, "Фото", "Не удалось прочитать файл:\n"
                                              + str(ex))
            return
        if not self._ai_available():
            return
        if not self.settings.get("api_key"):
            QMessageBox.information(
                self, "Диагноз по фото",
                "Для распознавания фото нужен облачный провайдер с ключом "
                "(Groq или OpenAI).\nOllama: используйте модель llava.")
            return
        kb = ("\n\nБаза знаний:\n" + self.kb_text) if self.kb_text else ""
        sys = ("Ты — эксперт по диагностике устройств по фотографиям экрана. "
               "На фото — консоль устройства (U-Boot/ТВ/приставка). Найди "
               "все ошибки, расшифруй каждую и составь план восстановления. "
               "Ответ строго по шаблону:\nОШИБКИ: <список>\nПРИЧИНА: <главная "
               "причина>\nПЛАН: <пронумерованные шаги с командами>.\n"
               "Пиши по-русски." + kb)
        messages = [{"role": "system", "content": sys},
                    {"role": "user", "content": [
                        {"type": "text",
                         "text": "Проанализируй этот снимок экрана."},
                        {"type": "image_url",
                         "image_url": {"url": "data:image/jpeg;base64,"
                                                + b64}}]}]
        self.ai_output.appendPlainText("— анализирую фото…\n")
        self._ask_ai(messages, lambda t: AnalysisDialog(t, self).exec())

    # ---------- Командная палитра (Ctrl+K) ----------
    def _show_palette(self):
        actions = [
            ("Подключиться", self.connect_ssh),
            ("Отключиться", self.disconnect_ssh),
            ("Полный анализ устройства", self._full_analysis),
            ("Объяснить вывод", self.explain_output),
            ("Исправить автоматически", self._auto_fix),
            ("Диагноз по фото", self._diagnose_photo),
            ("Создать сценарий восстановления", self._make_script),
            ("Следующий шаг сценария", self._copy_next_step),
            ("Обучиться на новой ошибке", self._learn_new),
            ("Применить найденное решение", self._apply_skill),
            ("Сохранить успешное решение", self._learn_success),
            ("Очистить терминал", self.term.clear),
            ("Настройки ИИ", self.edit_ai_settings),
            ("Проверить обновления", self._check_update),
        ]
        dlg = QDialog(self)
        dlg.setWindowTitle("Команды (Ctrl+K)")
        dlg.resize(420, 380)
        lay = QVBoxLayout(dlg)
        edit = QLineEdit()
        edit.setPlaceholderText("Введите команду…")
        lst = QListWidget()
        for name, fn in actions:
            QListWidgetItem(name, lst)
        lay.addWidget(edit)
        lay.addWidget(lst)
        lst.setCurrentRow(0)

        def refill():
            needle = edit.text().lower()
            lst.clear()
            for name, fn in actions:
                if needle in name.lower():
                    QListWidgetItem(name, lst)
            if lst.count():
                lst.setCurrentRow(0)

        def run_current():
            item = lst.currentItem()
            if item:
                dlg.accept()
                for name, fn in actions:
                    if name == item.text():
                        fn()
                        return

        edit.textChanged.connect(refill)
        edit.returnPressed.connect(run_current)
        lst.itemDoubleClicked.connect(lambda _i: run_current())
        edit.setFocus()
        dlg.exec()

    # ---------- Проверка обновлений (GitHub Releases) ----------
    def _check_update(self):
        today = datetime.date.today().isoformat()
        if self.config.get("last_update_check") == today:
            return
        self.config["last_update_check"] = today
        self._save_config()
        try:
            req = urllib.request.Request(
                "https://api.github.com/repos/Alexkkkkk/pytty-ai/"
                "releases/latest",
                headers={"User-Agent": "putty-ai"})
            with urllib.request.urlopen(req, timeout=6) as r:
                data = json.loads(r.read().decode("utf-8"))
            tag = str(data.get("tag_name", "")).lstrip("v")
            assets = data.get("assets") or []
            url = assets[0].get("browser_download_url", "") if assets \
                else data.get("html_url", "")
            if tag and url and tag != APP_VERSION:
                ret = QMessageBox.question(
                    self, "Обновление PuTTY-AI",
                    "Доступна версия %s.\nОткрыть страницу загрузки?" % tag,
                    QMessageBox.StandardButton.Yes |
                    QMessageBox.StandardButton.No)
                if ret == QMessageBox.StandardButton.Yes:
                    QDesktopServices.openUrl(QUrl(url))
        except Exception:
            pass

    # ---------- Семантическая память (Voyager skill library) ----------
    def _embed(self, text):
        """Вектор текста через OpenAI-совместимый /embeddings (Ollama и др.)."""
        text = text[:2000]
        if text in self._vec_cache:
            return self._vec_cache[text]
        model = self.settings.get("embed_model", "nomic-embed-text")
        headers = {"Content-Type": "application/json"}
        if self.settings.get("api_key"):
            headers["Authorization"] = "Bearer " + self.settings["api_key"]
        req = urllib.request.Request(
            self.settings["base_url"].rstrip("/") + "/embeddings",
            data=json.dumps({"model": model, "input": text}).encode(),
            headers=headers)
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read().decode("utf-8"))
        vec = resp["data"][0]["embedding"]
        if len(self._vec_cache) > 200:
            self._vec_cache.clear()
        self._vec_cache[text] = vec
        return vec

    def _find_best_skill(self, out):
        """Поиск навыка: сначала по смыслу (эмбеддинги), иначе по ключам."""
        best, best_s = None, 0.0
        if self.chk_semantic.isChecked() and \
                any("vec" in s for s in self.skills):
            try:
                now = time.time()
                if now - self._out_vec[0] > 10:
                    self._out_vec = (now, self._embed(out))
                ov = self._out_vec[1]
                for s in self.skills:
                    vec = s.get("vec")
                    if vec:
                        sc = _cosine(ov, vec)
                        if sc > best_s:
                            best, best_s = s, sc
                if best_s >= 0.72:
                    return best, best_s
                best, best_s = None, 0.0
            except Exception:
                pass  # эмбеддинги недоступны → ключевой поиск
        for s in self.skills:
            sc = self._skill_score(s, out)
            if sc > best_s:
                best, best_s = s, sc
        if best_s >= 0.5:
            return best, best_s
        return None, 0.0

    # ---------- Автокуррикулум (Voyager curriculum) ----------
    def _curriculum_update(self):
        cats = {
            "USB/FAT": ["usb", "fatload", "partition not valid"],
            "AVB/подпись": ["avb", "bad signature", "verity"],
            "eMMC": ["mmc", "emmc"],
            "NAND": ["nand"],
            "SPI": ["spi flash", "sf erase"],
            "Загрузка ядра": ["bootcmd", "kernel", "panic"],
        }
        blob = (self.kb_text.lower()
                + json.dumps(self.skills, ensure_ascii=False).lower())
        uncovered = [c for c, kws in cats.items()
                     if not any(k in blob for k in kws)]
        extra = "не покрыто: " + ", ".join(uncovered) if uncovered \
            else "все категории покрыты ✓"
        self.skill_status.setText("навыков: %d | %s" % (len(self.skills),
                                                        extra))

    # ---------- Ватчдог автоэскалации ----------
    def _watchdog_check(self, out):
        now = time.time()
        key = None
        for line in out.splitlines():
            ls = line.strip()
            low = ls.lower()
            if ls.startswith("**") or "error" in low or "fail" in low:
                key = ls[:60]
                break
        if not key:
            return
        self._err_history = [(t, k) for t, k in self._err_history
                             if now - t < 120]
        self._err_history.append((now, key))
        same = sum(1 for _, k in self._err_history if k == key)
        if same >= 3 and key not in self._wd_fired:
            self._wd_fired.add(key)
            self.ai_output.appendPlainText(
                "🚨 ВАТЧДОГ: ошибка повторяется (%d раз за 2 мин): %s\n"
                "Запускаю автодиагностику…\n" % (same, key))
            if self.settings.get("base_url"):
                QTimer.singleShot(1000, self._full_analysis)

    # ---------- Бригада: планировщик (CrewAI-паттерн) ----------
    def _plan_first(self):
        sys = ("Ты — планировщик восстановления. Составь краткий план (3-7 "
               "шагов) по выводу терминала. Каждая строка: «команда — "
               "критерий успеха». Без пояснений.")
        out = self.term.last_output(100)
        self.worker = AiWorker(self.settings,
                               [{"role": "system", "content": sys},
                                {"role": "user", "content": out}])
        self.worker.result.connect(self._plan_got)
        self.worker.failed.connect(lambda _e: self._agent_request())
        self.worker.start()

    def _plan_got(self, plan):
        self.ai_output.appendPlainText("📋 План бригады:\n%s\n" % plan)
        self._agent_history.append(
            {"role": "user", "content":
                "ПЛАН восстановления (следуй ему шаг за шагом; критик "
                "проверяет каждый шаг):\n" + plan})
        self._agent_request()

    # ---------- Самонастройка промптов (DSPy-паттерн) ----------
    def _tune_prompts(self):
        if not self._ai_available():
            return
        try:
            with open(os.path.join(self._base_dir, "runs.jsonl"),
                      encoding="utf-8") as f:
                lines = f.read().splitlines()[-40:]
        except OSError:
            lines = []
        fails = [l for l in lines if "подтверждено верификатором" not in l
                 and "решена" not in l]
        if len(fails) < 3:
            QMessageBox.information(
                self, "Тюнинг промптов",
                "Недостаточно неудачных запусков для анализа (нужно ≥3).\n"
                "Покрутите агента — включая на эмуляторе.")
            return
        sys = ("Ты — оптимизатор промптов. Ниже журнал неудачных запусков "
               "агента восстановления устройств. Предложи 3-5 конкретных "
               "дополнений к его системному промпту, предотвращающих похожие "
               "неудачи. Ответь готовым текстом для вставки в файл правок.")
        user = "\n".join(fails)
        self.ai_output.appendPlainText("— анализирую журнал запусков…\n")
        self._ask_ai([{"role": "system", "content": sys},
                      {"role": "user", "content": user}], self._apply_tuning)

    def _apply_tuning(self, text):
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        try:
            with open(os.path.join(self._base_dir, "prompt_tuning.md"),
                      "a", encoding="utf-8") as f:
                f.write("\n\n## Тюнинг от %s\n%s\n" % (stamp, text))
        except OSError:
            self.ai_output.appendPlainText("[тюнинг: не удалось записать]\n")
            return
        self.kb_text = self._load_kb()
        self.ai_output.appendPlainText(
            "⚒ Промпты настроены по журналу (prompt_tuning.md), "
            "вступает в силу сразу\n")

    # ---------- Полный анализ ----------
    def _full_analysis(self):
        if not self._ai_available():
            return
        kb = ("\n\nБаза знаний:\n" + self.kb_text) if self.kb_text else ""
        sys = ("Ты — эксперт-диагност по embedded-устройствам и U-Boot. "
               "Сделай ПОЛНЫЙ анализ вывода терминала и ответь СТРОГО по "
               "шаблону:\n"
               "1. УСТРОЙСТВО/ПЛАТФОРМА: что удалось определить "
               "(SoC, плата, загрузчик)\n"
               "2. СОСТОЯНИЕ: что сейчас происходит с устройством\n"
               "3. ОШИБКИ: список найденных ошибок с расшифровкой каждой\n"
               "4. КОРНЕВАЯ ПРИЧИНА: главная причина проблемы\n"
               "5. ПЛАН ВОССТАНОВЛЕНИЯ: пронумерованные шаги с точными "
               "командами для терминала\n"
               "6. РИСКИ: что может пойти не так, чего избегать\n"
               "Пиши по-русски, кратко и по делу." + kb)
        out = self.term.last_output(150)
        messages = [{"role": "system", "content": sys},
                    {"role": "user", "content": "Вывод терминала:\n" + out}]
        self.ai_output.appendPlainText("— выполняю полный анализ…\n")
        self._ask_ai(messages, self._show_analysis)

    def _show_analysis(self, text):
        dlg = AnalysisDialog(text, self)
        dlg.exec()

    def _save_script(self):
        text = self.script_view.toPlainText()
        if not text:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Сохранить сценарий", "recovery_script.txt",
            "Текстовые файлы (*.txt);;Все файлы (*)")
        if path:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(text)
                self.ai_output.appendPlainText(
                    "— сценарий сохранён: " + path + "\n")
            except OSError as ex:
                self.ai_output.appendPlainText(
                    "[ошибка сохранения: %s]\n" % ex)

    def _highlight_step(self, idx):
        """Подсветить в списке строку текущего шага (0-based)."""
        block = self.script_view.document().firstBlock()
        n = -1
        target = None
        while block.isValid():
            txt = block.text().strip()
            if txt and not txt.startswith("#") and not txt.startswith("```"):
                n += 1
                if n == idx:
                    target = block
                    break
            block = block.next()
        if target is None:
            return
        sel = QTextEdit.ExtraSelection()
        sel.format.setBackground(QColor("#2d5a27"))
        sel.cursor = QTextCursor(target)
        self.script_view.setExtraSelections([sel])
        self.script_view.setTextCursor(QTextCursor(target))

    # ---------- Авто-исправление (агент) ----------
    def _agent_prompt(self):
        kb = ("\n\nБаза знаний (опирайся на неё):\n" + self.kb_text) \
            if self.kb_text else ""
        if self.profile_combo.currentData() == "uboot":
            base = (
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
        else:
            base = (
                "Ты — автоматический агент в терминале Linux. Цель — "
                "исправить ошибку из вывода терминала. Отвечай ТОЛЬКО "
                "командой bash для следующего шага (без пояснений и "
                "markdown), WAIT — если нужно подождать, DONE — когда "
                "проблема решена.")
        if getattr(self, "chk_lazy", None) is not None and \
                self.chk_lazy.isChecked():
            base += LAZY_LADDER
        return base

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
        if self.chk_crew.isChecked():
            self._plan_first()   # бригада: сначала план
        else:
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
        try:
            rec = {"ts": datetime.datetime.now().isoformat(),
                   "result": msg,
                   "steps": getattr(self, "_agent_steps", 0),
                   "score": self._last_verify_score,
                   "profile": self.profile_combo.currentData()}
            with open(os.path.join(self._base_dir, "runs.jsonl"),
                      "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass
        self.agent_status.setText("готово: " + msg)
        self.ai_output.appendPlainText("— агент завершил работу (%s)\n" % msg)

    def _agent_got(self, text):
        if not self._agent_active:
            return
        cmd = text.strip().strip("`").strip()
        up = cmd.upper()
        if not cmd or up.startswith("DONE"):
            if self._verifying:
                self._agent_finish("проблема решена")
                return
            self.ai_output.appendPlainText("— агент считает проблему решенной, "
                                           "запускаю верификатор…\n")
            self._verify_result()
            return
        if self._agent_steps >= self.agent_steps_spin.value():
            self._reflect_and_retry("исчерпан лимит шагов")
            return
        if up.startswith("WAIT"):
            self._wait_count += 1
            if self._wait_count > 60:
                self._agent_finish("слишком долгое ожидание операции")
                return
            self.ai_output.appendPlainText("…жду завершения операции…")
            self.agent_status.setText("ожидание… (шаг %d)" % self._agent_steps)
            QTimer.singleShot(self.agent_delay_spin.value() * 1000,
                              self._agent_observe)
            return
        level, pat = self._safety(cmd)
        if level == "blocked":
            if not self.chk_danger.isChecked():
                self.ai_output.appendPlainText(
                    "⛔ Команда заблокирована правилами безопасности:\n"
                    "> " + cmd + "\n")
                self._agent_finish("остановлен: запрещённая команда (%s)" % pat)
                return
            ret = QMessageBox.warning(
                self, "⛔ ОПАСНАЯ КОМАНДА",
                "Команда может ПОЛНОСТЬЮ СТЕРЕТЬ флешку (eMMC/SPI/NAND)!\n\n"
                "> " + cmd + "\n\nПродолжить на свой страх и риск?",
                QMessageBox.StandardButton.Yes |
                QMessageBox.StandardButton.No)
            if ret != QMessageBox.StandardButton.Yes:
                self._agent_finish("опасная команда отклонена пользователем")
                return
        elif level == "risky":
            self.ai_output.appendPlainText(
                "⚠ Внимание: команда записывает во флеш (%s)\n" % pat)

        if self.chk_confirm.isChecked() and not self.chk_autopilot.isChecked():
            ret = QMessageBox.question(
                self, "Авто-исправление — шаг %d" % (self._agent_steps + 1),
                "Выполнить команду?\n\n" + cmd,
                QMessageBox.StandardButton.Yes |
                QMessageBox.StandardButton.No)
            if ret != QMessageBox.StandardButton.Yes:
                self._agent_finish("отменено пользователем")
                return
        # детект петли: агент повторяет одну команду → рефлексия
        first = cmd.splitlines()[0].strip()
        self._cmd_counts[first] = self._cmd_counts.get(first, 0) + 1
        if self._cmd_counts[first] >= 3:
            self._reflect_and_retry("агент зациклился на команде «%s»" % first)
            return
        self._agent_steps += 1
        self._agent_last_cmd = cmd
        self._wait_count = 0
        self.ai_output.appendPlainText("> " + cmd)
        self.agent_status.setText("шаг %d: %s"
                                  % (self._agent_steps,
                                     cmd.splitlines()[0][:60]))
        self._type_command(cmd)
        self._start_expect_wait()  # ждём приглашения консоли, не фикс. паузу

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
            if self.term.local_echo:
                self.term.insertPlainText(ch)
            self._send_to_server(ch)
        self.term.insert_remote("\n")
        self._send_to_server("\n")
        self.term.buffer = ""
        self._session_cmds.extend(
            [l.strip() for l in cmd.splitlines() if l.strip()])

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
        for label, seq in (("Пробел", " "), ("Tab", "\t"), ("Enter", "\r"),
                           ("Esc", "\x1b"), ("Ctrl+C", "\x03")):
            act_key = QAction(label, self)
            act_key.triggered.connect(lambda _=False, s=seq:
                                      self._send_key(s))
            tb.addAction(act_key)
        tb.addSeparator()
        act_photo = QAction("Диагноз по фото", self)
        act_photo.triggered.connect(self._diagnose_photo)
        tb.addAction(act_photo)
        tb.addSeparator()
        tb.addAction(act_exit)

        sc = QShortcut(QKeySequence("Ctrl+K"), self)
        sc.activated.connect(self._show_palette)

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
        self.chk_danger = QCheckBox(
            "Разрешить опасные команды (mmc erase, стирание флеш)")
        self.chk_danger.setChecked(False)
        self.chk_autopilot = QCheckBox(
            "Полное управление (автопилот: без подтверждений шагов)")
        self.chk_autopilot.setChecked(False)
        self.chk_autoanalysis = QCheckBox(
            "При подключении сразу запускать полный анализ")
        self.chk_autoanalysis.setChecked(False)
        self.chk_lazy = QCheckBox(
            "Ленивый режим (ponytail): минимум команд, без over-engineering")
        self.chk_lazy.setChecked(True)
        self.chk_bootcatch = QCheckBox(
            "Сам перехватывать загрузку (UART): по баннеру отправить клавишу")
        self.chk_bootcatch.setChecked(False)
        self.boot_key_combo = QComboBox()
        for label, key in (("Пробел → консоль", " "),
                           ("Tab → обновление с USB", "\t"),
                           ("Esc → консоль", "\x1b"),
                           ("x → консоль", "x"),
                           ("Enter", "\r"),
                           ("— (только наблюдать)", "")):
            self.boot_key_combo.addItem(label, key)
        self.boot_key_combo.setEnabled(False)
        self.chk_bootcatch.toggled.connect(self.boot_key_combo.setEnabled)
        row_b = QHBoxLayout()
        row_b.addWidget(self.chk_bootcatch)
        row_b.addWidget(self.boot_key_combo)
        l0.addLayout(row_b)
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
        l0.addWidget(self.chk_danger)
        l0.addWidget(self.chk_autopilot)
        l0.addWidget(self.chk_autoanalysis)
        l0.addWidget(self.chk_lazy)
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
            "QLabel { color: #7ee787; font-family: Consolas; "
            "background: #0d1117; border: 1px solid #30363d; "
            "border-radius: 5px; padding: 4px; }")
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

        # -- сценарий восстановления --
        g4 = QGroupBox("Сценарий восстановления (ИИ пишет последовательность команд)")
        l4 = QVBoxLayout(g4)
        self.script_view = QPlainTextEdit()
        self.script_view.setReadOnly(True)
        self.script_view.setFont(QFont("Consolas", 10))
        self.script_view.setMaximumHeight(160)
        row4 = QHBoxLayout()
        btn_script = QPushButton("Создать сценарий")
        btn_script.clicked.connect(self._make_script)
        btn_copy_script = QPushButton("Копировать всё")
        btn_copy_script.clicked.connect(self._copy_script)
        self.btn_next_step = QPushButton("Следующий шаг")
        self.btn_next_step.clicked.connect(self._copy_next_step)
        btn_save_script = QPushButton("Сохранить")
        btn_save_script.clicked.connect(self._save_script)
        row4.addWidget(btn_script)
        row4.addWidget(btn_copy_script)
        row4.addWidget(self.btn_next_step)
        row4.addWidget(btn_save_script)
        l4.addLayout(row4)
        l4.addWidget(self.script_view)

        # -- обучение: сохранение удачных решений --
        g5 = QGroupBox("Обучение (запоминает удачные решения)")
        l5 = QVBoxLayout(g5)
        btn_learn = QPushButton("Сохранить успешное решение в базу знаний")
        btn_learn.clicked.connect(self._learn_success)
        l5.addWidget(btn_learn)

        lay.addWidget(g1)
        lay.addWidget(g2)
        lay.addWidget(g3)
        lay.addWidget(g4)
        lay.addWidget(g5)

        # -- самообучение: программа переписывает свои правила --
        g6 = QGroupBox("Самообучение (переписывает свои правила)")
        l6 = QVBoxLayout(g6)
        btn_learn_new = QPushButton("Обучиться на новой ошибке")
        btn_learn_new.clicked.connect(self._learn_new)
        btn_apply_skill = QPushButton("Применить найденное решение")
        btn_apply_skill.clicked.connect(self._apply_skill)
        self.skill_status = QLabel("навыков: 0")
        l6.addWidget(btn_learn_new)
        l6.addWidget(btn_apply_skill)
        l6.addWidget(self.skill_status)
        lay.addWidget(g6)

        # -- автономия следующего уровня --
        g7 = QGroupBox("Автономия следующего уровня")
        l7 = QVBoxLayout(g7)
        self.chk_semantic = QCheckBox(
            "Семантическая память (эмбеддинги Ollama)")
        self.chk_semantic.setChecked(True)
        self.chk_crew = QCheckBox("Бригада: план → исполнение → критик")
        self.chk_crew.setChecked(False)
        btn_tune = QPushButton("Настроить промпты по журналу запусков")
        btn_tune.clicked.connect(self._tune_prompts)
        l7.addWidget(self.chk_semantic)
        l7.addWidget(self.chk_crew)
        l7.addWidget(btn_tune)
        lay.addWidget(g7)

        dock = QDockWidget("ИИ-помощник")
        dock.setWidget(panel)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)

    # ---------- SSH ----------
    def connect_ssh(self):
        if self.ssh and self.ssh.isRunning():
            QMessageBox.information(self, "Подключение", "Уже подключено.")
            return
        dlg = ConnectDialog(self._conn, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self._conn = {"type": dlg.conn_type.currentData(),
                      "host": dlg.host.text(),
                      "port": dlg.port.value(),
                      "user": dlg.user.text(),
                      "port_name": dlg.port_name.currentText(),
                      "baud": dlg.baud.currentText()}
        self._save_config()
        kind = dlg.conn_type.currentData()
        if kind == "mock":
            self.ssh = MockDevice()
            self.term.local_echo = False
            self.term.enter_seq = "\r"
        elif kind == "serial":
            if serial is None:
                QMessageBox.critical(self, "Ошибка",
                                     "Не установлен пакет pyserial:\n"
                                     "pip install pyserial")
                return
            port = dlg.port_name.currentText().strip()
            if not port:
                QMessageBox.warning(self, "Serial", "Укажите COM-порт.")
                return
            try:
                baud = int(dlg.baud.currentText().strip())
            except ValueError:
                baud = 115200
            self.ssh = SerialWorker(port, baud)
            self.term.local_echo = False
            self.term.enter_seq = "\r"
        else:
            if paramiko is None:
                QMessageBox.critical(self, "Ошибка",
                                     "Не установлен пакет paramiko:\n"
                                     "pip install paramiko")
                return
            self.ssh = SshWorker(dlg.host.text().strip(), dlg.port.value(),
                                 dlg.user.text().strip(),
                                 dlg.password.text(),
                                 dlg.keyfile.text().strip())
        self.ssh.dataReceived.connect(self._on_term_output)
        self.ssh.connected.connect(self._on_connected_auto)
        self.ssh.disconnected.connect(self._on_disconnected)
        self.ssh.start()

    def disconnect_ssh(self):
        if self.ssh:
            self.ssh.stop()
            self.ssh.wait(2000)
            self.ssh = None
        self.term.set_connected(False)
        self.term.local_echo = True
        self.term.enter_seq = "\n"
        self.term.insert_remote("\n[отключено]\n")

    def _on_connected_auto(self):
        """После подключения: отметить + (по галочке) автоанализ."""
        self.term.set_connected(True)
        self._boot_armed = True   # вооружаем авто-перехват загрузки
        if self.chk_autoanalysis.isChecked() and \
                self.settings.get("base_url"):
            QTimer.singleShot(2000, self._full_analysis)

    # ---------- авто-перехват загрузки (UART) ----------
    BOOT_BANNER_PATTERNS = ("U-Boot", "DRAM:", "BootROM", "BROM",
                            "HAL_DDR", "SPI Nor")

    def _try_boot_catch(self, text):
        """Увидели баннер загрузки → сами отправляем клавишу перехвата."""
        if not getattr(self, "_boot_armed", False):
            return
        if not self.chk_bootcatch.isChecked():
            return
        if not any(p in text for p in self.BOOT_BANNER_PATTERNS):
            return
        self._boot_armed = False
        label = self.boot_key_combo.currentText()
        self.ai_output.appendPlainText(
            "⚡ авто-перехват загрузки: отправляю «%s»\n" % label)
        self._send_boot_key(3)

    def _send_boot_key(self, count):
        key_seq = self.boot_key_combo.currentData()
        if not key_seq or count <= 0:
            return
        self._send_key(key_seq)
        QTimer.singleShot(350, lambda: self._send_boot_key(count - 1))

    def _on_disconnected(self, err):
        self.term.set_connected(False)
        if err:
            self.term.insert_remote(f"\n[ошибка SSH: {err}]\n")
        else:
            self.term.insert_remote("\n[соединение закрыто]\n")

    def _send_to_server(self, text):
        if self.ssh and self.ssh.isRunning():
            self.ssh.send(text)
            self._log_io("IN ", text)

    def _send_key(self, seq):
        """Быстрая отправка служебной клавиши (Пробел/Tab/Enter/Esc/Ctrl+C)."""
        if self.ssh and self.ssh.isRunning():
            self.ssh.send(seq)
            names = {" ": "Пробел", "\t": "Tab", "\r": "Enter",
                     "\x1b": "Esc", "\x03": "Ctrl+C"}
            self.ai_output.appendPlainText(
                "⌨ отправлено: %s\n" % names.get(seq, repr(seq)))

    # ---------- ИИ ----------
    def edit_ai_settings(self):
        dlg = AiSettingsDialog(self.settings, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.settings["base_url"] = dlg.base.text().strip()
            self.settings["api_key"] = dlg.key.text().strip()
            self.settings["model"] = dlg.model.text().strip()
            self.settings["base_url2"] = dlg.base2.text().strip()
            self.settings["api_key2"] = dlg.key2.text().strip()
            self.settings["model2"] = dlg.model2.text().strip()
            self.settings["llamafile_path"] = dlg.llamafile_path.text().strip()
            self._save_config()

    # ---------- llamafile: локальный движок в одном файле ----------
    def _llamafile_running(self):
        try:
            s = socket.create_connection(("127.0.0.1", 8080), timeout=1)
            s.close()
            return True
        except OSError:
            return False

    def _ensure_llamafile(self):
        """Если выбран llamafile и он не работает — запускаем его."""
        if "8080" not in str(self.settings.get("base_url", "")):
            return True
        if self._llamafile_running():
            return True
        path = str(self.settings.get("llamafile_path", "")).strip()
        if not path or not os.path.exists(path):
            QMessageBox.information(
                self, "llamafile",
                "Локальный движок не запущен.\n\nСкачайте llamafile "
                "(один .exe с моделью внутри):\n"
                "https://github.com/Mozilla-Ocho/llamafile\n\n"
                "Затем укажите путь к нему в «Настройки ИИ» — программа "
                "будет запускать его сама.")
            return False
        try:
            self.ai_output.appendPlainText("🚀 запускаю llamafile…\n")
            kwargs = {}
            if sys.platform.startswith("win"):
                kwargs["creationflags"] = 0x00000008  # DETACHED_PROCESS
            subprocess.Popen([path, "--server", "--port", "8080"],
                             **kwargs)
            for _ in range(60):  # ждём готовности до 60 сек
                self.msleep_check(1000)
                if self._llamafile_running():
                    self.ai_output.appendPlainText("✔ llamafile готов\n")
                    return True
        except OSError as ex:
            QMessageBox.warning(self, "llamafile",
                                "Не удалось запустить:\n" + str(ex))
        return self._llamafile_running()

    def msleep_check(self, ms):
        """Безопасная пауза с обработкой событий Qt."""
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    def _ai_available(self):
        if self._ai_busy:
            self.ai_output.appendPlainText("…ИИ занят, подождите…")
            return False
        if not self._ensure_llamafile():
            return False
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
        level, _pat = self._safety(cmd)
        if level != "ok":
            ret = QMessageBox.warning(
                self, "Опасная команда",
                "Команда может повредить данные на флешке!\n\n> " + cmd +
                "\n\nВыполнить?",
                QMessageBox.StandardButton.Yes |
                QMessageBox.StandardButton.No)
            if ret != QMessageBox.StandardButton.Yes:
                return
        # вставляем, только если терминал не занят своей строкой
        if self.term.buffer.strip():
            self.term.insert_remote("\n")
            self.term.buffer = ""
        for ch in cmd:
            self.term.buffer += ch
            if self.term.local_echo:
                self.term.insertPlainText(ch)
            self._send_to_server(ch)
        self.term.insert_remote("\n")
        self._send_to_server("\n")
        self.term.buffer = ""
        self._session_cmds.extend(
            [l.strip() for l in cmd.splitlines() if l.strip()])

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
        self._save_config()
        if self.ssh:
            self.ssh.stop()
            self.ssh.wait(2000)
        e.accept()


# ---------------------------------------------------------------------------
# Дизайн: тёмная тема в стиле современных IDE
# ---------------------------------------------------------------------------
STYLESHEET = """
QWidget {
    background-color: #1b1e23;
    color: #d7dae0;
    font-family: "Segoe UI";
    font-size: 13px;
}
QMainWindow::separator { background: #2a2e36; width: 3px; height: 3px; }
QToolBar {
    background: #23272e;
    border: none;
    border-bottom: 1px solid #2f343d;
    spacing: 4px;
    padding: 4px;
}
QToolButton {
    background: transparent;
    border-radius: 6px;
    padding: 5px 10px;
    color: #d7dae0;
}
QToolButton:hover { background: #2f3540; }
QToolButton:pressed { background: #3b4252; }
QPushButton {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #2f3540, stop:1 #262b33);
    border: 1px solid #3c4350;
    border-radius: 6px;
    padding: 6px 12px;
    color: #e2e6ec;
}
QPushButton:hover { border-color: #4f9cf9; background: #333a46; }
QPushButton:pressed { background: #1f242b; }
QPushButton:disabled { color: #6b7280; background: #23272e; }
QGroupBox {
    background: #20242b;
    border: 1px solid #2f343d;
    border-radius: 8px;
    margin-top: 14px;
    padding-top: 10px;
    font-weight: 600;
    color: #9fd0ff;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 10px;
    padding: 0 4px;
}
QLineEdit, QSpinBox, QComboBox, QPlainTextEdit {
    background: #14171c;
    border: 1px solid #333a45;
    border-radius: 5px;
    padding: 4px 6px;
    selection-background-color: #4f9cf9;
    selection-color: #ffffff;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus { border-color: #4f9cf9; }
QComboBox::drop-down { border: none; width: 22px; }
QComboBox QAbstractItemView {
    background: #1b1f26;
    border: 1px solid #333a45;
    selection-background-color: #4f9cf9;
}
QCheckBox { spacing: 6px; color: #c9ced6; }
QCheckBox::indicator {
    width: 15px;
    height: 15px;
    border: 1px solid #3c4350;
    border-radius: 4px;
    background: #14171c;
}
QCheckBox::indicator:checked {
    background: #4f9cf9;
    border-color: #4f9cf9;
}
QLabel { color: #c9ced6; background: transparent; }
QDockWidget { color: #9fd0ff; font-weight: 600; }
QDockWidget::title {
    background: #23272e;
    padding: 6px;
    border-bottom: 1px solid #2f343d;
}
QScrollBar:vertical {
    background: #16191e;
    width: 10px;
    border-radius: 5px;
}
QScrollBar::handle:vertical {
    background: #333a45;
    min-height: 24px;
    border-radius: 5px;
}
QScrollBar::handle:vertical:hover { background: #4f9cf9; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal {
    background: #16191e;
    height: 10px;
    border-radius: 5px;
}
QScrollBar::handle:horizontal {
    background: #333a45;
    min-width: 24px;
    border-radius: 5px;
}
QScrollBar::handle:horizontal:hover { background: #4f9cf9; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QMessageBox, QDialog { background: #1b1e23; }
QMenu { background: #23272e; border: 1px solid #333a45; }
QMenu::item:selected { background: #4f9cf9; }
QToolTip {
    background: #23272e;
    color: #d7dae0;
    border: 1px solid #333a45;
}
"""


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLESHEET)
    try:
        icon_path = os.path.join(
            os.path.dirname(os.path.abspath(sys.argv[0])), "app.ico")
        if os.path.exists(icon_path):
            app.setWindowIcon(QIcon(icon_path))
    except Exception:
        pass
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
