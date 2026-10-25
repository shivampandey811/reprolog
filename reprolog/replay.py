"""Replay a captured request against your app.

In-process (default): imports your app, sends the captured request straight to
the layer where reprolog sits (so outer middleware such as payload encryption,
origin checks and rate limits are skipped), and serves outbound HTTP/LLM calls
from the recording. Your database, Redis and other non-HTTP services are live.

Live (--target URL): sends the captured request to a running server over HTTP.
Outbound calls happen for real, and outer middleware runs."""
from __future__ import annotations

import asyncio
import copy
import importlib
import json
import os
import sys
import time
import urllib.error
import urllib.request

from .context import Capture, registry
from .redact import decode_body

SKIP_REPLAY_HEADERS = {"content-length", "host", "transfer-encoding", "connection"}


def load_app(path: str):
    """Import 'module:attr' (e.g. 'main:app') from the current directory."""
    if ":" not in path:
        path = f"{path}:app"
    module_name, attr = path.split(":", 1)
    cwd = os.getcwd()
    if cwd not in sys.path:
        sys.path.insert(0, cwd)
    module = importlib.import_module(module_name)
    app = module
    for part in attr.split("."):
        app = getattr(app, part)
    return app


def _apply_header_overrides(headers: list, overrides: list[str] | None) -> list:
    headers = [h for h in headers if h[0].lower() not in SKIP_REPLAY_HEADERS]
    for item in overrides or []:
        if ":" not in item:
            raise ValueError(f"Header override must look like 'Name: value', got {item!r}")
        name, value = item.split(":", 1)
        headers = [h for h in headers if h[0].lower() != name.strip().lower()]
        headers.append([name.strip(), value.strip()])
    return headers


def redacted_headers_in(record: dict) -> list:
    return [n for n, v in record["request"]["headers"] if v == "[REDACTED]"]


def _inner_target(app, full_stack: bool):
    """The ASGI callable to send the replayed request to."""
    if full_stack:
        return app
    # Starlette builds its middleware stack lazily on the first request.
    if getattr(app, "middleware_stack", "missing") is None and hasattr(app, "build_middleware_stack"):
        app.middleware_stack = app.build_middleware_stack()
    if registry.middleware is None:
        raise RuntimeError(
            "reprolog middleware is not installed in this app. Add `reprolog.init(app)` "
            "right after `app = FastAPI()`, or pass --full-stack."
        )
    return registry.middleware


