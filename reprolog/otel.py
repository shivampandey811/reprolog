"""Optional OpenTelemetry / Logfire integration.

Every span that ends while a request is being handled is copied into that
request's capture (database, Redis, HTTP client, your own logfire.span blocks),
so the viewer can draw the request as a span tree. Logfire log records
(`logfire.info`, `logfire.exception`, ...) become log lines attached to the
span they were written in."""
from __future__ import annotations

from .config import CONFIG
from .context import current
from .spans import MAX_SPANS, classify, span_url

_LEVELS = {1: "TRACE", 5: "DEBUG", 9: "INFO", 10: "NOTICE", 13: "WARNING", 17: "ERROR", 21: "FATAL"}

try:  # subclass the SDK class when available so isinstance checks pass
    from opentelemetry.sdk.trace import SpanProcessor as _Base
except Exception:  # pragma: no cover
    _Base = object


def _hex(n, width):
    return format(n, f"0{width}x") if n else None


def _clean_attrs(attrs: dict) -> dict:
    out = {}
    for k, v in attrs.items():
        if k.startswith("logfire.") and k not in ("logfire.msg",):
            continue
        out[k] = v if isinstance(v, (int, float, bool)) or v is None else str(v)[:2000]
    return out


class ReprologSpanProcessor(_Base):
    def on_start(self, span, parent_context=None):
        return None

    def on_end(self, span):
        cap = current.get()
        if cap is None:
            return
        try:
            attrs = dict(span.attributes or {})
            ctx = span.get_span_context() if hasattr(span, "get_span_context") else span.context
            span_id = _hex(ctx.span_id, 16)
            parent = getattr(span, "parent", None)
            parent_id = _hex(parent.span_id, 16) if parent is not None else None
            events = []
            for event in getattr(span, "events", ()) or ():
                ea = dict(event.attributes or {})
                events.append({"name": event.name, "time": (event.timestamp or 0) / 1e9,
                               "attributes": _clean_attrs(ea)})
                if event.name == "exception":
                    cap.logged_exceptions.append({
                        "type": str(ea.get("exception.type", "Exception")).split(".")[-1],
                        "message": str(ea.get("exception.message", "")),
                        "traceback": str(ea.get("exception.stacktrace", "")),
                    })

            if attrs.get("logfire.span_type") == "log":
                if len(cap.logs) < CONFIG.max_logs_per_request:
                    tb = next((e["attributes"].get("exception.stacktrace") for e in events
                               if e["name"] == "exception"), None)
                    extra = {k: v for k, v in _clean_attrs(attrs).items() if not k.startswith("code.") and k != "logfire.msg"}
                    cap.logs.append({
                        "time": (span.start_time or 0) / 1e9,
                        "level": _LEVELS.get(attrs.get("logfire.level_num"), "INFO"),
                        "logger": "logfire",
                        "message": attrs.get("logfire.msg") or span.name,
                        "where": f"{attrs.get('code.filepath', '')}:{attrs.get('code.lineno', '')}".strip(":"),
                        "fields": extra or None,
                        "span_id": parent_id,
                        "traceback": tb,
                    })
                return

            if len(cap.spans) >= MAX_SPANS:
                return
            status = getattr(getattr(span, "status", None), "status_code", None)
            is_error = (getattr(status, "name", "") == "ERROR") or any(e["name"] == "exception" for e in events)
            cap.spans.append({
                "id": span_id,
                "parent_id": parent_id,
                "name": attrs.get("logfire.msg") or span.name,
                "kind": classify(span.name, attrs),
                "start": (span.start_time or 0) / 1e9,
                "end": (span.end_time or span.start_time or 0) / 1e9,
                "status": "error" if is_error else "ok",
                "attributes": _clean_attrs(attrs),
                "events": events,
                "url": span_url(attrs),
            })
        except Exception:
            pass

    def shutdown(self):
        return None

    def force_flush(self, timeout_millis: int = 30000):
        return True


_PROCESSOR = ReprologSpanProcessor()


def _attach(candidate) -> bool:
    add = getattr(candidate, "add_span_processor", None)
    if not callable(add):
        return False
    try:
        add(_PROCESSOR)
        return True
    except Exception:
        return False


def _follow_swaps(proxy) -> bool:
    """Logfire's ProxyTracerProvider swaps in a fresh SDK provider on every
    logfire.configure() call, dropping processors added to the old one. Apps
    often configure again after reprolog.init(), so re-attach on each swap."""
    set_provider = getattr(proxy, "set_provider", None)
    if not callable(set_provider) or getattr(set_provider, "_reprolog", False):
        return callable(set_provider)

    def wrapped(provider, *args, **kwargs):
        result = set_provider(provider, *args, **kwargs)
        _attach(provider)
        return result

    wrapped._reprolog = True
    try:
        proxy.set_provider = wrapped
    except Exception:
        return False
    return True


def install() -> bool:
    """Attach the processor to the active tracer provider (Logfire sets one up)."""
    try:
        from opentelemetry import trace
    except Exception:
        return False
    provider = trace.get_tracer_provider()
    follows = _follow_swaps(provider)
    for candidate in (getattr(provider, "provider", None), getattr(provider, "_tracer_provider", None), provider):
        if _attach(candidate):
            return True
    return follows
