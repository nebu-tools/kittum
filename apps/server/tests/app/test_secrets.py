"""The rule for configured keys and passwords, and the refusal to start that enforces it.

Every key and password must be at least 12 characters long, not a common password, and not part
of what Kittum itself sends. The server, the worker, the CLI and recording all refuse to start
otherwise, with a message that names the setting and the rule, and never the value.
"""

from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kittum.api import create_app
from kittum.app import (
    SECRET_LENGTH,
    AppContext,
    ConfigurationError,
    build_backends,
    build_gateway,
    check_secrets,
    credentials,
)
from kittum.cli import main
from kittum.core import SystemClock
from kittum.models import ModelRegistry
from kittum.settings import Settings
from kittum.storage import FilesystemStore

RULE = f"must be at least {SECRET_LENGTH} characters long, not a common password"
BASE = Settings(
    database_url="postgresql://kittum:kittum-local-dev@127.0.0.1:55432/kittum",
    artifact_dir=Path("unused"),
)
REFUSED = [
    pytest.param(
        replace(BASE, database_url="postgresql://kittum:kittum@db/kittum"),
        "the database password in KITTUM_DATABASE_URL",
        "kittum",
        id="short",
    ),
    pytest.param(
        replace(BASE, database_url="postgresql://kittum:Administrator@db/kittum"),
        "the database password in KITTUM_DATABASE_URL",
        "Administrator",
        id="common",
    ),
    pytest.param(
        replace(BASE, openalex_api_key="example.invalid"),
        "KITTUM_OPENALEX_API_KEY",
        "example.invalid",
        id="part-of-what-kittum-sends",
    ),
]


@pytest.mark.parametrize(("settings", "name", "value"), REFUSED)
def test_a_weak_secret_is_refused_with_the_setting_and_the_rule(
    settings: Settings, name: str, value: str
) -> None:
    with pytest.raises(ConfigurationError, match=f"^{name} {RULE}") as refused:
        check_secrets(settings)
    assert f":{value}@" not in str(refused.value) and f" {value} " not in str(refused.value)


def test_the_dev_password_and_long_keys_are_taken_and_all_of_them_are_private() -> None:
    settings = replace(BASE, openalex_api_key="oa-key-5f1c2d9e", contact_email="me@example.org")
    check_secrets(settings)
    assert set(credentials(settings).private) >= {"kittum-local-dev", "oa-key-5f1c2d9e"}


@pytest.mark.parametrize(("settings", "name", "value"), REFUSED)
def test_a_process_refuses_to_start_before_it_touches_the_database(
    settings: Settings, name: str, value: str
) -> None:
    del value
    with pytest.raises(ConfigurationError, match=f"^{name} {RULE}"):
        AppContext.from_settings(settings)


def test_recording_refuses_to_start_by_the_same_rule(tmp_path: Path) -> None:
    weak = replace(BASE, database_url="postgresql://kittum:kittum@db/kittum")
    store = FilesystemStore(tmp_path)
    with pytest.raises(ConfigurationError, match=RULE):
        build_gateway(replace(weak, gateway_record=tmp_path / "s"), None, store, SystemClock())  # type: ignore[arg-type]
    with pytest.raises(ConfigurationError, match=RULE):
        build_backends(replace(weak, llm_record=tmp_path / "m"), ModelRegistry.load(), store)


def test_the_server_refuses_to_start(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("KITTUM_DATABASE_URL", "postgresql://kittum:kittum@127.0.0.1:1/kittum")
    monkeypatch.setenv("KITTUM_ARTIFACT_DIR", str(tmp_path))
    with (
        pytest.raises(ConfigurationError, match="KITTUM_DATABASE_URL " + RULE),
        TestClient(create_app()),
    ):
        pass


@pytest.mark.parametrize("command", [["serve"], ["worker", "--once"], ["project", "list"]])
def test_the_command_line_refuses_to_start(
    command: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("KITTUM_DATABASE_URL", "postgresql://kittum:kittum@127.0.0.1:1/kittum")
    monkeypatch.setenv("KITTUM_ARTIFACT_DIR", str(tmp_path))
    assert main(command) == 2
    err = capsys.readouterr().err
    assert err.startswith(f"kittum {command[0]}: the database password in KITTUM_DATABASE_URL")
    assert RULE in err and ":kittum@" not in err
