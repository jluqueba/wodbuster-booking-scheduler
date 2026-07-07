"""Encrypted persistence for the operator's ``.WBAuth`` cookie (US3.2, US3.3).

Sits between the paste form and the ``cookie_credential`` table. The
plaintext cookie:

- **enters** through :meth:`save` and is immediately encrypted via
  :class:`~wodbuster_worker.security.cipher.Cipher` (AES-256-GCM) before
  the row is written.
- **exits** through :meth:`load`, which decrypts on the way out and
  returns the plaintext ``str``. Callers hold the plaintext only for
  the duration of a single booking attempt or heartbeat probe; there
  is no long-lived in-memory cache.

Design decisions:

- **Session is injected per call, not held on the store.** Route
  handlers and scheduler jobs open a session via
  :func:`~wodbuster_worker.persistence.engine.get_session` and pass it
  in, so the store never manages a transaction on its own and
  ``save`` composes cleanly with the caller's commit-on-success block
  (US-003 upserts the cookie in the same transaction that clears the
  cookie-expiring alert — US4.4).
- **Upsert semantics on ``operator_id``.** Exactly one active row per
  operator (enforced by ``uq_cookie_credential_operator``). Re-pasting
  overwrites the ciphertext, nonce, and validation timestamps; it also
  clears ``projected_ttl_at`` so the next heartbeat is what re-derives
  the countdown from scratch.
- **Rejected validations MUST NOT reach this class.** The store trusts
  its caller and does not re-validate; that is
  :class:`~wodbuster_worker.security.cookie.CookieValidator`'s job.
  Validation-before-persist is enforced at the route layer (US-003.6).
- **Decryption failure is loud.** Wrong key or tampered bytes surface
  as :class:`CookieDecryptError` at the call site. Callers translate
  that into an operator-facing "paste again" prompt rather than
  crashing the request.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..security.cipher import Cipher, InvalidCipherText
from .models import CookieCredential


class CookieDecryptError(Exception):
    """Raised when a stored cookie cannot be decrypted.

    Usually means the ``cookie_encryption_key`` Key Vault secret was
    rotated without also rotating the encrypted rows, or that a
    persisted row was tampered with. Either way the operator must
    paste again; the store cannot recover on its own.
    """


class CookieStore:
    """Encrypted read/write path for one active cookie per operator.

    Instantiated once at startup with a :class:`Cipher` built from the
    Key Vault secret. Threadsafe (the underlying ``AESGCM`` primitive
    from ``cryptography`` is threadsafe, and the store adds no mutable
    state).
    """

    __slots__ = ("_cipher",)

    def __init__(self, cipher: Cipher) -> None:
        self._cipher = cipher

    def save(
        self,
        session: Session,
        operator_id: int,
        cookie_value: str,
        *,
        validated_at: datetime,
    ) -> None:
        """Upsert the encrypted cookie for ``operator_id``.

        Callers must already have validated ``cookie_value`` via
        :class:`~wodbuster_worker.security.cookie.CookieValidator`.
        This method is not idempotent from the outside because it
        overwrites the previous ciphertext even when the plaintext is
        identical; the semantics are "the operator says this is the
        cookie now, keep it".

        Does not commit; that is the session owner's responsibility.
        Blank input raises :class:`ValueError` because storing a blank
        cookie is never a valid operation and short-circuits earlier
        than the validator would have caught it.
        """
        if not cookie_value:
            raise ValueError("cookie_value must be a non-empty string")
        ciphertext, nonce = self._cipher.encrypt(cookie_value.encode("utf-8"))

        existing = session.execute(
            select(CookieCredential).where(
                CookieCredential.operator_id == operator_id
            )
        ).scalar_one_or_none()

        if existing is None:
            session.add(
                CookieCredential(
                    operator_id=operator_id,
                    cookie_ciphertext=ciphertext,
                    cookie_nonce=nonce,
                    last_validated_at=validated_at,
                    last_probe_status="valid",
                    # ``pasted_at`` and ``projected_ttl_at`` take their
                    # DB defaults (server-generated NOW() and NULL).
                )
            )
            return

        existing.cookie_ciphertext = ciphertext
        existing.cookie_nonce = nonce
        # ``pasted_at`` has a server_default of NOW() that only fires on
        # INSERT. Update it explicitly on the re-paste so the UI's
        # "pasted" indicator reflects reality.
        existing.pasted_at = datetime.now(tz=UTC)
        existing.last_validated_at = validated_at
        existing.last_probe_status = "valid"
        # A fresh paste invalidates any historical TTL projection; the
        # next heartbeat (US4.1) is what re-derives the countdown from
        # scratch.
        existing.projected_ttl_at = None

    def load(self, session: Session, operator_id: int) -> str | None:
        """Return the decrypted cookie for ``operator_id`` or ``None``.

        ``None`` means the operator has never pasted a cookie or the
        row was hard-deleted. Callers treat that as "route to the
        paste form" rather than as an error.

        Raises :class:`CookieDecryptError` when a row exists but its
        ciphertext / nonce fail authentication. The exception is
        deliberately opaque: the auth-tag failure alone should not
        leak whether the key or the payload was wrong.
        """
        row = session.execute(
            select(CookieCredential).where(
                CookieCredential.operator_id == operator_id
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        try:
            plaintext = self._cipher.decrypt(
                bytes(row.cookie_ciphertext), bytes(row.cookie_nonce)
            )
        except InvalidCipherText as exc:
            raise CookieDecryptError(
                f"cookie for operator {operator_id} failed authentication"
            ) from exc
        return plaintext.decode("utf-8")


__all__ = ["CookieDecryptError", "CookieStore"]
