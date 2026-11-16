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


SPEC_SOURCE = (
    "import { test } from '@playwright/test';\n"
    "test('t', async ({ page }) => {\n"
    "  await page.getByTestId('x').click();\n"
    "});\n"
)


@pytest.fixture
def app_spec(sandbox_config):
    """A spec file inside the app under test, plus its locator call id."""

    from pathlib import Path

    from play_agent.tools.patch_validation import find_locator_calls

    spec = Path(sandbox_config.app_path) / "tests" / "checkout.spec.ts"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text(SPEC_SOURCE)
    return spec, find_locator_calls(SPEC_SOURCE)[0].locator_call_id


def _belt(sandbox_config, **overrides):
    return SandboxedToolBelt(SandboxedToolBeltConfig(sandbox_config=sandbox_config, **overrides))


def test_run_verification_outside_with_block_raises(sandbox_config):
    # No Docker patch needed -- constructing a belt must not touch Docker
    # at all until __enter__, which is exactly what this test confirms.
    belt = SandboxedToolBelt(SandboxedToolBeltConfig(sandbox_config=sandbox_config))

    with pytest.raises(RuntimeError, match="outside its 'with' block"):
        belt.run_verification("checkout.spec.ts", "completes checkout")


def test_run_verification_uses_the_sandbox(sandbox_config, mock_docker_container, app_spec):
    spec, _ = app_spec
    mock_docker_container.exec_run.return_value = MagicMock(exit_code=0, output=b"1 passed")

    with _patched_client(mock_docker_container), _belt(sandbox_config, verification_reps=1) as belt:
        result = belt.run_verification(str(spec), "completes checkout", reps=1)

    assert result.outcome == "pass"
    mock_docker_container.exec_run.assert_called_once()


def test_run_verification_reps_argument_overrides_config_default(
    sandbox_config, mock_docker_container, app_spec
):
    spec, _ = app_spec
    mock_docker_container.exec_run.return_value = MagicMock(exit_code=0, output=b"1 passed")

    with _patched_client(mock_docker_container), _belt(sandbox_config, verification_reps=3) as belt:
        result = belt.run_verification(str(spec), "completes checkout", reps=1)

    assert result.reps_run == 1
    assert mock_docker_container.exec_run.call_count == 1


def test_run_verification_without_reps_uses_the_configured_default(
    sandbox_config, mock_docker_container, app_spec
):
    # The Driver calls run_verification(path, title) with no reps, so the
    # CLI's --reps (carried in the config) must apply. An earlier version
    # defaulted reps to 3, which silently overrode the configured value.
    spec, _ = app_spec
    mock_docker_container.exec_run.return_value = MagicMock(exit_code=0, output=b"ok")

    with _patched_client(mock_docker_container), _belt(sandbox_config, verification_reps=2) as belt:
        result = belt.run_verification(str(spec), "completes checkout")

    assert result.reps_run == 2
    assert mock_docker_container.exec_run.call_count == 2


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


def test_apply_patch_outside_with_block_raises_rather_than_editing_the_real_file(
    sandbox_config, app_spec
):
    spec, call_id = app_spec

    with pytest.raises(RuntimeError, match="no isolated workspace"):
        _belt(sandbox_config).apply_patch(str(spec), call_id, 'page.getByRole("button")')

    assert spec.read_text() == SPEC_SOURCE


def test_apply_patch_never_modifies_the_real_file(sandbox_config, mock_docker_container, app_spec):
    spec, call_id = app_spec

    with _patched_client(mock_docker_container), _belt(sandbox_config) as belt:
        result = belt.apply_patch(str(spec), call_id, 'page.getByRole("button")')

    assert result.applied is True
    assert spec.read_text() == SPEC_SOURCE


