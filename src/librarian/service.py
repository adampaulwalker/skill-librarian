"""Tool level operations for the skill librarian.

Every function here takes a verified ``Actor`` as its first argument and returns plain
English that a non-technical reader can act on. Nothing in this module trusts a name or
an email address that arrived as part of a tool argument - identity is resolved by the
server from the authenticated session and handed in.

A library can hold several collections of skills. Which collection a skill belongs to is
never assumed: it is looked up in the library's own manifest every time, so a person can
ask for a skill by name without knowing or caring where it is kept, and a name that turns
up in two collections is reported as the clash it is rather than quietly picked.

One convention worth stating because another module depends on it: a proposal records
``requested_by`` as a single string in the standard git form ``Name <email>``, because
that is the only field on a proposal that can carry both halves of the human's identity
through to the commit author.
"""

from __future__ import annotations

import difflib
import hmac
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from . import publisher
from .config import Config
from .errors import (
    DiffMismatch,
    InvalidSkill,
    LibrarianError,
    NotAuthorized,
    SkillNotFound,
    UnsafePath,
)
from .github import GitHubClient
from .marketplace import (
    MARKETPLACE_MANIFEST,
    PluginRef,
    SkillRef,
    list_all_skills,
    parse_marketplace,
    resolve_skill,
)
from .paths import assert_safe_repo_path, skill_dir, validate_skill_name
from .proposals import Proposal, ProposalStore, compute_diff_hash
from .skillfile import parse_skill, validate_frontmatter

# Server side limits. These are guards, not suggestions to a model.
MAX_FILES_PER_PROPOSAL = 20
MAX_FILE_BYTES = 200_000


@dataclass(frozen=True)
class Actor:
    """The human on whose behalf the librarian is acting.

    Both fields are empty when the connector could not tell us who is signed in.
    """

    name: str = ""
    email: str = ""

    @property
    def is_identified(self) -> bool:
        return bool(self.name.strip()) and bool(self.email.strip())

    @property
    def display_name(self) -> str:
        return self.name.strip() or "Someone"

    @property
    def git_identity(self) -> str:
        """The requester in the standard git form: ``Name <email>``."""
        if not self.is_identified:
            return ""
        return f"{self.name.strip()} <{self.email.strip()}>"


ANONYMOUS = Actor()


@dataclass(frozen=True)
class ProposalPreview:
    """What ``propose_edit`` hands back so the person can be shown the change."""

    proposal_id: str
    diff_hash: str
    summary: str
    diff: str
    message: str


def _error(kind: type[LibrarianError], message: str) -> LibrarianError:
    """Build one of the librarian's errors carrying a plain-English message.

    The error classes live in another module, so this tolerates either constructor
    shape and always makes sure the plain-English text is the one that reaches the
    reader.
    """
    try:
        err = kind(message)
    except TypeError:
        err = kind()
    if getattr(err, "user_message", None) != message:
        try:
            err.user_message = message  # type: ignore[attr-defined]
        except Exception:
            pass
    return err


def _require_identity(actor: Actor, action: str) -> None:
    if actor.is_identified:
        return
    raise _error(
        NotAuthorized,
        "I do not know who you are, so I cannot "
        + action
        + ". Every change to the shared skills is signed with the name of the person "
        "who asked for it, and a change with nobody's name on it is worse than no "
        "change at all. Please sign in to the connector and try again.",
    )


def _compute_diff_hash(base_sha: str, files: Mapping[str, str]) -> str:
    """A fingerprint of exactly the content the person was shown.

    There is one fingerprint recipe in this service and it lives in the proposals
    module. Working one out here as well would let the two drift apart, and a drifting
    fingerprint means an approval checked against the wrong thing.
    """
    return compute_diff_hash(base_sha, files)


def _entry_field(entry: Mapping[str, Any], *names: str) -> str:
    for name in names:
        value = entry.get(name)
        if isinstance(value, str) and value:
            return value
    return ""


def _read_optional_file(gh: GitHubClient, path: str, ref: str) -> str | None:
    """Return the file's text, or None when the file is not there yet."""
    try:
        text, _blob_sha = gh.get_file(path, ref)
    except SkillNotFound:
        return None
    return text


