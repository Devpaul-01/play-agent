"""An isolated working copy of the app under test (architecture Sec7).

Sec7: apply_patch writes to an isolated copy and "the original working-tree
file is never touched"; the deliverable is a diff the human applies. This
module is that copy.

One PatchWorkspace owns one scratch directory holding a copy of the whole
app. The sandbox mounts THAT copy (not the real checkout), the patched test
file lives in it, and the real file is only ever read -- to restore the
pristine text before each candidate and to produce the diff.

reset() is what makes the retry flow correct. The Driver's
VALIDATING_PROPOSAL step applies a candidate, but nothing reverts it if
verification fails, so without a reset a second candidate would be applied
on top of the first failed one and a "successful" diff could carry an
earlier wrong patch. Restoring the pristine file before every apply makes
each candidate independent and keeps locator_call_ids stable, since they
are derived from the pristine file's positions.
"""

from __future__ import annotations

import difflib
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from typing import Self

# Never needed to run a test, and potentially large or stale.
DEFAULT_IGNORE = (".git", "test-results", "playwright-report", "__pycache__")


class WorkspaceError(Exception):
    pass


class PatchWorkspace:
    def __init__(
        self,
        source_root: str | Path,
        ignore: tuple[str, ...] = DEFAULT_IGNORE,
        parent_dir: str | Path | None = None,
    ) -> None:
        self._source_root = Path(source_root).resolve()
        self._ignore = ignore
        self._parent_dir = None if parent_dir is None else str(parent_dir)
        self._tmp: Path | None = None
        self._root: Path | None = None

    def __enter__(self) -> Self:
        if not self._source_root.is_dir():
            raise WorkspaceError(f"app under test is not a directory: {self._source_root}")

        tmp = Path(tempfile.mkdtemp(prefix="play-agent-ws-", dir=self._parent_dir))
        root = tmp / "app"
        try:
            shutil.copytree(
                self._source_root,
                root,
                symlinks=True,
                ignore=shutil.ignore_patterns(*self._ignore),
            )
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise

        self._tmp, self._root = tmp, root
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._tmp is not None:
            shutil.rmtree(self._tmp, ignore_errors=True)
        self._tmp = self._root = None

    @property
    def root(self) -> Path:
        if self._root is None:
            raise WorkspaceError("workspace used outside its 'with' block")
        return self._root

    def relative(self, original: str | Path) -> PurePosixPath:
        """The path of a file inside the app under test, relative to the
        app root, as a POSIX path (what the container and a git-style diff
        both expect)."""

        resolved = Path(original).resolve()
        try:
            rel = resolved.relative_to(self._source_root)
        except ValueError:
            raise WorkspaceError(
                f"{original} is not inside the app under test ({self._source_root})"
            ) from None
        return PurePosixPath(*rel.parts)

    def workspace_path(self, original: str | Path) -> Path:
        return self.root.joinpath(*self.relative(original).parts)

    def reset(self, original: str | Path) -> Path:
        """Restore the workspace copy of a file to the original's current
        text, and return the workspace path."""

        source = Path(original).resolve()
        if not source.is_file():
            raise WorkspaceError(f"file not found: {original}")
        target = self.workspace_path(original)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        return target

    def diff(self, original: str | Path) -> str:
        """Unified diff from the original file to its patched workspace
        copy, with a/ b/ paths relative to the app root so it applies
        with `git apply` from that root."""

        rel = self.relative(original)
        before = Path(original).resolve().read_text()
        after = self.workspace_path(original).read_text()
        return "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"a/{rel}",
                tofile=f"b/{rel}",
            )
        )
