# PuTTY-AI (pytty-ai)

SSH-клиент в духе PuTTY с встроенным ИИ-помощником, написанный на Python + PyQt.

![Python](https://img.shields.io/badge/Python-3.8%2F3.11%2B-blue)
![PyQt](https://img.shields.io/badge/PyQt-5%2F6-green)
![License: MIT](https://img.shields.io/badge/License-MIT-yellow)

## Возможности

- 🔐 **SSH-подключение** — по паролю или приватному ключу (RSA/ED25519)
- 💻 **Терминал** — история команд (↑/↓), Ctrl+C, Ctrl+L, локальное эхо
- 🤖 **ИИ-ассистент:**
  - объяснение вывода и ошибок терминала по-русски
  - подбор команды по описанию задачи («найти файлы больше 100 МБ» → готовая команда)
  - автодополнение команд по Tab с учётом контекста (с кэшем)
- 🔌 **OpenAI-совместимый API** — OpenAI, Ollama (локально), LM Studio, vLLM

## Версии

| Файл | ОС | Python | Графика |
|------|-----|--------|---------|
| `putty_ai_win10.py` | Windows 10/11 | 3.11+ | PyQt6 |
| `putty_ai_win7.py` | Windows 7 | 3.8.x (последний для Win7) | PyQt5 |

## Быстрый старт (Windows 10/11)

```cmd
pip install -r requirements-win10.txt
python putty_ai_win10.py
```

## Windows 7

Установите [Python 3.8.10](https://www.python.org/downloads/release/python-3810/)
(галочка «Add Python to PATH»), затем:

```cmd
pip install -r requirements-win7.txt
python putty_ai_win7.py
```

## Настройка ИИ

Откройте в программе **«Настройки ИИ»**:

### Локально и бесплатно — Ollama (Win10/11)

```cmd
ollama pull qwen2.5-coder
```

- URL: `http://localhost:11434/v1`
- Ключ: пустой
- Модель: `qwen2.5-coder`

Данные никуда не уходят — всё на вашем ПК (8+ ГБ RAM).

### OpenAI (облако, работает в т.ч. на Win7)

- URL: `https://api.openai.com/v1`
- Ключ: с [platform.openai.com](https://platform.openai.com)
- Модель: `gpt-4o-mini`

## Сборка .exe

На Windows установите зависимости и запустите:

```cmd
build_exe_win10.bat    :: Windows 10/11
build_exe_win7.bat     :: Windows 7
```

Или вручную:

```cmd
pip install pyinstaller
pyinstaller --onefile --noconsole --name "PuTTY-AI" putty_ai_win10.py
```

Готовый файл: `dist\PuTTY-AI.exe` — переносится без установленного Python.

## Ограничения

Это демо-проект, а не полная замена PuTTY:

- нет X11- и порт-форвардинга, SCP, записи сессий
- эмуляция терминала упрощённая (vim и top работают, возможны артефакты)
- нет вставки из буфера обмена мышью в терминал

## Лицензия

MIT — делайте что хотите, упоминание автора приветствуется.
