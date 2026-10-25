"""reprolog: capture failing requests and replay them locally to reproduce the error.

Usage (FastAPI), right after `app = FastAPI()` and before other add_middleware calls:

    import reprolog
    reprolog.init(app)
"""
from __future__ import annotations

from .config import CONFIG, Config, configure
from .logcapture import add_log
from .middleware import ReprologMiddleware

__all__ = ["init", "ReprologMiddleware", "CONFIG", "Config", "add_log"]
__version__ = "0.1.0"


def init(app=None, *, otel: bool = True, **settings) -> dict:
    """Configure reprolog, patch outbound HTTP clients, hook logging, and add the
    middleware to `app` if given. Returns which integrations were enabled."""
    from . import logcapture, outbound

    if settings:
        configure(**settings)
    status = {"enabled": CONFIG.enabled}
    if not CONFIG.enabled:
        return status
    status.update(outbound.patch_all())
    logcapture.install()
    status["logging"] = True
    if otel:
        from . import otel as _otel

        status["otel"] = _otel.install()
    if app is not None:
        app.add_middleware(ReprologMiddleware)
        status["middleware"] = True
    return status
