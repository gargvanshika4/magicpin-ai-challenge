"""HTTP transport for Vera. All decision logic lives in the `vera` package.

Run:  uvicorn app:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1
(single worker: state is in-process by design.)
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

# Load .env from the project root (next to this file) before anything reads os.environ.
# Real environment variables still win (override=False), so Docker/Render settings are respected.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env", override=False)
except ImportError:  # python-dotenv is optional in production images
    pass
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from vera.engine import Engine
from vera.store import SCOPES, ContextStore

MAX_CONTEXT_BYTES = 500 * 1024
START = time.time()

store = ContextStore(os.getenv("VERA_STATE_FILE") or None)
engine = Engine(store)
app = FastAPI(title="Vera — magicpin merchant assistant", version=os.getenv("BOT_VERSION", "1.0.0"))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


async def _json(request: Request):
    raw = await request.body()
    if len(raw) > MAX_CONTEXT_BYTES:
        return None, JSONResponse(status_code=413, content={"accepted": False, "reason": "payload_too_large",
                                                            "details": f"max {MAX_CONTEXT_BYTES} bytes"})
    try:
        data = json.loads(raw or b"{}")
    except (ValueError, UnicodeDecodeError):
        return None, JSONResponse(status_code=400, content={"accepted": False, "reason": "invalid_json"})
    if not isinstance(data, dict):
        return None, JSONResponse(status_code=400, content={"accepted": False, "reason": "invalid_body"})
    return data, None


@app.get("/v1/healthz")
async def healthz():
    return {"status": "ok", "uptime_seconds": int(time.time() - START), "contexts_loaded": store.counts()}


@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": os.getenv("TEAM_NAME", "Team Vera"),
        "team_members": [m.strip() for m in os.getenv("TEAM_MEMBERS", "Your Name").split(",") if m.strip()],
        "model": os.getenv("BOT_MODEL", "deterministic-rules-v1 (no LLM in the decision path)"),
        "approach": ("Deterministic decision engine: per-trigger-kind strategies choose ONE dominant signal from "
                     "trigger + merchant state + category, write grounded copy, then a validator rejects any "
                     "ungrounded number, taboo word, URL or multi-CTA before sending."),
        "contact_email": os.getenv("CONTACT_EMAIL", "team@example.com"),
        "version": os.getenv("BOT_VERSION", "1.0.0"),
        "submitted_at": os.getenv("SUBMITTED_AT", "2026-04-26T08:00:00Z"),
    }


@app.post("/v1/context")
async def push_context(request: Request):
    data, err = await _json(request)
    if err:
        return err
    scope, cid, version, payload = data.get("scope"), data.get("context_id"), data.get("version"), data.get("payload")
    if scope not in SCOPES:
        return JSONResponse(status_code=400, content={"accepted": False, "reason": "invalid_scope",
                                                      "details": f"scope must be one of {list(SCOPES)}"})
    if not isinstance(cid, str) or not cid.strip():
        return JSONResponse(status_code=400, content={"accepted": False, "reason": "invalid_context_id"})
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        return JSONResponse(status_code=400, content={"accepted": False, "reason": "invalid_version"})
    if not isinstance(payload, dict):
        return JSONResponse(status_code=400, content={"accepted": False, "reason": "invalid_payload"})
    accepted, current = store.put(scope, cid, version, payload)
    if not accepted:
        return JSONResponse(status_code=409, content={"accepted": False, "reason": "stale_version",
                                                      "current_version": current})
    return {"accepted": True, "ack_id": f"ack_{cid}_v{version}", "stored_at": _now_iso()}


@app.post("/v1/tick")
async def tick(request: Request):
    data, err = await _json(request)
    if err:
        return err
    avail = data.get("available_triggers") or []
    if not isinstance(avail, list):
        avail = []
    try:
        actions = engine.tick(data.get("now"), avail)
    except Exception as e:  # never fail the judge's tick
        return {"actions": [], "error": type(e).__name__}
    return {"actions": actions}


@app.post("/v1/reply")
async def reply(request: Request):
    data, err = await _json(request)
    if err:
        return err
    if not data.get("conversation_id"):
        return JSONResponse(status_code=400, content={"reason": "missing_conversation_id"})
    try:
        return engine.reply(data)
    except Exception as e:
        return {"action": "wait", "wait_seconds": 1800,
                "rationale": f"Internal error ({type(e).__name__}); backing off rather than sending a bad message."}


@app.post("/v1/teardown")
async def teardown():
    store.clear()
    engine.reset_state()
    return {"ok": True}


if __name__ == "__main__":
    # `python app.py` -> honours PORT from .env (a shell's ${PORT:-8080} never sees .env).
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8080")), workers=1)