def test_apply_patch_diff_uses_app_relative_paths(sandbox_config, mock_docker_container, app_spec):
    spec, call_id = app_spec

    with _patched_client(mock_docker_container), _belt(sandbox_config) as belt:
        result = belt.apply_patch(str(spec), call_id, 'page.getByRole("button")')

    assert "--- a/tests/checkout.spec.ts" in result.diff
    assert "+++ b/tests/checkout.spec.ts" in result.diff
    assert 'getByRole("button")' in result.diff


def test_second_candidate_is_applied_to_the_pristine_file_not_on_top_of_the_first(
    sandbox_config, mock_docker_container, app_spec
):
    # The Driver never reverts a failed candidate, so the belt must start
    # every apply from the pristine file.
    spec, call_id = app_spec

    with _patched_client(mock_docker_container), _belt(sandbox_config) as belt:
        belt.apply_patch(str(spec), call_id, 'page.getByTestId("first-wrong-guess")')
        second = belt.apply_patch(str(spec), call_id, 'page.getByRole("button")')

    assert "first-wrong-guess" not in second.diff
    assert 'getByRole("button")' in second.diff


def test_rejected_patch_leaves_the_real_and_workspace_files_pristine(
    sandbox_config, mock_docker_container, app_spec
):
    spec, call_id = app_spec

    with _patched_client(mock_docker_container), _belt(sandbox_config) as belt:
        result = belt.apply_patch(str(spec), call_id, "eval('boom')")

        assert result.applied is False
        assert belt._workspace.workspace_path(spec).read_text() == SPEC_SOURCE

    assert spec.read_text() == SPEC_SOURCE


def test_sandbox_mounts_the_scratch_copy_not_the_real_app(
    sandbox_config, mock_docker_container
):
    mock_client = MagicMock()
    mock_client.containers.run.return_value = mock_docker_container

    with (
        patch("play_agent.sandbox.container.docker.from_env", return_value=mock_client),
        _belt(sandbox_config),
    ):
        volumes = mock_client.containers.run.call_args.kwargs["volumes"]

    from pathlib import Path

    mounted_sources = {str(Path(source).resolve()) for source in volumes}
    assert str(Path(sandbox_config.app_path).resolve()) not in mounted_sources


def test_verification_passes_an_app_relative_path_into_the_container(
    sandbox_config, mock_docker_container, app_spec
):
    spec, _ = app_spec
    mock_docker_container.exec_run.return_value = MagicMock(exit_code=0, output=b"ok")

    with _patched_client(mock_docker_container), _belt(sandbox_config) as belt:
        belt.run_verification(str(spec), "completes checkout", reps=1)

    command = mock_docker_container.exec_run.call_args.args[0]
    assert "tests/checkout.spec.ts" in command
    assert str(spec) not in command  # the host's absolute path means nothing in the container


def test_spec_outside_the_app_is_a_tool_error(sandbox_config, mock_docker_container, tmp_path):
    from play_agent.tools.contracts import ToolError

    elsewhere = tmp_path / "elsewhere.spec.ts"
    elsewhere.write_text(SPEC_SOURCE)

    with _patched_client(mock_docker_container), _belt(sandbox_config) as belt:
        with pytest.raises(ToolError, match="not inside the app"):
            belt.apply_patch(str(elsewhere), "L3C9", 'page.getByRole("button")')
        with pytest.raises(ToolError, match="not inside the app"):
            belt.run_verification(str(elsewhere), "t")


def test_sandbox_failure_during_verification_becomes_a_tool_error(
    sandbox_config, mock_docker_container, app_spec
):
    from play_agent.sandbox.container import SandboxError
    from play_agent.tools.contracts import ToolError

    spec, _ = app_spec

    with (
        _patched_client(mock_docker_container),
        _belt(sandbox_config) as belt,
        patch(
            "play_agent.tools.sandboxed_tool_belt.run_verification",
            side_effect=SandboxError("exec timed out"),
        ),
        pytest.raises(ToolError, match="sandbox failure during verification"),
    ):
        belt.run_verification(str(spec), "t")


