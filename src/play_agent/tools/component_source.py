"""Component-source evidence resolution (architecture Sec11/Sec2, milestone M3).

Sec11 lists "relevant component source file(s), read-only, resolved
deterministically" as evidence included per diagnosis call, and Sec2
explicitly flagged the resolution mechanism as an open implementation
detail pending the fixture app's actual frontend stack. That stack is
now a known quantity (Devpaul-01/fixture: React + TypeScript via Vite,
plain JSX, no SSR) -- this module resolves against exactly that shape.

Resolution strategy: structural matching, not source-map/stack-trace
resolution. There's no running bundled app to unwind a source map
against here -- what exists is the component source tree itself, read
directly off disk, and a locator's target description (a data-testid
value, or role+accessible-name text). So resolution is: search the
frontend's source files for JSX that plausibly renders what the
locator targets, and return the matching file(s) plus a small context
window around each match -- deliberately NOT the whole file, per
Sec11's "kept small by deliberate, disciplined construction."

This only handles the two locator shapes actually used by the fixture
app and allowlisted by patch_validation.py: getByTestId(<literal>) and
getByText/getByRole(<literal-or-options-with-name>). Other allowlisted
methods (getByLabel, getByPlaceholder, getByAltText, getByTitle) follow
the same text-match path as getByText, since from a JSX-search
perspective they're all "does this element's text/attribute content
match," not fundamentally different kinds of lookup.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# Methods that match against an exact data-testid attribute value.
_TESTID_METHODS = frozenset({"getByTestId"})

# Methods that match against visible text / accessible name content.
_TEXT_METHODS = frozenset(
    {"getByText", "getByRole", "getByLabel", "getByPlaceholder", "getByAltText", "getByTitle"}
)

_CONTEXT_LINES_BEFORE = 3
_CONTEXT_LINES_AFTER = 3


@dataclass(frozen=True)
class ComponentMatch:
    file_path: str
    start_line: int  # 1-indexed, inclusive
    end_line: int  # 1-indexed, inclusive
    snippet: str


@dataclass(frozen=True)
class ComponentSourceResult:
    locator_source_text: str
    matches: tuple[ComponentMatch, ...]

    @property
    def resolved(self) -> bool:
        return len(self.matches) > 0

    @property
    def ambiguous(self) -> bool:
        """More than one plausible match -- Sec2's modal-reuse case
        (task-detail-modal appears twice in TaskDetailPage.tsx) is a
        real example of this, not a hypothetical edge case."""
        return len(self.matches) > 1


def resolve_component_source(
    locator_source_text: str, component_src_root: str | Path
) -> ComponentSourceResult:
    """Search component_src_root (the frontend's src/ directory) for JSX
    plausibly rendering what locator_source_text targets.

    locator_source_text is a full locator expression as it appears in
    the test file, e.g. 'page.getByTestId("login-username")' or
    'page.getByRole("button", { name: "Sign out" })' -- the same
    source_text patch_validation.find_locator_calls already extracts,
    so callers don't need a second parsing pass to get this string.
    """

    target = _extract_target(locator_source_text)
    if target is None:
        return ComponentSourceResult(locator_source_text=locator_source_text, matches=())

    root = Path(component_src_root)
    matches: list[ComponentMatch] = []

    for source_file in sorted(root.rglob("*.tsx")):
        matches.extend(_search_file(source_file, target))

    return ComponentSourceResult(locator_source_text=locator_source_text, matches=tuple(matches))


@dataclass(frozen=True)
class _Target:
    kind: str  # "testid" | "text"
    value: str


def _extract_target(locator_source_text: str) -> _Target | None:
    testid_match = re.search(r'getByTestId\(\s*["\']([^"\']+)["\']\s*\)', locator_source_text)
    if testid_match:
        return _Target(kind="testid", value=testid_match.group(1))

    # getByRole("button", { name: "Sign out" }) or getByRole("button", { name: "Tasks", exact: true })
    role_name_match = re.search(r'\bname:\s*["\']([^"\']+)["\']', locator_source_text)
    if role_name_match:
        return _Target(kind="text", value=role_name_match.group(1))

    # getByText("..."), getByLabel("..."), getByPlaceholder("..."), etc.,
    # called with a single literal string argument.
    for method in _TEXT_METHODS:
        simple_match = re.search(rf'{method}\(\s*["\']([^"\']+)["\']\s*\)', locator_source_text)
        if simple_match:
            return _Target(kind="text", value=simple_match.group(1))

    return None


def _search_file(path: Path, target: _Target) -> list[ComponentMatch]:
    try:
        text = path.read_text()
    except (UnicodeDecodeError, OSError):
        return []

    lines = text.splitlines()
    matches: list[ComponentMatch] = []

    for line_no, line in enumerate(lines, start=1):
        if _line_matches(line, target):
            start = max(1, line_no - _CONTEXT_LINES_BEFORE)
            end = min(len(lines), line_no + _CONTEXT_LINES_AFTER)
            snippet = "\n".join(lines[start - 1 : end])
            matches.append(
                ComponentMatch(file_path=str(path), start_line=start, end_line=end, snippet=snippet)
            )

    return matches


def _line_matches(line: str, target: _Target) -> bool:
    if target.kind == "testid":
        # Exact attribute-value match: data-testid="login-username", not
        # a substring match, so "task-row-1" doesn't also match a search
        # for "task-row-10" purely because one contains the other.
        exact_pattern = rf'data-testid=["\']{ re.escape(target.value) }["\']'
        if re.search(exact_pattern, line) is not None:
            return True

        # Dynamic testid case: data-testid={`task-row-${task.id}`}. The
        # test's literal value ("task-row-task-1") is a *rendered*
        # instance of a template literal this module never evaluates --
        # there's no running app here to render it against. Instead,
        # match the template literal's static prefix against the start
        # of the target value: `task-row-${...}` has static prefix
        # "task-row-", which target.value "task-row-task-1" starts with.
        # This is deliberately a looser match than the exact-literal
        # case above, since it's approximating a value this module
        # cannot compute precisely.
        for template_match in re.finditer(r"data-testid=\{`([^`]*)`\}", line):
            prefix = template_match.group(1).split("${", 1)[0]
            if prefix and target.value.startswith(prefix):
                return True

        return False

    # text match: JSX text content containing the target string, e.g.
    # >Sign out< or >{error}... not attempted structurally -- this is a
    # substring match on the rendered line, which is deliberately
    # permissive (JSX text content doesn't have a single canonical
    # attribute to anchor on the way data-testid does).
    return target.value in line
