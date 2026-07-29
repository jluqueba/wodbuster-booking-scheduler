"""Seed a second, fake gym account into the LOCAL database.

Dev-only helper to exercise the multi-gym switcher UX without a real
second WodBuster gym (whose cookie could not be validated against a
non-existent subdomain). Refuses to run unless the effective
``WODBUSTER_ENV`` is ``local``, so it can never touch prod. Idempotent.

Usage (with the project venv active)::

    python scripts/seed_local_gym.py

Requires an existing ``operator_profile`` (run ``python -m
wodbuster_worker.bootstrap`` first) and ``demo-gym-local`` present in
``GYM_ALLOWLIST`` if you also want to see it in the /gyms add form.
"""

from __future__ import annotations

import sys

from sqlalchemy import select

from wodbuster_worker.config import Settings
from wodbuster_worker.persistence.engine import get_session
from wodbuster_worker.persistence.models import GymAccount, OperatorProfile

FAKE_SLUG = "demo-gym-local"
FAKE_DISPLAY = "Demo Gym (local)"
# 32 hex chars: matches the idu shape the client expects, points nowhere real.
FAKE_IDU = "00000000000000000000000000000000"


def main() -> int:
    """Insert the fake gym for the sole operator; return a POSIX exit code."""
    if Settings().wodbuster_env != "local":
        print("refusing to run: WODBUSTER_ENV must be 'local' (never seed prod).", file=sys.stderr)
        return 2

    with get_session() as session:
        operator = session.scalars(select(OperatorProfile).order_by(OperatorProfile.id)).first()
        if operator is None:
            print("no operator_profile found; run 'python -m wodbuster_worker.bootstrap' first.")
            return 1

        existing = session.scalars(
            select(GymAccount.id).where(
                GymAccount.user_id == operator.id,
                GymAccount.gym_slug == FAKE_SLUG,
            )
        ).first()
        if existing is not None:
            print(f"already seeded: {FAKE_SLUG} -> gym_account_id={existing}")
            return 0

        account = GymAccount(
            user_id=operator.id,
            gym_slug=FAKE_SLUG,
            display_name=FAKE_DISPLAY,
            idu=FAKE_IDU,
            active=True,
        )
        session.add(account)
        session.flush()
        print(f"seeded {FAKE_SLUG} -> gym_account_id={account.id} (operator_id={operator.id})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
