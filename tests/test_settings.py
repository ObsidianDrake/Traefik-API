import os
from pathlib import Path
import subprocess
import sys

import pytest
from pydantic import ValidationError

from route_api.settings import Settings


TOKEN = "test-only-dotenv-token-0123456789"
CLI = Path(__file__).resolve().parents[1] / "run_api.py"


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key in ("API_TOKEN", "TRAEFIK_CONFIG_DIR", "TRAEFIK_CONFIG_FORMAT", "TRAEFIK_ENTRYPOINT",
                "ALLOWED_UPSTREAM_HOSTS", "ALLOWED_UPSTREAM_CIDRS",
                "PROBE_TIMEOUT_SECONDS", "PROBE_ATTEMPTS",
                "PROBE_INTERVAL_SECONDS", "MAX_BODY_BYTES",
                "TRAEFIK_API_URL", "TRAEFIK_API_TIMEOUT_SECONDS"):
        monkeypatch.delenv(key, raising=False)


def test_load_cwd_dotenv_without_changing_process_environment(tmp_path):
    token = TOKEN + "${LITERAL}!$#"
    (tmp_path / ".env").write_text(
        f'\ufeff# configuration\nAPI_TOKEN="{token}"\n'
        'TRAEFIK_CONFIG_DIR="./folder with spaces"\n'
        'ALLOWED_UPSTREAM_HOSTS="*" # comment\n'
        'ALLOWED_UPSTREAM_CIDRS=\nPROBE_ATTEMPTS=2\n', encoding="utf-8")
    settings = Settings.from_env()
    assert settings.token.get_secret_value() == token
    assert settings.config_dir == Path("./folder with spaces")
    assert settings.allowed_hosts == ["*"]
    assert settings.allowed_cidrs == []
    assert settings.probe_attempts == 2
    assert "API_TOKEN" not in os.environ


def test_environment_including_empty_values_overrides_file(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(f"API_TOKEN={TOKEN}\nALLOWED_UPSTREAM_HOSTS=*\n")
    monkeypatch.setenv("API_TOKEN", TOKEN + "-override")
    monkeypatch.setenv("ALLOWED_UPSTREAM_HOSTS", "")
    settings = Settings.from_env()
    assert settings.token.get_secret_value() == TOKEN + "-override"
    assert settings.allowed_hosts == []


def test_explicit_file_replaces_default_file(tmp_path):
    (tmp_path / ".env").write_text("API_TOKEN=invalid\n")
    custom = tmp_path / "custom.env"
    custom.write_text(f"API_TOKEN={TOKEN}\n")
    assert Settings.from_env(custom).token.get_secret_value() == TOKEN
    with pytest.raises(FileNotFoundError):
        Settings.from_env(tmp_path / "missing.env")


def test_missing_default_file_uses_environment(monkeypatch):
    monkeypatch.setenv("API_TOKEN", TOKEN)
    assert Settings.from_env().token.get_secret_value() == TOKEN


def test_missing_token_still_fails():
    with pytest.raises(ValidationError):
        Settings.from_env()


def test_parent_dotenv_is_not_loaded(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(f"API_TOKEN={TOKEN}\n")
    child = tmp_path / "child"
    child.mkdir()
    monkeypatch.chdir(child)
    with pytest.raises(ValidationError):
        Settings.from_env()


def test_cli_default_and_explicit_env_file(tmp_path):
    default = tmp_path / ".env"
    default.write_text(f"API_TOKEN={TOKEN}\n")
    result = subprocess.run([sys.executable, str(CLI), "--check-config"], capture_output=True, text=True)
    assert result.returncode == 0
    assert TOKEN not in result.stdout + result.stderr
    default.rename(tmp_path / "custom.env")
    result = subprocess.run([sys.executable, str(CLI), "--env-file", "custom.env", "--check-config"],
                            capture_output=True, text=True)
    assert result.returncode == 0
    result = subprocess.run([sys.executable, str(CLI), "--env-file", "missing.env", "--check-config"],
                            capture_output=True, text=True)
    assert result.returncode == 2
    assert "cannot read env file" in result.stderr


def test_invalid_file_token_is_not_echoed(tmp_path):
    secret = "short-secret"
    (tmp_path / ".env").write_text(f"API_TOKEN={secret}\n")
    result = subprocess.run([sys.executable, str(CLI), "--check-config"], capture_output=True, text=True)
    assert result.returncode == 2
    assert secret not in result.stdout + result.stderr
