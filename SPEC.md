# Skill Librarian - build contract

Everything below is fixed. Build to it exactly. Do not redesign it.

## What this is

A remote Model Context Protocol server. It is added once to a Claude organization as a custom
connector, so its tools appear inside an ordinary Claude chat. It edits skills that live as files in
a private GitHub repository, on behalf of people who will never open a terminal and do not have
GitHub accounts.

**This is a general product, not a deliverable for one customer.** Any organization that hits the
single-editor wall should be able to point it at their own repository and use it. That has hard
consequences for the code:

- NOTHING about any particular organization, repository, plugin, or skill may be hardcoded. No
  customer names, no default repository, no default plugin directory, no assumptions about what the
  skills are for.
- A marketplace repository may hold MORE THAN ONE plugin, and a plugin may hold more than one skill.
  Layout is DISCOVERED by reading the marketplace manifest at runtime, never assumed.
- Everything organization-specific arrives as configuration or as the contents of the repository.
- Wording in errors and tool output must make sense to any team, so never assume the reader is in a
  particular industry or role.

## Verified constraints that the design must obey

These were verified against Anthropic's documentation on 2026-08-18. Violating any of them produces a
failure that is SILENT - no error, nothing happens, and the user concludes the tool is broken.

1. **A commit pushed straight to the default branch reaches nobody.** Organization sync fires only
   when a pull request is merged to the default branch AND that pull request includes a plugin
   version bump. So every publish MUST be: branch -> edit -> bump version -> pull request -> merge.
2. **The version string in `plugins/<plugin>/.claude-plugin/plugin.json` gates delivery.** If content
   changes but the version string does not, installed users keep the cached copy. Documented to
   happen "without warning".
3. **The marketplace repository must be private.** Public is rejected for organization marketplaces.
4. **Skill sources must be relative paths inside the marketplace repository** (`./plugins/x`). Skills
   cannot be pulled from another repository.
5. **Nothing is instant.** A sync can take up to 30 minutes and each person picks it up on their next
   session. No polling interval is published. Never promise a specific number; always express it as
   an approximate window and tell the user how to confirm it landed.

## Repository shape it operates on

```
<repo root>/
  .claude-plugin/marketplace.json          plugins[].version  (kept in step)
  plugins/<plugin>/
    .claude-plugin/plugin.json             version            (THE gate)
    skills/<skill-name>/SKILL.md           YAML frontmatter + markdown body
    skills/<skill-name>/reference/*.md     optional supporting files
```

## Module contract - build exactly these, with exactly these signatures

### `src/librarian/config.py`
```python
@dataclass(frozen=True)
class Config:
    repo_owner: str
    repo_name: str
    default_branch: str = "main"
    proposal_ttl_seconds: int = 900
    sync_estimate_minutes: int = 30

def load_config() -> Config: ...   # from env: LIBRARIAN_REPO_OWNER, LIBRARIAN_REPO_NAME, ...
```

### `src/librarian/errors.py`
`LibrarianError` base. Subclasses: `SkillNotFound`, `UnsafePath`, `InvalidSkill`, `ProposalNotFound`,
`ProposalExpired`, `DiffMismatch`, `PublishFailed`, `NotAuthorized`.
Every one carries a `.user_message` written in plain English for a non-technical reader, with no
jargon and no stack detail.

### `src/librarian/paths.py`
```python
def skill_dir(plugin_dir: str, skill_name: str) -> str      # repo-relative posix path
def validate_skill_name(name: str) -> str                   # returns normalized name
def validate_plugin_dir(plugin_dir: str) -> str             # must be a relative path under the repo
def assert_safe_repo_path(plugin_dir: str, path: str) -> str
```
Rules, enforced server-side, never by asking the model:
- Skill name must match `^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$`. Reject anything else.
- Every writable path must resolve inside `<plugin_dir>/skills/<validated-name>/`, where `plugin_dir`
  is DISCOVERED at runtime from the marketplace manifest and never hardcoded or taken from a tool
  argument unchecked.
- Reject `..`, absolute paths, backslashes, null bytes, leading `/`, and any path that is not
  `SKILL.md` or `reference/<name>.md`.
