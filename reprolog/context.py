"""Per-request state shared by the middleware, the log handler and outbound patches."""
from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Capture:
    mode: str = "record"  # "record" while serving, "replay" during reprolog replay
    outbound: list = field(default_factory=list)
    logs: list = field(default_factory=list)
    logged_exceptions: list = field(default_factory=list)
    # Spans that ended while this request was running (from OpenTelemetry / Logfire).
    spans: list = field(default_factory=list)
    root_span_id: Optional[str] = None
    trace_id: Optional[str] = None
    # Replay only: recorded outbound calls to serve instead of the network.
    mocks: list = field(default_factory=list)
    live_outbound: bool = False
    strict: bool = False
    unmatched: list = field(default_factory=list)

    def next_mock(self, method: str, url: str) -> Optional[dict]:
        """Return the first unused recorded call with the same method and URL.
        Falls back to the same method + URL without the query string."""
        base = url.split("?", 1)[0]
        for exact in (True, False):
            for rec in self.mocks:
                if rec.get("_used") or rec.get("method") != method:
                    continue
                rec_url = rec.get("url", "")
                if (exact and rec_url == url) or (not exact and rec_url.split("?", 1)[0] == base):
                    rec["_used"] = True
                    return rec
        return None


current: contextvars.ContextVar[Optional[Capture]] = contextvars.ContextVar("reprolog_capture", default=None)


class _Registry:
    """Process-wide handles the replay runner needs."""

    middleware = None          # the installed ReprologMiddleware instance
    replaying: bool = False
    replay_capture: Optional[Capture] = None
    last_replay_record: Optional[dict] = None


registry = _Registry()
