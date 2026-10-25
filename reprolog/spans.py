"""Helpers for span ids and for building a request's span tree."""
from __future__ import annotations

import uuid

MAX_SPANS = 500


def current_ids() -> tuple:
    """(trace_id, span_id) of the active OpenTelemetry span, as hex, or (None, None)."""
    try:
        from opentelemetry import trace

        ctx = trace.get_current_span().get_span_context()
        if ctx and ctx.span_id:
            return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")
    except Exception:
        pass
    return None, None


def new_id() -> str:
    return uuid.uuid4().hex[:16]


def classify(name: str, attrs: dict) -> str:
    system = str(attrs.get("db.system") or attrs.get("db.system.name") or "").lower()
    if system == "redis":
        return "redis"
    if system:
        return "db"
    if attrs.get("gen_ai.system") or attrs.get("gen_ai.operation.name"):
        return "llm"
    if attrs.get("http.method") or attrs.get("http.request.method"):
        return "http"
    return "internal"


def span_url(attrs: dict) -> str | None:
    return attrs.get("url.full") or attrs.get("http.url")


def build_tree(record: dict, cap) -> list:
    """Combine OTel spans, outbound calls and logs into one flat span list
    (each span has parent_id) rooted at the request span."""
    start, end = record["created_at"], record["created_at"] + (record["duration_ms"] or 0) / 1000
    root_id = cap.root_span_id or "root"
    req = record["request"]
    status = record["response"]["status"]
    root = {
        "id": root_id,
        "parent_id": None,
        "name": f"{req['method']} {req['path']}",
        "kind": "request",
        "start": start,
        "end": end,
        "status": "error" if record.get("error") or (status or 0) >= 500 else "ok",
        "attributes": {"http.status_code": status},
        "events": [],
    }
    spans = [root] + [dict(s) for s in cap.spans[:MAX_SPANS]]
    if len(spans) > 1:  # the request span covers everything that happened inside it
        root["start"] = min(root["start"], *(s["start"] for s in spans[1:]))
        root["end"] = max(root["end"], *(s["end"] for s in spans[1:]))
    ids = {s["id"] for s in spans}
    for s in spans[1:]:
        if s.get("parent_id") not in ids:
            s["parent_id"] = root_id

    # Attach each outbound call to the instrumentation span for the same URL
    # (Logfire instruments httpx/requests), or give it a span of its own.
    for i, call in enumerate(record.get("outbound") or []):
        match = None
        for s in spans:
            if s["kind"] in ("http", "llm") and s.get("url") == call["url"] and "outbound_index" not in s:
                match = s
                break
        if match is not None:
            match["outbound_index"] = i
            if call.get("kind") == "llm":
                match["kind"] = "llm"
            continue
        s_start = call.get("started_at") or start
        spans.append({
            "id": new_id(),
            "parent_id": call.get("span_id") if call.get("span_id") in ids else root_id,
            "name": f"{call['method']} {call['url'].split('?')[0]}",
            "kind": call.get("kind", "http"),
            "start": s_start,
            "end": s_start + (call.get("duration_ms") or 0) / 1000,
            "status": "error" if call.get("error") or (call["response"].get("status") or 0) >= 500 else "ok",
            "attributes": {"http.status_code": call["response"].get("status"), "url": call["url"]},
            "events": [],
            "outbound_index": i,
            "synthetic": True,
        })
        ids.add(spans[-1]["id"])

    for log in record.get("logs") or []:
        if log.get("span_id") not in ids:
            log["span_id"] = root_id
    if record.get("error") and record["error"].get("source") == "raised":
        root["events"].append({"name": "exception", "time": end, "attributes": {
            "exception.type": record["error"]["type"], "exception.message": record["error"]["message"]}})
    return spans
