"""Record outgoing HTTP calls (httpx and requests) made while handling a request,
and serve them back from the recording during a replay.

OpenAI, Anthropic and Google's google-genai SDKs all send through httpx, so
their calls are covered without any SDK-specific code."""
from __future__ import annotations

import base64
import time
from urllib.parse import urlsplit

from .config import CONFIG
from .context import current
from .redact import encode_body, headers_to_list, redact_headers
from .spans import current_ids


class ReplayMissError(RuntimeError):
    """Raised in strict replay when the app makes a call that was not recorded."""


def _is_llm(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) or host.endswith(h) for h in CONFIG.llm_hosts)


def _llm_usage(body_rec: dict) -> dict | None:
    """Pull token counts out of an OpenAI / Anthropic / Gemini JSON response."""
    if body_rec.get("encoding") != "text":
        return None
    import json

    try:
        data = json.loads(body_rec["data"])
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    usage = data.get("usage") or data.get("usageMetadata") or data.get("usage_metadata")
    if not isinstance(usage, dict):
        return None
    out = {"model": data.get("model") or data.get("modelVersion")}
    for key in ("prompt_tokens", "completion_tokens", "input_tokens", "output_tokens", "total_tokens",
                "promptTokenCount", "candidatesTokenCount", "thoughtsTokenCount", "totalTokenCount"):
        if key in usage:
            out[key] = usage[key]
    return out


def _record(method, url, req_headers, req_body, req_ctype, status, resp_headers, resp_body, resp_ctype,
            started, error=None, streamed=False):
    cap = current.get()
    if cap is None or len(cap.outbound) >= CONFIG.max_outbound_per_request:
        return
    body_rec = encode_body(resp_body, resp_ctype) if resp_body is not None else {
        "encoding": "empty", "data": "", "redacted": False, "note": "streamed body not captured" if streamed else None}
    entry = {
        "method": method,
        "url": url,
        "request": {"headers": redact_headers(req_headers), "body": encode_body(req_body or b"", req_ctype)},
        "response": {"status": status, "headers": resp_headers, "body": body_rec},
        "duration_ms": round((time.time() - started) * 1000, 1),
        "started_at": started,
        "span_id": current_ids()[1],
        "error": error,
        "kind": "llm" if _is_llm(url) else "http",
    }
    if entry["kind"] == "llm" and status:
        entry["usage"] = _llm_usage(body_rec)
    cap.outbound.append(entry)


def _mock_or_none(method: str, url: str):
    """During replay, return the recorded call to serve, or None to go live."""
    cap = current.get()
    if cap is None or cap.mode != "replay" or cap.live_outbound:
        return None
    rec = cap.next_mock(method, url)
    if rec is None:
        cap.unmatched.append({"method": method, "url": url})
        if cap.strict:
            raise ReplayMissError(f"Replay: no recorded response for {method} {url}")
    return rec


def _strip_encoding_headers(headers: list) -> list:
    skip = {"content-encoding", "content-length", "transfer-encoding"}
    return [[n, v] for n, v in headers if n.lower() not in skip]


def _recorded_bytes(rec: dict) -> bytes:
    body = rec["response"]["body"]
    if body.get("encoding") == "base64":
        return base64.b64decode(body["data"])
    return (body.get("data") or "").encode("utf-8")


# --------------------------------------------------------------------- httpx
_httpx_patched = False