- The two manifest files are writable ONLY by the version-bump code path, never by an edit tool.
- `.github/`, workflow files, and anything outside `plugin_dir` are never writable.

### `src/librarian/marketplace.py`
```python
@dataclass(frozen=True)
class PluginRef:
    name: str
    plugin_dir: str        # repo-relative, from the marketplace entry source
    version: str
    manifest_path: str     # <plugin_dir>/.claude-plugin/plugin.json

@dataclass(frozen=True)
class SkillRef:
    skill_name: str
    plugin: PluginRef
    skill_path: str        # <plugin_dir>/skills/<skill_name>/SKILL.md

def parse_marketplace(text: str) -> list[PluginRef]
def resolve_skill(gh: GitHubClient, cfg: Config, skill_name: str, ref: str) -> SkillRef
def list_all_skills(gh: GitHubClient, cfg: Config, ref: str) -> list[SkillRef]
```
Reads `.claude-plugin/marketplace.json` at the repo root and discovers every plugin from its
`plugins` array. Only relative-path sources (`./...`) are supported, matching what an organization
marketplace allows; any other source type is reported clearly as unsupported rather than ignored.

`resolve_skill` searches every plugin for the named skill. If two plugins both contain a skill with
the same name, that is an ambiguity and must raise a clear error naming both, never a silent pick of
the first. If none contain it, raise `SkillNotFound` listing what does exist.

### `src/librarian/skillfile.py`
```python
@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    frontmatter: dict
    body: str

def parse_skill(text: str) -> Skill
def render_skill(skill: Skill) -> str            # round-trips: parse(render(s)) == s
def validate_frontmatter(fm: dict) -> dict
```
Allowed frontmatter keys ONLY: `name`, `description`, `license`, `compatibility`, `metadata`,
`allowed-tools`. Reject unknown keys with a plain-English message. `description` is required and must
be non-empty. Preserve key order on render.

### `src/librarian/versioning.py`
```python
def parse_semver(v: str) -> tuple[int,int,int]
def bump_patch(v: str) -> str
def bump_minor(v: str) -> str
def bump_version(current: str, kind: str = "patch") -> str
```
Invalid or missing version is an error, never a silent default.

### `src/librarian/proposals.py`
```python
@dataclass(frozen=True)
class Proposal:
    id: str
    skill_name: str
    requested_by: str          # verified identity of the human, never model-supplied
    base_sha: str              # commit the diff was computed against
    files: dict[str, str]      # repo-relative path -> full new content
    diff_text: str             # unified diff, for the record
    plain_summary: str         # what changed, in plain English
    diff_hash: str             # sha256 over canonicalised files + base_sha
    created_at: float

class ProposalStore:
    def put(self, p: Proposal) -> None
    def get(self, pid: str) -> Proposal      # raises ProposalNotFound / ProposalExpired
    def delete(self, pid: str) -> None
```
`diff_hash` binds the approval to the exact content the user was shown. Approval MUST re-check it.

### `src/librarian/github.py`
```python
class GitHubClient(Protocol):
    def get_ref_sha(self, branch: str) -> str
    def get_file(self, path: str, ref: str) -> tuple[str, str]      # (text, blob_sha)
    def list_dir(self, path: str, ref: str) -> list[dict]
    def create_branch(self, name: str, from_sha: str) -> None
    def delete_branch(self, name: str) -> None
    def commit_files(self, branch: str, files: dict[str,str], message: str,
                     author_name: str, author_email: str) -> str    # returns commit sha
    def open_pr(self, head: str, base: str, title: str, body: str) -> int
    def merge_pr(self, number: int, commit_title: str, expected_head_sha: str) -> str
    def close_pr(self, number: int) -> None
    def list_commits(self, path: str, limit: int) -> list[dict]
    def commit_parents(self, sha: str) -> list[str]
```
Two implementations: `GitHubAppClient` (production - a GitHub App private key, minting short-lived
installation tokens, scoped to the single repository) and `TokenClient` (development only, reads
`LIBRARIAN_DEV_TOKEN`; must log a loud warning that it is not for production).

**Every method above is required of every client, including every stand-in used in tests.** None of
them is optional and none may be reached for with `getattr`. A method the real clients do not
implement is a method that silently does nothing in production while the tests stay green, which is
the drift that hid the first round of bugs here.

