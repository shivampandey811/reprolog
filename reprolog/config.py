"""Settings for reprolog. Every value can be set in code via reprolog.init(...)
or with an environment variable (shown next to each field)."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


DEFAULT_REDACT_HEADERS = {
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "api-key",
    "x-goog-api-key",
}

# Exact JSON / form keys whose values are replaced with "[REDACTED]".
# Kept exact on purpose: a loose "token" match would also hide LLM usage
# fields such as "prompt_tokens".
DEFAULT_REDACT_KEYS = (
    r"^(password|passwd|pwd|secret|client_secret|token|access_token|refresh_token|id_token|"
    r"api_key|apikey|authorization|auth|otp|card_number|cardnumber|cvv|cvc|ssn|pin)$"
)

REDACTED = "[REDACTED]"


@dataclass
class Config:
    enabled: bool = field(default_factory=lambda: _env_bool("REPROLOG_ENABLED", True))
    # SQLite file. Relative paths resolve against the process working directory.
    db_path: str = field(default_factory=lambda: os.getenv("REPROLOG_DB", ".reprolog/reprolog.db"))
    # Save a request when its status is >= this value (500 = server errors only).
    capture_min_status: int = field(default_factory=lambda: int(os.getenv("REPROLOG_MIN_STATUS", "500")))
    # Save every request, not just failures. Handy while exploring locally.
    capture_all: bool = field(default_factory=lambda: _env_bool("REPROLOG_CAPTURE_ALL", False))
    # Oldest captures beyond this count are deleted automatically.
    max_captures: int = field(default_factory=lambda: int(os.getenv("REPROLOG_MAX_CAPTURES", "5000")))
    max_body_bytes: int = field(default_factory=lambda: int(os.getenv("REPROLOG_MAX_BODY", str(256 * 1024))))
    max_logs_per_request: int = 300
    max_outbound_per_request: int = 100
    exclude_paths: set = field(default_factory=lambda: {"/health", "/docs", "/redoc", "/openapi.json", "/favicon.ico"})
    redact_headers: set = field(default_factory=lambda: set(DEFAULT_REDACT_HEADERS))
    redact_keys: str = DEFAULT_REDACT_KEYS
    # Keep auth headers in the local database so replays authenticate without
    # passing -H every time. Only safe because the database stays on your machine.
    keep_auth_headers: bool = field(default_factory=lambda: _env_bool("REPROLOG_KEEP_AUTH", False))
    # Read streamed outbound responses (e.g. LLM streaming) fully so they can be
    # replayed. Off by default because it turns streaming into a single response.
    capture_streaming_bodies: bool = field(default_factory=lambda: _env_bool("REPROLOG_CAPTURE_STREAMS", False))
    # Hosts tagged as LLM calls in the viewer.
    llm_hosts: tuple = (
        "api.openai.com",
        "generativelanguage.googleapis.com",
        "aiplatform.googleapis.com",
        "api.anthropic.com",
        "api.groq.com",
        "api.mistral.ai",
        "openai.azure.com",
        "openrouter.ai",
    )

    def __post_init__(self) -> None:
        self._key_re = re.compile(self.redact_keys, re.IGNORECASE)

    def is_redacted_key(self, key: str) -> bool:
        return bool(self._key_re.match(str(key)))

    def header_is_redacted(self, name: str) -> bool:
        name = name.lower()
        if self.keep_auth_headers and name in {"authorization", "cookie", "x-api-key"}:
            return False
        return name in self.redact_headers


CONFIG = Config()


def configure(**kwargs) -> Config:
    """Replace the global config, keeping env-derived defaults for anything not passed."""
    global CONFIG
    new = Config(**kwargs)
    CONFIG.__dict__.update(new.__dict__)
    return CONFIG