def patch_httpx() -> bool:
    global _httpx_patched
    if _httpx_patched:
        return True
    try:
        import httpx
    except ImportError:
        return False

    orig_sync = httpx.Client.send
    orig_async = httpx.AsyncClient.send

    def _build_response(rec, request):
        return httpx.Response(
            status_code=rec["response"]["status"] or 599,
            headers=_strip_encoding_headers(rec["response"].get("headers") or []),
            content=_recorded_bytes(rec),
            request=request,
        )

    def _req_parts(request):
        try:
            body = request.content
        except Exception:  # streaming request body that was not read
            body = None
        hdrs = headers_to_list(request.headers.multi_items())
        return hdrs, body, request.headers.get("content-type")

    def _resp_parts(response, stream: bool):
        body = None
        if not stream:
            body = response.content
        return headers_to_list(response.headers.multi_items()), body, response.headers.get("content-type")

    def send(self, request, *args, **kwargs):
        if current.get() is None:
            return orig_sync(self, request, *args, **kwargs)
        method, url = request.method, str(request.url)
        rec = _mock_or_none(method, url)
        if rec is not None:
            return _build_response(rec, request)
        started = time.time()
        stream = bool(kwargs.get("stream", False))
        hdrs, body, ctype = _req_parts(request)
        try:
            response = orig_sync(self, request, *args, **kwargs)
        except Exception as exc:
            _record(method, url, hdrs, body, ctype, None, [], None, None, started, error=repr(exc))
            raise
        if stream and CONFIG.capture_streaming_bodies:
            response.read()
            stream = False
        rh, rb, rct = _resp_parts(response, stream)
        _record(method, url, hdrs, body, ctype, response.status_code, rh, rb, rct, started, streamed=stream)
        return response

    async def asend(self, request, *args, **kwargs):
        if current.get() is None:
            return await orig_async(self, request, *args, **kwargs)
        method, url = request.method, str(request.url)
        rec = _mock_or_none(method, url)
        if rec is not None:
            return _build_response(rec, request)
        started = time.time()
        stream = bool(kwargs.get("stream", False))
        hdrs, body, ctype = _req_parts(request)
        try:
            response = await orig_async(self, request, *args, **kwargs)
        except Exception as exc:
            _record(method, url, hdrs, body, ctype, None, [], None, None, started, error=repr(exc))
            raise
        if stream and CONFIG.capture_streaming_bodies:
            await response.aread()
            stream = False
        rh, rb, rct = _resp_parts(response, stream)
        _record(method, url, hdrs, body, ctype, response.status_code, rh, rb, rct, started, streamed=stream)
        return response

    httpx.Client.send = send
    httpx.AsyncClient.send = asend
    _httpx_patched = True
    return True


# ------------------------------------------------------------------ requests
_requests_patched = False


def patch_requests() -> bool:
    global _requests_patched
    if _requests_patched:
        return True
    try:
        import requests
        from requests.structures import CaseInsensitiveDict
    except ImportError:
        return False

    orig = requests.Session.send

    def _build_response(rec, prepared):
        resp = requests.Response()
        resp.status_code = rec["response"]["status"] or 599
        resp.headers = CaseInsensitiveDict(dict(_strip_encoding_headers(rec["response"].get("headers") or [])))
        resp._content = _recorded_bytes(rec)
        resp.url = prepared.url
        resp.request = prepared
        resp.encoding = requests.utils.get_encoding_from_headers(resp.headers) or "utf-8"
        resp.reason = "Replayed"
        return resp

    def send(self, request, **kwargs):
        if current.get() is None:
            return orig(self, request, **kwargs)
        method, url = request.method, request.url
        rec = _mock_or_none(method, url)
        if rec is not None:
            return _build_response(rec, request)
        started = time.time()
        body = request.body
        if isinstance(body, str):
            body = body.encode("utf-8")
        elif body is not None and not isinstance(body, bytes):
            body = None  # file-like / generator body
        hdrs = headers_to_list(request.headers)
        ctype = request.headers.get("Content-Type")
        try:
            response = orig(self, request, **kwargs)
        except Exception as exc:
            _record(method, url, hdrs, body, ctype, None, [], None, None, started, error=repr(exc))
            raise
        stream = bool(kwargs.get("stream", False)) and not CONFIG.capture_streaming_bodies
        rb = None if stream else response.content
        _record(method, url, hdrs, body, ctype, response.status_code, headers_to_list(response.headers), rb,
                response.headers.get("Content-Type"), started, streamed=stream)
        return response

    requests.Session.send = send
    _requests_patched = True
    return True


def patch_all() -> dict:
    return {"httpx": patch_httpx(), "requests": patch_requests()}
