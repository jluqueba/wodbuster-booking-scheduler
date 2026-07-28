"""Component tests for the add-gym flow (US multi-gym P1).

Drives the ``/gyms`` routes end to end against a real Postgres schema,
with a fake discovery client injected on ``app.state`` so no live
WodBuster call occurs. Covers the happy path (validate + discover idu +
persist atomically), the allow-list guard (SEC-001), cookie rejection,
and duplicate protection.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from wodbuster_worker.persistence.cookie_store import CookieStore
from wodbuster_worker.security.cipher import Cipher
from wodbuster_worker.wodbuster_client.client import WodBusterAuthError

from .conftest import gym_account_id_for

_ALLOWLIST = "antworktrainingcenter,adwork"
_DISCOVERED_IDU = "bbbb1111cccc2222dddd3333eeee4444"


class _FakeDiscoveryClient:
    """Fake WodBuster discovery client: returns a scripted idu or raises."""

    def __init__(self, idu: str, *, raises: Exception | None = None) -> None:
        self._idu = idu
        self._raises = raises
        self.calls: list[str] = []

    def discover_idu(self, cookie_value: str) -> str:
        self.calls.append(cookie_value)
        if self._raises is not None:
            raise self._raises
        return self._idu


def _install_factory(
    app: FastAPI, *, idu: str = _DISCOVERED_IDU, raises: Exception | None = None
) -> list[str]:
    """Wire a real CookieStore + a fake discovery factory; return built-slugs log."""
    app.state.cipher = Cipher(b"k" * 32)
    app.state.cookie_store = CookieStore(app.state.cipher)
    built: list[str] = []

    def factory(slug: str) -> _FakeDiscoveryClient:
        built.append(slug)
        return _FakeDiscoveryClient(idu, raises=raises)

    app.state.gym_discovery_factory = factory
    return built


def _sign_in(
    app: FastAPI, subject_id: str, display_name: str, monkeypatch: pytest.MonkeyPatch
) -> TestClient:
    client = app.state.oauth.create_client("microsoft")

    async def fake_authorize_access_token(_request: Any) -> dict[str, Any]:
        return {"userinfo": {"sub": subject_id, "name": display_name}, "access_token": "t"}

    monkeypatch.setattr(client, "authorize_access_token", fake_authorize_access_token)
    tc = TestClient(app, follow_redirects=False)
    resp = tc.get("/auth/microsoft/callback?code=fake&state=fake")
    assert resp.status_code == 302, resp.text
    return tc


def _csrf(client: TestClient) -> str:
    token = client.cookies.get("wodbuster_csrf")
    assert token, "expected wodbuster_csrf cookie after sign-in"
    return token


def test_add_gym_validates_discovers_idu_and_persists(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory(gym_allowlist=_ALLOWLIST)
    built = _install_factory(app)

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        resp = client.post(
            "/gyms",
            data={
                "gym_slug": "adwork",
                "cookie_value": ".WBAuth-adwork",
                "display_name": "Adwork",
                "_csrf": _csrf(client),
            },
        )

    assert resp.status_code == 303
    assert "flash_kind=info" in resp.headers["location"]
    assert built == ["adwork"]  # a client was built for the validated slug only

    with postgres_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT idu, display_name FROM gym_account "
                "WHERE user_id = :u AND gym_slug = 'adwork'"
            ),
            {"u": op_id},
        ).one()
        assert row.idu == _DISCOVERED_IDU
        assert row.display_name == "Adwork"
        # Cookie was stored for the new gym account (atomic write).
        cookie_rows = conn.execute(
            text(
                "SELECT count(*) FROM cookie_credential cc "
                "JOIN gym_account ga ON ga.id = cc.gym_account_id "
                "WHERE ga.user_id = :u AND ga.gym_slug = 'adwork'"
            ),
            {"u": op_id},
        ).scalar_one()
        assert cookie_rows == 1


def test_add_gym_rejects_off_allowlist_slug_without_building_a_client(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory(gym_allowlist=_ALLOWLIST)
    built = _install_factory(app)

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        resp = client.post(
            "/gyms",
            data={
                "gym_slug": "adwork.evil.com",
                "cookie_value": ".WBAuth-x",
                "_csrf": _csrf(client),
            },
        )

    assert resp.status_code == 303
    assert "flash_kind=error" in resp.headers["location"]
    # SEC-001: no client was ever built from the crafted slug.
    assert built == []
    with postgres_engine.connect() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM gym_account WHERE user_id = :u AND gym_slug LIKE 'adwork%'"),
            {"u": op_id},
        ).scalar_one()
        assert count == 0


def test_add_gym_cookie_rejected_persists_nothing(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory(gym_allowlist=_ALLOWLIST)
    _install_factory(app, raises=WodBusterAuthError("redirected to login"))

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        resp = client.post(
            "/gyms",
            data={"gym_slug": "adwork", "cookie_value": ".WBAuth-x", "_csrf": _csrf(client)},
        )

    assert resp.status_code == 303
    assert "flash_kind=error" in resp.headers["location"]
    with postgres_engine.connect() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM gym_account WHERE user_id = :u AND gym_slug = 'adwork'"),
            {"u": op_id},
        ).scalar_one()
        assert count == 0  # FR-011: no half-provisioned account


def test_add_gym_duplicate_is_rejected(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # seed_operator already creates an 'antworktrainingcenter' gym account.
    _op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory(gym_allowlist=_ALLOWLIST)
    _install_factory(app)

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        resp = client.post(
            "/gyms",
            data={
                "gym_slug": "antworktrainingcenter",
                "cookie_value": ".WBAuth-x",
                "_csrf": _csrf(client),
            },
        )

    assert resp.status_code == 303
    assert "flash_kind=warning" in resp.headers["location"]


def test_gyms_list_shows_owned_and_offers_addable(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory(gym_allowlist=_ALLOWLIST)
    _install_factory(app)

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        # gym_account_id_for is a no-op here but keeps the seeded gym anchored.
        with postgres_engine.connect() as conn:
            gym_account_id_for(conn, op_id)
        resp = client.get("/gyms", follow_redirects=False)

    assert resp.status_code == 200
    body = resp.text
    assert "antworktrainingcenter" in body  # owned gym listed
    assert "adwork" in body  # addable option in the form


def test_gyms_page_renders_in_spanish_under_es_prefix(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The /gyms page follows the URL-prefix language like the rest of the app."""
    _op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory(gym_allowlist=_ALLOWLIST)
    _install_factory(app)

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        resp = client.get("/es/gyms", follow_redirects=False)

    assert resp.status_code == 200
    body = resp.text
    assert "Tus gimnasios" in body  # Spanish h1
    assert "Añadir gimnasio" in body  # Spanish add button
    assert "Your gyms" not in body  # no English leakage


