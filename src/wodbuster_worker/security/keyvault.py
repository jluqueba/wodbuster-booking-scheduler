"""Key Vault secret loader.

Runs once at process startup. In ``prod`` mode, resolves the seven
secrets listed in the plan (see F3.8) from Azure Key Vault using
``DefaultAzureCredential`` (which selects the user-assigned managed
identity inside the Container App and ``AzureCliCredential`` on the
operator's workstation, per ADR-0005). In ``local`` mode, passes the
values through from :class:`~wodbuster_worker.config.Settings`, which
in turn reads them from ``.env`` or the process environment.

The loader returns a frozen :class:`Secrets` model so that downstream
code can rely on immutability and pass the whole object around
without accidental mutation. Missing values remain ``None`` and callers
that need them are expected to raise a clear ``RuntimeError`` at the
call site (mirroring :meth:`Settings.require_app_base_url`).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from ..config import Settings


class SecretResolver(Protocol):
    """Minimal interface satisfied by ``SecretClient`` and test fakes."""

    def get_secret(self, name: str) -> str | None:  # pragma: no cover
        ...


# The seven secrets specified by tasks.md F3.8. Order matches that
# document. Each tuple entry is (Key Vault secret name, Settings field
# name, Secrets field name); the Settings and Secrets fields share
# names by convention so local passthrough stays trivial.
_SECRET_SPECS: tuple[tuple[str, str], ...] = (
    ("wodbuster-cookie-encryption-key", "cookie_encryption_key"),
    ("session-encryption-secret", "session_encryption_secret"),
    ("telegram-bot-token", "telegram_bot_token"),
    ("telegram-webhook-secret", "telegram_webhook_secret"),
    ("oauth-microsoft-client-secret", "oauth_microsoft_client_secret"),
    ("oauth-github-client-secret", "oauth_github_client_secret"),
    ("oauth-google-client-secret", "oauth_google_client_secret"),
    ("healthchecks-ping-url", "healthchecks_ping_url"),
)


class Secrets(BaseModel):
    """Immutable bag of runtime secrets.

    All fields are optional because local development frequently only
    needs a subset (e.g. the cookie key when working on encryption but
    not on Telegram). The prod loader still populates every field it
    successfully fetches; callers that require a specific secret should
    fail loudly when the field is ``None``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    cookie_encryption_key: str | None = None
    session_encryption_secret: str | None = None
    telegram_bot_token: str | None = None
    telegram_webhook_secret: str | None = None
    oauth_microsoft_client_secret: str | None = None
    oauth_github_client_secret: str | None = None
    oauth_google_client_secret: str | None = None
    healthchecks_ping_url: str | None = None


def load_secrets(
    settings: Settings,
    *,
    resolver_factory: Callable[[str], SecretResolver] | None = None,
) -> Secrets:
    """Return the seven runtime secrets for the current environment.

    In ``local`` mode reads directly from ``settings``; the loader
    never contacts Azure. In ``prod`` mode instantiates the resolver
    (``SecretClient`` with ``DefaultAzureCredential`` by default) and
    fetches each secret by name; a missing secret in Key Vault surfaces
    as ``None`` on the returned model instead of raising, so that a
    single missing secret does not tear down startup for unrelated code
    paths.

    The ``resolver_factory`` seam exists purely for the F4.T3 test that
    injects a fake resolver without patching global state.
    """
    if settings.wodbuster_env == "local":
        return _load_from_settings(settings)

    if settings.key_vault_url is None:
        raise RuntimeError(
            "KEY_VAULT_URL is not set. Configure it on the Container "
            "App or provide it via environment before starting in prod."
        )

    factory = resolver_factory if resolver_factory is not None else _default_resolver
    resolver = factory(str(settings.key_vault_url))
    return _load_from_resolver(resolver)


def _load_from_settings(settings: Settings) -> Secrets:
    return Secrets(**{field: getattr(settings, field, None) for _, field in _SECRET_SPECS})


def _load_from_resolver(resolver: SecretResolver) -> Secrets:
    payload: dict[str, str | None] = {}
    for secret_name, field in _SECRET_SPECS:
        payload[field] = resolver.get_secret(secret_name)
    return Secrets(**payload)


def _default_resolver(vault_url: str) -> SecretResolver:
    """Build the production ``SecretClient``-backed resolver.

    Imported lazily so unit tests and local mode do not pay the
    ``azure-identity`` import cost.
    """
    from azure.identity import DefaultAzureCredential
    from azure.keyvault.secrets import SecretClient

    client = SecretClient(vault_url=vault_url, credential=DefaultAzureCredential())
    return _SecretClientResolver(client)


class _SecretClientResolver:
    """Adapter turning ``SecretClient`` into the ``SecretResolver`` shape.

    Returns ``None`` when a secret is absent from Key Vault so that the
    loader can distinguish "not configured" from "vault unreachable".
    Any other Azure error propagates.
    """

    def __init__(self, client: object) -> None:
        self._client = client

    def get_secret(self, name: str) -> str | None:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            bundle = self._client.get_secret(name)  # type: ignore[attr-defined]
        except ResourceNotFoundError:
            return None
        value = getattr(bundle, "value", None)
        return value if isinstance(value, str) else None


__all__ = ["Secrets", "load_secrets"]
