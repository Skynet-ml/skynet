"""Tests for the hosted-link settings: build target, URL defaults and URL checks."""

# mypy: disable-error-code="call-arg"

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from core import config as config_module
from core.config import Settings

_ENV_VARS = (
    "APP_LOCAL_MODE",
    "SKYNET_LOCAL_MODE",
    "RAILWAY_ENVIRONMENT_NAME",
    "API_HOST",
    "APP_HOSTED_TARGET",
    "APP_HOSTED_APP_URL",
    "APP_HOSTED_API_URL",
    "APP_HOSTED_ALLOW_INSECURE_URL",
    "APP_ENVIRONMENT",
)
_STAGING_APP = "https://staging.skynetml.com"
_STAGING_API = "https://api-staging.skynetml.com"
_PRODUCTION_APP = "https://skynetml.com"
_PRODUCTION_API = "https://api.skynetml.com"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Remove ambient values and point the build stamp at an empty folder.

    Args:
        monkeypatch: Pytest fixture that restores the environment afterwards.
        tmp_path: Folder with no build stamp in it.
    """
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(config_module, "HOSTED_TARGET_STAMP", tmp_path / "hosted_target.txt")


def _local(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn on local mode on a loopback host.

    Args:
        monkeypatch: Pytest fixture used to set the environment.
    """
    monkeypatch.setenv("APP_LOCAL_MODE", "1")
    monkeypatch.setenv("API_HOST", "127.0.0.1")


def test_missing_stamp_falls_back_to_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """A build with no target and no stamp is treated as a release build.

    No packaging step writes the stamp yet, so a missing stamp must not send a
    release build to staging.

    Args:
        monkeypatch: Pytest fixture used to set the environment.
    """
    _local(monkeypatch)

    settings = Settings(_env_file=None)

    assert settings.hosted_target == "production"
    assert settings.hosted_app_url == _PRODUCTION_APP
    assert settings.hosted_api_url == _PRODUCTION_API


def test_dev_build_opts_in_to_staging(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dev build sets ``APP_HOSTED_TARGET=staging`` and links to staging.

    Args:
        monkeypatch: Pytest fixture used to set the environment.
    """
    _local(monkeypatch)
    monkeypatch.setenv("APP_HOSTED_TARGET", "staging")

    settings = Settings(_env_file=None)

    assert settings.hosted_target == "staging"
    assert settings.hosted_app_url == _STAGING_APP
    assert settings.hosted_api_url == _STAGING_API


def test_dev_env_example_opts_in_to_staging() -> None:
    """The dev env template sets the staging target, because the default is production."""
    env_example = Path(config_module.__file__).resolve().parents[1] / ".env.example"

    assert "APP_HOSTED_TARGET=staging" in env_example.read_text(encoding="utf-8").splitlines()


def test_release_stamp_links_to_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """The release build writes ``production`` into the stamp file.

    Args:
        monkeypatch: Pytest fixture used to set the environment.
    """
    _local(monkeypatch)
    config_module.HOSTED_TARGET_STAMP.write_text("production\n", encoding="utf-8")

    settings = Settings(_env_file=None)

    assert settings.hosted_target == "production"
    assert settings.hosted_app_url == _PRODUCTION_APP
    assert settings.hosted_api_url == _PRODUCTION_API


def test_unknown_stamp_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A damaged stamp stops the app instead of linking to a guessed site.

    Args:
        monkeypatch: Pytest fixture used to set the environment.
    """
    _local(monkeypatch)
    config_module.HOSTED_TARGET_STAMP.write_text("prod\n", encoding="utf-8")

    with pytest.raises(ValidationError, match="hosted target"):
        Settings(_env_file=None)


def test_explicit_urls_win_over_the_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit https URLs replace the target defaults; a trailing slash goes.

    Args:
        monkeypatch: Pytest fixture used to set the environment.
    """
    _local(monkeypatch)
    monkeypatch.setenv("APP_HOSTED_APP_URL", "https://app.example.test/")
    monkeypatch.setenv("APP_HOSTED_API_URL", "https://api.example.test")

    settings = Settings(_env_file=None)

    assert settings.hosted_app_url == "https://app.example.test"
    assert settings.hosted_api_url == "https://api.example.test"


@pytest.mark.parametrize(
    "url",
    [
        "http://api.example.test",
        "https://user:pw@api.example.test",
        "https://api.example.test/?x=1",
        "ftp://api.example.test",
        "api.example.test",
    ],
)
def test_local_mode_refuses_unsafe_hosted_urls(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    """Local mode accepts only a plain https origin for the hosted site.

    Args:
        monkeypatch: Pytest fixture used to set the environment.
        url: An unsafe value.
    """
    _local(monkeypatch)
    monkeypatch.setenv("APP_HOSTED_API_URL", url)

    with pytest.raises(ValidationError, match="APP_HOSTED_API_URL"):
        Settings(_env_file=None)


def test_dev_override_allows_http_on_loopback_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dev override opens http for a loopback hosted stack, and no other host.

    Args:
        monkeypatch: Pytest fixture used to set the environment.
    """
    _local(monkeypatch)
    monkeypatch.setenv("APP_HOSTED_ALLOW_INSECURE_URL", "1")
    monkeypatch.setenv("APP_HOSTED_API_URL", "http://127.0.0.1:8110")
    monkeypatch.setenv("APP_HOSTED_APP_URL", "http://localhost:3110")

    settings = Settings(_env_file=None)

    assert settings.hosted_api_url == "http://127.0.0.1:8110"
    monkeypatch.setenv("APP_HOSTED_API_URL", "http://api.example.test")
    with pytest.raises(ValidationError, match="APP_HOSTED_API_URL"):
        Settings(_env_file=None)


def test_hosted_mode_ignores_the_link_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hosted deployment never uses the link, so its URL values are not checked.

    Args:
        monkeypatch: Pytest fixture used to set the environment.
    """
    monkeypatch.setenv("APP_HOSTED_API_URL", "http://api.example.test")

    assert Settings(_env_file=None).local_mode is False


@pytest.mark.parametrize(
    ("environment", "expected"),
    [
        ("", (_PRODUCTION_APP, _PRODUCTION_API)),
        ("production", (_PRODUCTION_APP, _PRODUCTION_API)),
        ("staging", (_STAGING_APP, _STAGING_API)),
        ("skynet-pr-42", (_STAGING_APP, _STAGING_API)),
    ],
)
def test_hosted_public_origins_follow_the_environment(
    monkeypatch: pytest.MonkeyPatch, environment: str, expected: tuple[str, str]
) -> None:
    """A hosted deploy names staging origins on staging and previews, else production.

    Args:
        monkeypatch: Pytest fixture used to set the environment.
        environment: The ``APP_ENVIRONMENT`` value of the deploy.
        expected: The web app and API origins the deploy should name.
    """
    monkeypatch.setenv("APP_ENVIRONMENT", environment)

    assert Settings(_env_file=None).public_origins == expected


def test_local_public_origins_follow_the_build_stamp(monkeypatch: pytest.MonkeyPatch) -> None:
    """A staging Mac build names the staging origins.

    Args:
        monkeypatch: Pytest fixture used to set the environment.
    """
    _local(monkeypatch)
    config_module.HOSTED_TARGET_STAMP.write_text("staging\n", encoding="utf-8")

    assert Settings(_env_file=None).public_origins == (_STAGING_APP, _STAGING_API)
