"""Tests for eval.mutations.

Uses a locally-written copy of the real fixture app's LoginPage.tsx /
TaskFormPage.tsx / TasksPage.tsx content (not committed as a fixture
submodule, since this repo doesn't depend on that one at runtime) so
mutation application is tested against the actual JSX shape, the same
approach test_component_source.py already takes.
"""

from __future__ import annotations

import pytest

from play_agent.eval.mutations import (
    Mutation,
    MutationType,
    apply_mutation,
    prepare_mutated_copy,
)

LOGIN_PAGE_TSX = """\
export function LoginPage() {
  return (
    <form onSubmit={handleSubmit} data-testid="login-form">
      <label htmlFor="username">Username</label>
      <input
        id="username"
        data-testid="login-username"
        autoComplete="username"
      />
      <button type="submit" data-testid="login-submit" disabled={submitting}>
        Sign in
      </button>
    </form>
  );
}
"""

TASK_DETAIL_PAGE_TSX = """\
export function TaskDetailPage() {
  return (
    <div>
      {error && (
        <div className="modal" data-testid="task-detail-modal">
          <p data-testid="task-detail-error">{error}</p>
        </div>
      )}
      {task && (
        <div className="modal" data-testid="task-detail-modal">
          <h2 data-testid="task-detail-title">{task.title}</h2>
        </div>
      )}
    </div>
  );
}
"""


@pytest.fixture
def real_src_root(tmp_path):
    root = tmp_path / "real-src"
    (root / "pages").mkdir(parents=True)
    (root / "pages" / "LoginPage.tsx").write_text(LOGIN_PAGE_TSX)
    (root / "pages" / "TaskDetailPage.tsx").write_text(TASK_DETAIL_PAGE_TSX)
    return root


def test_prepare_mutated_copy_does_not_touch_the_original(real_src_root, tmp_path):
    dest = tmp_path / "mutated-copy"

    prepare_mutated_copy(real_src_root, dest)

    original_content = (real_src_root / "pages" / "LoginPage.tsx").read_text()
    copy_content = (dest / "pages" / "LoginPage.tsx").read_text()
    assert original_content == copy_content
    assert original_content == LOGIN_PAGE_TSX  # unchanged


def test_attribute_rename_applies_cleanly(real_src_root, tmp_path):
    mutated = prepare_mutated_copy(real_src_root, tmp_path / "mutated")
    mutation = Mutation(
        mutation_type=MutationType.ATTRIBUTE_RENAME,
        component_file="pages/LoginPage.tsx",
        original_testid="login-username",
        correct_repair_testid="username-field",
        description="test",
    )

    result = apply_mutation(mutation, mutated)

    assert result.applied is True
    mutated_source = (mutated / "pages" / "LoginPage.tsx").read_text()
    assert 'data-testid="username-field"' in mutated_source
    assert 'data-testid="login-username"' not in mutated_source

    # The original copy passed to prepare_mutated_copy is untouched --
    # confirms apply_mutation only ever writes into the mutated copy.
    original_source = (real_src_root / "pages" / "LoginPage.tsx").read_text()
    assert 'data-testid="login-username"' in original_source


def test_attribute_rename_preserves_everything_else_in_the_file(real_src_root, tmp_path):
    mutated = prepare_mutated_copy(real_src_root, tmp_path / "mutated")
    mutation = Mutation(
        mutation_type=MutationType.ATTRIBUTE_RENAME,
        component_file="pages/LoginPage.tsx",
        original_testid="login-username",
        correct_repair_testid="username-field",
        description="test",
    )

    apply_mutation(mutation, mutated)

    mutated_source = (mutated / "pages" / "LoginPage.tsx").read_text()
    # The other testids on the same page must be untouched.
    assert 'data-testid="login-form"' in mutated_source
    assert 'data-testid="login-submit"' in mutated_source
    assert "Sign in" in mutated_source


def test_element_removal_strips_the_attribute_entirely(real_src_root, tmp_path):
    mutated = prepare_mutated_copy(real_src_root, tmp_path / "mutated")
    mutation = Mutation(
        mutation_type=MutationType.ELEMENT_REMOVAL,
        component_file="pages/LoginPage.tsx",
        original_testid="login-username",
        correct_repair_testid=None,
        description="test",
    )

    result = apply_mutation(mutation, mutated)

    assert result.applied is True
    mutated_source = (mutated / "pages" / "LoginPage.tsx").read_text()
    assert 'data-testid="login-username"' not in mutated_source
    assert 'id="username"' in mutated_source  # the element itself remains, just unhooked
    # The other testids on the page (login-form, login-submit) must be
    # untouched -- this mutation targets exactly one attribute.
    assert 'data-testid="login-form"' in mutated_source
    assert 'data-testid="login-submit"' in mutated_source


def test_rename_missing_testid_fails_without_writing(real_src_root, tmp_path):
    mutated = prepare_mutated_copy(real_src_root, tmp_path / "mutated")
    mutation = Mutation(
        mutation_type=MutationType.ATTRIBUTE_RENAME,
        component_file="pages/LoginPage.tsx",
        original_testid="does-not-exist",
        correct_repair_testid="whatever",
        description="test",
    )

    result = apply_mutation(mutation, mutated)

    assert result.applied is False
    assert "not found" in result.reason


def test_rename_ambiguous_duplicate_testid_fails_closed(real_src_root, tmp_path):
    # TaskDetailPage.tsx's real duplicate -- task-detail-modal appears
    # in both conditional branches. Must refuse rather than rename both
    # (which would silently change more than the one intended element)
    # or guess which one was meant.
    mutated = prepare_mutated_copy(real_src_root, tmp_path / "mutated")
    mutation = Mutation(
        mutation_type=MutationType.ATTRIBUTE_RENAME,
        component_file="pages/TaskDetailPage.tsx",
        original_testid="task-detail-modal",
        correct_repair_testid="whatever",
        description="test",
    )

    result = apply_mutation(mutation, mutated)

    assert result.applied is False
    assert "ambiguous" in result.reason

    # Confirm neither occurrence was touched.
    mutated_source = (mutated / "pages" / "TaskDetailPage.tsx").read_text()
    assert mutated_source.count('data-testid="task-detail-modal"') == 2


def test_rename_missing_correct_repair_testid_fails(real_src_root, tmp_path):
    mutated = prepare_mutated_copy(real_src_root, tmp_path / "mutated")
    mutation = Mutation(
        mutation_type=MutationType.ATTRIBUTE_RENAME,
        component_file="pages/LoginPage.tsx",
        original_testid="login-username",
        correct_repair_testid=None,  # invalid for this mutation type
        description="test",
    )

    result = apply_mutation(mutation, mutated)

    assert result.applied is False
    assert "requires correct_repair_testid" in result.reason


def test_nonexistent_component_file_fails_without_raising(real_src_root, tmp_path):
    mutated = prepare_mutated_copy(real_src_root, tmp_path / "mutated")
    mutation = Mutation(
        mutation_type=MutationType.ATTRIBUTE_RENAME,
        component_file="pages/DoesNotExist.tsx",
        original_testid="x",
        correct_repair_testid="y",
        description="test",
    )

    result = apply_mutation(mutation, mutated)

    assert result.applied is False
    assert "not found" in result.reason
