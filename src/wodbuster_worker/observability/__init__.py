"""Observability bootstrap.

Owns process-wide logging configuration and the context-var helpers
used by request handlers and scheduler jobs to bind
per-invocation identifiers (operator id, request id) onto every log
record emitted downstream. Application Insights export lands with
US-002 / Phase 10 and is intentionally not wired here.
"""

from __future__ import annotations

from .logging import bind_operator, bind_request, configure_logging

__all__ = ["bind_operator", "bind_request", "configure_logging"]
