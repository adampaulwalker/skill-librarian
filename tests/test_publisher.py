"""Tests for the single write path.

The point of every test here is the same: a change that reaches GitHub but never reaches anyone in
Claude is a silent failure, and silence is what this module exists to make impossible.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import pytest

from librarian import publisher
from librarian.config import Config
from librarian.errors import PublishFailed, UnsafePath
from librarian.proposals import Proposal
from librarian.publisher import publish

PLUGIN_DIR = "plugins/people-project"
PLUGIN_MANIFEST = f"{PLUGIN_DIR}/.claude-plugin/plugin.json"
MARKETPLACE_MANIFEST = ".claude-plugin/marketplace.json"
SKILL_NAME = "how-we-write-briefs"
SKILL_FILE = f"{PLUGIN_DIR}/skills/{SKILL_NAME}/SKILL.md"
DEFAULT_BRANCH = "main"
START_VERSION = "1.4.2"

ORIGINAL_SKILL = "---\nname: how-we-write-briefs\ndescription: How to write a brief.\n---\n\nOld body.\n"
UPDATED_SKILL = "---\nname: how-we-write-briefs\ndescription: How to write a brief.\n---\n\nNew body.\n"


# ==============================================================================================
# A fake GitHub, in memory
# ==============================================================================================


class LocalFakeGitHub:
    """An in memory stand in for the real repository.

    It records everything it is asked to do so a test can prove what did and did not happen. It is
    deliberately permissive: it will happily commit straight to the shared branch if asked, because
    the test needs to prove that the publisher never asks.
    """

    app_name = "Skill Librarian"
    app_email = "librarian@atlanticlabs.invalid"

    def __init__(self, files: dict[str, str], default_branch: str = DEFAULT_BRANCH) -> None:
        self.default_branch = default_branch
        self.trees: dict[str, dict[str, str]] = {"sha-0": dict(files)}
        self.branches: dict[str, str] = {default_branch: "sha-0"}
        self.branch_history: dict[str, list[str]] = {default_branch: ["sha-0"]}
        self.created_branches: list[tuple[str, str]] = []
        self.commits: list[dict[str, Any]] = []
        self.pull_requests: list[dict[str, Any]] = []
        self.merged: list[int] = []
        self.calls: list[tuple[str, Any]] = []
        self.fail_on: set[str] = set()
        self._counter = 0

    # Helpers for the tests

    def _next_sha(self, kind: str) -> str:
        self._counter += 1
        return f"{kind}-sha-{self._counter}"

    def _move(self, branch: str, sha: str) -> None:
        self.branches[branch] = sha
        self.branch_history.setdefault(branch, []).append(sha)

    def tree_of(self, branch: str) -> dict[str, str]:
        return self.trees[self.branches[branch]]

    def version_on(self, branch: str, path: str) -> str:
        return json.loads(self.tree_of(branch)[path])["version"]

    def marketplace_version_on(self, branch: str) -> str:
        manifest = json.loads(self.tree_of(branch)[MARKETPLACE_MANIFEST])
        return manifest["plugins"][0]["version"]

    def simulate_someone_else_publishing(self, files: dict[str, str]) -> None:
        tree = dict(self.tree_of(self.default_branch))
        tree.update(files)
        sha = self._next_sha("other")
        self.trees[sha] = tree
        self._move(self.default_branch, sha)

    def _maybe_fail(self, name: str) -> None:
        if name in self.fail_on:
            raise RuntimeError(f"the fake GitHub was told to fail on {name}")

    # The client contract

    def get_ref_sha(self, branch: str) -> str:
        self.calls.append(("get_ref_sha", branch))
        self._maybe_fail("get_ref_sha")
        if branch not in self.branches:
            raise LookupError(f"no branch called {branch}")
        return self.branches[branch]

    def get_file(self, path: str, ref: str) -> tuple[str, str]:
        self.calls.append(("get_file", (path, ref)))
        self._maybe_fail("get_file")
        tree = self.trees[ref]
        if path not in tree:
            raise FileNotFoundError(path)
        text = tree[path]
        return text, hashlib.sha1(text.encode("utf-8")).hexdigest()

    def list_dir(self, path: str, ref: str) -> list[dict[str, Any]]:
        """One level of a folder, shaped exactly the way the real client shapes it.

        The real client hands back an entry per immediate child, each carrying a name, a
        full path, and whether it is a file or a folder. Anything less than that is not the
        contract the rest of the code reads against.
        """
        self.calls.append(("list_dir", (path, ref)))
        prefix = path.strip("/")
        prefix = prefix + "/" if prefix else ""
        entries: dict[str, dict[str, Any]] = {}
        for stored in sorted(self.trees[ref]):
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
                entries[head] = {"name": head, "path": full, "type": "file", "sha": ""}
        return list(entries.values())

    def create_branch(self, name: str, from_sha: str) -> None:
        self.calls.append(("create_branch", (name, from_sha)))
        self._maybe_fail("create_branch")
        if name in self.branches:
            raise ValueError(f"branch {name} already exists")
        self.branches[name] = from_sha
        self.branch_history.setdefault(name, []).append(from_sha)
        self.created_branches.append((name, from_sha))

    def commit_files(
        self,
        branch: str,
        files: dict[str, str],
        message: str,
        author_name: str,
        author_email: str,
    ) -> str:
        self.calls.append(("commit_files", branch))
        self._maybe_fail("commit_files")
        if branch not in self.branches:
            raise LookupError(f"no branch called {branch}")
        tree = dict(self.trees[self.branches[branch]])
        tree.update(files)
        sha = self._next_sha("commit")
        self.trees[sha] = tree
        self._move(branch, sha)
        self.commits.append(
            {
                "sha": sha,
                "branch": branch,
                "files": dict(files),
                "message": message,
                "author_name": author_name,
                "author_email": author_email,
                "committer_name": self.app_name,
                "committer_email": self.app_email,
            }
        )
        return sha

    def open_pr(self, head: str, base: str, title: str, body: str) -> int:
        self.calls.append(("open_pr", (head, base)))
        self._maybe_fail("open_pr")
        number = len(self.pull_requests) + 1
        self.pull_requests.append(
            {"number": number, "head": head, "base": base, "title": title, "body": body}
        )
        return number

    def merge_pr(self, number: int, commit_title: str) -> str:
        self.calls.append(("merge_pr", number))
        self._maybe_fail("merge_pr")
        pull_request = self.pull_requests[number - 1]
        base = pull_request["base"]
        tree = dict(self.trees[self.branches[base]])
        tree.update(self.trees[self.branches[pull_request["head"]]])
        sha = self._next_sha("merge")
        self.trees[sha] = tree
        self._move(base, sha)
        self.merged.append(number)
        return sha

    def list_commits(self, path: str, limit: int) -> list[dict[str, Any]]:
        self.calls.append(("list_commits", (path, limit)))
        touching = [c for c in self.commits if path in c["files"]]
        return list(reversed(touching))[:limit]


try:  # another agent may provide a shared fake; use it only if it behaves the way these tests need
    from tests.fakes import FakeGitHub as _SharedFakeGitHub  # type: ignore
except Exception:  # pragma: no cover - the shared fake is optional
    _SharedFakeGitHub = None


_REQUIRED_FAKE_ATTRS = ("commits", "pull_requests", "merged", "branches", "trees", "fail_on")


def new_client(files: dict[str, str]) -> Any:
    if _SharedFakeGitHub is not None:
        try:
            candidate = _SharedFakeGitHub(dict(files))
            text, _sha = candidate.get_file(PLUGIN_MANIFEST, candidate.get_ref_sha(DEFAULT_BRANCH))
            if text == files[PLUGIN_MANIFEST] and all(
                hasattr(candidate, attr) for attr in _REQUIRED_FAKE_ATTRS
            ):
                return candidate
        except Exception:  # pragma: no cover - fall back to the local fake
            pass
    return LocalFakeGitHub(dict(files))


# ==============================================================================================
# Fixtures
# ==============================================================================================


def plugin_manifest(version: str = START_VERSION) -> str:
    return (
        json.dumps(
            {
                "name": "people-project",
                "description": "Shared skills for the people team.",
                "version": version,
            },
            indent=2,
        )
        + "\n"
    )


def marketplace_manifest(version: str = START_VERSION) -> str:
    return (
        json.dumps(
            {
                "name": "atlantic-labs",
                "owner": {"name": "Atlantic Labs"},
                "plugins": [
                    {
                        "name": "people-project",
                        "source": "./plugins/people-project",
                        "version": version,
                    }
                ],
            },
            indent=2,
        )
        + "\n"
    )


def starting_files(version: str = START_VERSION) -> dict[str, str]:
    return {
        PLUGIN_MANIFEST: plugin_manifest(version),
        MARKETPLACE_MANIFEST: marketplace_manifest(version),
        SKILL_FILE: ORIGINAL_SKILL,
    }


@pytest.fixture
def cfg() -> Config:
    return Config(
        repo_owner="atlantic-labs",
        repo_name="claude-skills",
        default_branch=DEFAULT_BRANCH,
    )


@pytest.fixture
def client() -> Any:
    return new_client(starting_files())


def make_proposal(
    files: dict[str, str] | None = None,
    base_sha: str = "sha-0",
    requested_by: str = "Ellie Chen <ellie@atlanticlabs.example>",
) -> Proposal:
    payload = {SKILL_FILE: UPDATED_SKILL} if files is None else files
    return Proposal(
        id="prop-8f3a1c2d",
        skill_name=SKILL_NAME,
        requested_by=requested_by,
        base_sha=base_sha,
        files=payload,
        diff_text="--- a\n+++ b\n",
        plain_summary="Asks for the client name in the first line of every brief.",
        diff_hash="a" * 64,
        created_at=1_700_000_000.0,
    )


# ==============================================================================================
# The most important test in the suite
# ==============================================================================================


def test_publish_always_bumps_version(cfg: Config, client: Any, monkeypatch: Any) -> None:
    """A publish either moves the version number forward or it does not happen at all.

    A change that ships without a version bump reaches the repository and never reaches a single
    person in Claude, and nobody is told. So there is no path through publish that writes content
    while leaving the version where it was.
    """
    # The ordinary case: the version moves, and it moves in both places.
    result = publish(client, cfg, make_proposal())

    assert result.new_version != START_VERSION
    assert client.version_on(DEFAULT_BRANCH, PLUGIN_MANIFEST) == result.new_version
    assert client.marketplace_version_on(DEFAULT_BRANCH) == result.new_version
    assert client.tree_of(DEFAULT_BRANCH)[SKILL_FILE] == UPDATED_SKILL

    # Now the failure this test exists for. If the version calculation ever returns the version it
    # was given, for any reason at all, publish must refuse rather than ship a silent no op.
    stuck = new_client(starting_files())
    monkeypatch.setattr(publisher, "bump_version", lambda current, kind="patch": current)

    with pytest.raises(PublishFailed) as refused:
        publish(stuck, cfg, make_proposal())

    assert "version" in refused.value.user_message.lower()
    assert stuck.commits == []
    assert stuck.pull_requests == []
    assert stuck.merged == []
    assert stuck.tree_of(DEFAULT_BRANCH)[SKILL_FILE] == ORIGINAL_SKILL
    assert stuck.version_on(DEFAULT_BRANCH, PLUGIN_MANIFEST) == START_VERSION

    # The same holds when the version only appears to change because of stray whitespace.
    whitespace = new_client(starting_files())
    monkeypatch.setattr(publisher, "bump_version", lambda current, kind="patch": f"  {current} ")

    with pytest.raises(PublishFailed):
        publish(whitespace, cfg, make_proposal())

    assert whitespace.commits == []
    assert whitespace.merged == []
    assert whitespace.tree_of(DEFAULT_BRANCH)[SKILL_FILE] == ORIGINAL_SKILL


# ==============================================================================================
# The two manifests
# ==============================================================================================


def test_both_manifests_end_up_carrying_the_same_version(cfg: Config, client: Any) -> None:
    result = publish(client, cfg, make_proposal())

    plugin_version = client.version_on(DEFAULT_BRANCH, PLUGIN_MANIFEST)
    marketplace_version = client.marketplace_version_on(DEFAULT_BRANCH)

    assert plugin_version == marketplace_version == result.new_version
    assert plugin_version != START_VERSION


def test_both_manifests_are_written_in_the_same_commit(cfg: Config, client: Any) -> None:
    publish(client, cfg, make_proposal())

    assert len(client.commits) == 1
    written = client.commits[0]["files"]
    assert PLUGIN_MANIFEST in written
    assert MARKETPLACE_MANIFEST in written
    assert SKILL_FILE in written


def test_manifests_stay_readable_two_space_indent_and_trailing_newline(
    cfg: Config, client: Any
) -> None:
    publish(client, cfg, make_proposal())

    for path in (PLUGIN_MANIFEST, MARKETPLACE_MANIFEST):
        text = client.tree_of(DEFAULT_BRANCH)[path]
        assert text.endswith("\n")
        assert '\n  "' in text
        assert json.loads(text)


def test_a_marketplace_entry_that_is_missing_stops_the_publish(cfg: Config) -> None:
    files = starting_files()
    files[MARKETPLACE_MANIFEST] = json.dumps({"name": "atlantic-labs", "plugins": []}, indent=2)
    client = new_client(files)

    with pytest.raises(PublishFailed):
        publish(client, cfg, make_proposal())

    assert client.commits == []
    assert client.merged == []


# ==============================================================================================
# The route the change takes
# ==============================================================================================


def test_publish_never_writes_to_the_default_branch(cfg: Config, client: Any) -> None:
    """A commit pushed straight to the shared branch triggers no delivery at all."""
    result = publish(client, cfg, make_proposal())

    assert client.commits, "the change was never committed anywhere"
    for commit in client.commits:
        assert commit["branch"] != DEFAULT_BRANCH
        assert commit["branch"].startswith("librarian/")

    assert [call for call in client.calls if call == ("commit_files", DEFAULT_BRANCH)] == []

    branch_name, from_sha = client.created_branches[0]
    assert branch_name.startswith(f"librarian/{SKILL_NAME}-")
    assert from_sha == "sha-0"

    assert len(client.pull_requests) == 1
    assert client.pull_requests[0]["base"] == DEFAULT_BRANCH
    assert client.pull_requests[0]["head"] == branch_name
    assert client.merged == [result.pr_number]


def test_the_change_only_reaches_the_shared_branch_through_a_merge(
    cfg: Config, client: Any
) -> None:
    publish(client, cfg, make_proposal())

    # The shared branch moved exactly once, and it moved because of the merge.
    assert len(client.branch_history[DEFAULT_BRANCH]) == 2
    assert client.branch_history[DEFAULT_BRANCH][0] == "sha-0"
    assert client.branch_history[DEFAULT_BRANCH][1].startswith("merge-")


@pytest.mark.parametrize("failing_step", ["create_branch", "commit_files", "open_pr", "merge_pr"])
def test_a_failure_partway_through_leaves_no_merged_half_change(
    cfg: Config, failing_step: str
) -> None:
    client = new_client(starting_files())
    client.fail_on = {failing_step}

    with pytest.raises(PublishFailed) as refused:
        publish(client, cfg, make_proposal())

    assert refused.value.user_message
    assert client.merged == []

    shared = client.tree_of(DEFAULT_BRANCH)
    assert shared[SKILL_FILE] == ORIGINAL_SKILL
    assert json.loads(shared[PLUGIN_MANIFEST])["version"] == START_VERSION
    assert json.loads(shared[MARKETPLACE_MANIFEST])["plugins"][0]["version"] == START_VERSION


def test_a_failure_message_says_nothing_changed_in_plain_english(cfg: Config) -> None:
    client = new_client(starting_files())
    client.fail_on = {"merge_pr"}

    with pytest.raises(PublishFailed) as refused:
        publish(client, cfg, make_proposal())

    message = refused.value.user_message
    assert "not reached anyone" in message or "has not reached" in message
    for jargon in ("Traceback", "RuntimeError", "sha", "HTTP", "500", "branch", "API"):
        assert not re.search(rf"\b{jargon}\b", message, re.IGNORECASE)


def test_a_change_prepared_against_an_out_of_date_copy_is_refused(cfg: Config) -> None:
    client = new_client(starting_files())
    client.simulate_someone_else_publishing({PLUGIN_MANIFEST: plugin_manifest("1.4.3")})

    with pytest.raises(PublishFailed) as refused:
        publish(client, cfg, make_proposal(base_sha="sha-0"))

    assert "latest" in refused.value.user_message.lower()
    assert client.commits == []
    assert client.merged == []


# ==============================================================================================
# Revert
# ==============================================================================================


def test_revert_goes_forward_through_the_same_path(cfg: Config, client: Any) -> None:
    """Undoing a change is another publish, not a rewrite of history."""
    first = publish(client, cfg, make_proposal())
    assert client.tree_of(DEFAULT_BRANCH)[SKILL_FILE] == UPDATED_SKILL

    head_after_first = client.branches[DEFAULT_BRANCH]
    revert = make_proposal(files={SKILL_FILE: ORIGINAL_SKILL}, base_sha=head_after_first)
    revert = Proposal(
        id="prop-revert01",
        skill_name=revert.skill_name,
        requested_by=revert.requested_by,
        base_sha=revert.base_sha,
        files=revert.files,
        diff_text=revert.diff_text,
        plain_summary="Puts the wording back the way it was before the last change.",
        diff_hash="b" * 64,
        created_at=revert.created_at,
    )

    second = publish(client, cfg, revert)

    # The content is back, and the version went forward again rather than back to where it started.
    assert client.tree_of(DEFAULT_BRANCH)[SKILL_FILE] == ORIGINAL_SKILL
    assert second.new_version != first.new_version
    assert second.new_version != START_VERSION
    assert client.marketplace_version_on(DEFAULT_BRANCH) == second.new_version

    # Two publishes, two pull requests, two merges, and nothing was rewritten or pushed over.
    assert len(client.pull_requests) == 2
    assert client.merged == [1, 2]
    assert [call for call in client.calls if call == ("commit_files", DEFAULT_BRANCH)] == []

    history = client.branch_history[DEFAULT_BRANCH]
    assert history[0] == "sha-0"
    assert len(history) == 3
    assert len(set(history)) == 3


# ==============================================================================================
# What a change is allowed to touch
# ==============================================================================================


def test_a_proposal_cannot_write_the_plugin_manifest(cfg: Config, client: Any) -> None:
    proposal = make_proposal(files={PLUGIN_MANIFEST: plugin_manifest("9.9.9")})

    with pytest.raises(UnsafePath):
        publish(client, cfg, proposal)

    assert client.commits == []
    assert client.merged == []


def test_a_proposal_cannot_write_the_marketplace_manifest(cfg: Config, client: Any) -> None:
    proposal = make_proposal(files={MARKETPLACE_MANIFEST: marketplace_manifest("9.9.9")})

    with pytest.raises(UnsafePath):
        publish(client, cfg, proposal)

    assert client.commits == []


@pytest.mark.parametrize(
    "path",
    [
        f"{PLUGIN_DIR}/skills/{SKILL_NAME}/../../../.github/workflows/deploy.yml",
        "/etc/passwd",
        ".github/workflows/sync.yml",
        f"{PLUGIN_DIR}/README.md",
        "plugins/other-plugin/skills/x/SKILL.md",
        f"{PLUGIN_DIR}\\skills\\{SKILL_NAME}\\SKILL.md",
        f"{PLUGIN_DIR}/skills/",
        "",
    ],
)
def test_writes_outside_the_skills_folder_are_refused(cfg: Config, path: str) -> None:
    client = new_client(starting_files())

    with pytest.raises(UnsafePath):
        publish(client, cfg, make_proposal(files={path: "anything"}))

    assert client.commits == []
    assert client.merged == []


def test_a_change_with_no_files_is_refused(cfg: Config, client: Any) -> None:
    with pytest.raises(PublishFailed):
        publish(client, cfg, make_proposal(files={}))

    assert client.commits == []


# ==============================================================================================
# Attribution
# ==============================================================================================


def test_the_commit_author_is_the_person_who_asked(cfg: Config, client: Any) -> None:
    publish(client, cfg, make_proposal(requested_by="Ellie Chen <ellie@atlanticlabs.example>"))

    commit = client.commits[0]
    assert commit["author_name"] == "Ellie Chen"
    assert commit["author_email"] == "ellie@atlanticlabs.example"
    assert commit["committer_name"] != commit["author_name"]
    assert "Requested-By: Ellie Chen <ellie@atlanticlabs.example>" in commit["message"]


def test_a_requester_given_as_a_bare_email_still_gets_the_credit(cfg: Config, client: Any) -> None:
    publish(client, cfg, make_proposal(requested_by="ellie@atlanticlabs.example"))

    commit = client.commits[0]
    assert commit["author_email"] == "ellie@atlanticlabs.example"


def test_a_publish_with_no_named_requester_is_refused(cfg: Config, client: Any) -> None:
    with pytest.raises(PublishFailed):
        publish(client, cfg, make_proposal(requested_by="   "))

    assert client.merged == []


# ==============================================================================================
# What the pull request and the result say
# ==============================================================================================


def test_the_pull_request_states_the_change_and_names_the_version(
    cfg: Config, client: Any
) -> None:
    result = publish(client, cfg, make_proposal())

    pull_request = client.pull_requests[0]
    assert SKILL_NAME in pull_request["title"]
    assert result.new_version in pull_request["title"]

    body = pull_request["body"]
    assert "Asks for the client name in the first line of every brief." in body
    assert result.new_version in body
    assert SKILL_FILE in body
    assert "Ellie Chen" in body


def test_estimated_live_by_is_the_far_edge_of_the_sync_window(
    cfg: Config, client: Any
) -> None:
    """The field carries a moment. The wording that keeps it a window lives in service.py,
    where the sentence the person actually reads is built, and is asserted there."""
    from datetime import datetime, timedelta, timezone

    from librarian.publisher import estimated_live_by

    now = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)
    moment = estimated_live_by(cfg, now=now)

    expected = (now + timedelta(minutes=cfg.sync_estimate_minutes)).strftime(
        "%H:%M UTC on %d %B %Y"
    )
    assert moment == expected

    result = publish(client, cfg, make_proposal())
    assert result.estimated_live_by
    # It is a moment, not a sentence dressed up as one.
    assert "minutes" not in result.estimated_live_by


def test_the_result_points_at_the_commit_and_the_pull_request(cfg: Config, client: Any) -> None:
    result = publish(client, cfg, make_proposal())

    assert result.commit_sha == client.commits[0]["sha"]
    assert result.pr_number == client.pull_requests[0]["number"]
