"""Bootstrap the first operator identity (US9.7).

Usage::

    python -m wodbuster_worker.bootstrap

Interactive prompts collect ``provider``, ``subject_id``, and
``display_name``, then upsert an ``operator_profile`` + a
``federated_identity`` row keyed on ``(provider, subject_id)``. The
command is idempotent: rerunning with an already-registered pair is a
no-op and prints ``already registered``.

Rationale: OAuth callback deliberately refuses to auto-create
operators (FR-030). Seeding the very first operator therefore requires
an out-of-band step, which is this CLI. On subsequent installs an
operator with an existing session can add more identities via a future
admin UI (out of scope for US-009).

To discover a ``subject_id`` for a provider, sign in via the OAuth
flow once and read the denial log (the callback logs the presented
``(provider, subject_id)`` before rendering the denial page). See
README "Bootstrap the first operator" for the step-by-step.
"""

from __future__ import annotations

import sys

from sqlalchemy import select

from .auth.oauth import SUPPORTED_PROVIDERS
from .persistence.engine import get_session as db_session
from .persistence.models import FederatedIdentity, OperatorProfile


def main() -> int:
    """Interactive entry point. Returns a POSIX-style exit code."""
    print("wodbuster_worker.bootstrap: register the first operator identity")
    print(
        "Prompts:\n"
        "  provider     one of "
        + ", ".join(sorted(SUPPORTED_PROVIDERS))
        + "\n  subject_id   OAuth subject ID for that provider\n"
        "  display_name human-friendly label for the operator profile"
    )

    provider = _prompt("provider").strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        print(f"error: unknown provider {provider!r}", file=sys.stderr)
        return 2

    subject_id = _prompt("subject_id").strip()
    if not subject_id:
        print("error: subject_id must not be empty", file=sys.stderr)
        return 2

    display_name = _prompt("display_name").strip()
    if not display_name:
        print("error: display_name must not be empty", file=sys.stderr)
        return 2

    with db_session() as session:
        existing = session.execute(
            select(FederatedIdentity).where(
                FederatedIdentity.provider == provider,
                FederatedIdentity.subject_id == subject_id,
            )
        ).scalar_one_or_none()
        if existing is not None:
            print(
                f"already registered: {provider}/{subject_id} -> operator_id={existing.operator_id}"
            )
            return 0

        operator = OperatorProfile(display_name=display_name)
        session.add(operator)
        session.flush()  # populate operator.id

        identity = FederatedIdentity(
            operator_id=operator.id,
            provider=provider,
            subject_id=subject_id,
            display_name=display_name,
        )
        session.add(identity)

        # Commit happens on context-manager exit.
        print(f"registered: {provider}/{subject_id} -> operator_id={operator.id}")
    return 0


def _prompt(label: str) -> str:
    """Return a single line of user input.

    Extracted so tests can monkeypatch the input source without
    touching ``builtins.input`` globally.
    """
    return input(f"{label}: ")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
