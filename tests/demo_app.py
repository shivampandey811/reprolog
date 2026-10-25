"""Demo app that mimics chatmro_backend's layout: an outer middleware that
decrypts payloads, a catch-all exception handler, logging, and an outbound
"LLM" call made with requests."""
import base64
import logging
import os

import requests
from requests.adapters import BaseAdapter
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import reprolog

log = logging.getLogger("demo")
logging.basicConfig(level=logging.INFO)


class FakeLLM(BaseAdapter):
    """Pretend LLM API. Returns an empty choices list when DEMO_LLM=bad."""
    def send(self, request, **kwargs):
        resp = requests.Response()
        resp.status_code = 200
        resp.headers["Content-Type"] = "application/json"
        if os.getenv("DEMO_LLM", "bad") == "bad":
            resp._content = b'{"model":"fake-1","choices":[],"usage":{"prompt_tokens":12,"completion_tokens":0}}'
        else:
            resp._content = b'{"model":"fake-1","choices":[{"text":"hi"}],"usage":{"prompt_tokens":12,"completion_tokens":3}}'
        resp.url = request.url
        resp.request = request
        return resp

    def close(self):
        pass


session = requests.Session()
session.mount("https://llm.example", FakeLLM())


async def order(request: Request):
    data = await request.json()
    log.info("creating order for %s", data.get("sku"))
    llm = session.post("https://llm.example/v1/chat", json={"prompt": data["sku"]},
                       headers={"Authorization": "Bearer sk-secret"}).json()
    answer = llm["choices"][0]["text"]  # IndexError when the LLM returns no choices
    return JSONResponse({"ok": True, "answer": answer})


async def ok(request: Request):
    return JSONResponse({"ok": True})


async def handled_500(request: Request):
    try:
        1 / 0
    except ZeroDivisionError:
        log.exception("division failed")
        return JSONResponse({"detail": "oops"}, status_code=500)


async def on_error(request, exc):
    return JSONResponse({"detail": "Internal server error"}, status_code=500)


class Decrypt:
    """Like PayloadEncryptionMiddleware: client sends base64, routes see JSON."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        raw = b""
        while True:
            m = await receive()
            raw += m.get("body", b"")
            if not m.get("more_body"):
                break
        plain = base64.b64decode(raw) if raw else b""
        done = False

        async def new_receive():
            nonlocal done
            if not done:
                done = True
                return {"type": "http.request", "body": plain, "more_body": False}
            return await receive()
        await self.app(scope, new_receive, send)


app = Starlette(routes=[Route("/api/order", order, methods=["POST"]), Route("/ok", ok),
                        Route("/handled", handled_500)],
                exception_handlers={Exception: on_error})
reprolog.init(app, otel=False)      # innermost
app.add_middleware(Decrypt)          # outer


# --- fake OpenTelemetry spans so the viewer has a span tree to draw in tests ---
import time as _time

from reprolog.otel import ReprologSpanProcessor


class _Ctx:
    def __init__(self, sid):
        self.span_id, self.trace_id = sid, 0xABC


class _Ev:
    def __init__(self, name, ts, attrs):
        self.name, self.timestamp, self.attributes = name, ts, attrs


class FakeSpan:
    def __init__(self, sid, parent, name, start, end, attrs, events=()):
        self.context, self.parent, self.name = _Ctx(sid), (_Ctx(parent) if parent else None), name
        self.start_time, self.end_time, self.attributes, self.events = int(start * 1e9), int(end * 1e9), attrs, list(events)
        self.status = None


async def checkout(request: Request):
    proc = ReprologSpanProcessor()
    t = _time.time()
    proc.on_end(FakeSpan(0x10, None, "load cart", t, t + 0.004, {"cart.items": 3}))
    proc.on_end(FakeSpan(0x11, 0x10, "SELECT carts", t + 0.001, t + 0.003, {"db.system": "postgresql", "db.statement": "SELECT * FROM carts WHERE id = $1"}))
    proc.on_end(FakeSpan(0x12, 0x10, "logfire log", t + 0.002, t + 0.002, {"logfire.span_type": "log", "logfire.msg": "cart loaded", "logfire.level_num": 9}))
    proc.on_end(FakeSpan(0x13, None, "GET", t + 0.005, t + 0.006, {"db.system": "redis"}))
    log.warning("price missing for one item")
    data = session.post("https://llm.example/v1/chat", json={"prompt": "price"}).json()
    return JSONResponse({"total": data["choices"][0]["text"]})


app.router.routes.append(Route("/api/checkout", checkout, methods=["POST"]))
