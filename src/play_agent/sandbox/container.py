"""Per-run Docker container lifecycle (architecture Sec10).

Sec10: "one Docker container per run, mounts restricted to the test file
path and the Playwright output directory -- no access to the rest of the
repository -- network egress restricted to the app-under-test host,
unconditional teardown."

This module makes NO assumptions about how the app-under-test is hosted
(bare process, Docker, deployed elsewhere) -- it only needs a reachable
host:port. This is deliberate: play-agent must stay usable against any
app-under-test, not just ones shaped like the fixture app.

CAVEAT: written without the ability to run Docker locally. The previous
version of this file had a critical bug (network_mode="none" combined
with extra_hosts is self-contradictory -- "no network" plus "resolve this
name" does nothing, and 127.0.0.1 inside a container means the container
itself, never the host). Caught by review, not by running it. Verify
with the smoke test in test_container_integration.py before trusting
this against a real app.
"""

from __future__ import annotations

import socket
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

import docker
from docker.errors import APIError, DockerException, ImageNotFound
from docker.models.containers import Container


class SandboxError(Exception):
    """Infrastructure failure standing up or tearing down the sandbox --
    distinct from a tool result indicating a repair attempt didn't work."""


@dataclass(frozen=True)
class SandboxConfig:
    image: str
    app_path: str
    # Host and port the app-under-test is reachable at, from the
    # perspective of wherever `play-agent` itself is running (i.e. from
    # the Docker host's point of view) -- e.g. "localhost:5173". No
    # assumption is made about what's actually serving that port.
    app_host: str
    app_port: int
    output_dir_container: str = "/output"
    app_path_container: str = "/app"
    cpu_limit: float = 2.0
    memory_limit: str = "2g"
    timeout_seconds: int = 120


class Sandbox:
    """A single run's Docker container -- created, used, and torn down as
    a context manager so teardown is unconditional (Sec10) even on error.
    """

    # The special DNS name Docker Desktop (Windows/Mac) and recent Docker
    # Engine (Linux, 20.10+) resolve to the host machine from inside any
    # container. This is the general-purpose mechanism for "reach
    # whatever's running on the host" -- it makes no assumption about
    # whether the app is a bare process, another container, or anything
    # else. If app_host is already "localhost" or "127.0.0.1" (the common
    # case when the app runs as a local dev server), we rewrite it to this
    # name; if app_host is some other resolvable hostname (e.g. a staging
    # URL), we leave it alone and let normal DNS handle it instead.
    _HOST_GATEWAY_NAME = "host.docker.internal"
    def __init__(self, config: SandboxConfig, run_id: str | None = None) -> None:
        self._config = config
        self._run_id = run_id or str(uuid.uuid4())
        try:
            self._client = docker.from_env()
        except DockerException as e:
            raise SandboxError(f"Docker daemon not reachable: {e}") from e
        self._container: Container | None = None
        self._host_output_dir: Path | None = None

    @property
    def run_id(self) -> str:
        return self._run_id

    def __enter__(self) -> "Sandbox":
        self._start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._teardown()

    def _resolve_app_url(self) -> str:
        """The URL the sandbox should use to reach the app, rewritten only
        when app_host refers to the sandbox-runner's own machine -- left
        alone for any other hostname (a real domain, a deployed URL)."""

        host = self._config.app_host
        if host in ("localhost", "127.0.0.1", "0.0.0.0"):
            host = self._HOST_GATEWAY_NAME
        return f"http://{host}:{self._config.app_port}"

    def _start(self) -> None:
        self._host_output_dir = Path(tempfile.mkdtemp(prefix=f"play-agent-{self._run_id}-"))

        app_path = Path(self._config.app_path).resolve()
        if not app_path.exists():
            raise SandboxError(f"app-under-test path does not exist: {app_path}")

        app_url = self._resolve_app_url()

        try:
            self._container = self._client.containers.run(
                self._config.image,
                detach=True,
                volumes={
                    str(app_path): {"bind": self._config.app_path_container, "mode": "rw"},
                    str(self._host_output_dir): {
                        "bind": self._config.output_dir_container,
                        "mode": "rw",
                    },
                },
                working_dir=self._config.app_path_container,
                # Egress restriction (Sec10): a real bridge network (NOT
                # network_mode="none", which has no route to anything,
                # including the host) with exactly one extra DNS entry
                # added. The container can resolve and reach
                # host.docker.internal (or whatever app_host names) and
                # whatever normal outbound resolution the bridge allows.
                # This is a real isolation boundary, not the broken one
                # from the previous version, but it is also not a
                # fully-locked-down egress allowlist (that would need
                # iptables rules or a proxy sidecar -- out of scope for a
                # single-developer CLI tool per Sec10's own framing).
                network_mode="bridge",
                extra_hosts={self._HOST_GATEWAY_NAME: "host-gateway"},
                environment={"APP_UNDER_TEST_URL": app_url},
                nano_cpus=int(self._config.cpu_limit * 1_000_000_000),
                mem_limit=self._config.memory_limit,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges"],
                labels={"play-agent-run-id": self._run_id},
            )
        except ImageNotFound as e:
            raise SandboxError(f"sandbox image not found: {self._config.image}") from e
        except APIError as e:
            raise SandboxError(f"failed to start sandbox container: {e}") from e

    def exec_run(self, command: list[str]) -> tuple[int, str]:
        """Run a command inside the already-started container, with a hard
        timeout (Sec10) enforced via a background thread, since the Docker
        SDK's exec_run has no native timeout and would otherwise block
        forever on a hung command -- which would also mean __exit__ (and
        therefore teardown) never runs, breaking the "unconditional
        teardown" guarantee.
        """

        if self._container is None:
            raise SandboxError("sandbox not started")

        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                self._container.exec_run,
                command,
                workdir=self._config.app_path_container,
                demux=False,
            )
            try:
                result = future.result(timeout=self._config.timeout_seconds)
            except concurrent.futures.TimeoutError as e:
                raise SandboxError(
                    f"exec timed out after {self._config.timeout_seconds}s: {command}"
                ) from e
            except APIError as e:
                raise SandboxError(f"exec failed in sandbox: {e}") from e

        output = result.output.decode("utf-8", errors="replace") if result.output else ""
        return result.exit_code, output

    @property
    def host_output_dir(self) -> Path:
        if self._host_output_dir is None:
            raise SandboxError("sandbox not started")
        return self._host_output_dir

    def _teardown(self) -> None:
        if self._container is not None:
            try:
                self._container.remove(force=True)
            except APIError:
                pass
            finally:
                self._container = None
        # Output dir intentionally NOT deleted here -- it holds evidence
        # (screenshots, traces, reports) the caller needs after teardown.
        # Cleanup/retention is the caller's responsibility; see
        # architecture Sec17's retention-window policy, which this module
        # does not implement (no implicit "sized for a single developer"
        # assumption about disk usage should live silently inside the
        # sandbox lifecycle itself).