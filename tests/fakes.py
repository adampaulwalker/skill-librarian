"""An in-memory stand-in for GitHub, used by the whole test suite.

It stores a full snapshot of every file for each commit, which keeps the tests
easy to read: a commit is just "these were the files afterwards, and this is who
asked for it".

This fake holds the same promises the real client holds, and it is worth saying why
that matters rather than treating it as tidiness. A fake that is more permissive than
the thing it stands in for makes the suite prove nothing: the tests pass, and the
behaviour they were written to guarantee is missing from production. Three promises
are kept here deliberately, because each one is a promise the real client makes:

1. A commit is never written straight onto the branch everyone reads from. The name is
   compared the way the real client compares it, so no spelling of that branch slips
   past, and there is no way to switch the refusal off.
2. A merge names the exact saved change it is publishing, and is refused when the
   working copy has moved on to anything else since. That is what stops content
   nobody agreed to from being published.
3. A merge carries across what the working copy changed, laid over whatever the shared
   copy holds at that moment. It does not stamp a whole snapshot over the shared copy,
   because that would silently undo somebody else's change that landed in the meantime,
   and a test about two people publishing at once would then be testing nothing.

The shared copy can also be made to move partway through a piece of work, which is how
a test recreates two people publishing at the same time. See :meth:`after_next` and
:meth:`move_default_branch`.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from librarian.errors import LibrarianError, PublishFailed, SkillNotFound

#: The library manifest, always at the root of the repository.
MARKETPLACE_MANIFEST = ".claude-plugin/marketplace.json"

APP_COMMITTER_NAME = "Skill Librarian"
APP_COMMITTER_EMAIL = "skill-librarian@users.noreply.github.com"


@dataclass
class FakeCommit:
    sha: str
    message: str
    branch: str
    parents: list[str]
    files: dict[str, str]
    changed: set[str]
    author_name: str
    author_email: str
    committer_name: str
    committer_email: str
    date: str


@dataclass
class FakePullRequest:
    number: int
    head: str
    base: str
    title: str
    body: str
    merged: bool = False
    merge_commit_sha: str = ""


@dataclass
class FakeGitHubClient:
    """Implements the GitHubClient protocol against a dictionary."""

    default_branch: str = "main"
    committer_name: str = APP_COMMITTER_NAME
    committer_email: str = APP_COMMITTER_EMAIL
    fail_merge: bool = False

    commits: dict[str, FakeCommit] = field(default_factory=dict)
    branches: dict[str, str] = field(default_factory=dict)
    pull_requests: dict[int, FakePullRequest] = field(default_factory=dict)
    calls: list[tuple[str, Any]] = field(default_factory=list)
    merge_attempts: int = 0

    #: What each working copy has changed since it was cut, so a merge carries those files
    #: across rather than stamping a whole snapshot over the shared copy.
    branch_changes: dict[str, dict[str, str]] = field(default_factory=dict)
    #: One-shot callbacks to run straight after a named operation. See :meth:`after_next`.
    pending_hooks: dict[str, list[Callable[["FakeGitHubClient"], None]]] = field(
        default_factory=dict
    )
    #: Every hook that really did run, so a test can prove the thing it set up happened.
    hooks_fired: list[str] = field(default_factory=list)

    _counter: itertools.count = field(default_factory=lambda: itertools.count(1))

    def __post_init__(self) -> None:
        if self.default_branch not in self.branches:
            root = self._store_commit(
                message="Initial commit",
                branch=self.default_branch,
                parents=[],
                files={},
                changed=set(),
                author_name=self.committer_name,
                author_email=self.committer_email,
            )
            self.branches[self.default_branch] = root

    # -- helpers used by tests ------------------------------------------------

    def seed(self, files: dict[str, str], branch: str | None = None) -> str:
        """Put starting content into the repository without going through a commit path."""
        target = branch or self.default_branch
        head = self.branches[target]
        snapshot = dict(self.commits[head].files)
        snapshot.update(files)
        sha = self._store_commit(
            message="Seed content",
            branch=target,
            parents=[head],
            files=snapshot,
            changed=set(files),
            author_name=self.committer_name,
            author_email=self.committer_email,
        )
        self.branches[target] = sha
        return sha

    def files_on(self, ref: str) -> dict[str, str]:
        return dict(self.commits[self._resolve(ref)].files)

    def commit_list(self) -> list[FakeCommit]:
        return list(self.commits.values())

    # -- making the shared copy move partway through --------------------------

    def move_default_branch(self, files: dict[str, str]) -> str:
        """Somebody else publishes to the shared copy, laying these files over what is there.

        This is the whole point of the mechanism: a change that is already in flight has to
        cope with the shared copy no longer standing where it stood when the change started.
        """
        head = self.branches[self.default_branch]
        snapshot = dict(self.commits[head].files)
        snapshot.update(files)
        sha = self._store_commit(
            message="Somebody else published a change",
            branch=self.default_branch,
            parents=[head],
            files=snapshot,
            changed=set(files),
            author_name="Another Person",
            author_email="another@example.com",
        )
        self.branches[self.default_branch] = sha
        return sha

    def after_next(
        self, operation: str, callback: Callable[["FakeGitHubClient"], None]
    ) -> None:
        """Run ``callback`` once, straight after the next call to ``operation``.

        One shot on purpose. A callback that fires after every call to an operation makes a
        test pass or fail for reasons the test never intended, because publishing saves work
        more than once when it has to move a version number on.
        """
        self.pending_hooks.setdefault(operation, []).append(callback)

    def _fire(self, operation: str) -> None:
        waiting = self.pending_hooks.pop(operation, [])
        for callback in waiting:
            self.hooks_fired.append(operation)
            callback(self)

    def _is_shared_branch(self, branch: str) -> bool:
        """The same comparison the real client makes, so no spelling slips past the refusal."""
        cleaned = (branch or "").strip()
        if cleaned.startswith("refs/heads/"):
            cleaned = cleaned[len("refs/heads/") :]
        return cleaned.casefold() == (self.default_branch or "").strip().casefold()

    # -- protocol -------------------------------------------------------------

    def get_ref_sha(self, branch: str) -> str:
        self.calls.append(("get_ref_sha", branch))
        if branch not in self.branches:
            raise _fake_error(
                SkillNotFound,
                "I could not find the " + branch + " branch in the skills library.",
            )
        return self.branches[branch]

    def get_file(self, path: str, ref: str) -> tuple[str, str]:
        self.calls.append(("get_file", (path, ref)))
        snapshot = self.commits[self._resolve(ref)].files
        if path not in snapshot:
            raise _fake_error(
                SkillNotFound, "I could not find " + path + " in the skills library."
            )
        text = snapshot[path]
        return text, _blob_sha(text)

    def list_dir(self, path: str, ref: str) -> list[dict]:
        self.calls.append(("list_dir", (path, ref)))
        snapshot = self.commits[self._resolve(ref)].files
        prefix = path.strip("/")
        prefix = prefix + "/" if prefix else ""
        entries: dict[str, dict] = {}
        for stored in sorted(snapshot):
            if not stored.startswith(prefix):
                continue
            rest = stored[len(prefix) :]
            if not rest:
                continue
            head, _, tail = rest.partition("/")
            full = prefix + head
            if tail:
                entries.setdefault(head, {"name": head, "path": full, "type": "dir", "sha": ""})
            else:
                entries[head] = {
                    "name": head,
                    "path": full,
                    "type": "file",
                    "sha": _blob_sha(snapshot[stored]),
                    "size": len(snapshot[stored].encode("utf-8")),
                }
        return list(entries.values())

    def create_branch(self, name: str, from_sha: str) -> None:
        self.calls.append(("create_branch", (name, from_sha)))
        if name in self.branches:
            raise _fake_error(
                PublishFailed,
                "A change with that name is already being worked on. Nothing was published.",
            )
        if from_sha not in self.commits:
            raise _fake_error(
                PublishFailed,
                "I could not find the starting point for that change. Nothing was published.",
            )
        self.branches[name] = from_sha
        self.branch_changes[name] = {}
        self._fire("create_branch")

    def commit_files(
        self,
        branch: str,
        files: dict[str, str],
        message: str,
        author_name: str,
        author_email: str,
    ) -> str:
        self.calls.append(("commit_files", (branch, sorted(files), author_name)))
        # The real client nails this door shut with no way to open it, so this one does too.
        if self._is_shared_branch(branch):
            raise _fake_error(
                PublishFailed,
                "A change must be reviewed before it goes into the shared library. "
                "Nothing was published.",
            )
        if branch not in self.branches:
            raise _fake_error(
                PublishFailed,
                "I could not find the branch for that change. Nothing was published.",
            )
        if not files:
            raise _fake_error(
                PublishFailed,
                "There was nothing to save, so no change was made to the skills library.",
            )
        if not author_name.strip() or "@" not in author_email:
            raise _fake_error(
                PublishFailed,
                "I do not know who asked for this change, so I cannot record it. "
                "Nothing was published.",
            )
        head = self.branches[branch]
        snapshot = dict(self.commits[head].files)
        snapshot.update(files)
        trailer = f"Requested-By: {author_name} <{author_email}>"
        full_message = message.rstrip("\n")
        if trailer not in full_message.splitlines():
            full_message = full_message + "\n\n" + trailer
        sha = self._store_commit(
            message=full_message,
            branch=branch,
            parents=[head],
            files=snapshot,
            changed=set(files),
            author_name=author_name,
            author_email=author_email,
        )
        self.branches[branch] = sha
        # Remember what this working copy has changed, so a merge lays exactly these files over
        # whatever the shared copy holds by then rather than overwriting all of it.
        self.branch_changes.setdefault(branch, {}).update(files)
        self._fire("commit_files")
        return sha

    def open_pr(self, head: str, base: str, title: str, body: str) -> int:
        self.calls.append(("open_pr", (head, base, title)))
        if head not in self.branches:
            raise _fake_error(
                PublishFailed,
                "I could not find the change to put up for review. Nothing was published.",
            )
        number = len(self.pull_requests) + 1
        self.pull_requests[number] = FakePullRequest(
            number=number, head=head, base=base, title=title, body=body
        )
        self._fire("open_pr")
        return number

    def merge_pr(self, number: int, commit_title: str, expected_head_sha: str) -> str:
        """Merge, and only the saved change that was actually agreed to.

        ``expected_head_sha`` names that saved change. The real client hands it to GitHub,
        which refuses the merge when the working copy has been moved to anything else since,
        so this refuses on exactly the same terms. Without that, anybody able to write to the
        repository could swap the wording between the moment a person agrees to it and the
        moment it is published, and nothing in the suite would notice.
        """
        self.calls.append(("merge_pr", number))
        self.merge_attempts += 1
        pull = self.pull_requests.get(number)
        if pull is None or pull.merged or self.fail_merge:
            raise _fake_error(
                PublishFailed,
                "The change could not be merged into the shared library. "
                "Nothing has been published.",
            )
        pinned = (expected_head_sha or "").strip()
        if not pinned:
            raise _fake_error(
                PublishFailed,
                "I was not told which approved version of the change to publish, so nothing "
                "was published. Please ask for the change again.",
            )
        if self.branches[pull.head] != pinned:
            raise _fake_error(
                PublishFailed,
                "The change was edited after it was approved, so nothing was published. "
                "Please ask for the change again, read it through, and approve the new one.",
            )

        head_commit = self.commits[self.branches[pull.head]]
        base_sha = self.branches[pull.base]
        # What the working copy changed, laid over the shared copy as it stands right now. A
        # change somebody else published in the meantime survives, exactly as it would on
        # GitHub, so a test about two people publishing at once is testing something real.
        carried = dict(self.branch_changes.get(pull.head, {}))
        snapshot = dict(self.commits[base_sha].files)
        snapshot.update(carried)
        merge_sha = self._store_commit(
            message=commit_title,
            branch=pull.base,
            parents=[base_sha, head_commit.sha],
            files=snapshot,
            changed=set(carried),
            author_name=head_commit.author_name,
            author_email=head_commit.author_email,
        )
        self.branches[pull.base] = merge_sha
        pull.merged = True
        pull.merge_commit_sha = merge_sha
        self._fire("merge_pr")
        return merge_sha

    def list_commits(self, path: str, limit: int) -> list[dict]:
        """History for one file, or for everything inside one folder.

        GitHub treats the path it is given as a file when it names a file and as a folder
        when it names a folder, so this does the same. A skill's history is asked for by
        its folder, and anything narrower would answer that nothing ever happened.
        """
        self.calls.append(("list_commits", (path, limit)))
        prefix = path.rstrip("/") + "/"
        history: list[dict] = []
        sha: str | None = self.branches[self.default_branch]
        while sha:
            commit = self.commits[sha]
            if path in commit.changed or any(
                touched.startswith(prefix) for touched in commit.changed
            ):
                history.append(
                    {
                        "sha": commit.sha,
                        "message": commit.message,
                        "author_name": commit.author_name,
                        "author_email": commit.author_email,
                        "committer_name": commit.committer_name,
                        "date": commit.date,
                    }
                )
            if len(history) >= limit:
                break
            sha = commit.parents[-1] if commit.parents else None
        return history

    # -- internals ------------------------------------------------------------

    def _resolve(self, ref: str) -> str:
        if ref in self.branches:
            return self.branches[ref]
        if ref in self.commits:
            return ref
        raise _fake_error(
            SkillNotFound, "I could not find that version of the skills library."
        )

    def _store_commit(
        self,
        *,
        message: str,
        branch: str,
        parents: list[str],
        files: dict[str, str],
        changed: set[str],
        author_name: str,
        author_email: str,
    ) -> str:
        sha = f"{next(self._counter):040x}"
        self.commits[sha] = FakeCommit(
            sha=sha,
            message=message,
            branch=branch,
            parents=parents,
            files=files,
            changed=changed,
            author_name=author_name,
            author_email=author_email,
            committer_name=self.committer_name,
            committer_email=self.committer_email,
            date="2026-08-18T12:00:00Z",
        )
        return sha


def _blob_sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _fake_error(kind: type[LibrarianError], user_message: str) -> LibrarianError:
    return kind(user_message)


# ==============================================================================================
# Building a library that holds more than one collection of skills
# ==============================================================================================
#
# A marketplace repository may hold any number of plugins, and each plugin any number of
# skills. The helpers below build that shape so a test never has to hand-write the two
# manifests. Nothing here assumes one collection, one skill, or one owner.


@dataclass(frozen=True)
class PluginSpec:
    """One collection of skills, as a test wants it laid out in the repository."""

    name: str
    plugin_dir: str
    version: str = "1.0.0"
    #: Skill name -> the one-line description that goes in its front matter.
    skills: tuple[tuple[str, str], ...] = ()


def skill_text(skill_name: str, description: str, body: str = "How this one works.") -> str:
    """The text of a SKILL.md file, in the shape the parser expects."""
    return f"---\nname: {skill_name}\ndescription: {description}\n---\n\n{body}\n"


def plugin_manifest_text(name: str, version: str) -> str:
    """One collection's own manifest, which is the file that gates delivery."""
    return json.dumps({"name": name, "version": version}, indent=2) + "\n"


