# -*- coding: utf-8 -*-
"""
TerminalAI Sync Server — сервер синхронизации базы знаний + ИИ-релея.

Загрузите этот файл как main.py на bothost (terminalai.bothost.tech).

Переменные окружения (задаются в панели bothost):
  SYNC_TOKEN   — пароль мастерской (обязательно задайте!)
  AI_API_KEY   — ключ Groq/OpenAI (хранится ТОЛЬКО на сервере)
  AI_UPSTREAM  — https://api.groq.com/openai/v1 (по умолчанию)

Что умеет:
  GET  /health                 — проверка жизни
  GET  /api/sync/skills        — общие навыки (без пароля)
  GET  /api/sync/rules         — общие правила безопасности
  GET  /api/sync/cases         — общие случаи (текст)
  PUT  /api/sync/{name}        — отправка базы (нужен X-Token)
  POST /v1/chat/completions    — реле ИИ: программы ходят сюда,
                                 ключ Groq не светится на рабочих ПК
  POST /v1/embeddings          — реле эмбеддингов (семант. память)

В программе PuTTY-AI: «Настройки ИИ» → «Свой сервер» →
URL: https://terminalai.bothost.tech/v1
Ключ: <SYNC_TOKEN>
"""

import json
import os
import shutil
import subprocess
import threading
import urllib.request
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

app = FastAPI(title="TerminalAI Sync Server", version="1.0.0")

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA, exist_ok=True)

SYNC_TOKEN = os.environ.get("SYNC_TOKEN", "")
AI_UPSTREAM = os.environ.get("AI_UPSTREAM", "https://api.groq.com/openai/v1")
AI_API_KEY = os.environ.get("AI_API_KEY", "")

# --- локальные модели ---
# LLAMAFILE_URL: ссылка на .llamafile -> сервер сам скачает и запустит
# LOCAL_UPSTREAM: готовый OpenAI-совместимый endpoint (Ollama и т.п.)
# Ollama на любом ПК: LOCAL_UPSTREAM=http://192.168.1.50:11434/v1
LLAMAFILE_URL = os.environ.get("LLAMAFILE_URL", "")
# OLLAMA_MODEL: автоустановка Ollama на сервере и скачивание модели
# (например qwen2.5:0.5b). Работает, только если LOCAL_UPSTREAM не задан.
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "")
OLLAMA_PORT = int(os.environ.get("OLLAMA_PORT", "11435"))
OLLAMA_TGZ = os.environ.get(
    "OLLAMA_TGZ",
    "https://github.com/ollama/ollama/releases/latest/"
    "download/ollama-linux-amd64.tar.zst")
LOCAL_UPSTREAM = os.environ.get("LOCAL_UPSTREAM", "")
LOCAL_PORT = int(os.environ.get("LOCAL_PORT", "8081"))
_local_proc = None
_local_ready = {"ready": False}
MODEL_STATUS = {"stage": "не настроена", "pct": None}
AI_ACT = {"now": 0, "total": 0, "last": None, "last_ts": None}
AI_LOG = []   # журнал запросов: [{t, q, m}]
AI_LOAD = {}  # нагрузка по минутам: {"HH:MM": {n, ms_sum, ms_max}}
EVENTS = []   # живой лог: [{t, m}]


def _ev(msg):
    EVENTS.append({"t": _time.strftime("%H:%M:%S"), "m": str(msg)[:200]})
    if len(EVENTS) > 400:
        del EVENTS[:-300]


@app.middleware("http")
async def _log_middleware(request, call_next):
    t0 = _time.monotonic()
    response = await call_next(request)
    if request.url.path not in ("/api/events", "/api/stats"):
        _ev("%s %s -> %s (%dms)" % (request.method, request.url.path,
                                    response.status_code,
                                    int((_time.monotonic() - t0) * 1000)))
    return response


def _load_add(ms):
    b = _time.strftime("%H:%M")
    e = AI_LOAD.setdefault(b, {"n": 0, "ms_sum": 0.0, "ms_max": 0.0})
    e["n"] += 1
    e["ms_sum"] += ms
    if ms > e["ms_max"]:
        e["ms_max"] = ms
    if len(AI_LOAD) > 180:
        for k in sorted(AI_LOAD)[:-120]:
            del AI_LOAD[k]


def _load_summary():
    keys = sorted(AI_LOAD)[-60:]
    out = {}
    tot_n = 0
    tot_ms = 0.0
    for k in keys:
        e = AI_LOAD[k]
        out[k] = {"n": e["n"], "avg_ms": int(e["ms_sum"] / e["n"]),
                  "max_ms": int(e["ms_max"])}
        tot_n += e["n"]
        tot_ms += e["ms_sum"]
    return {"buckets": out,
            "avg_ms": int(tot_ms / tot_n) if tot_n else 0,
            "total": tot_n}


def _ai_log_add(q, model):
    AI_LOG.append({"t": _time.strftime("%H:%M"), "q": q[:80], "m": (model or "")[:40]})
    if len(AI_LOG) > 500:
        del AI_LOG[:-500]


def _ai_log_summary():
    today = _time.strftime("%Y-%m-%d")
    by_hour = {}
    for e in AI_LOG:
        by_hour[e["t"][:2]] = by_hour.get(e["t"][:2], 0) + 1
    return {"by_hour": by_hour,
            "recent": list(reversed(AI_LOG[-30:]))}