`merge_pr` takes `expected_head_sha`: the exact commit the human approved. It is sent to GitHub so
the merge is refused if the branch has moved since. It is never optional and never defaulted.

`delete_branch` takes the working branch away after a publish fails, so the next attempt at the same
proposal is not blocked by the leftover. Branch names are derived from the proposal, so the same
request produces the same name every time. Two rules:
- Deleting the default branch is refused outright, exactly as `commit_files` refuses to write to it.
- Deleting a branch that is already gone succeeds quietly. Cleanup runs on a path where something
  has already failed, and it must never replace that failure with a complaint of its own. Anything
  else, including a permission problem, is still reported.

`close_pr` withdraws a pull request that was opened and is not going to be published, via
`PATCH /repos/{owner}/{repo}/pulls/{number}` with state `closed`. It exists because a failure after
the pull request was opened used to leave it open. The branch is deleted, so the leftover pull
request points at a branch that is no longer there, which is confusing at best; and if the branch
deletion also failed, it is a live mergeable proposal nobody meant to leave behind. The same two
rules as `delete_branch` apply, for the same reason:
- Closing a pull request that is already closed, or that is not there at all, succeeds quietly. This
  runs on a path where something has already gone wrong, and that first failure is the one the
  person needs to hear about.
- Anything else is still reported, a lost permission most of all. A blanket silence would leave the
  repository filling with open pull requests nobody ever hears about.
- A number that does not name a pull request sends nothing at all. `True` counts as the number one
  in Python, so a caller that passed it by mistake would otherwise withdraw whichever pull request
  is numbered one, which is somebody else's.

`commit_parents` returns the parent shas of a commit via `GET /repos/{owner}/{repo}/commits/{sha}`,
in the order GitHub gives them. The first parent is the branch that was merged into and the rest are
what was merged in, which is how a merge can be checked for what it actually merged rather than for
what it was meant to merge. The order carries that meaning, so it is never sorted or deduplicated. A
commit that cannot be named sends nothing at all: an empty sha would ask GitHub for the whole commit
list instead of for one commit, and a list read as a commit looks exactly like a commit with no
parents. An answer of the wrong shape, or one that records no parents at all, is refused out loud
rather than flattened into an empty list, because a caller checking what a merge carried would
otherwise be told calmly that it carried nothing.

**Attribution is the point.** Commits set the git AUTHOR to the human who asked
(`author_name`, `author_email`) while the COMMITTER is the app. This is how the history names the
person who asked for a change without that person needing a GitHub account. Commit messages also
carry a `Requested-By:` trailer.

### `src/librarian/publisher.py`
```python
def publish(gh: GitHubClient, cfg: Config, proposal: Proposal, bump: str = "patch") -> PublishResult
```
The single write path. In order, no exceptions:
1. Resolve which plugin owns the skill, then re-read THAT plugin's `plugin.json` and the root
   `marketplace.json` at `base_sha`.
2. Compute the bumped version. Write it into BOTH manifests, kept in step. Only the entry for the
   owning plugin changes in `marketplace.json`; other plugins' entries must be left byte-identical.
3. Create a branch named `librarian/<skill>-<short-id>`.
4. Commit the proposal's files AND both manifests together, author = the requester.
5. Open a pull request whose body states the change in plain English and names the version.
6. Merge it into the default branch.
7. Return `PublishResult(commit_sha, pr_number, new_version, estimated_live_by)`.

`publish` MUST raise `PublishFailed` rather than proceed if the version did not change. A change that
ships without a version bump is the exact silent failure this whole project exists to prevent.

**The version guard must refuse BEFORE the merge, never after it.** Content must never land on the
default branch unless the version strictly increases, so the last thing `publish` does before calling
`merge_pr` is read the version currently on the default branch and compare it to the version this
change is about to ship. If the version being shipped is not strictly higher than the one already
there, `publish` raises `PublishFailed` and does not merge. Another publish landing in the gap can
take the default branch to the very number this change was going to ship under, and merging on top of
that puts the content in place while leaving the number that triggers delivery exactly where it was.
Reading that number and then merging anyway is not a guard.