# ==============================================================================================
# Which collection a skill belongs to
# ==============================================================================================


def _owning_plugin(gh: GitHubClient, cfg: Config, skill_name: str, ref: str) -> PluginRef:
    """The collection that holds an existing skill.

    Anything unclear is raised rather than guessed: a name that is in no collection raises
    :class:`SkillNotFound`, and a name that is in two raises the ambiguity by name.
    """
    return resolve_skill(gh, cfg, skill_name, ref).plugin


def _all_plugins(gh: GitHubClient, cfg: Config, ref: str) -> list[PluginRef]:
    """Every collection this library holds, in the order its manifest lists them."""
    text, _blob_sha = gh.get_file(MARKETPLACE_MANIFEST, ref)
    return parse_marketplace(text)


def _plugin_for_edit(
    gh: GitHubClient, cfg: Config, skill_name: str, ref: str, paths: list[str]
) -> PluginRef:
    """The collection an edit belongs in, whether the skill exists yet or not.

    For a skill that already exists the library manifest decides, and nothing else is
    consulted. For a skill that does not exist yet the file names decide, because they say
    where it is being added. If neither settles it, the person is asked rather than guessed
    at.
    """
    try:
        return _owning_plugin(gh, cfg, skill_name, ref)
    except SkillNotFound:
        pass

    plugins = _all_plugins(gh, cfg, ref)
    chosen = publisher.plugin_owning_paths(plugins, skill_name, sorted(paths))
    if chosen is not None:
        return chosen
    if len(plugins) == 1:
        return plugins[0]

    names = ", ".join(sorted(plugin.name for plugin in plugins))
    raise _error(
        SkillNotFound,
        f"There is no skill called {skill_name} yet, and this library keeps more than one "
        f"collection of skills: {names}. Tell me which collection it should go in and I "
        "will prepare it there.",
    )


def _list_skill_files(
    gh: GitHubClient, plugin: PluginRef, skill_name: str, ref: str
) -> dict[str, str]:
    """Every file that makes up a skill at a point in time, path -> text."""
    root = skill_dir(plugin.plugin_dir, skill_name)
    collected: dict[str, str] = {}
    main = _read_optional_file(gh, f"{root}/SKILL.md", ref)
    if main is not None:
        collected[f"{root}/SKILL.md"] = main
    try:
        entries = gh.list_dir(f"{root}/reference", ref)
    except (SkillNotFound, LibrarianError):
        entries = []
    for entry in entries:
        if _entry_field(entry, "type", "kind") not in ("file", "blob", ""):
            continue
        name = _entry_field(entry, "name")
        if not name.endswith(".md"):
            continue
        text = _read_optional_file(gh, f"{root}/reference/{name}", ref)
        if text is not None:
            collected[f"{root}/reference/{name}"] = text
    return collected


@dataclass(frozen=True)
class _FileChange:
    path: str
    short_path: str
    old_text: str | None
    new_text: str
    added: int
    removed: int

    @property
    def is_new(self) -> bool:
        return self.old_text is None


def _count_changed_lines(old_text: str, new_text: str) -> tuple[int, int]:
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    added = 0
    removed = 0
    for line in difflib.ndiff(old_lines, new_lines):
        if line.startswith("+ "):
            added += 1
        elif line.startswith("- "):
            removed += 1
    return added, removed


def _unified_diff(changes: list[_FileChange]) -> str:
    parts: list[str] = []
    for change in changes:
        old_lines = (change.old_text or "").splitlines(keepends=True)
        new_lines = change.new_text.splitlines(keepends=True)
        label = "(new file)" if change.is_new else change.short_path
        diff = difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"before/{label}",
            tofile=f"after/{change.short_path}",
        )
        text = "".join(line if line.endswith("\n") else line + "\n" for line in diff)
        if text:
            parts.append(text.rstrip("\n"))
    return "\n\n".join(parts)


