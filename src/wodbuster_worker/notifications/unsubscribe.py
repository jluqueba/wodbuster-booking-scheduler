"""Signed unsubscribe tokens for one-click email opt-out (ADR-0011).

A token encodes the operator id, signed and expiring, so the ``/unsubscribe``
link works without a login. Reuses the app's session-signing secret with a
dedicated salt so the token cannot be replayed as a session cookie or vice
versa.
"""

from __future__ import annotations

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

_SALT = "email-unsubscribe"
# Links live in a mailbox for a long time; a month keeps them usable without
# being effectively permanent.
_DEFAULT_MAX_AGE_S = 60 * 60 * 24 * 30


def make_unsubscribe_token(operator_id: int, *, secret: str) -> str:
    """Return a signed, expiring token for ``operator_id``."""
    return URLSafeTimedSerializer(secret, salt=_SALT).dumps(operator_id)


def read_unsubscribe_token(
    token: str, *, secret: str, max_age_s: int = _DEFAULT_MAX_AGE_S
) -> int | None:
    """Return the operator id from ``token``, or ``None`` if invalid/expired."""
    try:
        value = URLSafeTimedSerializer(secret, salt=_SALT).loads(token, max_age=max_age_s)
    except (BadSignature, SignatureExpired):
        return None
    return value if isinstance(value, int) else None


__all__ = ["make_unsubscribe_token", "read_unsubscribe_token"]
