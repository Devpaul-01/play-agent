"""Tests for component_source.resolve_component_source.

These run against a local copy of real fixture-app source files
(Devpaul-01/fixture) rather than synthetic snippets, since the whole
point of this module is to resolve against that stack's actual JSX
shape -- a synthetic fixture would risk testing a simplified version
of the problem instead of the real one.
"""

from __future__ import annotations

import pytest

from play_agent.tools.component_source import resolve_component_source

LOGIN_PAGE_TSX = """\
import { useState } from "react";

export function LoginPage() {
  const [username, setUsername] = useState("");

  return (
    <div className="page login-page">
      <div className="login-card">
        <h1>Mini App</h1>
        <form onSubmit={handleSubmit} data-testid="login-form">
          <label htmlFor="username">Username</label>
          <input
            id="username"
            data-testid="login-username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoComplete="username"
          />
          <button data-testid="login-submit" type="submit">
            Sign in
          </button>
        </form>
      </div>
    </div>
  );
}
"""

NAVBAR_TSX = """\
export function NavBar() {
  return (
    <nav className="navbar">
      <div className="navbar-brand">Mini App</div>
      <div className="navbar-links">
        <NavLink to="/tasks">Tasks</NavLink>
        <NavLink to="/tasks/new">New Task</NavLink>
      </div>
      <div className="navbar-user">
        <button className="nav-signout-btn" onClick={handleSignOut}>
          Sign out
        </button>
      </div>
    </nav>
  );
}
"""

TASK_DETAIL_PAGE_TSX = """\
export function TaskDetailPage() {
  return (
    <div>
      {error && (
        <div className="modal-overlay">
          <div className="modal" data-testid="task-detail-modal">
            <p data-testid="task-detail-error">{error}</p>
          </div>
        </div>
      )}
      {task && (
        <div className="modal-overlay">
          <div className="modal" data-testid="task-detail-modal">
            <h2 data-testid="task-detail-title">{task.title}</h2>
            <button data-testid="modal-edit-button">Edit</button>
            <button className="modal-close-text-btn">Close</button>
          </div>
        </div>
      )}
    </div>
  );
}
"""


@pytest.fixture
def frontend_src(tmp_path):
    src = tmp_path / "frontend" / "src"
    (src / "pages").mkdir(parents=True)
    (src / "components").mkdir(parents=True)
    (src / "pages" / "LoginPage.tsx").write_text(LOGIN_PAGE_TSX)
    (src / "pages" / "TaskDetailPage.tsx").write_text(TASK_DETAIL_PAGE_TSX)
    (src / "components" / "NavBar.tsx").write_text(NAVBAR_TSX)
    return src


def test_resolves_stable_testid_locator(frontend_src):
    result = resolve_component_source('page.getByTestId("login-username")', frontend_src)

    assert result.resolved is True
    assert len(result.matches) == 1
    assert result.matches[0].file_path.endswith("LoginPage.tsx")
    assert 'data-testid="login-username"' in result.matches[0].snippet


def test_testid_match_is_exact_not_substring(frontend_src):
    # A search for "login-username" must not also match a hypothetical
    # "login-username-confirm" field -- exact attribute-value match only.
    (frontend_src / "pages" / "Extra.tsx").write_text(
        '<input data-testid="login-username-confirm" />'
    )

    result = resolve_component_source('page.getByTestId("login-username")', frontend_src)

    assert len(result.matches) == 1
    assert result.matches[0].file_path.endswith("LoginPage.tsx")


def test_resolves_fragile_text_locator_via_role_name(frontend_src):
    # page.getByRole("button", { name: "Sign out" }) -- the fragile
    # NavBar case with no data-testid at all, per the fixture spec.
    result = resolve_component_source(
        'page.getByRole("button", { name: "Sign out" }).click()', frontend_src
    )

    assert result.resolved is True
    assert result.matches[0].file_path.endswith("NavBar.tsx")
    assert "Sign out" in result.matches[0].snippet


def test_resolves_fragile_text_locator_via_get_by_text(frontend_src):
    result = resolve_component_source('page.getByText("New Task")', frontend_src)

    assert result.resolved is True
    assert result.matches[0].file_path.endswith("NavBar.tsx")


def test_duplicate_testid_across_conditional_branches_is_ambiguous(frontend_src):
    # TaskDetailPage.tsx's two modal branches (error vs. loaded task)
    # both render data-testid="task-detail-modal" -- a real case from
    # the actual fixture app, not a contrived one.
    result = resolve_component_source('page.getByTestId("task-detail-modal")', frontend_src)

    assert result.resolved is True
    assert result.ambiguous is True
    assert len(result.matches) == 2
    assert all(m.file_path.endswith("TaskDetailPage.tsx") for m in result.matches)


def test_unresolvable_target_returns_no_matches(frontend_src):
    result = resolve_component_source('page.getByTestId("does-not-exist-anywhere")', frontend_src)

    assert result.resolved is False
    assert result.matches == ()


def test_unparseable_locator_expression_returns_no_matches(frontend_src):
    # A locator this module doesn't know how to extract a target from
    # (e.g. a chained/dynamic expression) should fail closed, not raise.
    result = resolve_component_source("page.locator('.some-css-class')", frontend_src)

    assert result.resolved is False


def test_snippet_includes_surrounding_context_not_just_the_matching_line(frontend_src):
    result = resolve_component_source('page.getByTestId("login-submit")', frontend_src)

    snippet = result.matches[0].snippet
    assert 'data-testid="login-submit"' in snippet
    # Surrounding JSX (the enclosing <button> tag's other lines) should
    # also be present -- not just the single matching line in isolation.
    assert "Sign in" in snippet


def test_searches_nested_directories_recursively(frontend_src):
    result = resolve_component_source('page.getByTestId("login-form")', frontend_src)

    assert result.resolved is True
    assert "pages" in result.matches[0].file_path