def _heartbeat():
    """Каждые 5 минут докладывает, что делает ИИ в простое."""
    import time as _t
    _t.sleep(120)
    while True:
        _t.sleep(300)
        if AI_ACT["last_ts"]:
            idle = int(_t.time() - AI_ACT["last_ts"]) // 60
        else:
            idle = None
        ready = _local_ready["ready"]
        st = MODEL_STATUS.get("stage", "?")
        if idle is None:
            _ev("💤 простой: обращений к ИИ ещё не было · модель: %s" % st)
        elif idle < 5:
            continue   # недавно был запрос — не спамим
        else:
            _ev("💤 простой %d мин · модель: %s · запросов сегодня: %d"
                % (idle, st, AI_ACT["total"]))


threading.Thread(target=_heartbeat, daemon=True).start()


def _read_text_or_none(fname):
    try:
        with open(os.path.join(DATA, fname), encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""

# Никакой ИИ не настроен? -> по умолчанию маленькая локальная модель
# (можно отключить, задав любой из: AI_API_KEY / LOCAL_UPSTREAM / LLAMAFILE_URL)
if not (AI_API_KEY or LOCAL_UPSTREAM or LLAMAFILE_URL or OLLAMA_MODEL):
    OLLAMA_MODEL = "qwen2.5:0.5b"
if AI_API_KEY or _read_text_or_none("ai_key.txt"):
    MODEL_STATUS.update(stage="Groq/OpenAI (облако)", pct=100)
elif OLLAMA_MODEL:
    MODEL_STATUS.update(stage="подготовка Ollama…", pct=0)
elif LLAMAFILE_URL:
    MODEL_STATUS.update(stage="подготовка llamafile…", pct=0)

FILES = {
    "skills": "skills.json",
    "rules": "learned_rules.json",
    "cases": "learned_cases.md",
    "aikey": "ai_key.txt",   # ключ ИИ: запись по токену, чтение запрещено
}

import time as _time
STATS = {"puts": 0, "gets": 0, "start": _time.time()}

HIST_PATH = os.path.join(DATA, "stats_history.json")
HIST = _read_json(HIST_PATH, {}) if False else None  # заглушка до определения


def _bump_daily(key):
    """+1 к сегодняшнему счётчику и сохранение истории."""
    day = _time.strftime("%Y-%m-%d")
    HIST.setdefault(day, {"puts": 0, "gets": 0, "ai": 0})
    HIST[day][key] = HIST[day].get(key, 0) + 1
    try:
        with open(HIST_PATH, "w", encoding="utf-8") as f:
            json.dump(HIST, f)
    except OSError:
        pass


def _read_json(fname, default):
    try:
        with open(os.path.join(DATA, fname), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


HIST = _read_json(HIST_PATH, {})


def _read_text(fname):
    try:
        with open(os.path.join(DATA, fname), encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def _check_token(x_token: Optional[str]):
    """Если SYNC_TOKEN не задан в env — используем пароль по умолчанию,
    чтобы сервер работал сразу после деплоя. Задайте env SYNC_TOKEN,
    чтобы сменить пароль."""
    effective = SYNC_TOKEN or "putty-ai-2026"
    if x_token != effective:
        raise HTTPException(status_code=403, detail="bad token")


def _path(name: str) -> str:
    return os.path.join(DATA, FILES[name])


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/sync/{name}")
def get_sync(name: str):
    """Скачивание общей базы — публично, как raw.githubusercontent."""
    if name == "aikey":
        raise HTTPException(status_code=404, detail="not found")
    if name not in FILES:
        raise HTTPException(status_code=404, detail="unknown file")
    p = _path(name)
    if not os.path.exists(p):
        if name == "cases":
            return PlainTextResponse("")
        return JSONResponse([])
    STATS["gets"] += 1
    _bump_daily("gets")
    with open(p, encoding="utf-8") as f:
        content = f.read()
    if name == "cases":
        return PlainTextResponse(content)
    try:
        return JSONResponse(json.loads(content))
    except ValueError:
        return JSONResponse([])


@app.put("/api/sync/{name}")
async def put_sync(name: str, request: Request,
                   x_token: Optional[str] = Header(None)):
    """Отправка базы — только с паролем мастерской."""
    _check_token(x_token)
    if name not in FILES:
        raise HTTPException(status_code=404, detail="unknown file")
    body = await request.body()
    if name != "cases":
        try:
            json.loads(body.decode("utf-8"))
        except ValueError:
            raise HTTPException(status_code=400, detail="bad json")
    STATS["puts"] += 1
    _bump_daily("puts")
    _ev("SYNC PUT %s (%d B)" % (name, len(body)))
    with open(_path(name), "wb") as f:
        f.write(body)
    return {"ok": True, "bytes": len(body)}


def _start_llamafile():
    global _local_proc, LOCAL_UPSTREAM
    if not LLAMAFILE_URL or _local_proc is not None:
        return
    path = os.path.join(DATA, "model.llamafile")
    try:
        if not os.path.exists(path) or os.path.getsize(path) < 10_000_000:
            tmp = path + ".part"
            MODEL_STATUS.update(stage="скачивание llamafile…", pct=0)
            with urllib.request.urlopen(LLAMAFILE_URL, timeout=900) as r, open(tmp, "wb") as f:
                total = int(r.headers.get("Content-Length") or 0)
                got = 0
                while True:
                    chunk = r.read(1024 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    if total:
                        MODEL_STATUS["pct"] = min(99, int(100 * got / total))
            os.replace(tmp, path)
        os.chmod(path, 0o755)
        log = open(os.path.join(DATA, "llamafile.log"), "ab")
        _local_proc = subprocess.Popen(
            [path, "--server", "--port", str(LOCAL_PORT), "--host", "127.0.0.1"],
            stdout=log, stderr=subprocess.STDOUT)
        LOCAL_UPSTREAM = "http://127.0.0.1:%d/v1" % LOCAL_PORT
    except Exception as ex:
        print("llamafile start failed:", ex)


def _start_ollama():
    """Автоустановка Ollama на сервере + скачивание модели."""
    global LOCAL_UPSTREAM
    if not OLLAMA_MODEL or LOCAL_UPSTREAM:
        return
    base = os.path.join(DATA, "ollama")
    binary = os.path.join(base, "ollama")
    try:
        os.makedirs(base, exist_ok=True)
        if not os.path.exists(binary):
            arc = os.path.join(base, "ollama.tar.zst")
            if not os.path.exists(arc) or os.path.getsize(arc) < 10_000_000:
                tmp = arc + ".part"
                req = urllib.request.Request(
                    OLLAMA_TGZ,
                    headers={"User-Agent": "Mozilla/5.0"})
                MODEL_STATUS.update(stage="скачивание Ollama (1.4 ГБ)…", pct=0)
                with urllib.request.urlopen(req, timeout=1800) as r, \
                        open(tmp, "wb") as f:
                    total = int(r.headers.get("Content-Length") or 0)
                    got = 0
                    while True:
                        chunk = r.read(1024 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                        got += len(chunk)
                        if total:
                            MODEL_STATUS["pct"] = min(99, int(100 * got / total))
                os.replace(tmp, arc)
            import tarfile
            MODEL_STATUS.update(stage="распаковка Ollama…", pct=None)
            if arc.endswith(".zst"):
                import zstandard
                dctx = zstandard.ZstdDecompressor()
                with open(arc, "rb") as fh, \
                        dctx.stream_reader(fh) as rdr, \
                        tarfile.open(fileobj=rdr, mode="r|") as tf:
                    tf.extractall(base)
            else:
                with tarfile.open(arc) as tf:
                    tf.extractall(base)
            # бинарник лежит в ./ollama-linux-amd64/ или корне архива
            for root, _dirs, files in os.walk(base):
                if "ollama" in files:
                    binary = os.path.join(root, "ollama")
                    break
            os.chmod(binary, 0o755)
        env = dict(os.environ, OLLAMA_HOST="127.0.0.1:%d" % OLLAMA_PORT)
        log = open(os.path.join(base, "serve.log"), "ab")
        subprocess.Popen([binary, "serve"], env=env,
                         stdout=log, stderr=subprocess.STDOUT)
        LOCAL_UPSTREAM = "http://127.0.0.1:%d/v1" % OLLAMA_PORT
        # ждём демон, затем качаем модель
        import time as _t
        for _ in range(120):
            _t.sleep(5)
            try:
                with urllib.request.urlopen(
                        "http://127.0.0.1:%d/" % OLLAMA_PORT, timeout=3) as r:
                    break
            except Exception:
                pass
        MODEL_STATUS.update(stage="скачивание модели %s…" % OLLAMA_MODEL, pct=0)
        import re as _re
        proc = subprocess.Popen([binary, "pull", OLLAMA_MODEL], env=env,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        for line in proc.stdout:
            try:
                log.write(line.encode("utf-8", "replace"))
            except Exception:
                pass
            m = _re.search(r"(\d+(?:\.\d+)?)\s*%", line)
            if m:
                MODEL_STATUS["pct"] = min(99, int(float(m.group(1))))
        proc.wait()
        MODEL_STATUS.update(stage="запуск модели…", pct=99)
    except Exception as ex:
        print("ollama start failed:", ex)


if LLAMAFILE_URL:
    threading.Thread(target=_start_llamafile, daemon=True).start()
if OLLAMA_MODEL and not LOCAL_UPSTREAM:
    threading.Thread(target=_start_ollama, daemon=True).start()


def _relay_target():
    """Цепочка: Groq/OpenAI -> Ollama (LOCAL_UPSTREAM) -> llamafile."""
    key = AI_API_KEY or _read_text("ai_key.txt").strip()
    if key:
        return AI_UPSTREAM, key
    if LLAMAFILE_URL and _local_ready["ready"]:
        return "http://127.0.0.1:%d/v1" % LOCAL_PORT, ""
    if LOCAL_UPSTREAM and (not OLLAMA_MODEL or _local_ready["ready"]):
        return LOCAL_UPSTREAM, ""
    return None, None


@app.get("/api/local_model")
def local_model_status():
    ollama_url = ("http://127.0.0.1:%d/v1" % OLLAMA_PORT
                  if OLLAMA_MODEL else None)
    return {"status": dict(MODEL_STATUS),
            "llamafile": {"url": LLAMAFILE_URL or None,
                          "running": bool(_local_proc and _local_proc.poll() is None),
                          "ready": _local_ready["ready"],
                          "port": LOCAL_PORT if LLAMAFILE_URL else None},
            "ollama": {"model": OLLAMA_MODEL or None,
                       "url": LOCAL_UPSTREAM if OLLAMA_MODEL else None,
                       "upstream": LOCAL_UPSTREAM if not OLLAMA_MODEL else None},
            "ready": _local_ready["ready"] or bool(LOCAL_UPSTREAM)}


def _local_watcher():
    """Помечает ready: llamafile — по /health, ollama — когда модель в /api/tags."""
    import time as _t
    checks = []
    if LLAMAFILE_URL:
        checks.append(("http://127.0.0.1:%d/health" % LOCAL_PORT, False))
    if OLLAMA_MODEL:
        checks.append(("http://127.0.0.1:%d/api/tags" % OLLAMA_PORT, True))
    while True:
        _t.sleep(5)
        if _local_ready["ready"] or not checks:
            continue
        for url, need_models in checks:
            try:
                with urllib.request.urlopen(url, timeout=3) as r:
                    if need_models:
                        body = json.loads(r.read().decode() or "{}")
                        if body.get("models"):
                            _local_ready["ready"] = True
                            MODEL_STATUS.update(stage="работает", pct=100)
                    else:
                        _local_ready["ready"] = True
                        MODEL_STATUS.update(stage="работает", pct=100)
            except Exception:
                pass


threading.Thread(target=_local_watcher, daemon=True).start()


def _relay(path: str, body: bytes):
    """Проксирование запроса к Groq/OpenAI серверным ключом."""
    upstream, key = _relay_target()
    if not upstream:
        raise HTTPException(status_code=503, detail="no AI backend: set AI_API_KEY or LLAMAFILE_URL / LOCAL_UPSTREAM")
    req = urllib.request.Request(
        upstream.rstrip("/") + "/" + path,
        data=body,
        headers=({**{"Content-Type": "application/json"}, **({"Authorization": "Bearer " + key} if key else {})}),
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return JSONResponse(json.loads(r.read().decode("utf-8")))
    except urllib.error.HTTPError as ex:
        raise HTTPException(status_code=ex.code, detail=ex.read().decode("utf-8", "replace")[:500])
    except Exception as ex:
        raise HTTPException(status_code=502, detail=str(ex))


@app.post("/v1/chat/completions")
async def relay_chat(request: Request, x_token: Optional[str] = Header(None)):
    _check_token(x_token)
    AI_ACT["now"] += 1
    AI_ACT["total"] += 1
    AI_ACT["last"] = _time.strftime("%H:%M:%S")
    AI_ACT["last_ts"] = _time.time()
    _bump_daily("ai")
    _t0 = _time.monotonic()
    try:
        raw = await request.body()
        try:
            _bj = json.loads(raw.decode("utf-8", "replace"))
            _q = next((m.get("content", "") for m in _bj.get("messages", [])
                       if m.get("role") == "user"), "")
            _ai_log_add(_q or "(пустой запрос)", _bj.get("model"))
            _ev("AI <<< %s" % (_q or "(пустой запрос)")[:120])
        except Exception:
            _ai_log_add("(запрос)", None)
        return _relay("chat/completions", raw)
    finally:
        AI_ACT["now"] -= 1
        _load_add((_time.monotonic() - _t0) * 1000.0)


@app.post("/v1/embeddings")
async def relay_emb(request: Request, x_token: Optional[str] = Header(None)):
    _check_token(x_token)
    AI_ACT["now"] += 1
    AI_ACT["total"] += 1
    _bump_daily("ai")
    _ai_log_add("(эмбеддинги)", None)
    try:
        return _relay("embeddings", await request.body())
    finally:
        AI_ACT["now"] -= 1


# ---------- запуск и начальное заполнение ----------
# Данные вшиты прямо в файл: bothost (и подобные) режут *.md из build-контекста
EMBEDDED = {
    "skills.json": "[]",
    "learned_rules.json": '{"dangerous": [], "risky": []}',
    "learned_cases.md": (
        "# Сохранённые удачные решения (обучение)\n\n"
        "Сюда программа дописывает случаи по кнопке "
        "«Сохранить успешное решение в базу знаний».\n"
    ),
}


def _seed_from_repo():
    """data/ пуста → берём файлы рядом с main.py, иначе встроенные данные."""
    import shutil
    here = os.path.dirname(os.path.abspath(__file__))
    for fname, embedded in EMBEDDED.items():
        dst = os.path.join(DATA, fname)
        if os.path.exists(dst):
            continue
        src = os.path.join(here, fname)
        if os.path.exists(src) and os.path.getsize(src) > 0:
            shutil.copyfile(src, dst)
        else:
            with open(dst, "w", encoding="utf-8") as f:
                f.write(embedded)


_seed_from_repo()



# ---------- Дашборд ----------
DASH_HTML = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TerminalAI — дашборд мастерской</title>
<style>
body{margin:0;background:#0d1117;color:#d7dae0;font-family:'Segoe UI',Arial,sans-serif}
header{padding:18px 26px;background:#161b22;border-bottom:1px solid #30363d;display:flex;align-items:center;gap:14px}
h1{font-size:19px;margin:0;color:#7ee787}
.dot{width:9px;height:9px;border-radius:50%;background:#3fb950;display:inline-block}
.wrap{padding:22px 26px;max-width:1100px;margin:0 auto}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px;margin-bottom:22px}
.card{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:14px 16px}
.card .v{font-size:26px;font-weight:700;color:#7ee787}
.card .l{font-size:12px;color:#8b949e;margin-top:4px}
table{width:100%;border-collapse:collapse;background:#161b22;border:1px solid #30363d;border-radius:10px;overflow:hidden;margin-bottom:22px}
th{background:#1c2128;text-align:left;padding:9px 12px;font-size:12px;color:#9fd0ff;text-transform:uppercase}
td{padding:8px 12px;border-top:1px solid #21262d;font-size:13px;vertical-align:top}
tr:hover td{background:#1c2128}
.badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px}
.b-red{background:#3d1d1d;color:#ff7b72}.b-gray{background:#21262d;color:#8b949e}
.small{color:#8b949e;font-size:12px}
.lvlbar{background:#21262d;border-radius:6px;height:10px;margin:8px 0 4px;overflow:hidden}
#lvl-fill{height:100%;width:0;background:linear-gradient(90deg,#238636,#7ee787);transition:width .6s}
.bars{display:flex;gap:6px;align-items:flex-end;height:120px;padding:14px;background:#161b22;border:1px solid #30363d;border-radius:10px;margin-bottom:22px}
.bar{flex:1;display:flex;flex-direction:column;justify-content:flex-end;gap:3px}
.bar .put,.bar .get{width:100%;border-radius:3px 3px 0 0;min-height:2px}
.bar .put{background:#7ee787}.bar .get{background:#1f6feb}.bar .ai{background:#bc8cff;height:100%;border-radius:3px 3px 0 0;min-height:2px}
.bar .d{font-size:10px;color:#8b949e;text-align:center}
.bar .n{font-size:10px;color:#c9d1d9;text-align:center}
a{color:#58a6ff}
#refresh{float:right}
#ai-status{display:flex;align-items:center;gap:10px;padding:10px 26px;background:#12261a;border-bottom:1px solid #238636;font-size:13px}
#ai-status .bar{flex:1;max-width:320px;height:8px;background:#21262d;border-radius:5px;overflow:hidden}
#ai-status .bar i{display:block;height:100%;background:linear-gradient(90deg,#238636,#7ee787);transition:width .5s}
.chat{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:14px;margin-bottom:22px}
#chat-log{max-height:340px;overflow-y:auto;margin-bottom:12px}
.msg{padding:8px 12px;border-radius:10px;margin:6px 0;max-width:85%;white-space:pre-wrap;font-size:13px;line-height:1.45}
.msg.user{background:#1f6feb;color:#fff;margin-left:auto}
.msg.bot{background:#21262d;border:1px solid #30363d}
.msg.err{background:#3d1d1d;color:#ff7b72;border:1px solid #f85149}
.chat-row{display:flex;gap:8px}
.chat-row input{flex:1;background:#0d1117;border:1px solid #30363d;border-radius:8px;color:#d7dae0;padding:9px 12px;font-size:13px}
.chat-row button{background:#238636;color:#fff;border:none;border-radius:8px;padding:9px 18px;cursor:pointer;font-size:13px}
.chat-row button:disabled{opacity:.5}
.chat-cfg{display:flex;gap:8px;margin-bottom:10px}
.chat-cfg input{width:180px;background:#0d1117;border:1px solid #30363d;border-radius:8px;color:#8b949e;padding:6px 10px;font-size:12px}
</style></head><body>
<header><span class="dot"></span><h1>TerminalAI — сервер мастерской</h1>
<span class="small" id="uptime"></span><span class="small" id="lmodel" style="margin-left:auto"></span></header>
<div id="ai-status"><span id="ai-stage">ИИ: проверка…</span><span class="bar"><i id="ai-pct" style="width:0%"></i></span><span id="ai-num"></span></div>
<div class="wrap">
<div class="cards">
<div class="card"><div class="v" id="c-skills">—</div><div class="l">навыков в базе</div></div>
<div class="card"><div class="v" id="c-cases">—</div><div class="l">сохранённых случаев</div></div>
<div class="card"><div class="v" id="c-danger">—</div><div class="l">правил блокировки</div></div>
<div class="card"><div class="v" id="c-puts">—</div><div class="l">загрузок базы (PUT)</div></div>
<div class="card"><div class="v" id="c-gets">—</div><div class="l">скачиваний (GET)</div></div>
<div class="card" style="grid-column:1/-1">
<div class="v" id="lvl-name" style="font-size:20px">—</div>
<div class="lvlbar"><div id="lvl-fill"></div></div>
<div class="l">уровень обучения мастерской · <span id="lvl-xp">0</span> XP</div>
</div>
</div>
<h3 style="color:#9fd0ff">Навыки мастерской</h3>
<table><thead><tr><th>Ошибка (триггер)</th><th>Решение</th><th>Использований</th><th></th></tr></thead>
<tbody id="skills"></tbody></table>
<h3 style="color:#9fd0ff">Активность мастерской (14 дней)</h3>
<div class="bars" id="chart"></div>
<h3 style="color:#9fd0ff">Активность ИИ (запросов в день)</h3>
<div class="bars" id="ai-chart"></div>
<h3 style="color:#9fd0ff">Нагрузка на ИИ (последний час)</h3>
<div class="bars" id="load-chart"></div>
<div class="small" id="load-info" style="margin:-12px 0 22px"></div>
<h3 style="color:#9fd0ff">ИИ сегодня по часам</h3>
<div class="bars" id="hour-chart"></div>
<h3 style="color:#9fd0ff">Журнал ИИ (live)</h3>
<table><thead><tr><th style="width:60px">Время</th><th>Запрос</th></tr></thead>
<tbody id="ailog"></tbody></table>
<h3 style="color:#9fd0ff">Правила безопасности</h3>
<table><thead><tr><th style="width:110px">Уровень</th><th>Паттерн</th></tr></thead>
<tbody id="rules"></tbody></table>
<div class="chat">
<h3 style="color:#9fd0ff;margin-top:0">💬 Спросить ИИ</h3>
<div class="chat-cfg">
<input id="chat-token" type="password" value="putty-ai-2026" placeholder="пароль (X-Token)">
<input id="chat-model" placeholder="модель" style="width:220px">
</div>
<div id="chat-log"></div>
<div class="chat-row">
<input id="chat-in" placeholder="Напишите вопрос… (Enter — отправить)">
<button id="chat-send" onclick="chatSend()">Отправить</button>
</div>
</div>
<p class="small">Обновление каждые 15 секунд · API: <a href="/log">живой лог</a> · <a href="/docs">/docs</a> · синхронизация: <code>/api/sync/*</code></p>
</div>
<script>
async function load(){
  try{
    const r = await fetch('/api/stats'); const d = await r.json();
    document.getElementById('c-skills').textContent = d.skills;
    document.getElementById('c-cases').textContent = d.cases;
    document.getElementById('c-danger').textContent = d.dangerous + d.risky;
    document.getElementById('c-puts').textContent = d.puts;
    document.getElementById('c-gets').textContent = d.gets;
    const up = d.uptime_s, h = Math.floor(up/3600), m = Math.floor(up%3600/60);
    document.getElementById('uptime').textContent = 'uptime ' + (h? h+'ч ':'') + m + 'м';
    const tb = document.getElementById('skills'); tb.innerHTML = '';
    (d.skills_data||[]).slice().reverse().forEach(s=>{
      const trg = (s.trigger||'').split('\\n')[0];
      const sol = (s.solution||[]).join('; ');
      tb.innerHTML += '<tr><td>'+escapeHtml(trg)+'</td><td class="small">'+escapeHtml(sol)+'</td><td>'+(s.hits||0)+'</td><td>'+(s.dangerous?'<span class="badge b-red">опасно</span>':'')+'</td></tr>';
    });
    if(d.model_status){
      const ms = d.model_status;
      document.getElementById('ai-stage').textContent = '🧠 ' + ms.stage;
      const p = (ms.pct == null) ? null : ms.pct;
      document.getElementById('ai-pct').style.width = (p == null ? 100 : p) + '%';
      document.getElementById('ai-num').textContent = (p == null) ? '' : p + '%';
    }
    const act = d.ai_activity || {now:0,total:0,last:null};
    if(act.now > 0){
      document.getElementById('ai-stage').textContent = '⚡ ИИ отвечает… (одновременно: ' + act.now + ')';
      document.getElementById('ai-pct').style.width = '100%';
      document.getElementById('ai-num').textContent = '';
    } else if(d.model_status && d.model_status.pct === 100){
      document.getElementById('ai-num').textContent = '· запросов: ' + act.total + (act.last ? ' · последний ' + act.last : '');
    }
    if(d.local_model){
      const lm = d.local_model;
      document.getElementById('lmodel').textContent = lm.ready
        ? '🧠 локальная модель: работает (порт ' + lm.port + ')'
        : (lm.url ? '🧠 локальная модель: загружается…' : '');
    }
    if(d.level){
      document.getElementById('lvl-name').textContent = d.level.name;
      document.getElementById('lvl-xp').textContent = d.level.xp;
      document.getElementById('lvl-fill').style.width = d.level.pct + '%';
    }
    const ch = document.getElementById('chart'); ch.innerHTML = '';
    const days = Object.keys(d.daily||{});
    const max = Math.max(1, ...days.map(k => (d.daily[k].puts||0)+(d.daily[k].gets||0)));
    days.forEach(k => {
      const v = d.daily[k];
      ch.innerHTML += '<div class="bar"><div class="n">'+(v.puts||0)+'</div>'
        + '<div class="put" style="height:'+Math.round(100*(v.puts||0)/max)+'%"></div>'
        + '<div class="get" style="height:'+Math.round(100*(v.gets||0)/max)+'%"></div>'
        + '<div class="n">'+(v.gets||0)+'</div>'
        + '<div class="d">'+k.slice(5)+'</div></div>';
    });
    if(!ch.innerHTML) ch.innerHTML = '<div class="small" style="padding:20px">пока нет активности</div>';
    const ach = document.getElementById('ai-chart'); ach.innerHTML = '';
    const amax = Math.max(1, ...days.map(k => (d.daily[k].ai||0)));
    days.forEach(k => {
      const v = d.daily[k].ai || 0;
      ach.innerHTML += '<div class="bar"><div class="n">' + v + '</div>'
        + '<div class="ai" style="height:' + Math.round(100 * v / amax) + '%"></div>'
        + '<div class="d">' + k.slice(5) + '</div></div>';
    });
    if(!ach.innerHTML) ach.innerHTML = '<div class="small" style="padding:20px">запросов к ИИ пока не было — задайте вопрос в чате ниже</div>';
    const lc = document.getElementById('load-chart'); lc.innerHTML = '';
    const lb = d.ai_load ? d.ai_load.buckets : {};
    const lkeys = Object.keys(lb);
    const lmax = Math.max(1, ...lkeys.map(k => lb[k].n));
    lkeys.forEach(k => {
      const v = lb[k];
      const slow = v.avg_ms > 3000;
      lc.innerHTML += '<div class="bar"><div class="n">' + v.n + '</div>'
        + '<div class="' + (slow ? 'ai' : 'put') + '" style="height:' + Math.round(100 * v.n / lmax) + '%"></div>'
        + '<div class="d">' + k + '</div></div>';
    });
    if(!lc.innerHTML) lc.innerHTML = '<div class="small" style="padding:20px">запросов не было — нагрузка нулевая</div>';
    document.getElementById('load-info').textContent = lkeys.length
      ? 'всего за час: ' + d.ai_load.total + ' · средний ответ: ' + d.ai_load.avg_ms + ' мс'
      : '';
    const hc = document.getElementById('hour-chart'); hc.innerHTML = '';
    const hours = d.ai_log ? d.ai_log.by_hour : {};
    const hmax = Math.max(1, ...Object.values(hours));
    for(let h = 0; h < 24; h += 2){
      const v = hours[String(h).padStart(2,'0')] || 0;
      hc.innerHTML += '<div class="bar"><div class="n">' + v + '</div>'
        + '<div class="ai" style="height:' + Math.round(100 * v / hmax) + '%"></div>'
        + '<div class="d">' + String(h).padStart(2,'0') + '</div></div>';
    }
    const lg = document.getElementById('ailog'); lg.innerHTML = '';
    (d.ai_log ? d.ai_log.recent : []).forEach(e => {
      lg.innerHTML += '<tr><td class="small">' + escapeHtml(e.t) + '</td><td>' + escapeHtml(e.q) + '</td></tr>';
    });
    if(!lg.innerHTML) lg.innerHTML = '<tr><td colspan="2" class="small">журнал пуст</td></tr>';
    if(!tb.innerHTML) tb.innerHTML = '<tr><td colspan="4" class="small">база пуста — отправьте навыки из программы кнопкой «На сервер»</td></tr>';
    const rb = document.getElementById('rules'); rb.innerHTML = '';
    (d.rules.dangerous||[]).forEach(p=>{ rb.innerHTML += '<tr><td><span class="badge b-red">блок</span></td><td class="small">'+escapeHtml(p)+'</td></tr>'; });
    (d.rules.risky||[]).forEach(p=>{ rb.innerHTML += '<tr><td><span class="badge b-gray">риск</span></td><td class="small">'+escapeHtml(p)+'</td></tr>'; });
  }catch(e){}
}
function escapeHtml(t){ return String(t).replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
const chatHist = [];
document.getElementById('chat-token').value = localStorage.getItem('srv_tok') || '';
document.getElementById('chat-model').value = localStorage.getItem('srv_mdl') || 'qwen2.5:0.5b';
document.getElementById('chat-in').addEventListener('keydown', e => { if(e.key === 'Enter') chatSend(); });

function chatAdd(cls, text){
  const log = document.getElementById('chat-log');
  const div = document.createElement('div');
  div.className = 'msg ' + cls;
  div.textContent = text;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

async function chatSend(){
  const inp = document.getElementById('chat-in');
  const btn = document.getElementById('chat-send');
  const text = inp.value.trim();
  if(!text) return;
  const token = document.getElementById('chat-token').value.trim();
  const model = document.getElementById('chat-model').value.trim() || 'qwen2.5:0.5b';
  localStorage.setItem('srv_tok', token);
  localStorage.setItem('srv_mdl', model);
  chatAdd('user', text);
  chatHist.push({role:'user', content:text});
  if(chatHist.length > 20) chatHist.splice(0, chatHist.length - 20);
  inp.value = ''; btn.disabled = true;
  chatAdd('bot', '…');
  const pending = document.getElementById('chat-log').lastChild;
  try{
    const r = await fetch('/v1/chat/completions', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-Token': token},
      body: JSON.stringify({model: model, messages: chatHist.slice(), max_tokens: 800})
    });
    const d = await r.json();
    if(r.ok && d.choices && d.choices[0]){
      const ans = d.choices[0].message.content;
      pending.textContent = ans;
      pending.className = 'msg bot';
      chatHist.push({role:'assistant', content:ans});
    } else {
      pending.textContent = 'Ошибка ' + r.status + ': ' + (d.detail || JSON.stringify(d)).slice(0, 300);
      pending.className = 'msg err';
      chatHist.pop();
    }
  }catch(e){
    pending.textContent = 'Сеть: ' + e;
    pending.className = 'msg err';
    chatHist.pop();
  }
  btn.disabled = false;
  document.getElementById('chat-log').scrollTop = 1e9;
}

load(); setInterval(load, 15000);
</script></body></html>"""


@app.get("/api/stats")
def api_stats():
    skills = _read_json("skills.json", [])
    rules = _read_json("learned_rules.json", {"dangerous": [], "risky": []})
    cases = _read_text("learned_cases.md")
    return {
        "uptime_s": int(_time.time() - STATS["start"]),
        "skills": len(skills),
        "cases": cases.count("## Удачный случай"),
        "dangerous": len(rules.get("dangerous", [])),
        "risky": len(rules.get("risky", [])),
        "puts": STATS["puts"],
        "gets": STATS["gets"],
        "skills_data": skills[-100:],
        "rules": rules,
        "level": _learning_level(skills, cases.count("## Удачный случай")),
        "local_model": local_model_status(),
        "model_status": dict(MODEL_STATUS),
        "ai_activity": dict(AI_ACT),
        "ai_log": _ai_log_summary(),
        "ai_load": _load_summary(),
        "daily": {d: HIST[d] for d in sorted(HIST)[-14:]},
    }


LEVELS = [(0, "Новичок"), (50, "Опытный"), (150, "Эксперт"),
          (400, "Мастер"), (1000, "Легенда мастерской")]


def _learning_level(skills, cases):
    xp = sum(int(s.get("hits", 0)) for s in skills) * 5 \
        + len(skills) * 20 + cases * 15
    name = LEVELS[0][1]
    floor = 0
    for need, lvl in LEVELS:
        if xp >= need:
            name, floor = lvl, need
    nxt = next((n for n, _ in LEVELS if n > xp), None)
    if nxt:
        pct = int(100 * (xp - floor) / (nxt - floor))
    else:
        pct = 100
    return {"xp": xp, "name": name, "pct": pct, "next": nxt}


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return HTMLResponse(DASH_HTML)


def _mem_mb():
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable"):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    return None


@app.get("/api/events")
def api_events():
    idle_s = None
    if AI_ACT["last_ts"]:
        idle_s = int(_time.time() - AI_ACT["last_ts"])
    return {"events": EVENTS[-250:], "n": len(EVENTS),
            "idle_s": idle_s, "ready": _local_ready["ready"],
            "now": _time.strftime("%H:%M:%S"),
            "model": OLLAMA_MODEL or (LLAMAFILE_URL and "llamafile") or "Groq/облако",
            "mem_mb": _mem_mb(),
            "ai_now": AI_ACT["now"], "ai_total": AI_ACT["total"]}


LOG_PAGE = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TerminalAI — живой лог</title>
<style>
body{margin:0;background:#0d1117;color:#7ee787;font-family:Consolas,monospace;font-size:13px}
header{padding:12px 20px;background:#161b22;border-bottom:1px solid #30363d;display:flex;gap:12px;align-items:center;position:sticky;top:0}
header b{color:#d7dae0;font-family:'Segoe UI',Arial}
button{background:#21262d;border:1px solid #30363d;color:#c9d1d9;border-radius:6px;padding:5px 12px;cursor:pointer}
a{color:#58a6ff;text-decoration:none;margin-left:auto}
#log{padding:14px 20px;white-space:pre-wrap;line-height:1.55}
.t{color:#8b949e}.post{color:#79c0ff}.get{color:#7ee787}.err{color:#ff7b72}.ai{color:#bc8cff}
.dot{width:8px;height:8px;border-radius:50%;background:#3fb950;display:inline-block;animation:bl 1.4s infinite}
@keyframes bl{50%{opacity:.3}}
</style></head><body>
<header><span class="dot"></span><b>Живой лог сервера</b>
<button id="pause" onclick="togglePause()">⏸ пауза</button>
<button onclick="document.getElementById('log').innerHTML=''">🧹 очистить</button>
<button id="sec" onclick="toggleSec()">⏱ каждую секунду: ВЫКЛ</button>
<span id="idle" class="t"></span>
<a href="/">← дашборд</a></header>
<div id="log"></div>
<script>
let n = 0, paused = false;
function esc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;')}
function cls(line){
  if(line.indexOf(' -> 5') > -1 || line.indexOf(' -> 4') > -1) return 'err';
  if(line.startsWith('AI <<<')) return 'ai';
  if(line.startsWith('POST')) return 'post';
  if(line.startsWith('GET')) return 'get';
  return '';
}
async function tick(){
  if(paused) return;
  try{
    const r = await fetch('/api/events'); const d = await r.json();
    const evs = d.events || [];
    const start = Math.max(0, evs.length - (d.n - n));
    for(let i = start; i < evs.length; i++){
      const e = evs[i];
      const div = document.createElement('div');
      div.innerHTML = '<span class="t">'+e.t+'</span> <span class="'+cls(e.m)+'">'+esc(e.m)+'</span>';
      document.getElementById('log').appendChild(div);
    }
    n = d.n;
    window.scrollTo(0, document.body.scrollHeight);
    const el = document.getElementById('idle');
    if(d.idle_s == null){ el.textContent = '💤 обращений к ИИ не было'; }
    else if(d.idle_s < 60){ el.textContent = '⚡ активен (запрос ' + d.idle_s + ' с назад)'; }
    else { el.textContent = '💤 простой ' + Math.floor(d.idle_s/60) + ' мин'; }
  }catch(e){}
}
function togglePause(){ paused = !paused; document.getElementById('pause').textContent = paused ? '▶ продолжить' : '⏸ пауза'; }

let secOn = false, secTimer = null, lastSec = '';
async function secTick(){
  try{
    const r = await fetch('/api/events'); const d = await r.json();
    let state;
    if(d.ai_now > 0) state = '⚡ ОТВЕЧАЕТ (одновременно: ' + d.ai_now + ')';
    else if(d.idle_s == null) state = '💤 обращений ещё не было';
    else state = '💤 простой ' + Math.floor(d.idle_s/60) + ' мин ' + (d.idle_s%60) + ' с';
    const line = d.now + ' ' + state + ' · модель: ' + d.model
      + (d.ready ? ' (загружена)' : ' (не готова)')
      + (d.mem_mb ? ' · RAM свободно: ' + d.mem_mb + ' МБ' : '')
      + ' · запросов: ' + d.ai_total;
    if(line === lastSec) return;
    lastSec = line;
    const div = document.createElement('div');
    div.innerHTML = '<span class="t">' + d.now + '</span> <span class="get">' + esc(line.slice(9)) + '</span>';
    document.getElementById('log').appendChild(div);
    if(!paused) window.scrollTo(0, document.body.scrollHeight);
  }catch(e){}
}
function toggleSec(){
  secOn = !secOn;
  document.getElementById('sec').textContent = '⏱ каждую секунду: ' + (secOn ? 'ВКЛ' : 'ВЫКЛ');
  if(secOn){ secTick(); secTimer = setInterval(secTick, 1000); }
  else { clearInterval(secTimer); }
}
setInterval(tick, 2000);
tick();
</script></body></html>"""


@app.get("/log")
@app.get("/loge")
def log_page():
    return HTMLResponse(LOG_PAGE)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0",
                port=int(os.environ.get("PORT", "3000")))
