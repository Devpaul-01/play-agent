"""Configuration loaded from environment variables.

Per architecture doc Sec19: "provider API key and app-under-test URL via
environment variables; nothing provider-specific hardcoded beyond the thin
interface." This module is the one place env vars are read -- nothing else
in the codebase should call os.environ directly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    groq_api_key: str
    groq_model: str

    app_under_test_path: str
    app_under_test_host: str

    db_path: str

    @staticmethod
    def from_env() -> Config:
        def require(name: str) -> str:
            value = os.environ.get(name)
            if not value:
                raise ConfigError(f"Missing required environment variable: {name}")
            return value

        return Config(
            groq_api_key=require("PLAY_AGENT_GROQ_API_KEY"),
            groq_model=os.environ.get("PLAY_AGENT_GROQ_MODEL", "llama-3.3-70b-versatile"),
            app_under_test_path=require("PLAY_AGENT_APP_PATH"),
            app_under_test_host=require("PLAY_AGENT_APP_HOST"),
            db_path=os.environ.get("PLAY_AGENT_DB_PATH", "play_agent.sqlite3"),
        )