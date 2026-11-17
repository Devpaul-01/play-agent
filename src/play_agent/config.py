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
    app_under_test_port: int

    sandbox_image: str

    db_path: str
    log_path: str
    # Where each run's report.md / repair.diff are written (Sec19).
    output_dir: str
    # M3/Sec11: the frontend's component source root, for
    # component-source evidence resolution. Optional -- a missing root
    # just means that evidence is omitted, not a config error, since
    # it's additive context rather than a hard dependency (see
    # SandboxedToolBeltConfig.component_src_root's own docstring).
    component_src_root: str | None

    @staticmethod
    def from_env() -> Config:
        def require(name: str) -> str:
            value = os.environ.get(name)
            if not value:
                raise ConfigError(f"Missing required environment variable: {name}")
            return value

        port_str = os.environ.get("PLAY_AGENT_APP_PORT", "3000")
        try:
            port = int(port_str)
        except ValueError as e:
            raise ConfigError(
                f"PLAY_AGENT_APP_PORT must be an integer, got: {port_str!r}"
            ) from e

        return Config(
            groq_api_key=require("PLAY_AGENT_GROQ_API_KEY"),
            groq_model=os.environ.get("PLAY_AGENT_GROQ_MODEL", "llama-3.3-70b-versatile"),
            app_under_test_path=require("PLAY_AGENT_APP_PATH"),
            app_under_test_host=require("PLAY_AGENT_APP_HOST"),
            app_under_test_port=port,
            sandbox_image=os.environ.get("PLAY_AGENT_SANDBOX_IMAGE", "play-agent-sandbox:latest"),
            db_path=os.environ.get("PLAY_AGENT_DB_PATH", "play_agent.sqlite3"),
            log_path=os.environ.get("PLAY_AGENT_LOG_PATH", "play_agent.log"),
            output_dir=os.environ.get("PLAY_AGENT_OUTPUT_DIR", "play-agent-output"),
            component_src_root=os.environ.get("PLAY_AGENT_COMPONENT_SRC_ROOT"),
        )