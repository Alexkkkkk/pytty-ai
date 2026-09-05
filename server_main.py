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
from fastapi.responses import JSONResponse, PlainTextResponse

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


def _check_token(x_token: Optional[str]):
    if SYNC_TOKEN and x_token != SYNC_TOKEN:
        raise HTTPException(status_code=403, detail="bad token")


def _path(name: str) -> str:
    return os.path.join(DATA, FILES[name])


@app.get("/")
def root():
    stats = {}
    for key, fname in FILES.items():
        p = os.path.join(DATA, fname)
        if os.path.exists(p):
            stats[key] = os.path.getsize(p)
        else:
            stats[key] = None
    return {
        "service": "TerminalAI Sync Server",
        "version": "1.0.0",
        "files": stats,
        "ai_relay": bool(AI_API_KEY),
        "endpoints": ["/health", "/api/sync/{skills,rules,cases}",
                      "/v1/chat/completions", "/v1/embeddings"],
    }


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
