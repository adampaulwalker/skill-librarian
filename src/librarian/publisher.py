"""The single write path.

Everything that reaches the shared repository goes through :func:`publish`.

The failure this module exists to prevent is silent. If a change is committed without the plugin
version number moving forward, Claude keeps serving the copy people already have and nobody is told
anything is wrong. So the version bump is not a courtesy step here, it is a precondition: if the new
version is not different from the old one, nothing is written at all.

A library can hold several collections of skills at once, so nothing here assumes there is only one.
The collection that owns the skill being published is worked out first, from the library's own
manifest, and it is the only collection whose version number moves. Every other collection's entry
in the library manifest is left exactly as it was, byte for byte, because a version number that
moves on its own would push a change at people who never asked for one.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import Config
from .errors import LibrarianError, PublishFailed, SkillNotFound, UnsafePath
from .github import GitHubClient
from .marketplace import (
    MARKETPLACE_MANIFEST,
    PLUGIN_MANIFEST_SUFFIX,
    SKILLS_DIR_NAME,
    PluginRef,
    parse_marketplace,
    resolve_skill,
)
from .paths import validate_plugin_dir, validate_skill_name
from .proposals import Proposal
from .versioning import bump_version

__all__ = [
    "MARKETPLACE_MANIFEST_PATH",
    "PLUGIN_MANIFEST_SUFFIX",
    "PublishResult",
    "estimated_live_by",
    "plugin_manifest_path",
    "plugin_owning_paths",
    "publish",
]

#: The library manifest, always at the root of the repository. Never writable by an edit.
MARKETPLACE_MANIFEST_PATH = MARKETPLACE_MANIFEST

_BRANCH_PREFIX = "librarian/"
_IDENTITY_WITH_EMAIL = re.compile(r"^(?P<name>[^<>]*?)\s*<(?P<email>[^<>\s]+)>$")
_FALLBACK_EMAIL_DOMAIN = "users.noreply.github.com"
_REFERENCE_DIR_NAME = "reference"
_REFERENCE_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}\.md$")
_SKILL_FILE_NAME = "SKILL.md"

_NOTHING_PUBLISHED = "Nothing has been published."


@dataclass(frozen=True)
class PublishResult:
    """What happened when a change was published."""

    commit_sha: str
    pr_number: int
    new_version: str
    estimated_live_by: str
    #: The collection the skill belongs to. Empty only if a caller builds this by hand.
    plugin_name: str = ""


def plugin_manifest_path(plugin: PluginRef) -> str:
    """Repo relative path of a collection's own manifest, the file that gates delivery."""
    return plugin.manifest_path


