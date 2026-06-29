"""Application configuration.

Implements the `Settings` class backed by `pydantic-settings`. The worker
runs in two modes, switched by the `WODBUSTER_ENV` environment variable:

- ``local``: settings are read from environment variables, optionally
  seeded by a ``.env`` file at repo root. This is the developer loop.
- ``prod``: settings are read from environment variables wired by
  Container Apps. Secrets land via Key Vault references (ADR-0005);
  resolution of those references is the job of the loader stubbed below
  and fully implemented under F4.4.

Construction must succeed in both modes without contacting Key Vault.
Secret access is deferred to call sites that need it, so that startup
errors (e.g. a missing secret in Key Vault) surface as a clear runtime
failure rather than masquerading as a configuration parse error.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AnyHttpUrl, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

WodBusterEnv = Literal["local", "prod"]


class Settings(BaseSettings):
    """Environment-driven configuration for the WodBuster worker.

    Field defaults are intentionally minimal in F1.6. Downstream phases
    (F2 / F3 / F4) extend this class with the additional fields they
    need; the contract here is the env-switching behaviour and the set
    of fields the bootstrap path touches before Key Vault is wired.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="",
        case_sensitive=False,
        extra="ignore",
    )

    wodbuster_env: WodBusterEnv = "local"
    sqlite_path: Path | None = None
    log_level: str = "INFO"
    app_base_url: AnyHttpUrl | None = None

    @model_validator(mode="after")
    def _apply_env_defaults(self) -> Settings:
        """Fill mode-dependent defaults.

        The SQLite file lives next to the source tree in ``local`` mode
        and on the mounted Azure Files volume (``/data``) in ``prod``
        (ADR-0002). When the operator sets ``SQLITE_PATH`` explicitly
        we honour the override regardless of mode.
        """
        if self.sqlite_path is None:
            self.sqlite_path = (
                Path("/data/wodbuster.db")
                if self.wodbuster_env == "prod"
                else Path("./wodbuster.db")
            )
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


def _load_from_keyvault(secret_name: str) -> str:
    """Resolve a secret from Azure Key Vault.

    Stub for F1.6. The real implementation lands in F4.4
    (`src/wodbuster_worker/security/keyvault.py`) and uses
    `DefaultAzureCredential` (UAMI in prod, `AzureCliCredential`
    locally) per ADR-0005. Calling this function today is a programmer
    error.
    """
    raise NotImplementedError(
        f"Key Vault secret resolution is not implemented yet "
        f"(requested: {secret_name!r}). Tracked in task F4.4."
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a process-wide cached `Settings` instance.

    Tests that need a clean slate should construct `Settings(...)`
    directly with explicit overrides instead of relying on this cache.
    """
    return Settings()
