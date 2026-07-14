"""Foundational tests for the Key Vault loader (F4.T3).

Verifies the environment switch on ``load_secrets`` and its resolver
seam:

- ``WODBUSTER_ENV=prod`` builds a resolver, fetches every secret name,
  and returns the values on a frozen :class:`Secrets` model.
- ``WODBUSTER_ENV=local`` never constructs a resolver and instead
  passes through the seven fields already present on ``Settings``.

The Azure SDK is not exercised directly: a fake resolver satisfying
the ``SecretResolver`` protocol stands in and records the requested
names so the test can assert coverage without importing
``azure.identity`` or ``azure.keyvault.secrets``.
"""

from __future__ import annotations

import pytest

from wodbuster_worker.config import Settings
from wodbuster_worker.security.keyvault import (
    Secrets,
    load_secrets,
)


class _FakeResolver:
    """Records every ``get_secret`` call and returns canned values."""

    def __init__(self, values: dict[str, str]) -> None:
        self._values = values
        self.calls: list[str] = []

    def get_secret(self, name: str) -> str | None:
        self.calls.append(name)
        return self._values.get(name)


_EXPECTED_SECRET_NAMES = (
    "wodbuster-cookie-encryption-key",
    "session-encryption-secret",
    "telegram-bot-token",
    "oauth-microsoft-client-secret",
    "oauth-github-client-secret",
    "oauth-google-client-secret",
    "healthchecks-ping-url",
)


def test_prod_mode_calls_resolver_for_every_secret() -> None:
    settings = Settings(
        wodbuster_env="prod",
        key_vault_url="https://kv.example.vault.azure.net/",  # type: ignore[arg-type]
    )
    fake = _FakeResolver({name: f"value-of-{name}" for name in _EXPECTED_SECRET_NAMES})

    secrets = load_secrets(settings, resolver_factory=lambda _url: fake)

    assert fake.calls == list(_EXPECTED_SECRET_NAMES)
    assert secrets.cookie_encryption_key == "value-of-wodbuster-cookie-encryption-key"
    assert secrets.session_encryption_secret == "value-of-session-encryption-secret"
    assert secrets.telegram_bot_token == "value-of-telegram-bot-token"
    assert secrets.oauth_microsoft_client_secret == "value-of-oauth-microsoft-client-secret"
    assert secrets.oauth_github_client_secret == "value-of-oauth-github-client-secret"
    assert secrets.oauth_google_client_secret == "value-of-oauth-google-client-secret"
    assert secrets.healthchecks_ping_url == "value-of-healthchecks-ping-url"


def test_prod_mode_returns_frozen_model() -> None:
    settings = Settings(
        wodbuster_env="prod",
        key_vault_url="https://kv.example.vault.azure.net/",  # type: ignore[arg-type]
    )
    fake = _FakeResolver(dict.fromkeys(_EXPECTED_SECRET_NAMES, "x"))

    secrets = load_secrets(settings, resolver_factory=lambda _url: fake)

    with pytest.raises((TypeError, ValueError)):
        secrets.cookie_encryption_key = "mutated"  # type: ignore[misc]


def test_prod_mode_without_vault_url_raises() -> None:
    settings = Settings(wodbuster_env="prod")

    with pytest.raises(RuntimeError, match="KEY_VAULT_URL"):
        load_secrets(settings)


def test_prod_mode_missing_secret_maps_to_none() -> None:
    settings = Settings(
        wodbuster_env="prod",
        key_vault_url="https://kv.example.vault.azure.net/",  # type: ignore[arg-type]
    )
    fake = _FakeResolver({"telegram-bot-token": "abc"})

    secrets = load_secrets(settings, resolver_factory=lambda _url: fake)

    assert secrets.telegram_bot_token == "abc"
    assert secrets.cookie_encryption_key is None
    assert secrets.healthchecks_ping_url is None


def test_local_mode_never_constructs_resolver() -> None:
    settings = Settings(
        wodbuster_env="local",
        cookie_encryption_key="local-cookie-key",
        telegram_bot_token="local-bot-token",
    )

    def _forbidden_factory(_url: str) -> _FakeResolver:
        raise AssertionError("resolver_factory must not be invoked in local mode")

    secrets = load_secrets(settings, resolver_factory=_forbidden_factory)

    assert isinstance(secrets, Secrets)
    assert secrets.cookie_encryption_key == "local-cookie-key"
    assert secrets.telegram_bot_token == "local-bot-token"
    assert secrets.session_encryption_secret is None
    assert secrets.oauth_microsoft_client_secret is None


def test_local_mode_returns_none_for_unset_fields() -> None:
    settings = Settings(wodbuster_env="local")

    secrets = load_secrets(settings)

    assert secrets == Secrets()
    assert all(getattr(secrets, field) is None for field in Secrets.model_fields)
