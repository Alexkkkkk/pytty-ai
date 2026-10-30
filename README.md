# PuTTY-AI v3 — SSH-клиент с ИИ: объясняет, подсказывает и САМ исправляет

## Что нового в v3

- 🤖 **Авто-исправление (агент)** — ИИ сам чинит ошибки: анализирует вывод
  терминала → выполняет команду → читает результат → повторяет, пока не
  исправит. Отвечает `WAIT` при долгих операциях (прошивка), `DONE` когда
  готово
- 🔒 **Безопасность агента**: подтверждение каждой команды (галочка),
  лимит шагов, повторное нажатие кнопки — стоп
- 📚 **База знаний U-Boot** (`u_boot_errors_kb.md`) — грузится автоматически
- 🔧 **Профиль «U-Boot / прошивка устройств»** — объяснения, команды и
  автодополнение под загрузчик

## Установка

```cmd
pip install -r requirements.txt
python putty_ai.py          :: Windows 10/11 (PyQt6, Python 3.11+)
python putty_ai_win7.py     :: Windows 7 (PyQt5, Python 3.8)
```

## Как пользоваться агентом

1. Подключитесь к устройству через «Подключиться»
2. Выберите профиль (например «U-Boot / прошивка устройств»)
3. Нажмите **«Исправить ошибку автоматически»**
4. Подтверждайте команды (или снимите галочку для полного автопилота —
   осторожно!)
5. Наблюдайте в журнале «Ответы ИИ» и статусе агента

⚠️ Агент работает через терминал самой программы (SSH). Во внешний
PuTTY по COM-порту печатать не может — для UART копируйте команды вручную.

## Настройка ИИ

В «Настройки ИИ» есть выпадающий список провайдеров — выбор сам подставит
URL и модель, останется вставить ключ:

- **Groq (рекомендуется):** очень быстрые ответы, есть бесплатный лимит.
  Ключ: https://console.groq.com/keys (регистрация бесплатная).
  URL `https://api.groq.com/openai/v1`, модель `llama-3.3-70b-versatile`
  (или `llama-3.1-8b-instant` — ещё быстрее, `qwen-qwq-32b` — для рассуждений).
  Работает даже на Win7 — это облачный сервис.
- **Ollama (Win10+, локально, без интернета):** `ollama pull qwen2.5-coder`,
  URL `http://localhost:11434/v1`, ключ пустой
- **OpenAI:** `https://api.openai.com/v1` + ключ, модель `gpt-4o-mini`
- **Свой сервер:** любой OpenAI-совместимый endpoint

## Сборка .exe (с базой знаний внутри)

```cmd
pip install pyinstaller
pyinstaller --onefile --noconsole --name "PuTTY-AI" --add-data "u_boot_errors_kb.md;." putty_ai.py
```

## Компиляция в .exe — полный комплект

В папке всё готово для сборки одним кликом:

| Файл | Назначение |
|------|-----------|
| `build_exe_win10.bat` | сборка для Win10/11 (двойной клик) |
| `build_exe_win7.bat` | сборка для Win7 (двойной клик) |
| `PuTTY-AI.spec` | spec-файл PyInstaller (используется win10-скриптом) |
| `requirements-build.txt` | зависимости для сборки Win10 |
| `requirements-build-win7.txt` | зависимости для сборки Win7 |
| `app.ico` | иконка программы (вшивается в exe) |
| `learned_cases.md` | стартовый файл обучения (вшивается в exe, рядом с exe дописывается) |

Просто запустите нужный .bat — он сам установит зависимости,
соберёт exe и положит его в `dist\PuTTY-AI.exe`.

Ручная сборка (Win10): `python -m PyInstaller --clean --noconfirm PuTTY-AI.spec`

## 🧬 Собственная модель ИИ (дообучение на ваших данных)

Программа копит ваш опыт (skills.json, learned_cases.md). Превратите его
в свою нейросеть:

```cmd
python export_dataset.py        :: данные -> dataset.jsonl (ShareGPT)
```

Далее откройте **colab_train_unsloth.ipynb** (Google Colab, бесплатный T4):
загрузите dataset.jsonl → Run all → через 30-60 минут скачайте GGUF.

Подключение: **Modelfile** (в архиве) → `ollama create putty-ai -f Modelfile`
→ в программе выберите модель putty-ai. Или через llamafile (Win7).

Чем больше работаете → больше навыков → умнее ваша модель. Цикл бесконечный.

## Support the project

If this project is useful to you, you can support its development with a voluntary donation in **TON**.

```text
UQCZ10Eh8IJ5sFC6JKn-YralMF45Bk5fkNT6SqTHSIRGxfQv
```

- [Open in a TON wallet](ton://transfer/UQCZ10Eh8IJ5sFC6JKn-YralMF45Bk5fkNT6SqTHSIRGxfQv?text=Project%20donation)
- [Verify the address in TON Viewer](https://tonviewer.com/UQCZ10Eh8IJ5sFC6JKn-YralMF45Bk5fkNT6SqTHSIRGxfQv)

Donations are voluntary and help fund development, testing, documentation, and maintenance.
Never share a seed phrase, private key, or confirmation code. Check the address and network before sending.

<!-- DONATION-CAMPAIGN -->
## Support the next release

![Support open-source projects](https://raw.githubusercontent.com/Alexkkkkk/mount-file/main/docs/assets/project-donation-ad.png)

Your support funds the next release: development, testing, documentation, and maintenance.

**Donate in TON:** `UQCZ10Eh8IJ5sFC6JKn-YralMF45Bk5fkNT6SqTHSIRGxfQv`

[Open in a TON wallet](ton://transfer/UQCZ10Eh8IJ5sFC6JKn-YralMF45Bk5fkNT6SqTHSIRGxfQv?text=Project%20support) · [Verify in TON Viewer](https://tonviewer.com/UQCZ10Eh8IJ5sFC6JKn-YralMF45Bk5fkNT6SqTHSIRGxfQv) · [Read the campaign](https://github.com/Alexkkkkk/mount-file/blob/main/docs/donation-campaign.md)

Donations are voluntary. Check the address and network before sending. Never share a seed phrase, private key, or confirmation code.
