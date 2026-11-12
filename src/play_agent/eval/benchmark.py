"""The benchmark scenario set: a fixed list of Mutations against the
real Devpaul-01/fixture app's component source, with ground truth.

Each scenario here was chosen by inspecting the real fixture app's
*.tsx files directly (not guessed) for a data-testid attribute that
appears exactly once in its file, so apply_mutation's ambiguity check
never rejects it. The fixture app's own README documents
task-detail-modal as appearing in two conditional branches -- that one
is deliberately excluded from this scenario set for exactly that
reason, not because it was missed.
"""

from __future__ import annotations

from play_agent.eval.mutations import Mutation, MutationType

# Relative to the fixture app's frontend/src directory.
BENCHMARK_SCENARIOS: tuple[Mutation, ...] = (
    Mutation(
        mutation_type=MutationType.ATTRIBUTE_RENAME,
        component_file="pages/LoginPage.tsx",
        original_testid="login-username",
        correct_repair_testid="username-field",
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
        description="Login submit button's data-testid renamed. The button's visible "
        "text ('Sign in') is unchanged, so a correct repair might reasonably use "
        "getByRole('button', { name: 'Sign in' }) instead of chasing the new testid.",
    ),
    Mutation(
        mutation_type=MutationType.ATTRIBUTE_RENAME,
        component_file="pages/TaskFormPage.tsx",
        original_testid="task-form-submit",
        correct_repair_testid="save-task-button",
        description="Task form's save button testid renamed.",
    ),
    Mutation(
        mutation_type=MutationType.ATTRIBUTE_RENAME,
        component_file="pages/TasksPage.tsx",
        original_testid="new-task-button",
        correct_repair_testid="create-task-button",
        description="The '+ New Task' button's testid renamed -- visible text unchanged.",
    ),
    Mutation(
        mutation_type=MutationType.ELEMENT_REMOVAL,
        component_file="pages/TaskFormPage.tsx",
        original_testid="task-title-input",
        correct_repair_testid=None,
        description="Task title input's data-testid removed entirely, with no renamed "
        "replacement anywhere in the file. There is no 'correct' repair here in the "
        "attribute_rename sense -- a well-behaved agent should recognize this as a "
        "genuine defect (the element may have lost its hook for a real reason) and "
        "either fall back to a different stable strategy (a <label> association, if "
        "one exists) or decline to repair, not fabricate a locator that happens to "
        "match something else on the page.",
    ),
)
