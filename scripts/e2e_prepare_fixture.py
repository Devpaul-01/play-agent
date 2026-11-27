"""Prepare a mutated copy of the fixture app for the end-to-end CI test.

Copies the fixture app to DEST, then renames one data-testid in the copy's
frontend source (login-username -> username-field). The copy's Playwright
test "shows an error on invalid credentials" then fails on a stale
locator, which is exactly what play-agent is meant to repair.

Usage: python scripts/e2e_prepare_fixture.py <fixture_root> <dest>
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from play_agent.eval.mutations import Mutation, MutationType, apply_mutation

MUTATION = Mutation(
    mutation_type=MutationType.ATTRIBUTE_RENAME,
    component_file="pages/LoginPage.tsx",
    original_testid="login-username",
    correct_repair_testid="username-field",
    test_file="auth.spec.ts",
    test_title="shows an error on invalid credentials",
    description="Login username input's data-testid renamed.",
)


def main(fixture_root: str, dest: str) -> int:
    dest_path = Path(dest)
    if dest_path.exists():
        shutil.rmtree(dest_path)
    shutil.copytree(fixture_root, dest_path, symlinks=True)

    result = apply_mutation(MUTATION, dest_path / "frontend" / "src")
    if not result.applied:
        print(f"mutation not applied: {result.reason}", file=sys.stderr)
        return 1
    print(f"mutated {MUTATION.component_file}: {MUTATION.original_testid} -> "
          f"{MUTATION.correct_repair_testid}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1], sys.argv[2]))
