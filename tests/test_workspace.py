"""Tests for tools.workspace.PatchWorkspace -- the isolated copy that keeps
the real test file untouched (Sec7)."""

from __future__ import annotations

import pytest

from play_agent.tools.workspace import PatchWorkspace, WorkspaceError


@pytest.fixture
def app(tmp_path):
    root = tmp_path / "app"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "a.spec.ts").write_text("line1\nline2\n")
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref")
    (root / "test-results").mkdir()
    (root / "test-results" / "results.json").write_text("{}")
    return root


def test_copies_the_app_but_skips_ignored_directories(app):
    with PatchWorkspace(app) as ws:
        assert (ws.root / "tests" / "a.spec.ts").read_text() == "line1\nline2\n"
        assert not (ws.root / ".git").exists()
        assert not (ws.root / "test-results").exists()


def test_workspace_is_a_different_directory_from_the_source(app):
    with PatchWorkspace(app) as ws:
        assert ws.root != app.resolve()
        assert not str(ws.root).startswith(str(app.resolve()))


def test_cleanup_removes_the_scratch_directory(app):
    with PatchWorkspace(app) as ws:
        scratch = ws.root.parent
        assert scratch.exists()

    assert not scratch.exists()


def test_cleanup_happens_even_when_the_body_raises(app):
    scratch = None
    with pytest.raises(RuntimeError), PatchWorkspace(app) as ws:
        scratch = ws.root.parent
        raise RuntimeError("boom")

    assert scratch is not None and not scratch.exists()


def test_editing_the_workspace_copy_never_touches_the_original(app):
    with PatchWorkspace(app) as ws:
        ws.workspace_path(app / "tests" / "a.spec.ts").write_text("PATCHED\n")

    assert (app / "tests" / "a.spec.ts").read_text() == "line1\nline2\n"


def test_reset_restores_the_pristine_text(app):
    original = app / "tests" / "a.spec.ts"
    with PatchWorkspace(app) as ws:
        copy = ws.workspace_path(original)
        copy.write_text("a failed candidate's patch\n")

        ws.reset(original)

        assert copy.read_text() == "line1\nline2\n"


def test_relative_returns_a_posix_path_inside_the_app(app):
    with PatchWorkspace(app) as ws:
        assert str(ws.relative(app / "tests" / "a.spec.ts")) == "tests/a.spec.ts"


def test_path_outside_the_app_is_rejected(app, tmp_path):
    elsewhere = tmp_path / "elsewhere.spec.ts"
    elsewhere.write_text("x")

    with PatchWorkspace(app) as ws, pytest.raises(WorkspaceError, match="not inside"):
        ws.relative(elsewhere)


def test_reset_of_a_missing_file_is_an_error(app):
    with PatchWorkspace(app) as ws, pytest.raises(WorkspaceError, match="not found"):
        ws.reset(app / "tests" / "missing.spec.ts")


def test_diff_uses_app_relative_a_b_paths_so_git_apply_works(app):
    original = app / "tests" / "a.spec.ts"
    with PatchWorkspace(app) as ws:
        ws.workspace_path(original).write_text("line1\nCHANGED\n")

        diff = ws.diff(original)

    assert "--- a/tests/a.spec.ts" in diff
    assert "+++ b/tests/a.spec.ts" in diff
    assert "-line2" in diff
    assert "+CHANGED" in diff


def test_diff_is_empty_when_nothing_changed(app):
    original = app / "tests" / "a.spec.ts"
    with PatchWorkspace(app) as ws:
        ws.reset(original)

        assert ws.diff(original) == ""


def test_root_outside_with_block_is_an_error(app):
    ws = PatchWorkspace(app)

    with pytest.raises(WorkspaceError, match="outside its 'with' block"):
        _ = ws.root


def test_missing_source_directory_is_an_error(tmp_path):
    with pytest.raises(WorkspaceError, match="not a directory"), PatchWorkspace(tmp_path / "nope"):
        pass


def test_diff_actually_applies_with_git_apply_from_the_app_root(app):
    import subprocess

    original = app / "tests" / "a.spec.ts"
    subprocess.run(["git", "init", "-q"], cwd=app, check=True)

    with PatchWorkspace(app) as ws:
        ws.workspace_path(original).write_text("line1\nCHANGED\n")
        diff = ws.diff(original)

    subprocess.run(["git", "apply"], cwd=app, input=diff, text=True, check=True)

    assert original.read_text() == "line1\nCHANGED\n"
