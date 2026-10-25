"""Pure ASGI middleware that captures failing requests.

Add it as the innermost middleware (right after `app = FastAPI()`, before any
other add_middleware call) so it sees the request exactly as your routes do:
after path rewriting, payload decryption and other outer middleware."""
from __future__ import annotations

import asyncio
import time
import traceback
import uuid

from . import storage
from .config import CONFIG
from .context import Capture, current, registry
from .redact import encode_body, header_value, headers_to_list, redact_headers
from .spans import build_tree, current_ids


class ReprologMiddleware:
    def __init__(self, app, **_ignored):
        self.app = app
        registry.middleware = self

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not CONFIG.enabled or scope.get("path") in CONFIG.exclude_paths:
            return await self.app(scope, receive, send)
        try:
            prepared = await self._prepare(scope, receive)
        except Exception:
            return await self.app(scope, receive, send)
        return await self._run(scope, send, *prepared)

    async def _prepare(self, scope, receive):
        """Read the request body up front (when small) and hand an identical
        stream to the app, so nothing downstream ever finds it consumed."""
        headers = headers_to_list(scope.get("headers") or [])
        ctype = (header_value(headers, "content-type") or "").lower()
        length = header_value(headers, "content-length")
        too_big = length is not None and length.isdigit() and int(length) > CONFIG.max_body_bytes
        if too_big or ctype.startswith("multipart/"):
            note = "body not captured (multipart upload)" if not too_big else "body not captured (larger than max_body_bytes)"
            return receive, b"", note, headers

        chunks, pending = [], None
        while True:
            message = await receive()
            if message["type"] == "http.request":
                chunks.append(message.get("body", b""))
                if not message.get("more_body", False):
                    break
            else:  # client disconnected before sending the full body
                pending = message
                break
        body = b"".join(chunks)
        delivered = False

        async def replay_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                if pending is not None and not body:
                    return pending
                return {"type": "http.request", "body": body, "more_body": False}
            if pending is not None:
                return pending
            return await receive()

        return replay_receive, body, None, headers

    async def _run(self, scope, send, receive, body, body_note, headers):
        replaying = registry.replaying
        if replaying and registry.replay_capture is not None:
            cap = registry.replay_capture
        else:
            cap = Capture()
        token = current.set(cap)
        started = time.time()
        resp = {"status": None, "headers": [], "body": bytearray(), "truncated": False}

        async def wrapped_send(message):
            try:
                if message["type"] == "http.response.start":
                    resp["status"] = message["status"]
                    resp["headers"] = headers_to_list(message.get("headers") or [])
                elif message["type"] == "http.response.body":
                    chunk = message.get("body", b"")
                    room = CONFIG.max_body_bytes - len(resp["body"])
                    if room > 0:
                        resp["body"] += chunk[:room]
                    if len(chunk) > max(room, 0):
                        resp["truncated"] = True
            except Exception:
                pass
            await send(message)

        exc: BaseException | None = None
        cancelled = False
        trace_id, cap.root_span_id = current_ids()
        cap.trace_id = trace_id
        try:
            await self.app(scope, receive, wrapped_send)
        except asyncio.CancelledError:
            cancelled = True
            raise
        except BaseException as e:  # recorded below, then re-raised untouched
            exc = e
            raise
        finally:
            current.reset(token)
            if not cancelled:
                try:
                    record = self._build_record(scope, headers, body, body_note, resp, cap, exc, started, trace_id)
                    if replaying:
                        registry.last_replay_record = record
                    elif self._should_save(record, cap, exc):
                        await asyncio.to_thread(storage.save, record)
                except Exception:
                    pass  # capturing must never break a request

    @staticmethod
    def _should_save(record, cap, exc) -> bool:
        if CONFIG.capture_all or exc is not None or cap.logged_exceptions:
            return True
        status = record["response"]["status"]
        return status is not None and status >= CONFIG.capture_min_status

    @staticmethod
    def _build_record(scope, headers, body, body_note, resp, cap, exc, started, trace_id) -> dict:
        ctype = header_value(headers, "content-type")
        body_rec = encode_body(body, ctype)
        if body_note:
            body_rec["note"] = body_note
        status = resp["status"]
        if status is None and exc is not None:
            status = 500  # the error handler outside this middleware sends the response

        error = None
        if exc is not None:
            error = {
                "type": type(exc).__name__,
                "message": str(exc),
                "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
                "source": "raised",
            }
        elif cap.logged_exceptions:
            first = cap.logged_exceptions[0]
            error = {**first, "source": "logged"}

        raw_path = scope.get("raw_path")
        record = {
            "id": uuid.uuid4().hex[:12],
            "created_at": started,
            "duration_ms": round((time.time() - started) * 1000, 1),
            "trace_id": trace_id,
            "request": {
                "method": scope.get("method"),
                "scheme": scope.get("scheme", "http"),
                "http_version": scope.get("http_version", "1.1"),
                "path": scope.get("path"),
                "raw_path": raw_path.decode("latin-1") if isinstance(raw_path, bytes) else raw_path,
                "root_path": scope.get("root_path", ""),
                "query_string": (scope.get("query_string") or b"").decode("latin-1"),
                "headers": redact_headers(headers),
                "body": body_rec,
                "client": list(scope["client"]) if scope.get("client") else None,
                "server": list(scope["server"]) if scope.get("server") else None,
            },
            "response": {
                "status": status,
                "headers": redact_headers(resp["headers"]),
                "body": encode_body(bytes(resp["body"]), header_value(resp["headers"], "content-type")),
                "truncated": resp["truncated"],
            },
            "error": error,
            "logs": cap.logs,
            "outbound": [{k: v for k, v in o.items() if k != "_used"} for o in cap.outbound],
        }
        record["spans"] = build_tree(record, cap)
        if cap.mode == "replay":
            record["replay"] = {"unmatched_outbound": cap.unmatched, "live_outbound": cap.live_outbound}
        return record
