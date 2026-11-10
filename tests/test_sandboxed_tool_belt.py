"""Tests for SandboxedToolBelt -- the concrete ToolBelt wiring
collect_failure_evidence, apply_patch, and run_verification to a real
Sandbox for the Driver.

Mocks the Docker client, same as test_sandbox_container.py -- these
confirm the wiring (right methods called with the right arguments, in
the right lifecycle order), not that a real container actually starts.
That proof is test_container_integration.py's job.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from play_agent.sandbox.container import SandboxConfig
from play_agent.tools.sandboxed_tool_belt import SandboxedToolBelt, SandboxedToolBeltConfig


@pytest.fixture
def sandbox_config(tmp_path):
    app_dir = tmp_path / "fixture-app"
    app_dir.mkdir()
    return SandboxConfig(
        image="play-agent-fixture:latest",
        app_path=str(app_dir),
        app_host="localhost",
        app_port=5173,
    )


@pytest.fixture
def mock_docker_container():
    mock_container = MagicMock()
    mock_container.status = "running"  # satisfies Sandbox._wait_until_running
    return mock_container


def _patched_client(mock_container):
    mock_client = MagicMock()
    mock_client.containers.run.return_value = mock_container
    return patch("play_agent.sandbox.container.docker.from_env", return_value=mock_client)


def test_run_verification_outside_with_block_raises(sandbox_config):
    # No Docker patch needed -- constructing a belt must not touch Docker
    # at all until __enter__, which is exactly what this test confirms.
    belt = SandboxedToolBelt(SandboxedToolBeltConfig(sandbox_config=sandbox_config))

    with pytest.raises(RuntimeError, match="outside its 'with' block"):
        belt.run_verification("checkout.spec.ts", "completes checkout")


def test_run_verification_uses_the_sandbox(sandbox_config, mock_docker_container):
    mock_docker_container.exec_run.return_value = MagicMock(exit_code=0, output=b"1 passed")

    with _patched_client(mock_docker_container), SandboxedToolBelt(
        SandboxedToolBeltConfig(sandbox_config=sandbox_config, verification_reps=1)
    ) as belt:
        result = belt.run_verification("checkout.spec.ts", "completes checkout", reps=1)

    assert result.outcome == "pass"
    mock_docker_container.exec_run.assert_called_once()


def test_run_verification_reps_argument_overrides_config_default(sandbox_config, mock_docker_container):
    mock_docker_container.exec_run.return_value = MagicMock(exit_code=0, output=b"1 passed")

    with _patched_client(mock_docker_container), SandboxedToolBelt(
        SandboxedToolBeltConfig(sandbox_config=sandbox_config, verification_reps=3)
    ) as belt:
        result = belt.run_verification("checkout.spec.ts", "completes checkout", reps=1)

    assert result.reps_run == 1
    assert mock_docker_container.exec_run.call_count == 1


def test_collect_failure_evidence_does_not_require_started_sandbox(sandbox_config, tmp_path):
    # Unsandboxed by design -- should work even before __enter__, with no
    # Docker patch needed since construction never touches Docker.
    report = tmp_path / "results.json"
    report.write_text(
        json.dumps(
            {
                "suites": [
                    {
                        "file": "checkout.spec.ts",
                        "specs": [
                            {
                                "title": "completes checkout",
                                "tests": [
                                    {
                                        "results": [
                                            {
                                                "status": "failed",
                                                "error": {"message": "boom", "stack": ""},
                                                "attachments": [],
                                            }
                                        ]
                                    }
                                ],
                            }
                        ],
                    }
                ]
            }
        )
    )
    test_file = tmp_path / "checkout.spec.ts"
    test_file.write_text("// test file")

    belt = SandboxedToolBelt(
        SandboxedToolBeltConfig(sandbox_config=sandbox_config, report_path=str(report))
    )
    evidence = belt.collect_failure_evidence(str(test_file), "completes checkout")

    assert evidence.error_message == "boom"


def test_apply_patch_does_not_require_started_sandbox(sandbox_config, tmp_path):
    test_file = tmp_path / "checkout.spec.ts"
    test_file.write_text(
        "import { test } from '@playwright/test';\n"
        "test('t', async ({ page }) => {\n"
        "  await page.getByTestId('x').click();\n"
        "});\n"
    )

    from play_agent.tools.patch_validation import find_locator_calls

    source = test_file.read_text()
    target = find_locator_calls(source)[0]

    belt = SandboxedToolBelt(SandboxedToolBeltConfig(sandbox_config=sandbox_config))
    result = belt.apply_patch(str(test_file), target.locator_call_id, 'page.getByRole("button")')

    assert result.applied is True


def test_context_manager_starts_and_tears_down_sandbox(sandbox_config, mock_docker_container):
    with _patched_client(mock_docker_container), SandboxedToolBelt(
        SandboxedToolBeltConfig(sandbox_config=sandbox_config)
    ):
        pass

    mock_docker_container.remove.assert_called_once_with(force=True)
