"""Azure Monitor OpenTelemetry wire-up + custom metrics (US2.6).

Two responsibilities:

- :func:`configure_azure_monitor_if_enabled` bootstraps the Azure
  Monitor OpenTelemetry distro when the container ships with an
  ``APPLICATIONINSIGHTS_CONNECTION_STRING`` env var. That single
  call auto-instruments FastAPI, httpx, and SQLAlchemy so every
  request, outbound HTTP call, and DB query lands as a distributed
  trace in Application Insights. Structured logs go through the
  same exporter and stay correlated with their originating trace.

- :func:`get_meter` + the four :class:`Histogram` / observable-
  gauge accessors expose the custom metrics the plan calls for
  (:mod:`docs/features/wodbuster-booking-worker/tasks.md` US2.6):

  ================================== ====== ==================================
  Metric                             Kind   Emitting site
  ================================== ====== ==================================
  ``booking_attempt_latency_ms``     hist   :meth:`BookingExecutor.book`
  ``cookie_probe_duration_ms``       hist   :meth:`HeartbeatProbe.run`
  ``notification_dispatch_lag_s``    hist   :class:`NotificationDispatcher`
  ``outbox_queue_depth``             gauge  observable callback
  ================================== ====== ==================================

Local dev without the connection string boots normally — the meter
returns no-op instruments, so metric calls at emission sites cost
nothing and never crash.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

import structlog
from opentelemetry import metrics
from opentelemetry.metrics import Histogram, Meter, ObservableGauge

_log = structlog.get_logger(__name__)

_METER_NAME = "wodbuster_worker"
_METER_VERSION = "0.1.0"

_configured = False


def configure_azure_monitor_if_enabled() -> bool:
    """Boot the Azure Monitor OTel distro when the connection string is set.

    Returns ``True`` when the distro was configured, ``False`` when
    ``APPLICATIONINSIGHTS_CONNECTION_STRING`` is missing / empty.
    Idempotent — subsequent calls after a successful bootstrap are
    no-ops so tests and repeated lifespan invocations do not stack
    exporters.
    """
    global _configured
    if _configured:
        return True
    connection_string = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING", "").strip()
    if not connection_string:
        _log.info("azuremonitor.disabled_no_connection_string")
        return False
    try:
        # Deferred import: keeps the ``opentelemetry`` install
        # optional at import time and avoids paying the distro's
        # start-up cost when the connection string is absent.
        from azure.monitor.opentelemetry import configure_azure_monitor

        configure_azure_monitor(
            connection_string=connection_string,
            # ``azure-monitor`` picks up service-name + version from
            # the OTEL_SERVICE_NAME env var if set; keep the wire-up
            # explicit so local overrides work without touching the
            # container-app env.
            resource_attributes={
                "service.name": os.environ.get("OTEL_SERVICE_NAME", "wodbuster-worker"),
                "service.version": _METER_VERSION,
            },
        )
    except Exception:  # pragma: no cover - defensive; startup must not crash
        _log.exception("azuremonitor.configure_failed")
        return False
    _configured = True
    _log.info("azuremonitor.configured")
    return True


def get_meter() -> Meter:
    """Return the shared ``wodbuster_worker`` meter.

    Returns a real meter when the distro has been configured, and
    the OTel API's no-op meter otherwise. Callers do not need to
    branch: recording on a no-op instrument is safe and free.
    """
    return metrics.get_meter(_METER_NAME, _METER_VERSION)


# ---------------------------------------------------------------------------
# Custom instruments — created lazily so a test that never boots the
# distro does not pay the instantiation cost.
# ---------------------------------------------------------------------------


_booking_attempt_latency: Histogram | None = None
_cookie_probe_duration: Histogram | None = None
_notification_dispatch_lag: Histogram | None = None
_outbox_queue_depth_gauge: ObservableGauge | None = None


def booking_attempt_latency_ms() -> Histogram:
    """Histogram of end-to-end ``BookingExecutor.book`` latency in ms.

    Recorded once per attempted booking regardless of terminal
    status; label the value with the terminal (``granted`` / ``full``
    / ``cookie_invalid`` / ``skipped`` / ...) so slow-path buckets
    are separable in the Application Insights dashboard.
    """
    global _booking_attempt_latency
    if _booking_attempt_latency is None:
        _booking_attempt_latency = get_meter().create_histogram(
            name="wodbuster.booking_attempt_latency_ms",
            description="Wall-clock latency of a booking attempt, ms.",
            unit="ms",
        )
    return _booking_attempt_latency


def cookie_probe_duration_ms() -> Histogram:
    """Histogram of ``HeartbeatProbe.run`` latency in ms."""
    global _cookie_probe_duration
    if _cookie_probe_duration is None:
        _cookie_probe_duration = get_meter().create_histogram(
            name="wodbuster.cookie_probe_duration_ms",
            description="Wall-clock latency of a cookie probe, ms.",
            unit="ms",
        )
    return _cookie_probe_duration


def notification_dispatch_lag_seconds() -> Histogram:
    """Histogram of ``notification_outbox.enqueued_at`` → dispatch delay."""
    global _notification_dispatch_lag
    if _notification_dispatch_lag is None:
        _notification_dispatch_lag = get_meter().create_histogram(
            name="wodbuster.notification_dispatch_lag_seconds",
            description=(
                "Seconds between a notification-outbox row being enqueued and it being dispatched."
            ),
            unit="s",
        )
    return _notification_dispatch_lag


def register_outbox_queue_depth_gauge(
    callback: Callable[[], int],
) -> ObservableGauge:
    """Register (once) the observable gauge that samples the outbox depth.

    ``callback`` returns the current count of pending outbox rows.
    The distro polls the observable per its default cadence (60s);
    the callback runs on that thread, so keep it cheap — a
    ``SELECT count(*)`` with the appropriate ``WHERE`` is fine.
    """
    global _outbox_queue_depth_gauge
    if _outbox_queue_depth_gauge is not None:
        return _outbox_queue_depth_gauge

    def _observe(_options: Any) -> list[metrics.Observation]:
        try:
            depth = callback()
        except Exception:  # pragma: no cover - keep the meter alive
            _log.exception("azuremonitor.outbox_depth_callback_failed")
            return []
        return [metrics.Observation(depth)]

    _outbox_queue_depth_gauge = get_meter().create_observable_gauge(
        name="wodbuster.outbox_queue_depth",
        description="Pending rows in notification_outbox (dispatched_at IS NULL).",
        callbacks=[_observe],
    )
    return _outbox_queue_depth_gauge


__all__ = [
    "booking_attempt_latency_ms",
    "configure_azure_monitor_if_enabled",
    "cookie_probe_duration_ms",
    "get_meter",
    "notification_dispatch_lag_seconds",
    "register_outbox_queue_depth_gauge",
]
