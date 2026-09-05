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

FILES = {
    "skills": "skills.json",
    "rules": "learned_rules.json",
    "cases": "learned_cases.md",
}

import time as _time
STATS = {"puts": 0, "gets": 0, "start": _time.time()}


def _read_json(fname, default):
    try:
        with open(os.path.join(DATA, fname), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


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
    if name not in FILES:
        raise HTTPException(status_code=404, detail="unknown file")
    p = _path(name)
    if not os.path.exists(p):
        if name == "cases":
            return PlainTextResponse("")
        return JSONResponse([])
    STATS["gets"] += 1
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
    with open(_path(name), "wb") as f:
        f.write(body)
    return {"ok": True, "bytes": len(body)}


def _relay(path: str, body: bytes):
    """Проксирование запроса к Groq/OpenAI серверным ключом."""
    if not AI_API_KEY:
        raise HTTPException(status_code=503, detail="AI_API_KEY not set")
    req = urllib.request.Request(
        AI_UPSTREAM.rstrip("/") + "/" + path,
        data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + AI_API_KEY},
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
    return _relay("chat/completions", await request.body())


@app.post("/v1/embeddings")
async def relay_emb(request: Request, x_token: Optional[str] = Header(None)):
    _check_token(x_token)
    return _relay("embeddings", await request.body())


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
a{color:#58a6ff}
#refresh{float:right}
</style></head><body>
<header><span class="dot"></span><h1>TerminalAI — сервер мастерской</h1>
<span class="small" id="uptime"></span></header>
<div class="wrap">
<div class="cards">
<div class="card"><div class="v" id="c-skills">—</div><div class="l">навыков в базе</div></div>
<div class="card"><div class="v" id="c-cases">—</div><div class="l">сохранённых случаев</div></div>
<div class="card"><div class="v" id="c-danger">—</div><div class="l">правил блокировки</div></div>
<div class="card"><div class="v" id="c-puts">—</div><div class="l">загрузок базы (PUT)</div></div>
<div class="card"><div class="v" id="c-gets">—</div><div class="l">скачиваний (GET)</div></div>
</div>
<h3 style="color:#9fd0ff">Навыки мастерской</h3>
<table><thead><tr><th>Ошибка (триггер)</th><th>Решение</th><th>Использований</th><th></th></tr></thead>
<tbody id="skills"></tbody></table>
<h3 style="color:#9fd0ff">Правила безопасности</h3>
<table><thead><tr><th style="width:110px">Уровень</th><th>Паттерн</th></tr></thead>
<tbody id="rules"></tbody></table>
<p class="small">Обновление каждые 15 секунд · API: <a href="/docs">/docs</a> · синхронизация: <code>/api/sync/*</code></p>
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
      const trg = (s.trigger||'').split('\n')[0];
      const sol = (s.solution||[]).join('; ');
      tb.innerHTML += '<tr><td>'+escapeHtml(trg)+'</td><td class="small">'+escapeHtml(sol)+'</td><td>'+(s.hits||0)+'</td><td>'+(s.dangerous?'<span class="badge b-red">опасно</span>':'')+'</td></tr>';
    });
    if(!tb.innerHTML) tb.innerHTML = '<tr><td colspan="4" class="small">база пуста — отправьте навыки из программы кнопкой «На сервер»</td></tr>';
    const rb = document.getElementById('rules'); rb.innerHTML = '';
    (d.rules.dangerous||[]).forEach(p=>{ rb.innerHTML += '<tr><td><span class="badge b-red">блок</span></td><td class="small">'+escapeHtml(p)+'</td></tr>'; });
    (d.rules.risky||[]).forEach(p=>{ rb.innerHTML += '<tr><td><span class="badge b-gray">риск</span></td><td class="small">'+escapeHtml(p)+'</td></tr>'; });
  }catch(e){}
}
function escapeHtml(t){ return String(t).replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
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
    }


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return HTMLResponse(DASH_HTML)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0",
                port=int(os.environ.get("PORT", "3000")))
