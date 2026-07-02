"""Application configuration.

Implements the `Settings` class backed by `pydantic-settings`. The worker
runs in two modes, switched by the `WODBUSTER_ENV` environment variable:

- ``local``: settings are read from environment variables, optionally
  seeded by a ``.env`` file at repo root. This is the developer loop.
- ``prod``: settings are read from environment variables wired by
  Container Apps. Secrets land via Key Vault references (ADR-0005);
  resolution of those references is the job of the loader stubbed below
  and fully implemented under F4.4.

Construction must succeed in both modes without contacting Key Vault or
Postgres. Secret access and DSN materialization are deferred to call
sites that need them, so that startup errors surface as clear runtime
failures rather than masquerading as configuration parse errors.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import AnyHttpUrl, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

WodBusterEnv = Literal["local", "prod"]


class Settings(BaseSettings):
    """Environment-driven configuration for the WodBuster worker.

    Field defaults are intentionally minimal. Downstream phases extend
    this class with the additional fields they need; the contract here
    is the env-switching behaviour, the Postgres coordinate block
    consumed by ``persistence.engine``, and the set of fields the
    bootstrap path touches before Key Vault is wired.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="",
        case_sensitive=False,
        extra="ignore",
    )

    wodbuster_env: WodBusterEnv = "local"
    log_level: str = "INFO"
    app_base_url: AnyHttpUrl | None = None

    # Postgres connection coordinates (ADR-0002). In ``prod`` these are
    # wired by Container Apps and point at the Flexible Server; the
    # password stays ``None`` because the worker authenticates with an
    # Entra token acquired by the runtime UAMI at connect time
    # (ADR-0005). In ``local`` they default to the docker-compose
    # service defined in ``docker-compose.yml``.
    postgres_host: str | None = None
    postgres_port: int = 5432
    postgres_db: str | None = None
    postgres_user: str | None = None
    postgres_password: str | None = None

    # Key Vault coordinates. Optional locally (secrets come from ``.env``),
    # required in prod (see :func:`security.keyvault.load_secrets`).
    key_vault_url: AnyHttpUrl | None = None

    # Local-mode secret passthrough. In prod these stay ``None`` and the
    # real values arrive via ``security.keyvault.load_secrets``. Naming
    # deliberately mirrors the fields on ``security.keyvault.Secrets`` so
    # the passthrough is a straight attribute copy.
    cookie_encryption_key: str | None = None
    session_encryption_secret: str | None = None
    telegram_bot_token: str | None = None
    oauth_microsoft_client_secret: str | None = None
    oauth_github_client_secret: str | None = None
    oauth_google_client_secret: str | None = None
    healthchecks_ping_url: str | None = None

    @model_validator(mode="after")
    def _apply_env_defaults(self) -> Settings:
        """Fill mode-dependent Postgres defaults.

        In ``local`` mode we fall back to the docker-compose service
        (``docker-compose.yml``): ``localhost:5432`` with the
        ``wodbuster/wodbuster/wodbuster`` triplet. In ``prod`` we leave
        every field ``None`` so misconfiguration surfaces at
        ``require_postgres_dsn()`` rather than being masked by a bogus
        default.
        """
        if self.wodbuster_env == "local":
            if self.postgres_host is None:
                self.postgres_host = "localhost"
            if self.postgres_db is None:
                self.postgres_db = "wodbuster"
            if self.postgres_user is None:
                self.postgres_user = "wodbuster"
            if self.postgres_password is None:
                self.postgres_password = "wodbuster"
        return self

    def require_app_base_url(self) -> AnyHttpUrl:
        """Return the public base URL or fail loudly.

        ``app_base_url`` is optional locally (the worker can be poked
        on ``localhost``), but mandatory in ``prod`` because OAuth
        callbacks and Telegram deep links need a canonical origin. We
        deliberately raise only when a caller actually needs the value,
        so that ``Settings()`` itself stays cheap and side-effect free.
        """
        if self.app_base_url is None:
            raise RuntimeError(
                "APP_BASE_URL is not set. Configure it on the Container "
                "App (prod) or in your local .env before invoking flows "
                "that need a public origin (OAuth callbacks, Telegram "
                "deep links)."
            )
        return self.app_base_url

    def require_postgres_dsn(self) -> str:
        """Return a fully-qualified SQLAlchemy Postgres URL.

        Emits a ``postgresql+psycopg://`` DSN. ``sslmode=require`` is
        set in ``prod`` so Azure Database for PostgreSQL Flexible
        Server (which mandates TLS) accepts the handshake; in ``local``
        we use ``sslmode=disable`` because the docker-compose
        ``postgres:16-alpine`` image ships without TLS configured and
        would otherwise reject the client with "server does not
        support SSL, but SSL was required".

        The password is intentionally omitted from the URL: in prod
        the engine injects a fresh Entra token per connection via a
        ``do_connect`` listener; in local mode ``engine.build_engine``
        passes ``postgres_password`` through ``connect_args`` instead.
        This keeps the DSN safe to log.
        """
        missing = [
            name
            for name, value in (
                ("POSTGRES_HOST", self.postgres_host),
                ("POSTGRES_DB", self.postgres_db),
                ("POSTGRES_USER", self.postgres_user),
            )
            if value is None or value == ""
        ]
        if missing:
            raise RuntimeError(
                "Postgres configuration incomplete: missing "
                + ", ".join(missing)
                + ". Set the POSTGRES_* env vars (see .env.example)."
            )
        # mypy: the ``missing`` guard proves these are non-None.
        assert self.postgres_host is not None
        assert self.postgres_db is not None
        assert self.postgres_user is not None
        sslmode = "require" if self.wodbuster_env == "prod" else "disable"
        return (
            f"postgresql+psycopg://{self.postgres_user}"
            f"@{self.postgres_host}:{self.postgres_port}"
            f"/{self.postgres_db}?sslmode={sslmode}"
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a process-wide cached `Settings` instance.

    Tests that need a clean slate should construct `Settings(...)`
    directly with explicit overrides instead of relying on this cache.
    """
    return Settings()
