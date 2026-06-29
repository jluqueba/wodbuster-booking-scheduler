"""Smoke test for the test runner.

Verifies that pytest collects and runs at least one test and that the
package layout (src/wodbuster_worker) imports cleanly. Replaced by real
unit coverage as later tasks land.
"""

from __future__ import annotations

import wodbuster_worker


def test_package_imports_and_exposes_version() -> None:
    assert wodbuster_worker.__version__ == "0.1.0"


def test_health_endpoint_returns_ok_payload() -> None:
    from wodbuster_worker.app import health

    assert health() == {"status": "ok"}
