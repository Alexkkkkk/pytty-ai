# -*- coding: utf-8 -*-
"""Тесты FastAPI-сервера синхронизации main.py (TestClient, без сети)."""
import os, sys, json, importlib
os.environ["SYNC_TOKEN"] = "test-token-123"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

main = importlib.import_module("main")
client = TestClient(main.app)

HEADERS = {"X-Token": "test-token-123"}


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_diagnostics_require_token():
    assert client.get("/api/stats").status_code == 403
    assert client.get("/api/events").status_code == 403
    assert client.get("/api/keys").status_code == 403


def test_events_return_monotonic_ids():
    main._ev("test event")
    r = client.get("/api/events", headers=HEADERS)
    assert r.status_code == 200
    data = r.json()
    assert data["events"]
    ids = [event["id"] for event in data["events"]]
    assert ids == sorted(ids)
    assert data["seq"] >= ids[-1]
    assert data["first_seq"] == ids[0]


def test_bearer_auth_is_accepted_by_openai_routes():
    headers = {"Authorization": "Bearer test-token-123"}
    r = client.post("/v1/chat/completions", headers=headers, json={
        "model": "test", "messages": [{"role": "user", "content": "ping"}]
    })
    assert r.status_code == 503
    r = client.get("/v1/models", headers=headers)
    assert r.status_code == 503


def test_get_sync_public():
    r = client.get("/api/sync/skills")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_put_sync_requires_token():
    r = client.put("/api/sync/skills", data='[{"trigger": "t"}]')
    assert r.status_code == 403


def test_put_and_get_roundtrip():
    payload = [{"trigger": "bad crc", "solution": ["reset"]}]
    r = client.put("/api/sync/skills", data=json.dumps(payload), headers=HEADERS)
    assert r.status_code == 200
    assert r.json().get("ok") is True
    r2 = client.get("/api/sync/skills")
    assert r2.status_code == 200
    assert any(s.get("trigger") == "bad crc" for s in r2.json())


def test_put_bad_json_rejected():
    r = client.put("/api/sync/skills", data="not json{{", headers=HEADERS)
    assert r.status_code == 400


def test_key_and_token_endpoints_reject_bad_json():
    assert client.put("/api/keys", data="not json{{",
                      headers=HEADERS).status_code == 400
    assert client.post("/api/token", data="not json{{",
                       headers=HEADERS).status_code == 400


def test_unknown_file_404():
    assert client.get("/api/sync/nope").status_code == 404
    assert client.put("/api/sync/nope", data="[]", headers=HEADERS).status_code == 404


def test_aikey_not_readable():
    assert client.get("/api/sync/aikey").status_code == 404


def test_cases_plain_text():
    r = client.put("/api/sync/cases", data="## Кейс\nтекст", headers=HEADERS)
    assert r.status_code == 200
    r2 = client.get("/api/sync/cases")
    assert r2.status_code == 200
    assert "Кейс" in r2.text
