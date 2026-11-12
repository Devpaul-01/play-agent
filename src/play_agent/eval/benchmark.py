"""The benchmark scenario set: a fixed list of Mutations against the
real Devpaul-01/fixture app's component source, with ground truth and
the real fixture-app test that exercises each one.

Each scenario here was chosen by inspecting the real fixture app's
*.tsx files AND its tests/*.spec.ts files directly (not guessed): a
testid that (a) appears exactly once in its component file, so
apply_mutation's ambiguity check never rejects it, and (b) is actually
exercised by an existing Playwright test, so the scenario is runnable
end-to-end by the comparison runner, not just by apply_mutation in
isolation. The fixture app's own README documents task-detail-modal as
appearing in two conditional branches, and its new-task-button/
new-task-link are never exercised by the real test suite -- both
excluded from this set for those reasons, not missed.
"""

from __future__ import annotations

from play_agent.eval.mutations import Mutation, MutationType

# component_file is relative to the fixture app's frontend/src
# directory; test_file is relative to its tests/ directory.
BENCHMARK_SCENARIOS: tuple[Mutation, ...] = (
    Mutation(
        mutation_type=MutationType.ATTRIBUTE_RENAME,
        component_file="pages/LoginPage.tsx",
        original_testid="login-username",
        correct_repair_testid="username-field",
        test_file="auth.spec.ts",
        test_title="logs in with valid credentials and lands on the tasks page",
        description="Login username input's data-testid renamed -- a correct repair "
        "should find the input via the new testid, a role-based lookup, or the "
        "associated <label> (the fixture app's own LoginPage.tsx has an explicit "
        "htmlFor='username' label), not just happen to still pass by luck.",
    ),
    Mutation(
        mutation_type=MutationType.ATTRIBUTE_RENAME,
        component_file="pages/LoginPage.tsx",
        original_testid="login-submit",
        correct_repair_testid="auth-submit-button",
        test_file="auth.spec.ts",
        test_title="logs in with valid credentials and lands on the tasks page",
        description="Login submit button's data-testid renamed. The button's visible "
        "text ('Sign in') is unchanged, so a correct repair might reasonably use "
        "getByRole('button', { name: 'Sign in' }) instead of chasing the new testid.",
    ),
    Mutation(
        mutation_type=MutationType.ATTRIBUTE_RENAME,
        component_file="pages/TaskFormPage.tsx",
        original_testid="task-form-submit",
        correct_repair_testid="save-task-button",
        test_file="task-form.spec.ts",
        test_title="creates a new task and lands back on the list with it visible",
        description="Task form's save button testid renamed.",
    ),
    Mutation(
        mutation_type=MutationType.ELEMENT_REMOVAL,
        component_file="pages/TaskFormPage.tsx",
        original_testid="task-title-input",
        correct_repair_testid=None,
        test_file="task-form.spec.ts",
        test_title="creates a new task and lands back on the list with it visible",
        description="Task title input's data-testid removed entirely, with no renamed "
        "replacement anywhere in the file. There is no 'correct' repair here in the "
        "attribute_rename sense -- a well-behaved agent should recognize this as a "
        "genuine defect (the element may have lost its hook for a real reason) and "
        "either fall back to a different stable strategy (a <label> association, if "
        "one exists) or decline to repair, not fabricate a locator that happens to "
        "match something else on the page.",
    ),
)
