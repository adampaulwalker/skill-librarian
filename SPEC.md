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
    def commit_files(self, branch: str, files: dict[str,str], message: str,
                     author_name: str, author_email: str) -> str    # returns commit sha
    def open_pr(self, head: str, base: str, title: str, body: str) -> int
    def merge_pr(self, number: int, commit_title: str) -> str
    def list_commits(self, path: str, limit: int) -> list[dict]
```
Two implementations: `GitHubAppClient` (production - a GitHub App private key, minting short-lived
installation tokens, scoped to the single repository) and `TokenClient` (development only, reads
`LIBRARIAN_DEV_TOKEN`; must log a loud warning that it is not for production).

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
- Approving with a stale or altered `diff_hash` is refused.
- An expired proposal is refused.
- Path traversal, absolute paths, and writes to `.github/` or the manifests are all refused.
- Unknown frontmatter keys are refused; `parse`/`render` round-trips exactly.
- The commit author is the human requester and the committer is the app.
- Revert produces a new forward commit through the same path, never a force push.

## Style

Python 3.12+. Standard library plus `fastapi`, `httpx`, `pydantic`, `PyJWT`, `PyYAML`, `pytest`.
Type hints throughout. No cleverness. Error messages a non-technical reader could act on without help.
