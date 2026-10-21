import importlib
import os
import subprocess
import sys

import pytest
from pydantic import ValidationError


def settings_module():
    return importlib.import_module("app.runtime.settings")


def test_default_options_are_offline_and_bounded():
    options = settings_module().RunOptions()
    assert options.mode == "offline_demo"
    assert options.token_budget == 30_000


@pytest.mark.parametrize("budget", [0, -1, 1_000_001, True])
def test_invalid_budgets_are_rejected(budget):
    with pytest.raises(ValidationError):
        settings_module().RunOptions(token_budget=budget)


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:11434/v1", "http://[::1]:1234/v1", "http://localhost:8080/v1"]
)
def test_local_model_accepts_loopback_without_real_key(tmp_path, url):
    settings = settings_module().Settings(data_dir=tmp_path, base_url=url)
    settings.validate_mode(settings_module().RunOptions(mode="local", model="test"))


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/v1",
        "http://127.0.0.1.evil.test/v1",
        "file:///tmp/model",
        "http://user:password@localhost/v1",
    ],
)
def test_local_model_rejects_nonlocal_or_credential_urls(tmp_path, url):
    with pytest.raises(ValueError):
        settings_module().Settings(data_dir=tmp_path, base_url=url).validate_mode(
            settings_module().RunOptions(mode="local")
        )


def test_remote_is_disabled_by_default(tmp_path):
    settings = settings_module().Settings(
        data_dir=tmp_path, api_key="test-secret", base_url="https://example.com/v1"
    )
    with pytest.raises(ValueError, match="remote"):
        settings.validate_mode(settings_module().RunOptions(mode="remote"))
    assert "test-secret" not in repr(settings)


def test_from_env_ignores_old_key_in_offline_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("CODECRAFT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CODECRAFT_MODE", "offline_demo")
    monkeypatch.setenv("ZHIPU_API_KEY", "must-not-load")
    settings = settings_module().Settings.from_env()
    assert settings.data_dir == tmp_path
    assert settings.api_key.get_secret_value() == ""


def test_import_llm_never_loads_dotenv(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "ZHIPU_API_KEY"}
    code = "import os; import app.runtime.llm; assert 'ZHIPU_API_KEY' not in os.environ"
    result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
