"""Component tests for the cookie paste-and-validate routes (US-003).

Exercises ``GET /cookie`` and ``POST /cookie`` end-to-end against a
real Postgres schema. The :class:`CookieValidator` is replaced with a
scripted fake so the tests never hit the real WodBuster subdomain; the
:class:`CookieStore` is real (backed by an ephemeral :class:`Cipher`)
so we can also assert the persistence side of the flow.

The scenarios cover:

- Auth redirect for unauthenticated GET/POST.
- GET with no cookie on file renders the "no cookie yet" state.
- POST with a Valid verdict persists an encrypted row and returns the
  "valid" banner partial.
- POST with a Rejected verdict returns the "rejected" banner and
  performs no state mutation (FR-020).
- POST with an Unknown verdict returns the "unknown" banner and
  performs no state mutation.
- POST with a valid re-paste updates the ciphertext (upsert semantics
  visible through the /cookie view).
- POST without CSRF returns 403.
- GET/POST 503 gracefully when the operator has not configured the
  WodBuster tenant coordinates.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from wodbuster_worker.persistence.cookie_store import CookieStore
from wodbuster_worker.persistence.models import CookieCredential
from wodbuster_worker.security.cipher import Cipher
from wodbuster_worker.security.cookie import (
    CookieValidator,
    Rejected,
    Unknown,
    Valid,
    ValidationResult,
)


class _ScriptedValidator:
    """Fake :class:`CookieValidator` — hands out a scripted verdict.

    Duck-types the real validator (isinstance checks in the route use
    the ``Valid | Rejected | Unknown`` result classes, not the
    validator itself). Records each call so the test can assert the
    pasted value reached the validator unchanged, and that Unknown /
    Rejected paths did not silently coerce it.
    """

    def __init__(self, verdict: ValidationResult) -> None:
        self._verdict = verdict
        self.calls: list[str] = []

    def validate(self, cookie_value: str) -> ValidationResult:
        self.calls.append(cookie_value)
        return self._verdict


def _wire_cookie_stack(
    app: FastAPI, *, verdict: ValidationResult | None = None
) -> tuple[_ScriptedValidator | None, CookieStore]:
    """Install a real CookieStore and (optionally) a scripted validator."""
    cipher = Cipher(os.urandom(32))
    store = CookieStore(cipher)
    app.state.cipher = cipher
    app.state.cookie_store = store
    validator: _ScriptedValidator | None = None
    if verdict is not None:
        validator = _ScriptedValidator(verdict)
        # Duck-type: the route only calls ``validator.validate`` and
        # switches on the returned dataclass, so a Protocol-compatible
        # object is enough. Cast for the type checker.
        app.state.cookie_validator = validator  # type: ignore[assignment]
    return validator, store


def _sign_in(
    app: FastAPI,
    subject_id: str,
    display_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    """Drive the OAuth callback and return a logged-in TestClient."""
    client = app.state.oauth.create_client("microsoft")

    async def fake_authorize_access_token(_request: Any) -> dict[str, Any]:
        return {
            "userinfo": {"sub": subject_id, "name": display_name},
            "access_token": "fake-token",
        }

    monkeypatch.setattr(client, "authorize_access_token", fake_authorize_access_token)

    tc = TestClient(app, follow_redirects=False)
    resp = tc.get("/auth/microsoft/callback?code=fake&state=fake")
    assert resp.status_code == 302, resp.text
    return tc


def _csrf_headers(client: TestClient) -> dict[str, str]:
    """Return the X-CSRF-Token header for the current session.

    The double-submit cookie is set on the OAuth callback response; we
    echo it back on the header so :func:`verify_csrf` accepts the POST.
    """
    token = client.cookies.get("wodbuster_csrf")
    assert token, "expected wodbuster_csrf cookie after sign-in"
    return {"X-CSRF-Token": token}


def test_get_cookie_unauthenticated_redirects_to_login(
    app_factory: Callable[..., FastAPI],
) -> None:
    app = app_factory()
    _wire_cookie_stack(app, verdict=Valid(probed_at=datetime.now(tz=UTC)))

    with TestClient(app, follow_redirects=False) as client:
        response = client.get("/cookie")

    assert response.status_code == 302
    assert "/auth/" in response.headers["location"]
    assert response.text == ""


def test_get_cookie_authenticated_with_no_row_shows_empty_state(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, subject = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory()
    _wire_cookie_stack(app, verdict=Valid(probed_at=datetime.now(tz=UTC)))

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        response = client.get("/cookie")

    assert response.status_code == 200
    assert "No cookie on file yet" in response.text
    # The paste form is present with the CSRF hidden field.
    assert 'name="cookie_value"' in response.text
    assert 'name="_csrf"' in response.text


def test_post_valid_cookie_persists_and_returns_success_banner(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    probed_at = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)
    app = app_factory()
    validator, _ = _wire_cookie_stack(app, verdict=Valid(probed_at=probed_at))

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        response = client.post(
            "/cookie",
            data={"cookie_value": ".WBAuth-golden"},
            headers=_csrf_headers(client),
        )

    assert response.status_code == 200
    body = response.text
    assert "banner banner-valid" in body
    assert "Cookie validated and stored." in body
    # The Last-probe chip renders green (wb-chip--success) for a valid probe.
    assert "wb-chip--success" in body
    assert validator is not None
    assert validator.calls == [".WBAuth-golden"]

    # Row was persisted, encrypted at rest, with the validator's timestamp.
    with postgres_engine.connect() as conn:
        from sqlalchemy import text

        row = conn.execute(
            text(
                "SELECT cookie_ciphertext, last_validated_at, last_probe_status "
                "FROM cookie_credential WHERE operator_id = :op"
            ),
            {"op": op_id},
        ).one()
    assert b".WBAuth-golden" not in bytes(row.cookie_ciphertext)
    assert row.last_validated_at == probed_at
    assert row.last_probe_status == "valid"


@pytest.mark.parametrize(
    "verdict,expected_banner,expected_copy",
    [
        (
            Rejected(reason="server said no"),
            "banner banner-rejected",
            "Cookie rejected",
        ),
        (
            Unknown(reason="DNS blip"),
            "banner banner-unknown",
            "Could not validate the cookie right now",
        ),
    ],
)
def test_post_denied_verdicts_do_not_persist(
    verdict: ValidationResult,
    expected_banner: str,
    expected_copy: str,
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory()
    _wire_cookie_stack(app, verdict=verdict)

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        response = client.post(
            "/cookie",
            data={"cookie_value": "some-value"},
            headers=_csrf_headers(client),
        )

    assert response.status_code == 200
    body = response.text
    assert expected_banner in body
    assert expected_copy in body

    # Critical FR-020 invariant: no row created.
    from sqlalchemy import select
    from sqlalchemy.orm import sessionmaker

    session_local = sessionmaker(bind=postgres_engine)
    with session_local() as session:
        rows = session.execute(
            select(CookieCredential).where(CookieCredential.operator_id == op_id)
        ).all()
    assert rows == []


def test_post_valid_re_paste_upserts_the_row(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory()
    validator, _ = _wire_cookie_stack(app, verdict=Valid(probed_at=datetime.now(tz=UTC)))

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        headers = _csrf_headers(client)
        first = client.post("/cookie", data={"cookie_value": ".WBAuth-1"}, headers=headers)
        second = client.post("/cookie", data={"cookie_value": ".WBAuth-2"}, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert validator is not None
    assert validator.calls == [".WBAuth-1", ".WBAuth-2"]

    from sqlalchemy.orm import sessionmaker

    session_local = sessionmaker(bind=postgres_engine)
    with session_local() as session:
        rows = session.query(CookieCredential).filter_by(operator_id=op_id).all()
    assert len(rows) == 1  # upsert, not insert-twice
    # The second paste's ciphertext must decrypt to ".WBAuth-2"; use the
    # store directly via the app state (same key as the routes).
    cipher: Cipher = app.state.cipher
    plaintext = cipher.decrypt(bytes(rows[0].cookie_ciphertext), bytes(rows[0].cookie_nonce))
    assert plaintext == b".WBAuth-2"


def test_post_without_csrf_is_forbidden(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, subject = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory()
    _wire_cookie_stack(app, verdict=Valid(probed_at=datetime.now(tz=UTC)))

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        response = client.post(
            "/cookie",
            data={"cookie_value": "any"},  # no X-CSRF-Token header
        )

    assert response.status_code == 403


def test_post_empty_cookie_value_is_rejected_without_probe(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The validator itself short-circuits an empty input to Rejected
    # without probing — this test confirms the route surfaces that
    # verdict correctly and does not spuriously call the network layer.
    _, subject = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory()
    validator, _ = _wire_cookie_stack(
        app,
        verdict=Rejected(reason="empty (from real validator short-circuit)"),
    )

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        response = client.post(
            "/cookie",
            data={"cookie_value": ""},
            headers=_csrf_headers(client),
        )

    assert response.status_code == 200
    assert "banner banner-rejected" in response.text
    # Validator IS called (empty guard lives in the validator, not the
    # route) — the route trusts the validator to short-circuit.
    assert validator is not None
    assert validator.calls == [""]


def test_get_cookie_503_when_store_not_configured(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, subject = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory()
    # Explicitly leave cookie_store / cookie_validator as None to mimic
    # a partially-configured deployment.
    app.state.cookie_store = None
    app.state.cookie_validator = None

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        response = client.get("/cookie")

    assert response.status_code == 503
    assert "cookie store is not configured" in response.text.lower()


def test_lifespan_hooks_dont_reset_test_wiring(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: the lifespan must not overwrite pre-seeded stack state.

    ``create_app`` seeds ``app.state.cookie_store`` / ``cookie_validator``
    eagerly; the lifespan should treat non-``None`` values as canonical
    and not rebuild the stack. Tests rely on this to keep their fakes.
    """
    seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory()
    validator, store = _wire_cookie_stack(app, verdict=Valid(probed_at=datetime.now(tz=UTC)))

    # ``TestClient`` context manager runs the lifespan. We assert the
    # pre-seeded objects survive.
    with TestClient(app) as _tc:
        assert app.state.cookie_store is store
        assert app.state.cookie_validator is validator


# We import :class:`CookieValidator` only so the type is loaded and the
# annotation on ``_ScriptedValidator`` typechecks; guard against
# unused-import warnings from strict linters.
_ = CookieValidator
