"""Synthetic benchmark generator: controlled mutations of a known-passing
component source, with ground truth, per architecture Sec16.

Sec16: "primarily synthetic -- controlled mutations of a known-passing
test suite's locators (attribute rename, DOM restructure, sibling
reorder, element removal), giving known ground truth for false-repair
measurement." This module implements two of those four mutation types
concretely -- attribute rename and element removal -- against the real
fixture app's actual JSX. DOM restructure and sibling reorder are
documented as a deliberately deferred follow-up (see the module-level
NOT_YET_IMPLEMENTED set below), not silently missing: Sec16's own
"deferred to future work" section already accepts a smaller first
version of the generator over a larger undone one.

A mutation targets one data-testid attribute in one component source
file. Ground truth is the pair (original value, correct repair target)
-- knowing the original value is what makes false-repair measurable at
all (Sec16: "verification passed but the locator resolved to the wrong
element -- requires ground-truth labels, which is why the synthetic
benchmark matters"). Mutations are applied to a COPY of the component
source, never the original -- see apply_mutation's docstring.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class MutationType(str, Enum):
    ATTRIBUTE_RENAME = "attribute_rename"
    ELEMENT_REMOVAL = "element_removal"
    # Deferred (Sec16's own "deferred to future work," not an oversight
    # here): DOM_RESTRUCTURE and SIBLING_REORDER need a real JSX AST
    # transform to do safely -- a regex/text mutation risks producing
    # invalid JSX for anything beyond a single attribute's value, which
    # would make the "known-passing" half of "controlled mutations of a
    # known-passing test suite" untrue before the mutation is even applied.


@dataclass(frozen=True)
class Mutation:
    """One mutation: where it applies, what it changes, and the ground
    truth a benchmark run checks a repair against."""

    mutation_type: MutationType
    component_file: str  # relative to the component source root
    original_testid: str
    description: str
    # The value a CORRECT repair should resolve to. For attribute_rename,
    # this is the new value the mutation introduced (the locator should
    # be repaired to find it there). For element_removal, there is no
    # correct non-empty repair -- a correctly-behaving agent should
    # reject the repair, not propose one, so this is None.
    correct_repair_testid: str | None


@dataclass(frozen=True)
class MutationResult:
    mutation: Mutation
    applied: bool
    reason: str | None = None


def apply_mutation(mutation: Mutation, mutated_src_root: str | Path) -> MutationResult:
    """Apply one mutation to the component source tree rooted at
    mutated_src_root, IN PLACE at that root.

    mutated_src_root must already be a copy of the real component
    source (callers are expected to shutil.copytree the real fixture
    app's frontend/src before calling this, exactly once per mutation
    they want isolated) -- this function does not copy anything itself,
    since a benchmark run typically wants one mutation applied to one
    fresh copy, not N mutations accumulating on the same tree.
    """

    target = Path(mutated_src_root) / mutation.component_file
    if not target.exists():
        return MutationResult(
            mutation=mutation, applied=False, reason=f"component file not found: {target}"
        )

    source = target.read_text()

    if mutation.mutation_type == MutationType.ATTRIBUTE_RENAME:
        return _apply_attribute_rename(mutation, target, source)

    if mutation.mutation_type == MutationType.ELEMENT_REMOVAL:
        return _apply_element_removal(mutation, target, source)

    return MutationResult(
        mutation=mutation,
        applied=False,
        reason=f"mutation type {mutation.mutation_type} not yet implemented",
    )


def _apply_attribute_rename(mutation: Mutation, target: Path, source: str) -> MutationResult:
    if mutation.correct_repair_testid is None:
        return MutationResult(
            mutation=mutation,
            applied=False,
            reason="attribute_rename requires correct_repair_testid (the new value)",
        )

    pattern = rf'data-testid=(["\']){ re.escape(mutation.original_testid) }\1'
    new_text, count = re.subn(
        pattern, f'data-testid=\\1{mutation.correct_repair_testid}\\1', source
    )

    if count == 0:
        return MutationResult(
            mutation=mutation,
            applied=False,
            reason=f"original_testid {mutation.original_testid!r} not found in {target}",
        )
    if count > 1:
        # Ambiguous target -- renaming every occurrence would silently
        # change more than the one element this mutation is meant to
        # isolate (e.g. TaskDetailPage.tsx's duplicate
        # task-detail-modal across conditional branches). Fail closed
        # rather than guess which occurrence was intended.
        return MutationResult(
            mutation=mutation,
            applied=False,
            reason=f"original_testid {mutation.original_testid!r} matched {count} times "
            f"in {target} -- ambiguous, not applied",
        )

    target.write_text(new_text)
    return MutationResult(mutation=mutation, applied=True)


def _apply_element_removal(mutation: Mutation, target: Path, source: str) -> MutationResult:
    pattern = rf'\s*data-testid=["\']{ re.escape(mutation.original_testid) }["\']'
    new_text, count = re.subn(pattern, "", source)

    if count == 0:
        return MutationResult(
            mutation=mutation,
            applied=False,
            reason=f"original_testid {mutation.original_testid!r} not found in {target}",
        )
    if count > 1:
        return MutationResult(
            mutation=mutation,
            applied=False,
            reason=f"original_testid {mutation.original_testid!r} matched {count} times "
            f"in {target} -- ambiguous, not applied",
        )

    target.write_text(new_text)
    return MutationResult(mutation=mutation, applied=True)


def prepare_mutated_copy(real_src_root: str | Path, dest_root: str | Path) -> Path:
    """Copy the real component source tree to dest_root, for one
    mutation to be applied to. A thin wrapper around shutil.copytree,
    named for what it's for in a benchmark run rather than left as a
    bare stdlib call at every call site, and so a future switch to a
    different copy mechanism (e.g. skipping node_modules-adjacent
    files) has exactly one place to change.
    """

    dest = Path(dest_root)
    shutil.copytree(real_src_root, dest)
    return dest
