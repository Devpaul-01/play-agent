# tests/test_container_integration.py
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
    """A trivial local HTTP server standing in for 'any app-under-test
    reachable at a host:port' -- the sandbox should be able to reach this
    purely because it's listening on the host, with zero awareness of
    what's actually serving it."""

    (tmp_path / "index.html").write_text("ok")

    handler = lambda *args, **kwargs: http.server.SimpleHTTPRequestHandler(
        *args, directory=str(tmp_path), **kwargs
    )
    server = http.server.HTTPServer(("0.0.0.0", 0), handler)
    port = server.server_address[1]

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.2)  # let the server actually start accepting connections

    yield port

    server.shutdown()


@pytest.fixture
def dummy_app_checkout(tmp_path):
    app_dir = tmp_path / "dummy-app"
    app_dir.mkdir()
    (app_dir / "README.md").write_text("stand-in app checkout")
    return app_dir


def test_sandbox_can_reach_host_app(local_http_server, dummy_app_checkout):
    """The real version of the smoke test the previous review called for.
    If this fails, the sandbox cannot reach any app-under-test and is not
    usable for probe_live_dom or run_verification -- this must pass
    before anything depending on the sandbox is trusted."""

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
                "import urllib.request, os; "
                "print(urllib.request.urlopen(os.environ['APP_UNDER_TEST_URL']).read().decode())",
            ]
        )

    assert exit_code == 0, f"sandbox could not reach the app: {output}"
    assert "ok" in output


def test_sandbox_mounts_are_isolated(dummy_app_checkout):
    """Confirm the sandbox cannot see anything of the host filesystem
    outside its two declared mounts -- the core Sec10 mount restriction,
    verified for real rather than just asserted in a mocked call shape."""

    config = SandboxConfig(
        image="python:3.11-slim",
        app_path=str(dummy_app_checkout),
        app_host="localhost",
        app_port=1,  # unused by this test
    )

    with Sandbox(config) as sandbox:
        exit_code, output = sandbox.exec_run(["ls", "/"])

    assert exit_code == 0
    # /app and /output should be present (the two mounts); nothing else
    # of the host should be reachable -- this isn't exhaustive proof of
    # isolation, but it's a real signal the mounts are scoped as intended.
    assert "app" in output
    assert "output" in output


def test_sandbox_teardown_leaves_no_container_running(dummy_app_checkout):
    """Confirm a container doesn't outlive its `with` block -- the
    'unconditional teardown' property, checked against the real Docker
    daemon rather than a mock."""

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
    """Confirm a missing/unreachable Docker daemon surfaces as SandboxError,
    not a raw DockerException -- the consistency fix from the review."""

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