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
    """Insert an operator_profile row and return its id.

    Component tests need the FK target to exist before the store can
    write a cookie row. This helper is intentionally tiny; adding it
    to conftest as a shared fixture would couple the auth tests to
    this file.
    """
    with engine.begin() as conn:
        return int(
            conn.execute(
                text(
                    "INSERT INTO operator_profile (display_name) "
                    "VALUES (:n) RETURNING id"
                ),
                {"n": name},
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
        rows = session.query(CookieCredential).filter_by(operator_id=op_id).all()
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
        row = (
            session.query(CookieCredential).filter_by(operator_id=op_id).one()
        )
        row.projected_ttl_at = datetime(2027, 12, 31, tzinfo=UTC)
        session.commit()

    with session_factory() as session:
        store.save(session, op_id, ".WBAuth-2", validated_at=second_validated)
        session.commit()

    with session_factory() as session:
        row = (
            session.query(CookieCredential).filter_by(operator_id=op_id).one()
        )
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
        row = (
            session.query(CookieCredential).filter_by(operator_id=op_id).one()
        )
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
        CookieStore(write_cipher).save(
            session, op_id, ".WBAuth-x", validated_at=validated_at
        )
        session.commit()

    with session_factory() as session, pytest.raises(
        CookieDecryptError, match=str(op_id)
    ):
        CookieStore(read_cipher).load(session, op_id)


def test_save_rejects_empty_cookie(
    postgres_engine: Engine,
    session_factory: sessionmaker[Session],
    store: CookieStore,
) -> None:
    op_id = _make_operator(postgres_engine)

    with session_factory() as session, pytest.raises(ValueError, match="non-empty"):
        store.save(session, op_id, "", validated_at=datetime.now(tz=UTC))
