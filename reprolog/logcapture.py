"""Attach Python log records emitted during a request to that request's capture."""
from __future__ import annotations

import logging
import time
import traceback

from .config import CONFIG
from .context import current
from .spans import current_ids


class ReprologLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        cap = current.get()
        if cap is None or len(cap.logs) >= CONFIG.max_logs_per_request:
            return
        try:
            entry = {
                "time": record.created,
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
                "where": f"{record.pathname}:{record.lineno}",
                "span_id": current_ids()[1],
            }
            if record.exc_info and record.exc_info[1] is not None:
                tb = "".join(traceback.format_exception(*record.exc_info))
                entry["traceback"] = tb
                cap.logged_exceptions.append(
                    {"type": type(record.exc_info[1]).__name__, "message": str(record.exc_info[1]), "traceback": tb}
                )
            cap.logs.append(entry)
        except Exception:  # never let logging break the app
            pass


_installed = False


def install() -> None:
    global _installed
    if _installed:
        return
    logging.getLogger().addHandler(ReprologLogHandler(level=logging.DEBUG))
    _installed = True


def add_log(level: str, message: str, **fields) -> None:
    """Record a custom log line on the current request (used by integrations)."""
    cap = current.get()
    if cap is None or len(cap.logs) >= CONFIG.max_logs_per_request:
        return
    cap.logs.append({"time": time.time(), "level": level.upper(), "logger": fields.pop("logger", "app"),
                     "message": message, "fields": fields or None, "span_id": current_ids()[1]})
