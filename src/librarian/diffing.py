"""Turn a set of file edits into something a person can actually read.

Two outputs are produced from the same comparison:

* ``diff_text`` - a standard unified diff, kept purely for the record so that a
  developer can audit exactly what happened later.
* ``plain_summary`` - one or two sentences of ordinary English, written for
  somebody who has never seen a diff in their life. This is what the person
  approving the change is shown.
"""

from __future__ import annotations

import difflib
from collections.abc import Mapping
from dataclasses import dataclass

__all__ = [
    "FileChange",
    "changed_files",
    "unified_diff_for_file",
    "diff_files",
    "summarize_changes",
    "describe_changes",
]

_NO_FILE = "/dev/null"


@dataclass(frozen=True)
class FileChange:
    """One file's worth of change, already counted up."""

    path: str
    lines_added: int
    lines_removed: int
    is_new_file: bool
    is_deleted_file: bool

    @property
    def is_empty(self) -> bool:
        """True when nothing at all changed in this file."""
        return self.lines_added == 0 and self.lines_removed == 0


def _lines(text: str) -> list[str]:
    """Split text into lines for counting, ignoring a trailing newline."""
    return text.splitlines()


def _count(old_text: str, new_text: str) -> tuple[int, int]:
    """Return (lines added, lines removed) between two versions of a file."""
    old_lines = _lines(old_text)
    new_lines = _lines(new_text)
    added = 0
    removed = 0
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        removed += i2 - i1
        added += j2 - j1
    return added, removed


def changed_files(
    old_files: Mapping[str, str],
    new_files: Mapping[str, str],
) -> list[FileChange]:
    """Compare two sets of files and describe every file that is not identical.

    Paths are returned in sorted order so the result is always the same for the
    same input.
    """
    changes: list[FileChange] = []
    for path in sorted(set(old_files) | set(new_files)):
        old_text = old_files.get(path)
        new_text = new_files.get(path)
        if old_text == new_text:
            continue
        added, removed = _count(old_text or "", new_text or "")
        changes.append(
            FileChange(
                path=path,
                lines_added=added,
                lines_removed=removed,
                is_new_file=old_text is None,
                is_deleted_file=new_text is None,
            )
        )
    return changes


def unified_diff_for_file(path: str, old_text: str | None, new_text: str | None) -> str:
    """Return a unified diff for a single file.

    Pass ``None`` for ``old_text`` when the file is being created, or ``None``
    for ``new_text`` when it is being deleted.
    """
    from_label = _NO_FILE if old_text is None else "a/" + path
    to_label = _NO_FILE if new_text is None else "b/" + path
    lines = difflib.unified_diff(
        _lines(old_text or ""),
        _lines(new_text or ""),
        fromfile=from_label,
        tofile=to_label,
        lineterm="",
    )
    return "\n".join(lines)


def diff_files(old_files: Mapping[str, str], new_files: Mapping[str, str]) -> str:
    """Return one unified diff covering every file that changed.

    Files are listed in sorted order. An empty string means nothing changed.
    """
    blocks: list[str] = []
    for change in changed_files(old_files, new_files):
        block = unified_diff_for_file(
            change.path,
            old_files.get(change.path),
            new_files.get(change.path),
        )
        if block:
            blocks.append(block)
    if not blocks:
        return ""
    return "\n".join(blocks) + "\n"


def _lines_phrase(count: int) -> str:
    """Say '1 line' or '4 lines'."""
    if count == 1:
        return "1 line"
    return f"{count} lines"


def _describe_one(change: FileChange) -> str:
    """One sentence about one file, in plain English."""
    if change.is_new_file:
        return f"Added a new file called {change.path}, with {_lines_phrase(change.lines_added)} in it."
    if change.is_deleted_file:
        return f"Removed the file {change.path}, which had {_lines_phrase(change.lines_removed)} in it."
    if change.lines_added and change.lines_removed:
        return (
            f"{_lines_phrase(change.lines_added)} added and "
            f"{_lines_phrase(change.lines_removed)} removed in {change.path}."
        )
    if change.lines_added:
        return f"{_lines_phrase(change.lines_added)} added in {change.path}."
    if change.lines_removed:
        return f"{_lines_phrase(change.lines_removed)} removed in {change.path}."
    return f"A small formatting change at the very end of {change.path}, with no wording changed."


def summarize_changes(
    old_files: Mapping[str, str],
    new_files: Mapping[str, str],
) -> str:
    """Describe the whole change set in ordinary English.

    Examples of what comes back:

    * "Nothing changed."
    * "3 lines added and 1 line removed in SKILL.md."
    * "2 files changed. 3 lines added in SKILL.md. Added a new file called
      reference/tone.md, with 8 lines in it."
    """
    changes = changed_files(old_files, new_files)
    if not changes:
        return "Nothing changed."
    sentences = [_describe_one(change) for change in changes]
    if len(changes) == 1:
        return sentences[0]
    return f"{len(changes)} files changed. " + " ".join(sentences)


def describe_changes(
    old_files: Mapping[str, str],
    new_files: Mapping[str, str],
) -> tuple[str, str]:
    """Return (unified diff for the record, plain English summary for the person)."""
    return diff_files(old_files, new_files), summarize_changes(old_files, new_files)
