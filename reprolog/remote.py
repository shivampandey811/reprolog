"""Send captures to the cloud dashboard of the account that owns REPROLOG_TOKEN.

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
import urllib.parse
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


def fetch(capture_id: str):
    """Download one capture from the cloud (for `reprolog replay <id>`); None if not found."""
    url = f"{CONFIG.endpoint.rstrip('/')}/api/captures/{capture_id}?project={urllib.parse.quote(CONFIG.project)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {CONFIG.token}", "User-Agent": "reprolog-python"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            rec = json.loads(resp.read())
    except urllib.error.HTTPError as err:
        if err.code == 404:
            return None
        raise SystemExit(f"Could not download capture {capture_id}: {err.code} {err.read()[:200].decode('utf-8', 'replace')}")
    rec.pop("_curl", None)
    return rec


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


# Per-minute request metrics for every request (not just saved failures), keyed by
# (minute, method, route). Latency goes into fixed buckets so the server can merge
# minutes from many processes and still compute p50/p95.
LATENCY_BUCKETS_MS = (5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000)
_MAX_ROUTES_PER_MINUTE = 200
_metrics: dict = {}
_metrics_lock = threading.Lock()
_metrics_thread = None


def record_metric(method: str, route: str, status, duration_ms: float, failed: bool) -> None:
    if not enabled():
        return
    minute = int(time.time() // 60)
    key = (minute, method or "GET", (route or "/")[:200])
    with _metrics_lock:
        row = _metrics.get(key)
        if row is None:
            if sum(1 for k in _metrics if k[0] == minute) >= _MAX_ROUTES_PER_MINUTE:
                key = (minute, method or "GET", "(other)")
                row = _metrics.get(key)
            if row is None:
                row = _metrics[key] = [0, 0, 0.0, [0] * (len(LATENCY_BUCKETS_MS) + 1)]
        row[0] += 1
        row[1] += 1 if failed or (status or 0) >= 500 else 0
        row[2] += duration_ms or 0
        idx = next((i for i, b in enumerate(LATENCY_BUCKETS_MS) if (duration_ms or 0) <= b), len(LATENCY_BUCKETS_MS))
        row[3][idx] += 1
    _start_metrics()


def _take_metrics(everything: bool = False) -> list:
    current = int(time.time() // 60)
    out = []
    with _metrics_lock:
        for key in [k for k in _metrics if everything or k[0] < current]:
            count, errors, total_ms, buckets = _metrics.pop(key)
            out.append({"minute": key[0], "method": key[1], "route": key[2], "count": count,
                        "errors": errors, "total_ms": round(total_ms, 1), "buckets": buckets})
    return out


def _start_metrics() -> None:
    global _metrics_thread
    with _lock:
        if _metrics_thread is None or not _metrics_thread.is_alive():
            _metrics_thread = threading.Thread(target=_run_metrics, name="reprolog-metrics", daemon=True)
            _metrics_thread.start()


def _run_metrics() -> None:
    while True:
        time.sleep(15)
        rows = _take_metrics()
        if rows:
            _post([], metrics=rows)


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


def _post(batch: list, attempts: int = 3, metrics: list | None = None) -> None:
    url = CONFIG.endpoint.rstrip("/") + "/v1/ingest"
    payload = {"project": CONFIG.project, "captures": batch}
    if metrics:
        payload["metrics"] = metrics
    data = json.dumps(payload, default=str).encode("utf-8")
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
    rows = _take_metrics(everything=True)
    if rows:
        _post([], metrics=rows)
