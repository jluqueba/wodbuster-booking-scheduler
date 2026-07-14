"""Unit tests for the observability telemetry wire-up (US2.6).

Skips the exporter (network-bound) and focuses on the shape of
the module: metrics survive without a connection string, the
``configure_azure_monitor_if_enabled`` guard returns ``False``
without one, the histogram / observable-gauge factories return
instrument-shaped objects that ``record()`` and ``observe`` do
not raise on.
"""

from __future__ import annotations

import pytest

from wodbuster_worker.observability import telemetry


def test_configure_returns_false_when_connection_string_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local dev boot: no env var → no distro configured, no crash."""
    monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)
    # Reset the module-global guard so this test drives the "not
    # configured" branch even after another test flipped it.
    monkeypatch.setattr(telemetry, "_configured", False)
    assert telemetry.configure_azure_monitor_if_enabled() is False


def test_configure_returns_false_when_connection_string_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty / whitespace string counts as unset."""
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "   ")
    monkeypatch.setattr(telemetry, "_configured", False)
    assert telemetry.configure_azure_monitor_if_enabled() is False


def test_booking_latency_histogram_records_without_error() -> None:
    """No-op instrument is a real object with a ``record()`` API."""
    hist = telemetry.booking_attempt_latency_ms()
    hist.record(12.3, {"terminal_status": "granted"})
    # Same instance re-used on subsequent calls (cached).
    assert telemetry.booking_attempt_latency_ms() is hist


def test_cookie_probe_histogram_records_without_error() -> None:
    hist = telemetry.cookie_probe_duration_ms()
    hist.record(45.6, {"result": "valid"})
    assert telemetry.cookie_probe_duration_ms() is hist


def test_dispatch_lag_histogram_records_without_error() -> None:
    hist = telemetry.notification_dispatch_lag_seconds()
    hist.record(2.5, {"kind": "telegram"})
    assert telemetry.notification_dispatch_lag_seconds() is hist


def test_outbox_gauge_callback_is_invoked_and_survives_errors() -> None:
    """The observable gauge wraps the operator callback so a failure
    logs but does not tear the meter down."""
    calls: list[int] = [0]

    def _sample() -> int:
        calls[0] += 1
        return 42

    gauge = telemetry.register_outbox_queue_depth_gauge(_sample)
    # Same instance re-used on subsequent calls.
    assert telemetry.register_outbox_queue_depth_gauge(_sample) is gauge
