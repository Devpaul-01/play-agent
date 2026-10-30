"""Tests for Sandbox container lifecycle (Sec10).

IMPORTANT CAVEAT: these tests mock the Docker client entirely -- they
verify the *shape* of what play-agent asks Docker to do (volume mounts,
network mode, resource limits, timeout enforcement), not that a real
container actually starts, networks correctly, or survives real
teardown. That proof lives in test_container_integration.py, which runs
against a real Docker daemon in CI's `integration` job -- this file
alone is not sufficient evidence the sandbox works.
"""

from __future__ import annotations

import concurrent.futures
from unittest.mock import MagicMock, patch

import pytest
from docker.errors import APIError, DockerException

from play_agent.sandbox.container import Sandbox, SandboxConfig, SandboxError


@pytest.fixture
def config(tmp_path):
    app_dir = tmp_path / "fixture-app"
    app_dir.mkdir()
    return SandboxConfig(
        image="play-agent-fixture:latest",
        app_path=str(app_dir),
        app_host="localhost",
        app_port=5173,
    )


def test_start_mounts_exactly_app_and_output_dirs(config):
    mock_client = MagicMock()
    mock_client.containers.run.return_value = MagicMock()

    with patch("play_agent.sandbox.container.docker.from_env", return_value=mock_client):
        with Sandbox(config):
            pass

    call_kwargs = mock_client.containers.run.call_args.kwargs
    volumes = call_kwargs["volumes"]

    assert len(volumes) == 2
    assert any(str(k).endswith("fixture-app") for k in volumes)


def test_start_uses_bridge_network_with_host_gateway(config):
    # Replaces the old (buggy) assertion that network_mode == "none" --
    # "none" has no route to anything, including the host, which was the
    # actual bug. The corrected version uses a real bridge network plus
    # Docker's host-gateway mechanism to reach the host machine.
    mock_client = MagicMock()
    mock_client.containers.run.return_value = MagicMock()

    with patch("play_agent.sandbox.container.docker.from_env", return_value=mock_client):
        with Sandbox(config):
            pass

    call_kwargs = mock_client.containers.run.call_args.kwargs
    assert call_kwargs["network_mode"] == "bridge"
    assert call_kwargs["extra_hosts"] == {"host.docker.internal": "host-gateway"}


def test_localhost_app_host_rewritten_to_host_gateway(config):
    # app_host="localhost" (the common case: app runs as a local dev
    # server) must be rewritten to host.docker.internal in the URL passed
    # into the container -- 127.0.0.1/localhost INSIDE the container would
    # mean the container itself, never the host.
    mock_client = MagicMock()
    mock_client.containers.run.return_value = MagicMock()

    with patch("play_agent.sandbox.container.docker.from_env", return_value=mock_client):
        with Sandbox(config):
            pass

    call_kwargs = mock_client.containers.run.call_args.kwargs
    assert call_kwargs["environment"]["APP_UNDER_TEST_URL"] == "http://host.docker.internal:5173"


def test_non_local_app_host_left_unrewritten(tmp_path):
    # A real external hostname (e.g. a staging URL) must NOT be forced
    # through the host-gateway rewrite -- that path only applies when the
    # app is local relative to the sandbox runner. This is the
    # general-purpose guarantee: play-agent must not assume every
    # app-under-test runs on the same machine as play-agent itself.
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    config = SandboxConfig(
        image="x", app_path=str(app_dir), app_host="staging.example.com", app_port=443
    )
    mock_client = MagicMock()
    mock_client.containers.run.return_value = MagicMock()

    with patch("play_agent.sandbox.container.docker.from_env", return_value=mock_client):
        with Sandbox(config):
            pass

    call_kwargs = mock_client.containers.run.call_args.kwargs
    assert call_kwargs["environment"]["APP_UNDER_TEST_URL"] == "http://staging.example.com:443"


def test_start_drops_all_capabilities(config):
    mock_client = MagicMock()
    mock_client.containers.run.return_value = MagicMock()

    with patch("play_agent.sandbox.container.docker.from_env", return_value=mock_client):
        with Sandbox(config):
            pass

    call_kwargs = mock_client.containers.run.call_args.kwargs
    assert call_kwargs["cap_drop"] == ["ALL"]


def test_missing_app_path_raises_before_container_starts(tmp_path):
    config = SandboxConfig(
        image="play-agent-fixture:latest",
        app_path=str(tmp_path / "does-not-exist"),
        app_host="localhost",
        app_port=5173,
    )
    mock_client = MagicMock()

    with patch("play_agent.sandbox.container.docker.from_env", return_value=mock_client):
        with pytest.raises(SandboxError, match="does not exist"):
            with Sandbox(config):
                pass

    mock_client.containers.run.assert_not_called()


def test_teardown_called_even_when_body_raises(config):
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_client.containers.run.return_value = mock_container

    with patch("play_agent.sandbox.container.docker.from_env", return_value=mock_client):
        with pytest.raises(ValueError):
            with Sandbox(config):
                raise ValueError("simulated failure mid-run")

    mock_container.remove.assert_called_once_with(force=True)


def test_teardown_swallows_docker_api_error_but_not_body_exception(config):
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_container.remove.side_effect = APIError("daemon unreachable")
    mock_client.containers.run.return_value = mock_container

    with patch("play_agent.sandbox.container.docker.from_env", return_value=mock_client):
        with pytest.raises(ValueError, match="original error"):
            with Sandbox(config):
                raise ValueError("original error")


def test_exec_run_before_start_raises():
    mock_client = MagicMock()
    with patch("play_agent.sandbox.container.docker.from_env", return_value=mock_client):
        sandbox = Sandbox.__new__(Sandbox)
        sandbox._config = SandboxConfig(image="x", app_path="/tmp", app_host="localhost", app_port=1)
        sandbox._run_id = "test"
        sandbox._client = mock_client
        sandbox._container = None
        sandbox._host_output_dir = None

        with pytest.raises(SandboxError, match="not started"):
            sandbox.exec_run(["echo", "hi"])


def test_exec_run_times_out_on_hung_command(config):
    # Confirms the timeout fix: a command that never returns must raise
    # SandboxError rather than hang forever (which, before the fix, would
    # also have meant __exit__ never runs and the container leaks).
    import time

    def hanging_exec_run(*args, **kwargs):
        time.sleep(5)  # longer than the configured timeout below

    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_container.exec_run.side_effect = hanging_exec_run
    mock_client.containers.run.return_value = mock_container

    fast_timeout_config = SandboxConfig(
        image=config.image,
        app_path=config.app_path,
        app_host=config.app_host,
        app_port=config.app_port,
        timeout_seconds=1,
    )

    with patch("play_agent.sandbox.container.docker.from_env", return_value=mock_client):
        with Sandbox(fast_timeout_config) as sandbox:
            with pytest.raises(SandboxError, match="timed out"):
                sandbox.exec_run(["sleep", "10"])


def test_docker_daemon_unreachable_raises_sandbox_error(config):
    # Consistency fix: Sandbox's own constructor failure must raise
    # SandboxError, not leak docker's DockerException to the caller.
    with patch(
        "play_agent.sandbox.container.docker.from_env",
        side_effect=DockerException("daemon not reachable"),
    ):
        with pytest.raises(SandboxError, match="Docker daemon not reachable"):
            Sandbox(config)