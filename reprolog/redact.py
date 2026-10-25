"""Remove secrets from headers and bodies before anything is stored."""
from __future__ import annotations

import base64
import json
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode

from .config import CONFIG, REDACTED


def headers_to_list(raw: Iterable) -> list:
    """ASGI headers (list of byte pairs) or a mapping -> list of [name, value] strings."""
    out = []
    items = raw.items() if hasattr(raw, "items") else raw
    for name, value in items:
        if isinstance(name, bytes):
            name = name.decode("latin-1")
        if isinstance(value, bytes):
            value = value.decode("latin-1")
        out.append([str(name), str(value)])
    return out


def redact_headers(headers: list) -> list:
    return [[n, REDACTED if CONFIG.header_is_redacted(n) else v] for n, v in headers]


def redact_obj(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: (REDACTED if CONFIG.is_redacted_key(k) else redact_obj(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_obj(v) for v in obj]
    return obj


def encode_body(body: bytes, content_type: str | None) -> dict:
    """Return a JSON-safe body record with secrets removed.

    {"encoding": "text" | "base64" | "empty", "data": str, "redacted": bool}
    """
    if not body:
        return {"encoding": "empty", "data": "", "redacted": False}
    ctype = (content_type or "").lower()
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return {"encoding": "base64", "data": base64.b64encode(body).decode("ascii"), "redacted": False}

    if "json" in ctype or (not ctype and text[:1] in "{["):
        try:
            parsed = json.loads(text)
        except ValueError:
            pass
        else:
            cleaned = redact_obj(parsed)
            return {"encoding": "text", "data": json.dumps(cleaned, ensure_ascii=False), "redacted": cleaned != parsed}
    if "x-www-form-urlencoded" in ctype:
        pairs = parse_qsl(text, keep_blank_values=True)
        cleaned = [(k, REDACTED if CONFIG.is_redacted_key(k) else v) for k, v in pairs]
        return {"encoding": "text", "data": urlencode(cleaned), "redacted": cleaned != pairs}
    return {"encoding": "text", "data": text, "redacted": False}


def decode_body(rec: dict | None) -> bytes:
    if not rec or rec.get("encoding") == "empty":
        return b""
    if rec.get("encoding") == "base64":
        return base64.b64decode(rec["data"])
    return rec.get("data", "").encode("utf-8")


def header_value(headers: list, name: str) -> str | None:
    name = name.lower()
    for n, v in headers:
        if n.lower() == name:
            return v
    return None
