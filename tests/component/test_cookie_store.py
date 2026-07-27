"""Component tests for :class:`CookieStore` (US3.T2, US3.T3 backend part).

Exercises the save/load path against a real Postgres 16 database
(shared with the other component tests via the ``postgres_engine``
fixture in ``conftest.py``). Skips when Postgres is unreachable.

Focus areas:

- Round-trip: save then load returns the original plaintext.
- Upsert semantics: repeated saves for the same operator keep exactly
  one row and refresh the timestamps.
- Encryption at rest: the persisted ciphertext bytes never contain
  the plaintext.
- Isolation: one operator's cookie is invisible to another operator.
- Decryption failure: swapping to a different key surfaces as
  :class:`CookieDecryptError`.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from wodbuster_worker.persistence.cookie_store import (
    CookieDecryptError,
    CookieStore,
)
from wodbuster_worker.persistence.models import CookieCredential
from wodbuster_worker.security.cipher import Cipher


@pytest.fixture
def cipher() -> Cipher:
    """A fresh random-key cipher per test — keeps tests independent."""
    return Cipher(os.urandom(32))


@pytest.fixture
def store(cipher: Cipher) -> CookieStore:
    return CookieStore(cipher)


@pytest.fixture
def session_factory(postgres_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(
        bind=postgres_engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )


def _make_operator(engine: Engine, name: str = "Alice") -> int:
    """Insert an operator_profile + one gym_account and return the gym id.

    Multi-gym (ADR-0007): :class:`CookieStore` keys off ``gym_account_id``,
    so component tests need a gym account (and its FK operator) to exist
    before the store can write a cookie row. The returned id is the
    ``gym_account_id`` the store save/load path expects.
    """
    with engine.begin() as conn:
        op_id = int(
            conn.execute(
                text("INSERT INTO operator_profile (display_name) VALUES (:n) RETURNING id"),
                {"n": name},
            ).scalar_one()
        )
        return int(
            conn.execute(
                text(
                    "INSERT INTO gym_account (user_id, gym_slug, display_name, idu) "
                    "VALUES (:op, 'antworktrainingcenter', :n, :idu) RETURNING id"
                ),
                {"op": op_id, "n": name, "idu": f"idu{op_id:032d}"[:32]},
            ).scalar_one()
        )


def test_save_then_load_round_trips_the_cookie(
    postgres_engine: Engine,
    session_factory: sessionmaker[Session],
    store: CookieStore,
) -> None:
    op_id = _make_operator(postgres_engine)
    validated_at = datetime.now(tz=UTC)

    with session_factory() as session:
        store.save(session, op_id, ".WBAuth-alpha", validated_at=validated_at)
        session.commit()

    with session_factory() as session:
        assert store.load(session, op_id) == ".WBAuth-alpha"


def test_load_returns_none_when_no_row_exists(
    postgres_engine: Engine,
    session_factory: sessionmaker[Session],
    store: CookieStore,
) -> None:
    op_id = _make_operator(postgres_engine, name="Bob")

    with session_factory() as session:
        assert store.load(session, op_id) is None


def test_cookie_ciphertext_is_bound_to_its_gym_account(
    postgres_engine: Engine,
    session_factory: sessionmaker[Session],
    store: CookieStore,
) -> None:
    """SEC-005: a ciphertext row moved to a different gym account fails to
    decrypt, because the GCM associated data binds it to its owning gym
    account. Cross-gym cookie confusion becomes a cryptographic failure,
    not merely an application-layer convention (ADR-0007 hardening).
    """
    ga_a = _make_operator(postgres_engine, name="Alice")
    ga_b = _make_operator(postgres_engine, name="Bob")  # no cookie of its own

    with session_factory() as session:
        store.save(session, ga_a, ".WBAuth-secret", validated_at=datetime.now(tz=UTC))
        session.commit()

    # Simulate a bug or malicious write re-pointing A's ciphertext at B.
    with session_factory() as session:
        session.execute(
            text("UPDATE cookie_credential SET gym_account_id = :b WHERE gym_account_id = :a"),
            {"a": ga_a, "b": ga_b},
        )
        session.commit()

    # Loading under B must fail authentication, not silently decrypt A's cookie.
    with session_factory() as session, pytest.raises(CookieDecryptError):
        store.load(session, ga_b)


def test_load_accepts_legacy_cookie_encrypted_without_aad(
    postgres_engine: Engine,
    session_factory: sessionmaker[Session],
    store: CookieStore,
    cipher: Cipher,
) -> None:
    """Migration compatibility: a cookie encrypted before the SEC-005 AAD
    binding (single-gym era) is still decryptable through ``load``.

    The multi-gym migration moves the existing production ciphertext onto
    the seeded gym account WITHOUT re-encrypting it, so it carries no
    associated data. ``load`` must fall back to a no-AAD decrypt or the
    scheduler could not read the cookie and bookings would break.
    """
    ga = _make_operator(postgres_engine, name="Legacy")
    # Encrypt exactly like the single-gym CookieStore did: no AAD.
    ciphertext, nonce = cipher.encrypt(b".WBAuth-legacy")

    with session_factory() as session:
        session.add(
            CookieCredential(
                gym_account_id=ga,
                cookie_ciphertext=ciphertext,
                cookie_nonce=nonce,
                last_validated_at=datetime.now(tz=UTC),
                last_probe_status="valid",
            )
        )
        session.commit()

    with session_factory() as session:
        assert store.load(session, ga) == ".WBAuth-legacy"


def test_repeat_save_keeps_exactly_one_row_per_operator(
    postgres_engine: Engine,
    session_factory: sessionmaker[Session],
    store: CookieStore,
) -> None:
    op_id = _make_operator(postgres_engine)
    validated_at = datetime.now(tz=UTC)

    with session_factory() as session:
        store.save(session, op_id, ".WBAuth-first", validated_at=validated_at)
        session.commit()
    with session_factory() as session:
        store.save(session, op_id, ".WBAuth-second", validated_at=validated_at)
        session.commit()

    with session_factory() as session:
        rows = session.query(CookieCredential).filter_by(gym_account_id=op_id).all()
        assert len(rows) == 1
        assert store.load(session, op_id) == ".WBAuth-second"


def test_repeat_save_refreshes_timestamps_and_clears_projected_ttl(
    postgres_engine: Engine,
    session_factory: sessionmaker[Session],
    store: CookieStore,
) -> None:
    op_id = _make_operator(postgres_engine)

    first_validated = datetime(2026, 1, 1, tzinfo=UTC)
    second_validated = datetime(2026, 6, 1, tzinfo=UTC)

    # Prime: first save + fake a projected TTL so we can prove it gets
    # cleared on re-paste.
    with session_factory() as session:
        store.save(session, op_id, ".WBAuth-1", validated_at=first_validated)
        session.commit()
    with session_factory() as session:
        row = session.query(CookieCredential).filter_by(gym_account_id=op_id).one()
        row.projected_ttl_at = datetime(2027, 12, 31, tzinfo=UTC)
        session.commit()

    with session_factory() as session:
        store.save(session, op_id, ".WBAuth-2", validated_at=second_validated)
        session.commit()

    with session_factory() as session:
        row = session.query(CookieCredential).filter_by(gym_account_id=op_id).one()
        assert row.last_validated_at == second_validated
        # ``pasted_at`` is refreshed on every save (server_default fires
        # only on INSERT); after two saves it must reflect the second.
        assert row.pasted_at is not None
        assert row.pasted_at > first_validated
        # A fresh paste discards any historical TTL projection.
        assert row.projected_ttl_at is None


def test_plaintext_is_never_persisted(
    postgres_engine: Engine,
    session_factory: sessionmaker[Session],
    store: CookieStore,
) -> None:
    op_id = _make_operator(postgres_engine)
    plaintext = "unique-search-token-xyzzy"
    validated_at = datetime.now(tz=UTC)

    with session_factory() as session:
        store.save(session, op_id, plaintext, validated_at=validated_at)
        session.commit()

    with session_factory() as session:
        row = session.query(CookieCredential).filter_by(gym_account_id=op_id).one()
        assert plaintext.encode() not in bytes(row.cookie_ciphertext)
        assert plaintext.encode() not in bytes(row.cookie_nonce)
        # Nonces are 96 bits per NIST; assert we did not accidentally
        # persist plaintext in the nonce column.
        assert len(bytes(row.cookie_nonce)) == 12


def test_cookies_are_isolated_between_operators(
    postgres_engine: Engine,
    session_factory: sessionmaker[Session],
    store: CookieStore,
) -> None:
    alice = _make_operator(postgres_engine, name="Alice")
    bob = _make_operator(postgres_engine, name="Bob")
    validated_at = datetime.now(tz=UTC)

    with session_factory() as session:
        store.save(session, alice, "cookie-alice", validated_at=validated_at)
        store.save(session, bob, "cookie-bob", validated_at=validated_at)
        session.commit()

    with session_factory() as session:
        assert store.load(session, alice) == "cookie-alice"
        assert store.load(session, bob) == "cookie-bob"


def test_load_with_different_key_raises_decrypt_error(
    postgres_engine: Engine,
    session_factory: sessionmaker[Session],
) -> None:
    # Save with one cipher, try to load with another — simulates the
    # "key rotated without also re-encrypting rows" failure mode.
    op_id = _make_operator(postgres_engine)
    write_cipher = Cipher(os.urandom(32))
    read_cipher = Cipher(os.urandom(32))
    validated_at = datetime.now(tz=UTC)

    with session_factory() as session:
        CookieStore(write_cipher).save(session, op_id, ".WBAuth-x", validated_at=validated_at)
        session.commit()

    with session_factory() as session, pytest.raises(CookieDecryptError, match=str(op_id)):
        CookieStore(read_cipher).load(session, op_id)


def test_save_rejects_empty_cookie(
    postgres_engine: Engine,
    session_factory: sessionmaker[Session],
    store: CookieStore,
) -> None:
    op_id = _make_operator(postgres_engine)

    with session_factory() as session, pytest.raises(ValueError, match="non-empty"):
        store.save(session, op_id, "", validated_at=datetime.now(tz=UTC))


def test_ciphertext_is_bound_to_gym_account(
    postgres_engine: Engine,
    session_factory: sessionmaker[Session],
    store: CookieStore,
) -> None:
    """SEC-005: a ciphertext row re-pointed to another gym account fails
    to decrypt.

    :class:`CookieStore` binds ``gym_account_id`` into the AEAD
    associated data, so a persisted ciphertext is cryptographically
    tied to the gym account it was written for. Swapping the row's
    ``gym_account_id`` (a row-swap / replay against a sibling gym
    account, even one owned by the same operator) invalidates the auth
    tag and surfaces as :class:`CookieDecryptError` rather than
    leaking the other account's cookie.
    """
    gym_a = _make_operator(postgres_engine, name="GymA")
    gym_b = _make_operator(postgres_engine, name="GymB")
    validated_at = datetime.now(tz=UTC)

    with session_factory() as session:
        store.save(session, gym_a, ".WBAuth-secret", validated_at=validated_at)
        session.commit()

    # Re-point the persisted row at a different gym account, leaving the
    # ciphertext + nonce untouched (simulates a row-swap / replay).
    with session_factory() as session:
        row = session.query(CookieCredential).filter_by(gym_account_id=gym_a).one()
        row.gym_account_id = gym_b
        session.commit()

    with session_factory() as session, pytest.raises(CookieDecryptError, match=str(gym_b)):
        store.load(session, gym_b)
