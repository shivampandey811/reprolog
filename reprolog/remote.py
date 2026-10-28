"""Send saved captures to the hosted dashboard (REPROLOG_TOKEN + REPROLOG_ENDPOINT).

A daemon thread batches records and POSTs them to <endpoint>/v1/ingest with the
stdlib (urllib), so the outbound recorder never sees these calls. Requests never
wait on it: if the queue is full or the server is down, records are dropped."""
from __future__ import annotations

import json
import queue
import sys
import threading
import time
import urllib.error
import urllib.request

from .config import CONFIG

_QUEUE: "queue.Queue[dict]" = queue.Queue(maxsize=500)
_BATCH = 20
_FLUSH_SECONDS = 2.0
_lock = threading.Lock()
_thread = None
_warned = set()
_pending = 0  # queued or in flight, for flush()

# Never send credentials to the server, even when REPROLOG_KEEP_AUTH keeps them locally.
_SECRET_HEADERS = {"authorization", "proxy-authorization", "cookie", "x-api-key", "api-key"}


def enabled() -> bool:
    return bool(CONFIG.token and CONFIG.endpoint)


def send(record: dict) -> None:
    if not enabled():
        return
    global _pending
    _start()
    with _lock:
        _pending += 1
    try:
        _QUEUE.put_nowait(_scrub(record))
    except queue.Full:
        with _lock:
            _pending -= 1
        _warn("queue-full", "reprolog: hosted upload queue is full, dropping captures")


def _scrub(record: dict) -> dict:
    req = dict(record.get("request") or {})
    headers = req.get("headers")
    if isinstance(headers, dict):
        req["headers"] = {k: ("[REDACTED]" if k.lower() in _SECRET_HEADERS else v) for k, v in headers.items()}
    elif isinstance(headers, list):
        req["headers"] = [[k, "[REDACTED]" if str(k).lower() in _SECRET_HEADERS else v] for k, v in headers]
    return {**record, "request": req}


def _start() -> None:
    global _thread
    with _lock:
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_run, name="reprolog-upload", daemon=True)
            _thread.start()


def _run() -> None:
    global _pending
    while True:
        batch = [_QUEUE.get()]
        deadline = time.time() + _FLUSH_SECONDS
        while len(batch) < _BATCH and time.time() < deadline:
            try:
                batch.append(_QUEUE.get(timeout=max(0.0, deadline - time.time())))
            except queue.Empty:
                break
        try:
            _post(batch)
        finally:
            with _lock:
                _pending -= len(batch)


def _post(batch: list, attempts: int = 3) -> None:
    url = CONFIG.endpoint.rstrip("/") + "/v1/ingest"
    data = json.dumps({"project": CONFIG.project, "captures": batch}, default=str).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {CONFIG.token}",
        "User-Agent": "reprolog-python",
    })
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read() or b"{}")
            if body.get("dropped"):
                _warn("limit", f"reprolog: daily limit reached, {body['dropped']} captures dropped")
            return
        except urllib.error.HTTPError as err:
            if err.code < 500:  # bad token, wrong project, too large: retrying won't help
                _warn(f"http-{err.code}", f"reprolog: upload refused ({err.code}): {err.read()[:200].decode('utf-8', 'replace')}")
                return
        except Exception:
            pass
        time.sleep(2 ** attempt)
    _warn("down", f"reprolog: could not reach {url}, dropping {len(batch)} captures")


def _warn(key: str, message: str) -> None:
    if key not in _warned:  # say it once, not on every request
        _warned.add(key)
        print(message, file=sys.stderr)


def flush(timeout: float = 10.0) -> None:
    """Wait until queued captures are sent (useful in scripts and tests)."""
    end = time.time() + timeout
    while _pending > 0 and time.time() < end:
        time.sleep(0.1)