The readback after the merge is damage detection only. It confirms what landed and reports a problem
that already happened; it can never be the thing that prevents it, because by then the content is on
the branch everyone reads from. Detecting a bad merge is not the same as refusing one.

**A rebuild must refuse when an approved file changed underneath it.** When the default branch has
moved, `publish` may start its working branch again from where the default branch now stands and
re-commit the approved content there. That rebuild silently destroys somebody else's work whenever
the two changes touched the same file: the approved content is whole-file content, so writing it on
top of a newer starting point replaces whatever that file now holds, and the other person's edit is
gone with no conflict, no error, and nothing in the result that says it ever existed. So before
rebuilding, `publish` re-reads every file the proposal writes as it stands on the new starting point
and compares it to how that same file stood at `base_sha`. If any one of them differs, it raises
`PublishFailed` and does not rebuild, telling the person in plain English that somebody else changed
the same wording and that the change needs to be prepared again from the latest copy. Proving that a
rebuild leaves alone a file this change never touched proves nothing at all; the case that destroys
work is the one where both sides edited the SAME file, and that is the case the guard and its test
must be about.

If anything fails after the branch was created, `publish` calls `delete_branch` so the same proposal
can be tried again without colliding on the branch name. If the failure happened after the pull
request was opened, `publish` also calls `close_pr`, so the attempt does not leave an open pull
request pointing at a branch that has just been taken away. A failure while cleaning up is swallowed,
because the original failure is what the person needs to hear about.

### `src/librarian/service.py`
Tool-level operations, each taking a verified `actor` (name + email), each returning plain English:
`list_skills`, `read_skill`, `propose_edit`, `approve`, `history`, `revert`.
`propose_edit` takes the FULL new content of the files, computed by the caller. It never accepts a
shell command, a raw path, or a patch to apply blindly.

### `src/librarian/server.py`
FastAPI application exposing the above as MCP tools. Identity comes from the authenticated
connector session, never from a tool argument. If identity is unavailable, `propose_edit` still
works but `approve` refuses, because an unattributed publish is worse than no publish.

## Testing requirements

Use pytest. A fake in-memory `GitHubClient` for everything. Required tests, at minimum:

- **`test_publish_always_bumps_version`** - the regression that must fail loudly if a publish ever
  ships without a version bump. This is the most important test in the suite.
- A repository holding two or more plugins works: skills resolve to the right plugin, and publishing
  a skill in one plugin leaves the other plugin's manifest entry untouched.
- A duplicate skill name across two plugins raises an ambiguity error rather than picking one.
- Nothing in the codebase hardcodes an organization, repository, plugin, or skill name. Assert this
  with a test that greps the package source for a small denylist of such strings.
- Both manifests always end up carrying the same version.
- Publish goes through a pull request and merge, never a direct push to the default branch.
- A publish whose version would not be strictly higher than the one already on the default branch is
  refused before `merge_pr` is ever called, proved by there being no merge in the recorded calls.
- Every stand-in offers every method the `GitHubClient` contract offers, spelled the same way, so a
  test can never be green about behaviour production does not have.
- A failed publish takes its working branch away, and the same proposal can then be published again.
- A publish that fails after the pull request was opened leaves no open pull request behind.
- Deleting the default branch is refused; deleting a branch that is already gone is not an error.
- Closing a pull request that is already closed, or that is not there, is not an error; a permission
  problem or a server problem while closing one still is, however the answer happens to be worded.
- A rebuild onto a newer starting point is refused when somebody else changed one of the very files
  this change writes, proved by that file still holding their content afterwards and by the publish
  having raised. A rebuild that only leaves untouched files alone is not evidence of anything.
- Approving with a stale or altered `diff_hash` is refused.
- An expired proposal is refused.
- Path traversal, absolute paths, and writes to `.github/` or the manifests are all refused.
- Unknown frontmatter keys are refused; `parse`/`render` round-trips exactly.
- The commit author is the human requester and the committer is the app.
- Revert produces a new forward commit through the same path, never a force push.

## Style

Python 3.12+. Standard library plus `fastapi`, `httpx`, `pydantic`, `PyJWT`, `PyYAML`, `pytest`.
Type hints throughout. No cleverness. Error messages a non-technical reader could act on without help.
