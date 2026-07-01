"""Shared pytest fixtures for the wodbuster_worker test suite.

Currently populated with an isolation fixture that prevents Settings()
constructions inside tests from picking up the developer's ambient
`.env` (which is present after F3.8 secret seeding). Without this,
tests that assert unset fields are `None` fail with the real seeded
values.

Component and contract layers will add SQLite, httpx mock, and
APScheduler fixtures here in later phases.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_from_repo_dot_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Run every test from a scratch directory.

    pydantic-settings reads `.env` from the current working directory,
    so chdir'ing to a per-test tmp_path guarantees Settings() never
    picks up the operator's real secrets. Tests that need repo files
    resolve them via `__file__`-relative paths (see
    tests/component/test_migrations.py).
    """
    monkeypatch.chdir(tmp_path)