def marketplace_manifest_text(*specs: PluginSpec, owner: str = "an-organization") -> str:
    """The library manifest that lists every collection and where each one lives."""
    return (
        json.dumps(
            {
                "name": owner,
                "owner": {"name": owner},
                "plugins": [
                    {
                        "name": spec.name,
                        "source": "./" + spec.plugin_dir,
                        "version": spec.version,
                    }
                    for spec in specs
                ],
            },
            indent=2,
        )
        + "\n"
    )


def library_files(*specs: PluginSpec, owner: str = "an-organization") -> dict[str, str]:
    """Every file a library of these collections holds, as path -> text."""
    files: dict[str, str] = {
        MARKETPLACE_MANIFEST: marketplace_manifest_text(*specs, owner=owner)
    }
    for spec in specs:
        files[f"{spec.plugin_dir}/.claude-plugin/plugin.json"] = plugin_manifest_text(
            spec.name, spec.version
        )
        for skill_name, description in spec.skills:
            files[f"{spec.plugin_dir}/skills/{skill_name}/SKILL.md"] = skill_text(
                skill_name, description
            )
    return files


def library(*specs: PluginSpec, owner: str = "an-organization") -> FakeGitHubClient:
    """A fake repository already holding these collections on its default branch."""
    client = FakeGitHubClient()
    client.seed(library_files(*specs, owner=owner))
    return client