def publish(gh: GitHubClient, cfg: Config, proposal: Proposal, bump: str = "patch") -> PublishResult:
    """Publish an approved proposal, in the fixed order that makes the change actually arrive.

    Raises :class:`PublishFailed` if the version number would not move, and
    :class:`UnsafePath` if the proposal tries to write a file it is not allowed to write.
    """
    skill_name = _skill_name_of(proposal)
    cleaned = _cleaned_paths(proposal)

    # Step 1. Work out which collection owns this skill, then read that collection's manifest and
    # the library manifest at the commit the change was prepared against.
    plugin = _owning_plugin(gh, cfg, proposal, skill_name, cleaned)
    files_to_write = _checked_proposal_files(plugin, skill_name, cleaned)

    base_plugin_manifest = _read_manifest(gh, plugin.manifest_path, proposal.base_sha)
    base_marketplace_text = _read_text(gh, MARKETPLACE_MANIFEST, proposal.base_sha)
    old_version = _current_version(base_plugin_manifest, plugin)

    # The branch is cut from wherever the shared copy is now, so read the version there too. If
    # somebody else published in the meantime, publishing this one would quietly move the version
    # backwards, which is the same silent failure in a different coat.
    head_sha = gh.get_ref_sha(cfg.default_branch)
    if head_sha == proposal.base_sha:
        head_plugin_manifest = base_plugin_manifest
        head_marketplace_text = base_marketplace_text
    else:
        head_plugin_manifest = _read_manifest(gh, plugin.manifest_path, head_sha)
        head_marketplace_text = _read_text(gh, MARKETPLACE_MANIFEST, head_sha)
        if _current_version(head_plugin_manifest, plugin) != old_version:
            raise PublishFailed(
                "Someone else published a change to these skills while this one was waiting, so "
                "this change is no longer based on the latest copy. Please ask for the change "
                "again and it will start from the current version.",
                detail=f"{plugin.name} moved from {old_version} before this change was published",
            )

    # Step 2. Work out the new version and put it into both manifests, kept in step.
    new_version = _bumped_version(old_version, bump)
    if new_version == old_version:
        raise PublishFailed(
            "The version number did not move forward, so this change was not published. Without a "
            "new version number the change would reach the repository but never reach anyone in "
            "Claude, and nobody would be told. Nothing has been changed."
        )

    plugin_text = _render_json(_with_version(head_plugin_manifest, new_version))
    marketplace_text = _marketplace_text_with_version(head_marketplace_text, plugin, new_version)
    _assert_manifests_in_step(
        plugin_text, marketplace_text, head_marketplace_text, plugin, new_version
    )

    # Work out who this commit belongs to before anything is written. A change is only published
    # when it can be recorded under the name of the person who asked for it.
    author_name, author_email = _split_identity(proposal.requested_by)

    # Step 3. Cut a branch. The shared branch is never written to directly, because a change pushed
    # straight to it does not trigger any delivery at all.
    branch = _branch_name(cfg, proposal)

    try:
        gh.create_branch(branch, head_sha)
    except LibrarianError:
        raise
    except Exception as exc:  # noqa: BLE001 - turned into a plain English message below
        raise PublishFailed(
            "A working copy for this change could not be started, so nothing has been changed for "
            "anyone. Please try again.",
            detail=f"create_branch failed: {exc}",
        ) from exc

    # Step 4. One commit carries the edited files and both manifests together, so there is no state
    # where the content moved and the version did not.
    payload: dict[str, str] = dict(files_to_write)
    payload[plugin.manifest_path] = plugin_text
    payload[MARKETPLACE_MANIFEST] = marketplace_text

    try:
        commit_sha = gh.commit_files(
            branch,
            payload,
            _commit_message(proposal, skill_name, new_version, author_name, author_email),
            author_name,
            author_email,
        )
    except LibrarianError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PublishFailed(
            "The change could not be saved, so nothing has been changed for anyone. Please try "
            "again.",
            detail=f"commit_files failed: {exc}",
        ) from exc

    # Step 5. Open the pull request.
    title = f"Update the {skill_name} skill (version {new_version})"
    try:
        pr_number = gh.open_pr(
            branch,
            cfg.default_branch,
            title,
            _pull_request_body(
                cfg, proposal, skill_name, plugin, new_version, author_name, author_email,
                files_to_write,
            ),
        )
    except LibrarianError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PublishFailed(
            "The change was saved on its own working copy but could not be put forward for merging, "
            "so nothing has been changed for anyone yet. Please try again.",
            detail=f"open_pr failed: {exc}",
        ) from exc

    # Step 6. Merge it. Only a merge into the shared branch makes the change reach people.
    try:
        gh.merge_pr(pr_number, title)
    except LibrarianError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PublishFailed(
            "The change is ready and waiting but it could not be merged, so it has not reached "
            "anyone yet. Nothing in the shared skills has changed. Please try again.",
            detail=f"merge_pr failed: {exc}",
        ) from exc

    # Step 7.
    return PublishResult(
        commit_sha=commit_sha,
        pr_number=pr_number,
        new_version=new_version,
        estimated_live_by=estimated_live_by(cfg),
        plugin_name=plugin.name,
    )


def estimated_live_by(cfg: Config, now: datetime | None = None) -> str:
    """A window, never a promise. Nothing about this delivery is instant or precisely timed."""
    minutes = max(1, int(cfg.sync_estimate_minutes))
    moment = now or datetime.now(timezone.utc)
    return (moment + timedelta(minutes=minutes)).strftime("%H:%M UTC on %d %B %Y")


# ==============================================================================================
# Which collection owns the skill
# ==============================================================================================