def test_workspace_is_removed_on_exit(sandbox_config, mock_docker_container):
    with _patched_client(mock_docker_container), _belt(sandbox_config) as belt:
        scratch = belt._workspace.root.parent
        assert scratch.exists()

    assert not scratch.exists()


def test_workspace_is_removed_if_the_sandbox_fails_to_start(sandbox_config):
    from play_agent.sandbox.container import SandboxError

    created = []
    real_enter = __import__("play_agent.tools.workspace", fromlist=["PatchWorkspace"]).PatchWorkspace.__enter__

    def spy_enter(self):
        result = real_enter(self)
        created.append(result.root.parent)
        return result

    with patch("play_agent.tools.workspace.PatchWorkspace.__enter__", spy_enter), patch(
        "play_agent.tools.sandboxed_tool_belt.Sandbox"
    ) as sandbox_cls:
        sandbox_cls.return_value.__enter__.side_effect = SandboxError("daemon down")
        with pytest.raises(SandboxError):
            _belt(sandbox_config).__enter__()

    assert created and not created[0].exists()


def test_context_manager_starts_and_tears_down_sandbox(sandbox_config, mock_docker_container):
    with _patched_client(mock_docker_container), SandboxedToolBelt(
        SandboxedToolBeltConfig(sandbox_config=sandbox_config)
    ):
        pass

    mock_docker_container.remove.assert_called_once_with(force=True)


def test_collect_failure_evidence_without_component_src_root_leaves_context_none(
    sandbox_config, tmp_path
):
    # component_src_root defaults to None -- evidence collection must
    # not be affected at all when it's unset.
    report = tmp_path / "results.json"
    report.write_text(
        json.dumps(
            {
                "suites": [
                    {
                        "file": "checkout.spec.ts",
                        "specs": [
                            {
                                "title": "t",
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
    test_file.write_text('page.getByTestId("login-username").fill("x");')

    belt = SandboxedToolBelt(
        SandboxedToolBeltConfig(sandbox_config=sandbox_config, report_path=str(report))
    )
    evidence = belt.collect_failure_evidence(str(test_file), "t")

    assert evidence.component_source_context is None


def test_collect_failure_evidence_resolves_component_source_when_configured(
    sandbox_config, tmp_path
):
    report = tmp_path / "results.json"
    report.write_text(
        json.dumps(
            {
                "suites": [
                    {
                        "file": "checkout.spec.ts",
                        "specs": [
                            {
                                "title": "t",
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
    test_file.write_text('page.getByTestId("login-username").fill("x");')

    component_src = tmp_path / "frontend" / "src"
    component_src.mkdir(parents=True)
    (component_src / "LoginPage.tsx").write_text(
        '<input data-testid="login-username" value={username} />'
    )

    belt = SandboxedToolBelt(
        SandboxedToolBeltConfig(
            sandbox_config=sandbox_config,
            report_path=str(report),
            component_src_root=str(component_src),
        )
    )
    evidence = belt.collect_failure_evidence(str(test_file), "t")

    assert evidence.component_source_context is not None
    assert "login-username" in evidence.component_source_context
    assert "LoginPage.tsx" in evidence.component_source_context


def test_collect_failure_evidence_unresolvable_locator_leaves_context_none(
    sandbox_config, tmp_path
):
    report = tmp_path / "results.json"
    report.write_text(
        json.dumps(
            {
                "suites": [
                    {
                        "file": "checkout.spec.ts",
                        "specs": [
                            {
                                "title": "t",
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
    test_file.write_text('page.getByTestId("nonexistent-testid").fill("x");')

    component_src = tmp_path / "frontend" / "src"
    component_src.mkdir(parents=True)
    (component_src / "LoginPage.tsx").write_text('<input data-testid="login-username" />')

    belt = SandboxedToolBelt(
        SandboxedToolBeltConfig(
            sandbox_config=sandbox_config,
            report_path=str(report),
            component_src_root=str(component_src),
        )
    )
    evidence = belt.collect_failure_evidence(str(test_file), "t")

    assert evidence.component_source_context is None
