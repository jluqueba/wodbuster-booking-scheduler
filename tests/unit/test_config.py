"""Unit tests for `wodbuster_worker.config`.

Covers the environment-switching and default-path behaviour of the
``Settings`` model. The Key Vault loader itself lives in
``tests/unit/test_config_keyvault.py`` (task F4.T3).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wodbuster_worker.config import Settings

_ENV_VARS = (
    "WODBUSTER_ENV",
    "LOG_LEVEL",
    "APP_BASE_URL",
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear any inherited config vars so the file under test wins."""
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)


def test_local_mode_reads_overrides_from_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "WODBUSTER_ENV=local\n"
        "LOG_LEVEL=DEBUG\n"
        "POSTGRES_HOST=db.local\n"
        "POSTGRES_PORT=6543\n"
        "POSTGRES_DB=custom\n"
        "POSTGRES_USER=alice\n"
        "POSTGRES_PASSWORD=s3cret\n",
        encoding="utf-8",
    )

    settings = Settings(_env_file=str(env_file))  # type: ignore[call-arg]

    assert settings.wodbuster_env == "local"
    assert settings.log_level == "DEBUG"
    assert settings.postgres_host == "db.local"
    assert settings.postgres_port == 6543
    assert settings.postgres_db == "custom"
    assert settings.postgres_user == "alice"
    assert settings.postgres_password == "s3cret"
    assert settings.app_base_url is None


def test_local_mode_defaults_when_env_file_is_silent(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("WODBUSTER_ENV=local\n", encoding="utf-8")

    settings = Settings(_env_file=str(env_file))  # type: ignore[call-arg]

    assert settings.wodbuster_env == "local"
    assert settings.log_level == "INFO"
    # docker-compose defaults injected by the model validator when local
    # mode is picked and no override is present.
    assert settings.postgres_host == "localhost"
    assert settings.postgres_port == 5432
    assert settings.postgres_db == "wodbuster"
    assert settings.postgres_user == "wodbuster"
    assert settings.postgres_password == "wodbuster"
    assert settings.app_base_url is None


def test_prod_mode_constructs_without_touching_keyvault(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "WODBUSTER_ENV=prod\n"
        "POSTGRES_HOST=pg-yrv2tv7mfjvma.postgres.database.azure.com\n"
        "POSTGRES_DB=wodbuster\n"
        "POSTGRES_USER=id-yrv2tv7mfjvma\n",
        encoding="utf-8",
    )

    settings = Settings(_env_file=str(env_file))  # type: ignore[call-arg]

    assert settings.wodbuster_env == "prod"
    # In prod mode local docker-compose defaults must NOT be applied;
    # the runtime env supplies real coordinates.
    assert settings.postgres_host == "pg-yrv2tv7mfjvma.postgres.database.azure.com"
    assert settings.postgres_db == "wodbuster"
    assert settings.postgres_user == "id-yrv2tv7mfjvma"
    # Password is never set in prod (Entra token flow); the field stays None.
    assert settings.postgres_password is None

    with pytest.raises(RuntimeError, match="APP_BASE_URL"):
        settings.require_app_base_url()
