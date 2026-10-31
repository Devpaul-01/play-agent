"""Real integration test against a real Docker daemon -- not mocked.

This is the test that actually proves the sandbox can reach the host
machine, which the mocked unit tests in test_sandbox_container.py cannot
prove (they only check the shape of the Docker API call, not that it
works). Run this locally once Docker is confirmed available, and it runs
automatically in CI's `integration` job on every push.

Uses a trivial public image (python:3.11-slim) and a local HTTP server
as a stand-in for "some app-under-test" -- deliberately NOT the fixture
app, to keep this test from depending on that repo existing or being
checked out, and to prove play-agent's sandbox is genuinely general-
purpose rather than fixture-app-specific.
"""

from __future__ import annotations

import http.server
import threading
import time

import pytest

from play_agent.sandbox.container import Sandbox, SandboxConfig, SandboxError


@pytest.fixture
def local_http_server(tmp_path):
    (tmp_path / "index.html").write_text("ok")

    handler = lambda *args, **kwargs: http.server.SimpleHTTPRequestHandler(
        *args, directory=str(tmp_path), **kwargs
    )
    server = http.server.HTTPServer(("0.0.0.0", 0), handler)
    port = server.server_address[1]

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.2)

    yield port

    server.shutdown()


@pytest.fixture
def dummy_app_checkout(tmp_path):
    app_dir = tmp_path / "dummy-app"
    app_dir.mkdir()
    (app_dir / "README.md").write_text("stand-in app checkout")
    return app_dir


def test_sandbox_can_reach_host_app(local_http_server, dummy_app_checkout):
    config = SandboxConfig(
        image="python:3.11-slim",
        app_path=str(dummy_app_checkout),
        app_host="localhost",
        app_port=local_http_server,
    )

    with Sandbox(config) as sandbox:
        exit_code, output = sandbox.exec_run(
            [
                "python3",
                "-c",
                (
                    "import urllib.request, os; "
                    "print(urllib.request.urlopen(os.environ['APP_UNDER_TEST_URL']).read().decode())"
                ),
            ]
        )

    assert exit_code == 0, f"sandbox could not reach the app: {output}"
    assert "ok" in output


def test_sandbox_mounts_are_isolated(dummy_app_checkout):
    config = SandboxConfig(
        image="python:3.11-slim",
        app_path=str(dummy_app_checkout),
        app_host="localhost",
        app_port=1,
    )

    with Sandbox(config) as sandbox:
        exit_code, output = sandbox.exec_run(["ls", "/"])

    assert exit_code == 0
    assert "app" in output
    assert "output" in output


def test_sandbox_teardown_leaves_no_container_running(dummy_app_checkout):
    import docker

    config = SandboxConfig(
        image="python:3.11-slim",
        app_path=str(dummy_app_checkout),
        app_host="localhost",
        app_port=1,
    )

    with Sandbox(config) as sandbox:
        run_id = sandbox.run_id

    client = docker.from_env()
    remaining = client.containers.list(
        all=True, filters={"label": f"play-agent-run-id={run_id}"}
    )
    assert remaining == [], "container was not removed on teardown"


def test_sandbox_missing_docker_daemon_raises_sandbox_error(dummy_app_checkout, monkeypatch):
    import docker

    def broken_from_env(*args, **kwargs):
        raise docker.errors.DockerException("daemon not reachable")

    monkeypatch.setattr(docker, "from_env", broken_from_env)

    config = SandboxConfig(
        image="python:3.11-slim",
        app_path=str(dummy_app_checkout),
        app_host="localhost",
        app_port=1,
    )

    with pytest.raises(SandboxError, match="Docker daemon not reachable"):
        Sandbox(config)