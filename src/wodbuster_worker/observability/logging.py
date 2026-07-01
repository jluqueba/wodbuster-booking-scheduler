"""Structured logging with structlog + stdlib integration.

Everything the worker emits (application code, uvicorn/FastAPI, the
Azure SDK, APScheduler, SQLAlchemy) flows through the stdlib
``logging`` root logger. We install a structlog ``ProcessorFormatter``
on that root logger so third-party libraries land in the same JSON
stream as structlog-native call sites, and configure structlog itself
to add per-invocation context bound via :func:`bind_operator` /
:func:`bind_request`.

Application Insights export is deliberately out of scope. That plugs
into the same root logger later (US-002 / Phase 10) without changing
this module's surface.
"""

from __future__ import annotations

import logging
from typing import Any

import structlog
from structlog.contextvars import (
    bind_contextvars,
    clear_contextvars,
    merge_contextvars,
)

_configured = False


def configure_logging(log_level: str) -> None:
    """Install the JSON structlog pipeline on the root logger.

    Idempotent: repeated calls (e.g. from a test fixture and then from
    the lifespan hook) rebuild the configuration rather than stacking
    handlers. Level accepts the canonical stdlib names ("DEBUG",
    "INFO", "WARNING", "ERROR", "CRITICAL").
    """
    global _configured

    numeric_level = logging.getLevelNamesMapping().get(log_level.upper())
    if numeric_level is None:
        raise ValueError(
            f"invalid log level {log_level!r}; expected one of "
            f"{sorted(logging.getLevelNamesMapping())}"
        )

    # Processors shared between structlog-native calls and stdlib
    # foreign_pre_chain. Order matters: contextvars first (so ids are
    # present), then metadata, then the final renderer decides the
    # wire format.
    shared_processors: list[structlog.types.Processor] = [
        merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.processors.format_exc_info,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        # foreign_pre_chain runs on records that did NOT originate in
        # structlog (uvicorn access log, third-party libs). It gives
        # them the same shared pre-processing before final rendering.
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)

    root = logging.getLogger()
    # Replace existing handlers so re-invocation (tests) does not
    # duplicate output.
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(numeric_level)

    _configured = True


def bind_operator(operator_id: int | str) -> None:
    """Attach an operator identifier to the current context.

    Backed by ``contextvars`` so it composes correctly with asyncio
    tasks and threaded scheduler jobs. Call at the top of a request
    handler or scheduled job; the id then appears on every subsequent
    log line until :func:`clear_context` is invoked.
    """
    bind_contextvars(operator_id=operator_id)


def bind_request(request_id: str) -> None:
    """Attach a request correlation id to the current context."""
    bind_contextvars(request_id=request_id)


def clear_context() -> None:
    """Clear every context var bound in this task or thread.

    Handlers that own a context boundary (middleware, scheduled jobs)
    should call this on exit so context does not leak into an unrelated
    invocation on the same worker thread.
    """
    clear_contextvars()


def get_logger(name: str | None = None, **initial_values: Any) -> Any:
    """Return a structlog logger bound to ``name``.

    Thin convenience wrapper so call sites do not have to import
    structlog directly; also gives us a single seam if the underlying
    library ever changes.
    """
    return structlog.get_logger(name, **initial_values)


__all__ = [
    "bind_operator",
    "bind_request",
    "clear_context",
    "configure_logging",
    "get_logger",
]
