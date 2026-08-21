"""First-operator bootstrap behavior."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from wodbuster_worker import bootstrap
from wodbuster_worker.persistence.models import FederatedIdentity, OperatorProfile


def test_bootstrap_creates_active_admin(
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = iter(("microsoft", "bootstrap-subject", "First Admin"))
    monkeypatch.setattr(bootstrap, "_prompt", lambda _label: next(answers))

    assert bootstrap.main() == 0

    with Session(postgres_engine) as session:
        profile = session.execute(
            select(OperatorProfile).where(OperatorProfile.display_name == "First Admin")
        ).scalar_one()
        identity = session.execute(
            select(FederatedIdentity).where(
                FederatedIdentity.operator_id == profile.id,
                FederatedIdentity.provider == "microsoft",
                FederatedIdentity.subject_id == "bootstrap-subject",
            )
        ).scalar_one()

    assert profile.status == "active"
    assert profile.is_admin is True
    assert identity.display_name == "First Admin"
