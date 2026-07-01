"""Unit tests for `wodbuster_worker.config`.

Seeds the F4.T3 test surface. Only the `local` mode is exercised here:
the `prod`-mode Key Vault fake lives under F4.4 / F4.T3, after the real
loader exists.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wodbuster_worker.config import Settings, _load_from_keyvault

_ENV_VARS = ("WODBUSTER_ENV", "SQLITE_PATH", "LOG_LEVEL", "APP_BASE_URL")


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
        f"SQLITE_PATH={tmp_path / 'custom.db'}\n",
        encoding="utf-8",
    )

    settings = Settings(_env_file=str(env_file))  # type: ignore[call-arg]

    assert settings.wodbuster_env == "local"
    assert settings.log_level == "DEBUG"
    assert settings.sqlite_path == tmp_path / "custom.db"
    assert settings.app_base_url is None


def test_local_mode_defaults_when_env_file_is_silent(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("WODBUSTER_ENV=local\n", encoding="utf-8")

    settings = Settings(_env_file=str(env_file))  # type: ignore[call-arg]

    assert settings.wodbuster_env == "local"
    assert settings.log_level == "INFO"
    assert settings.sqlite_path == Path("./wodbuster.db")
    assert settings.app_base_url is None


def test_prod_mode_constructs_without_touching_keyvault(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("WODBUSTER_ENV=prod\n", encoding="utf-8")

    settings = Settings(_env_file=str(env_file))  # type: ignore[call-arg]

    assert settings.wodbuster_env == "prod"
    assert settings.sqlite_path == Path("/data/wodbuster.db")

    with pytest.raises(RuntimeError, match="APP_BASE_URL"):
        settings.require_app_base_url()


def test_keyvault_loader_is_stubbed_until_f4_4() -> None:
    with pytest.raises(NotImplementedError, match=r"F4\.4"):
        _load_from_keyvault("telegram-bot-token")
