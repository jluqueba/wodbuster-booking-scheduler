"""Authlib OAuth clients for Microsoft, GitHub, and Google (US9.2).

Constructs an :class:`authlib.integrations.starlette_client.OAuth`
registry with three clients. Client IDs come from :class:`Settings`
(env passthrough); client secrets come from :class:`Secrets` (KV in
prod, ``.env`` locally).

The provider config lines are deliberate:

- **Microsoft** uses OIDC discovery against the ``consumers`` tenant so
  only *personal* Microsoft accounts (@outlook.com, @hotmail.com,
  @live.com and linked-account Microsoft IDs) are accepted, and
  ``jwks_uri`` / ``userinfo_endpoint`` come from the well-known
  metadata document. ``common`` or ``organizations`` would let any
  Entra tenant sign in, which is a bigger surface than this project
  needs.
- **GitHub** uses its OAuth 2.0 endpoints; user identity ships in
  ``/user`` (numeric ``id`` field, stringified to fit the
  ``federated_identity.subject_id`` column).
- **Google** uses OIDC discovery so the ``jwks_uri`` / ``userinfo``
  come from the well-known metadata document; scopes stay minimal.

:func:`extract_identity` normalizes the per-provider user-info shapes
into ``(provider, subject_id, display_name)`` so the allow-list check
in :mod:`auth.routes` can query the ``federated_identity`` table
generically.
"""

from __future__ import annotations

from typing import Any

from authlib.integrations.starlette_client import OAuth

from ..config import Settings
from ..security.keyvault import Secrets

# Provider identifiers accepted throughout the auth surface. Anything
# outside this set is a 404 at the routing layer; the router imports
# this constant.
SUPPORTED_PROVIDERS = frozenset({"microsoft", "github", "google"})


def build_oauth(settings: Settings, secrets: Secrets) -> OAuth:
    """Register the three OAuth clients and return the :class:`OAuth`.

    Missing client IDs or client secrets are tolerated at construction
    time so that a partial local setup (e.g. only GitHub configured)
    still boots; per-provider ``authorize_redirect`` calls raise
    :class:`RuntimeError` when the provider they target has no ID or
    secret. This means the ``/health`` probe never depends on all three
    providers being configured.
    """
    oauth = OAuth()

    _maybe_register_microsoft(oauth, settings, secrets)
    _maybe_register_github(oauth, settings, secrets)
    _maybe_register_google(oauth, settings, secrets)

    return oauth


def _maybe_register_microsoft(
    oauth: OAuth, settings: Settings, secrets: Secrets
) -> None:
    if not settings.oauth_microsoft_client_id or not secrets.oauth_microsoft_client_secret:
        return
    oauth.register(
        name="microsoft",
        client_id=settings.oauth_microsoft_client_id,
        client_secret=secrets.oauth_microsoft_client_secret,
        server_metadata_url=(
            "https://login.microsoftonline.com/consumers/v2.0/.well-known/openid-configuration"
        ),
        client_kwargs={"scope": "openid email profile"},
    )


def _maybe_register_github(
    oauth: OAuth, settings: Settings, secrets: Secrets
) -> None:
    if not settings.oauth_github_client_id or not secrets.oauth_github_client_secret:
        return
    oauth.register(
        name="github",
        client_id=settings.oauth_github_client_id,
        client_secret=secrets.oauth_github_client_secret,
        access_token_url="https://github.com/login/oauth/access_token",
        authorize_url="https://github.com/login/oauth/authorize",
        # GitHub's user endpoint is REST, not OIDC. Authlib knows how
        # to call ``api_base_url + userinfo_endpoint`` for a fetch, and
        # returns the JSON body.
        api_base_url="https://api.github.com/",
        userinfo_endpoint="user",
        client_kwargs={"scope": "read:user user:email"},
    )


def _maybe_register_google(
    oauth: OAuth, settings: Settings, secrets: Secrets
) -> None:
    if not settings.oauth_google_client_id or not secrets.oauth_google_client_secret:
        return
    oauth.register(
        name="google",
        client_id=settings.oauth_google_client_id,
        client_secret=secrets.oauth_google_client_secret,
        server_metadata_url=(
            "https://accounts.google.com/.well-known/openid-configuration"
        ),
        client_kwargs={"scope": "openid email profile"},
    )


def extract_identity(
    provider: str,
    user_info: dict[str, Any],
) -> tuple[str, str, str]:
    """Normalize per-provider user info into ``(provider, subject_id, name)``.

    Called from the OAuth callback right after Authlib returns the
    user-info payload. The ``subject_id`` string is what
    :func:`federated_identity` rows key on and what the bootstrap
    command inserts; it is treated as opaque past this point.

    Raises :class:`ValueError` when the provider name is unknown or
    when the required subject field is missing (which would indicate
    an upstream API contract break rather than a normal login).
    """
    if provider == "microsoft":
        subject = user_info.get("sub")
        if not isinstance(subject, str) or not subject:
            raise ValueError("microsoft user_info missing 'sub'")
        name = _first_str(user_info.get("name"), user_info.get("email"), subject)
        return ("microsoft", subject, name)

    if provider == "github":
        raw_id = user_info.get("id")
        if raw_id is None:
            raise ValueError("github user_info missing 'id'")
        subject = str(raw_id)
        name = _first_str(
            user_info.get("login"),
            user_info.get("name"),
            subject,
        )
        return ("github", subject, name)

    if provider == "google":
        subject = user_info.get("sub")
        if not isinstance(subject, str) or not subject:
            raise ValueError("google user_info missing 'sub'")
        name = _first_str(user_info.get("name"), user_info.get("email"), subject)
        return ("google", subject, name)

    raise ValueError(f"unknown provider: {provider!r}")


def _first_str(*candidates: object) -> str:
    """Return the first non-empty string in ``candidates``.

    Providers vary in which display field they populate; this helper
    keeps :func:`extract_identity` compact without a chain of
    per-branch ``if isinstance(...)`` blocks. Falls back to an empty
    string only if every candidate is missing; callers pass the
    ``subject_id`` last so that path is defensive.
    """
    for candidate in candidates:
        if isinstance(candidate, str) and candidate:
            return candidate
    return ""
