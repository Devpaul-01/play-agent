"""Tests for Config.from_env -- this module had no tests before; added
alongside the app_under_test_port and sandbox_image fields the CLI needs
to build a SandboxConfig, which this file's absence let drift unnoticed.
"""

from __future__ import annotations

import pytest

from play_agent.config import Config, ConfigError

REQUIRED_ENV = {
    "PLAY_AGENT_GROQ_API_KEY": "test-key",
    "PLAY_AGENT_APP_PATH": "/tmp/fixture-app",
    "PLAY_AGENT_APP_HOST": "localhost",
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    # Config.from_env is the one place allowed to read os.environ (per
    # its own module docstring) -- strip every PLAY_AGENT_* var first so
    # a developer's real shell environment can't leak into a test.
    for key in list(__import__("os").environ):
        if key.startswith("PLAY_AGENT_"):
            monkeypatch.delenv(key, raising=False)


def _set_required(monkeypatch, overrides: dict[str, str] | None = None):
    env = {**REQUIRED_ENV, **(overrides or {})}
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def test_missing_required_var_raises_config_error(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.delenv("PLAY_AGENT_GROQ_API_KEY", raising=False)

    with pytest.raises(ConfigError, match="PLAY_AGENT_GROQ_API_KEY"):
        Config.from_env()


def test_defaults_applied_when_optional_vars_absent(monkeypatch):
    _set_required(monkeypatch)

    config = Config.from_env()

    assert config.groq_model == "llama-3.3-70b-versatile"
    assert config.app_under_test_port == 3000
    assert config.sandbox_image == "play-agent-sandbox:latest"
    assert config.db_path == "play_agent.sqlite3"


def test_all_vars_read_when_present(monkeypatch):
    _set_required(
        monkeypatch,
        {
            "PLAY_AGENT_GROQ_MODEL": "llama-3.1-8b-instant",
            "PLAY_AGENT_APP_PORT": "5173",
            "PLAY_AGENT_SANDBOX_IMAGE": "custom-image:v2",
            "PLAY_AGENT_DB_PATH": "/tmp/custom.sqlite3",
        },
    )

    config = Config.from_env()

    assert config.groq_model == "llama-3.1-8b-instant"
    assert config.app_under_test_port == 5173
    assert config.sandbox_image == "custom-image:v2"
    assert config.db_path == "/tmp/custom.sqlite3"


def test_non_integer_app_port_raises_config_error(monkeypatch):
    _set_required(monkeypatch, {"PLAY_AGENT_APP_PORT": "not-a-port"})

    with pytest.raises(ConfigError, match="PLAY_AGENT_APP_PORT must be an integer"):
        Config.from_env()
