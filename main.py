# -*- coding: utf-8 -*-
"""Stable entrypoint for the TerminalAI server.

The legacy implementation is kept in server_legacy.py in the repository.
This small loader makes the dashboard key usable by the relay and keeps
background Groq learning opt-in so it cannot consume the account quota.
"""
import hashlib
import hmac
import os
import urllib.request

# The old implementation enables this by default; make learning opt-in here.
os.environ["LEARN_GROQ"] = "0"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LEGACY_PATH = os.path.join(BASE_DIR, "server_legacy.py")
LEGACY_URL = "https://raw.githubusercontent.com/Alexkkkkk/pytty-ai/main/server_legacy.py"

if not os.path.exists(LEGACY_PATH):
    tmp = LEGACY_PATH + ".part"
    with urllib.request.urlopen(LEGACY_URL, timeout=60) as response, open(tmp, "wb") as out:
        out.write(response.read())
    os.replace(tmp, LEGACY_PATH)

import server_legacy as _impl

# Keep the self-update endpoint targeting this loader, not the downloaded copy.
_impl.__file__ = __file__


def _relay_target():
    stored_key = _impl._load_keys().get("groq_api_key", "")
    key = _impl.AI_API_KEY or _impl._read_text("ai_key.txt").strip() or str(stored_key).strip()
    if key:
        return _impl.AI_UPSTREAM, key
    if _impl.LLAMAFILE_URL and _impl._local_ready["ready"]:
        return "http://127.0.0.1:%d/v1" % _impl.LOCAL_PORT, ""
    if _impl.LOCAL_UPSTREAM and (not _impl.OLLAMA_MODEL or _impl._local_ready["ready"]):
        return _impl.LOCAL_UPSTREAM, ""
    return None, None


def _check_token(token):
    expected = _impl._effective_token()
    stored_groq = str(_impl._load_keys().get("groq_api_key", "")).strip()
    if token and (hmac.compare_digest(token, expected) or
                  (stored_groq and hmac.compare_digest(token, stored_groq))):
        return
    raise _impl.HTTPException(status_code=403, detail="bad token")


# Patch the functions used by the already-registered FastAPI route handlers.
_impl._relay_target = _relay_target
_impl._check_token = _check_token
app = _impl.app