def plugin_owning_paths(
    plugins: list[PluginRef], skill_name: str, paths: list[str]
) -> PluginRef | None:
    """The one collection whose skill folder holds every one of these paths, if there is one.

    This is how a skill that does not exist yet finds its home: the file names say which
    collection it is being added to. Nothing is guessed - if the paths do not all sit inside a
    single collection, this answers with nothing and the caller asks the person.
    """
    matches = [
        plugin
        for plugin in plugins
        if paths
        and all(path.startswith(skill_folder(plugin, skill_name)) for path in paths)
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def skill_folder(plugin: PluginRef, skill_name: str) -> str:
    """The folder one skill lives in, with a trailing slash so prefix checks are exact."""
    return f"{plugin.plugin_dir}/{SKILLS_DIR_NAME}/{skill_name}/"


def _owning_plugin(
    gh: GitHubClient, cfg: Config, proposal: Proposal, skill_name: str, paths: dict[str, str]
) -> PluginRef:
    """Find the collection this skill belongs to, or refuse to publish."""
    try:
        return resolve_skill(gh, cfg, skill_name, proposal.base_sha).plugin
    except SkillNotFound:
        # Not there yet, so this is a new skill and the file names say where it is going.
        pass
    except LibrarianError as exc:
        raise _publish_failure(exc) from exc

    plugins = _plugins_at(gh, cfg, proposal.base_sha)
    chosen = plugin_owning_paths(plugins, skill_name, sorted(paths))
    if chosen is not None:
        return chosen
    if len(plugins) == 1:
        # There is only one collection, so there is nothing to choose between. The file names are
        # checked next, and a name that does not belong is refused there with a clear reason.
        return plugins[0]
    names = ", ".join(sorted(plugin.name for plugin in plugins))
    raise PublishFailed(
        f'There is no skill called "{skill_name}" yet, and this library keeps more than one '
        f"collection of skills: {names}. Please say which collection the new skill belongs in, so "
        "it is added in the right place. " + _NOTHING_PUBLISHED,
        detail=f"new skill {skill_name!r} did not name a collection",
    )


def _plugins_at(gh: GitHubClient, cfg: Config, ref: str) -> list[PluginRef]:
    text = _read_text(gh, MARKETPLACE_MANIFEST, ref)
    try:
        return parse_marketplace(text)
    except LibrarianError as exc:
        raise _publish_failure(exc) from exc


def _publish_failure(exc: LibrarianError) -> PublishFailed:
    """Repeat a plain English reason back, making clear that nothing reached anyone."""
    message = (exc.user_message or "").strip()
    if not message:
        return PublishFailed(detail=exc.detail)
    if "nothing has been" not in message.lower():
        message = f"{message} {_NOTHING_PUBLISHED}"
    return PublishFailed(message, detail=exc.detail)


# ==============================================================================================
# Reading and writing the two manifests
# ==============================================================================================


def _read_text(gh: GitHubClient, path: str, ref: str) -> str:
    try:
        text, _blob_sha = gh.get_file(path, ref)
    except LibrarianError as exc:
        raise _publish_failure(exc) from exc
    except Exception as exc:  # noqa: BLE001
        raise PublishFailed(
            "One of the settings files that decides how these skills are delivered could not be "
            "read, so nothing was published. Someone with access to the library needs to check "
            "that the file is there.",
            detail=f"could not read {path} at {ref}: {exc}",
        ) from exc
    if not isinstance(text, str):
        raise PublishFailed(
            "One of the settings files that decides how these skills are delivered could not be "
            "read as text, so nothing was published. Someone with access to the library needs to "
            "check it.",
            detail=f"{path} at {ref} was {type(text).__name__}, not text",
        )
    return text


def _read_manifest(gh: GitHubClient, path: str, ref: str) -> dict[str, Any]:
    text = _read_text(gh, path, ref)
    return _loaded_manifest(text, path)


def _loaded_manifest(text: str, path: str) -> dict[str, Any]:
    try:
        loaded = json.loads(text)
    except ValueError as exc:
        raise PublishFailed(
            "One of the settings files that decides how these skills are delivered is damaged, so "
            "nothing was published. Someone with access to the library needs to fix that file "
            "first.",
            detail=f"{path} is not valid JSON: {exc}",
        ) from exc
    if not isinstance(loaded, dict):
        raise PublishFailed(
            "One of the settings files that decides how these skills are delivered is not in the "
            "shape this tool expects, so nothing was published. Someone with access to the library "
            "needs to check it.",
            detail=f"{path} holds {type(loaded).__name__}, not an object",
        )
    return loaded


def _current_version(manifest: dict[str, Any], plugin: PluginRef) -> str:
    version = manifest.get("version")
    if not isinstance(version, str) or not version.strip():
        raise PublishFailed(
            f'The collection of skills called "{plugin.name}" has no version number recorded, so '
            "there is nothing to move forward from and nothing was published. Someone with access "
            "to the library needs to add one.",
            detail=f"{plugin.manifest_path} has no usable version",
        )
    return version.strip()


def _bumped_version(current: str, bump: str) -> str:
    try:
        candidate = bump_version(current, bump)
    except LibrarianError as exc:
        raise _publish_failure(exc) from exc
    except Exception as exc:  # noqa: BLE001
        raise PublishFailed(
            "The next version number could not be worked out from the current one, so nothing was "
            "published. Someone with access to the library needs to check the version number.",
            detail=f"bump_version({current!r}, {bump!r}) failed: {exc}",
        ) from exc

    if not isinstance(candidate, str) or not candidate.strip():
        raise PublishFailed(
            "The next version number came back empty, so nothing was published. Nothing has been "
            "changed for anyone.",
            detail=f"bump_version({current!r}, {bump!r}) returned {candidate!r}",
        )
    return candidate.strip()


def _with_version(manifest: dict[str, Any], new_version: str) -> dict[str, Any]:
    updated = dict(manifest)
    updated["version"] = new_version
    return updated


def _marketplace_text_with_version(text: str, plugin: PluginRef, new_version: str) -> str:
    """Record the new version against one collection, and touch nothing else in the file.

    The entry for the collection being published is rewritten. Every other entry keeps the exact
    bytes it already had, including whatever spacing the team writes their manifest with, so a
    publish can never look like a change to somebody else's collection.
    """
    index = _entry_index(text, plugin)
    data = _loaded_manifest(text, MARKETPLACE_MANIFEST)
    entries = data.get("plugins")
    if not isinstance(entries, list) or index >= len(entries):
        raise PublishFailed(
            "The library's list of skill collections does not have an entry for these skills, so "
            "the new version could not be recorded there and nothing was published. Someone with "
            "access to the library needs to add that entry.",
            detail=f"no entry at position {index} for {plugin.name!r}",
        )
    entry = entries[index]
    if not isinstance(entry, dict):
        raise PublishFailed(
            "The library's list of skill collections is not in the shape this tool expects, so "
            "nothing was published. Someone with access to the library needs to check it.",
            detail=f"entry {index} is {type(entry).__name__}, not an object",
        )

    updated_entry = _with_version(entry, new_version)
    spliced = _replaced_entry_text(text, index, updated_entry)
    if spliced is not None:
        return spliced

    # The file could not be edited in place, so it is written out again from scratch. Every
    # entry still carries exactly the settings it had, only the spacing may be tidied.
    rewritten = dict(data)
    rewritten_entries = list(entries)
    rewritten_entries[index] = updated_entry
    rewritten["plugins"] = rewritten_entries
    return _render_json(rewritten)


def _entry_index(text: str, plugin: PluginRef) -> int:
    """Where this collection sits in the library manifest, read from the manifest itself."""
    try:
        listed = parse_marketplace(text)
    except LibrarianError as exc:
        raise _publish_failure(exc) from exc
    for index, candidate in enumerate(listed):
        if candidate.name == plugin.name and candidate.plugin_dir == plugin.plugin_dir:
            return index
    raise PublishFailed(
        f'The library\'s list of skill collections has no entry for "{plugin.name}", so the new '
        "version could not be recorded there and nothing was published. Someone with access to the "
        "library needs to add that entry.",
        detail=f"{plugin.name!r} at {plugin.plugin_dir!r} is not listed",
    )


def _assert_manifests_in_step(
    plugin_text: str,
    marketplace_text: str,
    marketplace_before: str,
    plugin: PluginRef,
    new_version: str,
) -> None:
    """Read back what is about to be committed and confirm it says what it has to say."""
    written_plugin = _loaded_manifest(plugin_text, plugin.manifest_path)
    if written_plugin.get("version") != new_version:
        raise PublishFailed(
            "The new version number was not written into this collection's settings, so nothing "
            "was published. Nothing has been changed for anyone.",
            detail=f"{plugin.manifest_path} did not take version {new_version}",
        )

    written = _loaded_manifest(marketplace_text, MARKETPLACE_MANIFEST)
    before = _loaded_manifest(marketplace_before, MARKETPLACE_MANIFEST)
    written_entries = written.get("plugins")
    before_entries = before.get("plugins")
    if not isinstance(written_entries, list) or not isinstance(before_entries, list):
        raise PublishFailed(
            "The library's list of skill collections is not in the shape this tool expects, so "
            "nothing was published. Someone with access to the library needs to check it.",
            detail="the plugins key is not a list",
        )
    if len(written_entries) != len(before_entries):
        raise PublishFailed(
            "Recording the new version would have added or removed a collection of skills, so "
            "nothing was published. Nothing has been changed for anyone.",
            detail=f"{len(before_entries)} entries became {len(written_entries)}",
        )

    index = _entry_index(marketplace_text, plugin)
    if not isinstance(written_entries[index], dict):
        raise PublishFailed(
            "The library's list of skill collections is not in the shape this tool expects, so "
            "nothing was published. Someone with access to the library needs to check it.",
            detail=f"entry {index} is not an object",
        )
    if written_entries[index].get("version") != new_version:
        raise PublishFailed(
            "The two settings files ended up with different version numbers, so nothing was "
            "published. Nothing has been changed for anyone.",
            detail=f"entry {index} did not take version {new_version}",
        )

    for position, (was, now) in enumerate(zip(before_entries, written_entries)):
        if position == index:
            continue
        if was != now:
            raise PublishFailed(
                "Recording the new version would have changed another collection of skills as "
                "well, so nothing was published. Nothing has been changed for anyone.",
                detail=f"entry {position} changed while publishing {plugin.name!r}",
            )


def _render_json(data: dict[str, Any]) -> str:
    """Two space indent and a trailing newline, so the change stays easy to read in the history."""
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


# ==============================================================================================
# Editing one entry of a JSON file without disturbing the rest of it
# ==============================================================================================


class _ScanFailed(Exception):
    """The manifest could not be edited in place, so it will be written out again instead."""


def _replaced_entry_text(text: str, index: int, entry: dict[str, Any]) -> str | None:
    """Swap one entry of the plugins list for a new one, leaving every other byte alone.

    Returns nothing if the file cannot be walked through confidently, in which case the caller
    writes the whole file out again rather than risk a careless edit.
    """
    try:
        spans = _entry_spans(text)
    except _ScanFailed:
        return None
    if index >= len(spans):
        return None

    start, end = spans[index]
    line_start = text.rfind("\n", 0, start) + 1
    indent = text[line_start:start]
    if indent.strip():
        indent = ""
    rendered = json.dumps(entry, indent=2, ensure_ascii=False).replace("\n", "\n" + indent)
    return text[:start] + rendered + text[end:]


def _entry_spans(text: str) -> list[tuple[int, int]]:
    """Where each entry of the plugins list starts and ends in the raw text of the manifest."""
    start, end = _root_member_span(text, "plugins")
    if text[start] != "[":
        raise _ScanFailed()

    spans: list[tuple[int, int]] = []
    index = _skip_space(text, start + 1)
    if index < end and text[index] == "]":
        return spans
    while True:
        value_end = _value_end(text, index)
        spans.append((index, value_end))
        index = _skip_space(text, value_end)
        if index >= len(text):
            raise _ScanFailed()
        if text[index] == ",":
            index = _skip_space(text, index + 1)
            continue
        if text[index] == "]":
            return spans
        raise _ScanFailed()


def _root_member_span(text: str, key: str) -> tuple[int, int]:
    """Where the value of one top level key starts and ends."""
    index = _skip_space(text, 0)
    if index >= len(text) or text[index] != "{":
        raise _ScanFailed()
    index = _skip_space(text, index + 1)
    while True:
        if index >= len(text):
            raise _ScanFailed()
        if text[index] != '"':
            raise _ScanFailed()
        name_end = _string_end(text, index)
        try:
            name = json.loads(text[index:name_end])
        except ValueError as exc:
            raise _ScanFailed() from exc
        index = _skip_space(text, name_end)
        if index >= len(text) or text[index] != ":":
            raise _ScanFailed()
        value_start = _skip_space(text, index + 1)
        value_end = _value_end(text, value_start)
        if name == key:
            return value_start, value_end
        index = _skip_space(text, value_end)
        if index < len(text) and text[index] == ",":
            index = _skip_space(text, index + 1)
            continue
        raise _ScanFailed()


def _skip_space(text: str, index: int) -> int:
    while index < len(text) and text[index] in " \t\r\n":
        index += 1
    return index


def _string_end(text: str, index: int) -> int:
    """The position just past a quoted string that starts at ``index``."""
    index += 1
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == '"':
            return index + 1
        index += 1
    raise _ScanFailed()


def _value_end(text: str, index: int) -> int:
    """The position just past the JSON value that starts at ``index``."""
    if index >= len(text):
        raise _ScanFailed()
    char = text[index]
    if char == '"':
        return _string_end(text, index)
    if char in "[{":
        depth = 0
        while index < len(text):
            here = text[index]
            if here == '"':
                index = _string_end(text, index)
                continue
            if here in "[{":
                depth += 1
            elif here in "]}":
                depth -= 1
                if depth == 0:
                    return index + 1
            index += 1
        raise _ScanFailed()
    end = index
    while end < len(text) and text[end] not in ",]} \t\r\n":
        end += 1
    if end == index:
        raise _ScanFailed()
    return end


# ==============================================================================================
# Paths that may be written
# ==============================================================================================


def _skill_name_of(proposal: Proposal) -> str:
    try:
        return validate_skill_name(proposal.skill_name)
    except LibrarianError as exc:
        raise _publish_failure(exc) from exc


def _cleaned_paths(proposal: Proposal) -> dict[str, str]:
    """Tidy every file name and refuse the ones that point somewhere they should not."""
    if not proposal.files:
        raise PublishFailed(
            "There is nothing in this change to publish, so nothing was changed.",
            detail="the proposal carried no files",
        )

    cleaned: dict[str, str] = {}
    for raw_path, content in proposal.files.items():
        path = _clean_path(raw_path)
        if not isinstance(content, str):
            raise PublishFailed(
                "The new contents of one of the files could not be read as text, so nothing was "
                "changed.",
                detail=f"{path} held {type(content).__name__}, not text",
            )
        cleaned[path] = content
    return cleaned


def _checked_proposal_files(
    plugin: PluginRef, skill_name: str, files: dict[str, str]
) -> dict[str, str]:
    """Every file in the proposal has to be part of this one skill, in this one collection.

    The manifests are deliberately not writable here. They are only ever written by the version
    bump above, so an edit can never quietly rewrite the files that control delivery.

    The collection folder is checked here as well as wherever the edit came from. It arrives
    from the library's own manifest, which is repository content that anybody able to open a
    pull request can change, so the last gate before a write gets no more trust in it than the
    first gate did. Every writable path is anchored to that folder, so a bad value there would
    weaken every other check in this function.
    """
    root = f"{validate_plugin_dir(plugin.plugin_dir)}/{SKILLS_DIR_NAME}/{skill_name}/"
    checked: dict[str, str] = {}

    for path, content in files.items():
        if path == MARKETPLACE_MANIFEST or path.endswith("/" + PLUGIN_MANIFEST_SUFFIX):
            raise UnsafePath(
                "This change tried to edit one of the settings files that control how the skills "
                "are delivered. Those are only ever updated by the publishing step itself, so the "
                "change was not made.",
                detail=f"{path} is a manifest",
            )
        if not path.startswith(root) or len(path) == len(root):
            raise UnsafePath(
                "This change tried to write a file that is not part of this skill, so it was not "
                "made. Only a skill's own instructions and the notes stored with it can be edited "
                "here.",
                detail=f"{path} is not inside {root}",
            )
        _assert_allowed_inside_skill(path, path[len(root) :])
        checked[path] = content

    return checked


def _assert_allowed_inside_skill(path: str, remainder: str) -> None:
    """Inside a skill folder, only the instructions file and reference notes may be written."""
    if remainder == _SKILL_FILE_NAME:
        return
    parts = remainder.split("/")
    if len(parts) == 2 and parts[0] == _REFERENCE_DIR_NAME and _REFERENCE_FILE.match(parts[1]):
        return
    raise UnsafePath(
        "This change tried to write a kind of file I do not handle. A skill is made of its "
        "instructions file and notes stored alongside it, and nothing else, so the change was not "
        "made.",
        detail=f"{path} is not an allowed file inside a skill",
    )


def _clean_path(raw_path: Any) -> str:
    if not isinstance(raw_path, str):
        raise UnsafePath(
            "One of the file names in this change is not readable, so it was not made.",
            detail=f"a file name was {type(raw_path).__name__}, not text",
        )

    path = raw_path.strip()
    while path.startswith("./"):
        path = path[2:]

    rejected = (
        not path
        or path.startswith("/")
        or path.startswith("~")
        or "\\" in path
        or "\x00" in path
        or "//" in path
        or ":" in path
        or any(part in ("", ".", "..") for part in path.split("/"))
    )
    if rejected:
        raise UnsafePath(
            "One of the file names in this change points somewhere it is not allowed to go, so the "
            "change was not made.",
            detail=f"refused the file name {raw_path!r}",
        )
    return path


# ==============================================================================================
# Branch, commit message and pull request wording
# ==============================================================================================


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", value.strip().lower()).strip("-")


def _branch_name(cfg: Config, proposal: Proposal) -> str:
    skill = _slug(proposal.skill_name) or "skill"
    short_id = _slug(proposal.id)[:8] or "change"
    branch = f"{_BRANCH_PREFIX}{skill}-{short_id}"
    if branch == cfg.default_branch:
        raise PublishFailed(
            "The change could not be given a working copy of its own, so nothing was published.",
            detail=f"the branch name collided with {cfg.default_branch!r}",
        )
    return branch


def _split_identity(requested_by: str) -> tuple[str, str]:
    """Turn the verified identity of the requester into a git author name and email."""
    if not isinstance(requested_by, str) or not requested_by.strip():
        raise PublishFailed(
            "It is not clear who asked for this change, so it was not published. A change is only "
            "published when it can be recorded under the name of the person who asked for it.",
            detail="the proposal carried no requester",
        )

    identity = requested_by.strip()
    match = _IDENTITY_WITH_EMAIL.match(identity)
    if match:
        email = match.group("email").strip()
        name = match.group("name").strip() or email
        return name, email

    if "@" in identity and " " not in identity:
        return identity, identity

    fallback = _slug(identity) or "requester"
    return identity, f"{fallback}@{_FALLBACK_EMAIL_DOMAIN}"


def _commit_message(
    proposal: Proposal,
    skill_name: str,
    new_version: str,
    author_name: str,
    author_email: str,
) -> str:
    summary = (proposal.plain_summary or "").strip() or "No summary was given."
    return (
        f"Update the {skill_name} skill (version {new_version})\n"
        "\n"
        f"{summary}\n"
        "\n"
        f"Requested-By: {author_name} <{author_email}>\n"
    )


def _pull_request_body(
    cfg: Config,
    proposal: Proposal,
    skill_name: str,
    plugin: PluginRef,
    new_version: str,
    author_name: str,
    author_email: str,
    files: dict[str, str],
) -> str:
    summary = (proposal.plain_summary or "").strip() or "No summary was given."
    file_lines = "\n".join(f"- {path}" for path in sorted(files))
    return (
        f"{author_name} asked for a change to the {skill_name} skill, which is part of the "
        f"{plugin.name} collection.\n"
        "\n"
        "What changed, in plain English:\n"
        f"{summary}\n"
        "\n"
        "Files updated:\n"
        f"{file_lines}\n"
        "\n"
        f"The version number of the {plugin.name} collection moves to {new_version}. That version "
        "number is what tells Claude to pick the change up. Without it the change would sit here "
        "and never reach anyone. No other collection in this library is touched.\n"
        "\n"
        f"Once this is merged the change is {estimated_live_by(cfg)}\n"
        "\n"
        f"Requested-By: {author_name} <{author_email}>\n"
    )