async def _lifespan(app, phase_timeout=30):
    """Run the app's lifespan startup (if it has one). Returns a shutdown coroutine."""
    queue_in: asyncio.Queue = asyncio.Queue()
    queue_out: asyncio.Queue = asyncio.Queue()

    async def receive():
        return await queue_in.get()

    async def send(message):
        await queue_out.put(message)

    task = asyncio.create_task(app({"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}}, receive, send))
    await queue_in.put({"type": "lifespan.startup"})
    done, _ = await asyncio.wait({task, asyncio.create_task(queue_out.get())}, timeout=phase_timeout,
                                 return_when=asyncio.FIRST_COMPLETED)

    async def shutdown():
        if task.done():
            return
        await queue_in.put({"type": "lifespan.shutdown"})
        try:
            await asyncio.wait_for(task, timeout=phase_timeout)
        except Exception:
            task.cancel()

    return shutdown


async def replay_record(record: dict, app, *, header_overrides=None, live_outbound=False, strict=False,
                        full_stack=False, run_lifespan=True) -> dict:
    """Replay in-process and return a result dict with the new record and a verdict."""
    target = _inner_target(app, full_stack)
    shutdown = None
    if run_lifespan:
        try:
            shutdown = await _lifespan(app)
        except Exception:
            shutdown = None

    req = record["request"]
    headers = _apply_header_overrides(req["headers"], header_overrides)
    body = decode_body(req.get("body"))
    if body:
        headers.append(["content-length", str(len(body))])
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": req.get("http_version", "1.1"),
        "method": req["method"],
        "scheme": req.get("scheme", "http"),
        "path": req["path"],
        "raw_path": (req.get("raw_path") or req["path"]).encode("latin-1"),
        "root_path": req.get("root_path", ""),
        "query_string": (req.get("query_string") or "").encode("latin-1"),
        "headers": [(n.lower().encode("latin-1"), v.encode("latin-1")) for n, v in headers],
        "client": tuple(req["client"]) if req.get("client") else ("127.0.0.1", 0),
        "server": tuple(req["server"]) if req.get("server") else ("127.0.0.1", 8000),
        "state": {},
        "extensions": {},
    }
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    status_holder = {"status": None}

    async def send(message):
        if message["type"] == "http.response.start":
            status_holder["status"] = message["status"]

    cap = Capture(mode="replay", mocks=copy.deepcopy(record.get("outbound") or []),
                  live_outbound=live_outbound, strict=strict)
    registry.replaying = True
    registry.replay_capture = cap
    registry.last_replay_record = None
    raised = None
    started = time.time()
    try:
        await target(scope, receive, send)
    except Exception as exc:  # the app's own error handler lives outside this layer
        raised = exc
    finally:
        registry.replaying = False
        registry.replay_capture = None
        if shutdown is not None:
            try:
                await shutdown()
            except Exception:
                pass

    new = registry.last_replay_record or {
        "request": req,
        "response": {"status": status_holder["status"] or (500 if raised else None)},
        "error": {"type": type(raised).__name__, "message": str(raised)} if raised else None,
        "logs": cap.logs,
        "outbound": cap.outbound,
    }
    new["duration_ms"] = round((time.time() - started) * 1000, 1)
    new.setdefault("replay", {})
    new["replay"]["unmatched_outbound"] = cap.unmatched
    new["replay"]["mocked_outbound"] = sum(1 for m in cap.mocks if m.get("_used"))
    new["replay"]["recorded_outbound"] = len(cap.mocks)
    return {"original": record, "replayed": new, "verdict": verdict(record, new)}


def verdict(original: dict, replayed: dict) -> dict:
    o_err, r_err = original.get("error") or {}, replayed.get("error") or {}
    o_status, r_status = (original.get("response") or {}).get("status"), (replayed.get("response") or {}).get("status")
    same_error = bool(o_err) and o_err.get("type") == r_err.get("type")
    reproduced = same_error or (not o_err and o_status == r_status and (o_status or 0) >= 400)
    if reproduced:
        summary = "Reproduced: the same error happened again."
    elif r_err:
        summary = f"Different error this time: {r_err.get('type')}."
    elif (r_status or 0) >= 500:
        summary = f"Failed again with status {r_status}, but without the original error."
    else:
        summary = f"Not reproduced: the request succeeded with status {r_status}."
    return {"reproduced": reproduced, "summary": summary, "original_status": o_status, "replayed_status": r_status}


def replay_live(record: dict, base_url: str, header_overrides=None, timeout=60) -> dict:
    req = record["request"]
    headers = _apply_header_overrides(req["headers"], header_overrides)
    url = base_url.rstrip("/") + (req.get("root_path") or "") + req["path"]
    if req.get("query_string"):
        url += "?" + req["query_string"]
    body = decode_body(req.get("body")) or None
    http_req = urllib.request.Request(url, data=body, method=req["method"])
    for name, value in headers:
        http_req.add_header(name, value)
    started = time.time()
    try:
        with urllib.request.urlopen(http_req, timeout=timeout) as resp:
            status, text = resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        status, text = err.code, err.read().decode("utf-8", "replace")
    return {"status": status, "body": text[:4000], "duration_ms": round((time.time() - started) * 1000, 1), "url": url}


def run(record: dict, app_path: str, **kwargs) -> dict:
    app = load_app(app_path)
    return asyncio.run(replay_record(record, app, **kwargs))


def dumps(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False, default=str)