def _active(engine: Engine, gym_account_id: int) -> bool:
    with engine.connect() as conn:
        return bool(
            conn.execute(
                text("SELECT active FROM gym_account WHERE id = :i"), {"i": gym_account_id}
            ).scalar_one()
        )


def _gym_id(engine: Engine, user_id: int, slug: str) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                text("SELECT id FROM gym_account WHERE user_id = :u AND gym_slug = :s"),
                {"u": user_id, "s": slug},
            ).scalar_one()
        )


def _cookie_ciphertext(engine: Engine, gym_account_id: int) -> bytes | None:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT cookie_ciphertext FROM cookie_credential WHERE gym_account_id = :g"),
            {"g": gym_account_id},
        ).scalar_one_or_none()


def test_deactivate_then_reactivate_toggles_active_flag(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FR-006: the owner can deactivate a gym account and reactivate it."""
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory(gym_allowlist=_ALLOWLIST)
    _install_factory(app)
    with postgres_engine.connect() as conn:
        gid = gym_account_id_for(conn, op_id)

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        assert _active(postgres_engine, gid) is True

        resp = client.post(f"/gyms/{gid}/deactivate", data={"_csrf": _csrf(client)})
        assert resp.status_code == 303
        assert "flash_kind=info" in resp.headers["location"]
        assert _active(postgres_engine, gid) is False

        resp2 = client.post(f"/gyms/{gid}/reactivate", data={"_csrf": _csrf(client)})
        assert resp2.status_code == 303
        assert _active(postgres_engine, gid) is True


def test_refresh_cookie_is_isolated_to_one_gym(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FR-005: refreshing gym A's cookie leaves gym B's cookie untouched."""
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory(gym_allowlist=_ALLOWLIST)
    _install_factory(app)

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        # Add a second gym (adwork) — this stores a cookie for it.
        add = client.post(
            "/gyms",
            data={
                "gym_slug": "adwork",
                "cookie_value": ".WBAuth-adwork",
                "_csrf": _csrf(client),
            },
        )
        assert add.status_code == 303
        adwork_id = _gym_id(postgres_engine, op_id, "adwork")
        antwork_id = _gym_id(postgres_engine, op_id, "antworktrainingcenter")

        # Seed an initial cookie for the first gym via refresh.
        r0 = client.post(
            f"/gyms/{antwork_id}/cookie",
            data={"cookie_value": ".WBAuth-antwork", "_csrf": _csrf(client)},
        )
        assert r0.status_code == 303

        # Snapshot gym B (adwork) before refreshing gym A again.
        before = _cookie_ciphertext(postgres_engine, adwork_id)
        assert before is not None

        r1 = client.post(
            f"/gyms/{antwork_id}/cookie",
            data={"cookie_value": ".WBAuth-antwork-rotated", "_csrf": _csrf(client)},
        )
        assert r1.status_code == 303
        assert "flash_kind=info" in r1.headers["location"]

    # Gym B's stored cookie is byte-identical: the refresh touched only gym A.
    assert _cookie_ciphertext(postgres_engine, adwork_id) == before
    assert _cookie_ciphertext(postgres_engine, antwork_id) is not None


def test_gym_scoped_routes_reject_another_users_account_with_404(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SEC-002 / CC-003: a user cannot deactivate or refresh another
    user's gym account; the probe returns 404 with no state change."""
    op_a, _subject_a = seed_operator(provider="microsoft", display_name="Alice")
    _op_b, subject_b = seed_operator(provider="microsoft", display_name="Bob")
    app = app_factory(gym_allowlist=_ALLOWLIST)
    _install_factory(app)
    a_gid = _gym_id(postgres_engine, op_a, "antworktrainingcenter")
    assert _active(postgres_engine, a_gid) is True

    # Signed in as B, attempt to act on A's gym account.
    with _sign_in(app, subject_b, "Bob", monkeypatch) as client:
        deny = client.post(f"/gyms/{a_gid}/deactivate", data={"_csrf": _csrf(client)})
        assert deny.status_code == 404

        deny2 = client.post(
            f"/gyms/{a_gid}/cookie",
            data={"cookie_value": ".WBAuth-evil", "_csrf": _csrf(client)},
        )
        assert deny2.status_code == 404

    # A's account is untouched and never grew a cookie.
    assert _active(postgres_engine, a_gid) is True
    assert _cookie_ciphertext(postgres_engine, a_gid) is None