def _describe_changes(
    actor: Actor,
    skill_name: str,
    plugin: PluginRef,
    changes: list[_FileChange],
    note: str,
    description_before: str,
    description_after: str,
) -> str:
    lines = [
        f"{actor.display_name} asked to change the skill called {skill_name}, which is "
        f"part of the {plugin.name} collection.",
        "",
        "What changes:",
    ]
    for change in changes:
        if change.is_new:
            line_count = len(change.new_text.splitlines())
            lines.append(
                f"- {change.short_path} is new, with {_plural(line_count, 'line', 'lines')}."
            )
        else:
            lines.append(
                f"- {change.short_path}: "
                f"{_plural(change.added, 'line added', 'lines added')}, "
                f"{_plural(change.removed, 'line removed', 'lines removed')}."
            )
    if description_after and description_after != description_before:
        lines.append("")
        lines.append(
            "The one-line description people see when this skill is offered to them "
            f"now reads: {description_after}"
        )
    if note.strip():
        lines.append("")
        lines.append(f"Reason given: {note.strip()}")
    return "\n".join(lines)


def _plural(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def _sync_wording(cfg: Config, skill_name: str, new_version: str) -> str:
    return (
        "It is not on everyone's machine yet. Sharing happens in the background and "
        f"usually finishes within about {cfg.sync_estimate_minutes} minutes, though it "
        "can take longer, and each person picks the change up the next time they start "
        "a new chat. To check it landed, start a fresh chat and ask me to read "
        f"{skill_name} back to you - it will show version {new_version}."
    )


def list_skills(actor: Actor, gh: GitHubClient, cfg: Config) -> str:
    """Every skill in the library, grouped by the collection it belongs to."""
    found = list_all_skills(gh, cfg, cfg.default_branch)
    if not found:
        return (
            "There are no skills in the library yet. Once one is added it will show up "
            "here with its description."
        )

    by_collection: dict[str, list[SkillRef]] = {}
    for skill in found:
        by_collection.setdefault(skill.plugin.name, []).append(skill)

    collections = sorted(by_collection)
    if len(collections) == 1:
        opening = (
            f"There are {_plural(len(found), 'skill', 'skills')} in the library, all in "
            f"the {collections[0]} collection:"
        )
    else:
        opening = (
            f"There are {_plural(len(found), 'skill', 'skills')} in the library, kept in "
            f"{_plural(len(collections), 'collection', 'collections')}:"
        )
    lines = [opening]

    for collection in collections:
        if len(collections) > 1:
            lines.append("")
            lines.append(f"In the {collection} collection:")
        else:
            lines.append("")
        for skill in by_collection[collection]:
            description = _description_of(gh, skill, cfg.default_branch)
            if description:
                lines.append(f"- {skill.skill_name}: {description}")
            else:
                lines.append(f"- {skill.skill_name}: no description is set on this one yet.")

    repeated = sorted(
        {
            skill.skill_name
            for skill in found
            if sum(1 for other in found if other.skill_name == skill.skill_name) > 1
        }
    )
    if repeated:
        lines.append("")
        lines.append(
            "One name is used in more than one collection: "
            + ", ".join(repeated)
            + ". I cannot open or change a skill whose name is used twice, because I "
            "would have no way of knowing which one you mean. Ask for one of them to be "
            "renamed and I can work with both."
        )

    lines.append("")
    lines.append("Ask me to read any of these and I will show you what it says.")
    return "\n".join(lines)


def _description_of(gh: GitHubClient, skill: SkillRef, ref: str) -> str:
    text = _read_optional_file(gh, skill.skill_path, ref)
    if text is None:
        return ""
    try:
        return parse_skill(text).description
    except LibrarianError:
        return ""


def read_skill(actor: Actor, gh: GitHubClient, cfg: Config, skill_name: str) -> str:
    """The full current text of one skill, plus any supporting files."""
    name = validate_skill_name(skill_name)
    found = resolve_skill(gh, cfg, name, cfg.default_branch)
    text = _read_optional_file(gh, found.skill_path, cfg.default_branch)
    if text is None:
        raise _error(
            SkillNotFound,
            f"There is no skill called {name} in the library. Ask me to list the "
            "skills and I will show you what is there.",
        )
    skill = parse_skill(text)
    lines = [
        f"Skill: {name}",
        f"Collection: {found.plugin.name}",
        f"Description: {skill.description}",
        "",
        "This is the whole file, exactly as it is today:",
        "",
        text.rstrip("\n"),
    ]
    supporting = [
        path.rsplit("/", 1)[-1]
        for path in sorted(_list_skill_files(gh, found.plugin, name, cfg.default_branch))
        if "/reference/" in path
    ]
    if supporting:
        lines.append("")
        lines.append(
            "It also has supporting notes in a folder called reference: "
            + ", ".join(supporting)
            + "."
        )
    return "\n".join(lines)


def propose_edit(
    actor: Actor,
    gh: GitHubClient,
    cfg: Config,
    store: ProposalStore,
    skill_name: str,
    files: Mapping[str, str],
    note: str = "",
) -> ProposalPreview:
    """Check a proposed change from top to bottom, then hold it for approval.

    ``files`` carries the FULL new text of each file. Every path and every skill file
    is validated before anything is stored, so a proposal that exists is a proposal
    that is safe to publish.
    """
    name = validate_skill_name(skill_name)

    if not isinstance(files, Mapping) or not files:
        raise _error(
            InvalidSkill,
            "I did not receive any new text to save, so there is nothing to propose.",
        )
    if len(files) > MAX_FILES_PER_PROPOSAL:
        raise _error(
            InvalidSkill,
            f"That change touches {len(files)} files. I only handle up to "
            f"{MAX_FILES_PER_PROPOSAL} at a time, so please split it into smaller "
            "changes.",
        )
    for raw_path, content in files.items():
        if not isinstance(raw_path, str) or not isinstance(content, str):
            raise _error(
                InvalidSkill,
                "Each file needs a name and the full new text to go in it. Something "
                "in that change was not text.",
            )

    base_sha = gh.get_ref_sha(cfg.default_branch)
    plugin = _plugin_for_edit(gh, cfg, name, base_sha, list(files))
    root = skill_dir(plugin.plugin_dir, name)

    # Validate every path against the collection that owns the skill. A path that is not
    # inside this one skill's folder is refused here, whatever it claims to be.
    checked: dict[str, str] = {}
    for raw_path, content in files.items():
        path = assert_safe_repo_path(plugin.plugin_dir, raw_path)
        if not path.startswith(root + "/"):
            raise _error(
                UnsafePath,
                f"I can only change files that belong to the skill called {name}. "
                f"The file {raw_path} sits somewhere else, so I have not saved "
                "anything.",
            )
        if len(content.encode("utf-8")) > MAX_FILE_BYTES:
            raise _error(
                InvalidSkill,
                f"The new text for {path.rsplit('/', 1)[-1]} is too large for me to "
                "handle. Please shorten it and try again.",
            )
        checked[path] = content

    main_path = f"{root}/SKILL.md"
    existing = _list_skill_files(gh, plugin, name, base_sha)

    if main_path not in checked and main_path not in existing:
        raise _error(
            InvalidSkill,
            f"A new skill needs its main file, SKILL.md, before I can save anything "
            f"for {name}.",
        )

    # Validate the skill file itself before anything is stored.
    description_before = ""
    if main_path in existing:
        try:
            description_before = parse_skill(existing[main_path]).description
        except LibrarianError:
            description_before = ""
    description_after = description_before
    if main_path in checked:
        parsed = parse_skill(checked[main_path])
        validate_frontmatter(dict(parsed.frontmatter))
        description_after = parsed.description
        if parsed.name and parsed.name != name:
            raise _error(
                InvalidSkill,
                f"The new text says this skill is called {parsed.name}, but you asked "
                f"me to change {name}. Renaming a skill is not something I do, so I "
                "have not saved anything.",
            )

    changes: list[_FileChange] = []
    for path in sorted(checked):
        new_text = checked[path]
        old_text = existing.get(path)
        if old_text is None:
            old_text = _read_optional_file(gh, path, base_sha)
        if old_text == new_text:
            continue
        added, removed = _count_changed_lines(old_text or "", new_text)
        changes.append(
            _FileChange(
                path=path,
                short_path=path[len(root) + 1 :],
                old_text=old_text,
                new_text=new_text,
                added=added,
                removed=removed,
            )
        )

    if not changes:
        raise _error(
            InvalidSkill,
            f"The text you gave me is exactly what {name} already says, so there is "
            "nothing to change.",
        )

    changed_files = {change.path: change.new_text for change in changes}
    diff_text = _unified_diff(changes)
    summary = _describe_changes(
        actor, name, plugin, changes, note, description_before, description_after
    )
    diff_hash = _compute_diff_hash(base_sha, changed_files)
    proposal = Proposal(
        id=uuid.uuid4().hex[:12],
        skill_name=name,
        requested_by=actor.git_identity,
        base_sha=base_sha,
        files=changed_files,
        diff_text=diff_text,
        plain_summary=summary,
        diff_hash=diff_hash,
        created_at=time.time(),
    )
    store.put(proposal)

    message_lines = [
        summary,
        "",
        "Nothing has changed yet. Read the difference below, and if it is right, "
        "tell me to go ahead.",
        "",
        diff_text,
    ]
    if not actor.is_identified:
        message_lines += [
            "",
            "One thing to sort out first: I do not know who you are, so I cannot put "
            "your name on this change. Please sign in to the connector before "
            "approving it.",
        ]
    return ProposalPreview(
        proposal_id=proposal.id,
        diff_hash=proposal.diff_hash,
        summary=summary,
        diff=diff_text,
        message="\n".join(message_lines),
    )


def approve(
    actor: Actor,
    gh: GitHubClient,
    cfg: Config,
    store: ProposalStore,
    proposal_id: str,
    diff_hash: str,
    bump: str = "patch",
) -> str:
    """Publish a change the person has already been shown, and only that change."""
    _require_identity(actor, "publish this change")

    proposal = store.get(proposal_id)

    supplied = diff_hash.strip() if isinstance(diff_hash, str) else ""
    if not supplied or not hmac.compare_digest(supplied, proposal.diff_hash):
        raise _error(
            DiffMismatch,
            "This does not match the change I showed you, so I have not published "
            "anything. That usually means the change was edited after you saw it, or "
            "an older approval was reused. Ask me to show the change again and "
            "approve the new one.",
        )
    recomputed = _compute_diff_hash(proposal.base_sha, proposal.files)
    if not hmac.compare_digest(recomputed, proposal.diff_hash):
        raise _error(
            DiffMismatch,
            "The saved change no longer matches what you were shown, so I have not "
            "published anything. Please ask for the change again and approve the "
            "fresh copy.",
        )
    if not proposal.requested_by.strip():
        raise _error(
            NotAuthorized,
            "This change has nobody's name attached to it, so I will not publish it. "
            "Please sign in and ask for the change again.",
        )

    result = publisher.publish(gh, cfg, proposal, bump)
    store.delete(proposal.id)

    live_by = _format_live_by(getattr(result, "estimated_live_by", None))
    collection = getattr(result, "plugin_name", "") or ""
    where = f" in the {collection} collection" if collection else ""
    lines = [
        f"Done. The skill called {proposal.skill_name}{where} has been updated and the "
        f"change is now part of the shared library as version {result.new_version}.",
        "",
        proposal.plain_summary,
        "",
        f"It went in as change number {result.pr_number}, and your name is on it.",
        "",
        _sync_wording(cfg, proposal.skill_name, result.new_version),
    ]
    if live_by:
        lines.append(f"Best guess for when everyone has it: around {live_by}.")
    return "\n".join(lines)


def history(
    actor: Actor,
    gh: GitHubClient,
    cfg: Config,
    skill_name: str,
    limit: int = 10,
) -> str:
    """Recent changes to one skill: who asked for it, when, and what changed."""
    name = validate_skill_name(skill_name)
    if limit < 1:
        limit = 1
    if limit > 50:
        limit = 50
    plugin = _owning_plugin(gh, cfg, name, cfg.default_branch)
    commits = gh.list_commits(skill_dir(plugin.plugin_dir, name), limit)
    if not commits:
        return (
            f"The skill called {name} has not been changed since it was added, so "
            "there is nothing to show."
        )
    lines = [
        f"The last {_plural(len(commits), 'change', 'changes')} to {name}, which is part "
        f"of the {plugin.name} collection:",
        "",
    ]
    for commit in commits:
        who = _commit_requester(commit)
        when = _commit_date(commit)
        what = _commit_headline(commit)
        reference = _commit_sha(commit)[:7]
        lines.append(f"- {when}, {who} asked for this: {what} (reference {reference})")
    lines.append("")
    lines.append(
        "If you want to go back to how it read at any point in that list, tell me the "
        "reference next to it and I will prepare the change for you to look at."
    )
    return "\n".join(lines)


def revert(
    actor: Actor,
    gh: GitHubClient,
    cfg: Config,
    store: ProposalStore,
    skill_name: str,
    commit_sha: str,
    note: str = "",
) -> ProposalPreview:
    """Prepare a change that puts a skill back to how it read at an earlier point.

    Nothing is rewritten and nothing is undone in place. The older text is proposed as
    a brand new change, so approving it adds a fresh entry to the history in the same
    way every other change does.
    """
    name = validate_skill_name(skill_name)
    reference = commit_sha.strip() if isinstance(commit_sha, str) else ""
    if not reference:
        raise _error(
            SkillNotFound,
            "I need the reference of the earlier change you want to go back to. Ask "
            f"me for the history of {name} and tell me one of the references it "
            "lists.",
        )

    plugin = _owning_plugin(gh, cfg, name, cfg.default_branch)
    old_files = _list_skill_files(gh, plugin, name, reference)
    if not old_files:
        raise _error(
            SkillNotFound,
            f"I could not find the skill called {name} as it stood at {reference}, so "
            "there is nothing to go back to. Ask me for its history and pick a "
            "reference from that list.",
        )

    reason = note.strip() or f"Putting {name} back to how it read at {reference[:7]}."
    preview = propose_edit(actor, gh, cfg, store, name, old_files, reason)
    message = (
        f"This would put {name} back to how it read at {reference[:7]}. Nothing has "
        "changed yet, and nothing will be erased: approving this adds a new entry to "
        "the history rather than removing the ones in between. Any file added since "
        "then is left where it is.\n\n" + preview.message
    )
    return ProposalPreview(
        proposal_id=preview.proposal_id,
        diff_hash=preview.diff_hash,
        summary=preview.summary,
        diff=preview.diff,
        message=message,
    )


def _commit_sha(commit: Mapping[str, Any]) -> str:
    return _entry_field(commit, "sha", "id", "commit_sha")


def _commit_body(commit: Mapping[str, Any]) -> Mapping[str, Any]:
    inner = commit.get("commit")
    return inner if isinstance(inner, Mapping) else {}


def _commit_message(commit: Mapping[str, Any]) -> str:
    message = _entry_field(commit, "message", "commit_message")
    if message:
        return message
    return _entry_field(_commit_body(commit), "message")


def _commit_headline(commit: Mapping[str, Any]) -> str:
    message = _commit_message(commit)
    for line in message.splitlines():
        if line.strip():
            return line.strip()
    return "a change with no note on it"


def _commit_requester(commit: Mapping[str, Any]) -> str:
    """Prefer the trailer the librarian writes, then the git author."""
    for line in _commit_message(commit).splitlines():
        if line.lower().startswith("requested-by:"):
            value = line.split(":", 1)[1].strip()
            if value:
                return value.split("<")[0].strip() or value
    author = _commit_body(commit).get("author")
    if isinstance(author, Mapping):
        name = _entry_field(author, "name")
        if name:
            return name
    name = _entry_field(commit, "author_name", "author")
    return name or "someone whose name was not recorded"


def _commit_date(commit: Mapping[str, Any]) -> str:
    raw = _entry_field(commit, "date", "created_at", "committed_at")
    if not raw:
        author = _commit_body(commit).get("author")
        if isinstance(author, Mapping):
            raw = _entry_field(author, "date")
    parsed = _parse_timestamp(raw)
    if parsed is None:
        return "at an unrecorded time"
    return "on " + parsed.strftime("%d %B %Y").lstrip("0")


def _parse_timestamp(raw: str) -> datetime | None:
    if not raw:
        return None
    text = raw.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _format_live_by(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, (int, float)):
        moment = datetime.fromtimestamp(float(value), tz=timezone.utc)
    elif isinstance(value, str):
        parsed = _parse_timestamp(value)
        if parsed is None:
            return value
        moment = parsed
    else:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.strftime("%H:%M on %d %B %Y").lstrip("0")
