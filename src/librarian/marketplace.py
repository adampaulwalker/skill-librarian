"""Discovering what a library actually holds, by reading its own manifest.

Nothing in this service knows in advance how a team has arranged its repository. One team
keeps a single collection of skills, another keeps six, and each collection can hold any
number of skills. So the layout is read at runtime from the manifest at

    .claude-plugin/marketplace.json

which lists every collection and where it lives inside the repository. Everything else in
the service asks this module where a skill is, rather than assuming a folder name.

Two rules matter more than the parsing:

1. Only a collection stored inside this same repository can be used, written as a relative
   path beginning with ``./``. That is what an organization marketplace allows. A source of
   any other kind is reported by name as unsupported, never quietly skipped, because a
   silently missing collection looks exactly like a missing skill to the person asking.
2. If two collections both hold a skill with the same name, that is refused as an
   ambiguity naming both collections. Picking the first would edit one team's skill while
   somebody believed they were editing the other one's.
"""

from __future__ import annotations

import json
import logging
import posixpath
import unicodedata
from dataclasses import dataclass
from typing import Any

from .config import Config
from .errors import LibrarianError, SkillNotFound, UnsafePath
from .github import GitHubClient
from .paths import validate_skill_name

__all__ = [
    "MARKETPLACE_MANIFEST",
    "PLUGIN_MANIFEST_SUFFIX",
    "SKILLS_DIR_NAME",
    "SKILL_FILE_NAME",
    "PluginRef",
    "SkillRef",
    "parse_marketplace",
    "resolve_skill",
    "list_all_skills",
]

_LOG = logging.getLogger(__name__)

#: The manifest that lists every collection. Always at this exact path, at the repository root.
MARKETPLACE_MANIFEST = ".claude-plugin/marketplace.json"
#: Each collection carries its own manifest at this path, relative to the collection folder.
PLUGIN_MANIFEST_SUFFIX = ".claude-plugin/plugin.json"
#: Skills live in this folder inside a collection.
SKILLS_DIR_NAME = "skills"
#: A collection keeps its own settings, including its version number, in this folder.
PLUGIN_SETTINGS_DIR_NAME = ".claude-plugin"
#: The instructions file that makes a folder a skill.
SKILL_FILE_NAME = "SKILL.md"

_MAX_SOURCE_LENGTH = 200
_MAX_PLUGIN_NAME_LENGTH = 100

_REPAIR_HINT = "Someone with access to the library needs to look at it."

_MSG_MANIFEST_MISSING = (
    "This library does not have the list that says which collections of skills it holds, so "
    "I do not know where anything is kept. " + _REPAIR_HINT
)
_MSG_MANIFEST_UNREADABLE = (
    "The list of skill collections in this library is damaged, so I could not read it. Nothing "
    "has been changed. " + _REPAIR_HINT
)
_MSG_MANIFEST_SHAPE = (
    "The list of skill collections in this library is not in the shape I expect, so I could not "
    "read it. Nothing has been changed. " + _REPAIR_HINT
)
_MSG_NO_PLUGINS = (
    "This library does not list any collections of skills yet, so there is nothing for me to "
    "read. " + _REPAIR_HINT
)


@dataclass(frozen=True)
class PluginRef:
    """One collection of skills, as the library's own manifest describes it."""

    name: str
    #: Where the collection lives, relative to the top of the repository.
    plugin_dir: str
    #: The version recorded in the manifest entry. Empty when the entry does not carry one.
    version: str
    #: The collection's own manifest, which is the file that gates delivery.
    manifest_path: str


@dataclass(frozen=True)
class SkillRef:
    """One skill, and the collection it belongs to."""

    skill_name: str
    plugin: PluginRef
    #: The skill's instructions file, relative to the top of the repository.
    skill_path: str


# ==============================================================================================
# Reading the manifest
# ==============================================================================================


def parse_marketplace(text: str) -> list[PluginRef]:
    """Turn the text of the library manifest into the collections it describes.

    Collections come back in the order the manifest lists them. Anything the manifest asks for
    that this service cannot honour is refused with a message naming the collection, rather
    than dropped, so that a person never sees a collection simply vanish.
    """
    data = _load_manifest_json(text)

    entries = data.get("plugins")
    if entries is None:
        raise LibrarianError(_MSG_NO_PLUGINS, detail="the manifest has no plugins key")
    if not isinstance(entries, list):
        raise LibrarianError(
            _MSG_MANIFEST_SHAPE,
            detail=f"the plugins key is {type(entries).__name__}, not a list",
        )
    if not entries:
        raise LibrarianError(_MSG_NO_PLUGINS, detail="the plugins list is empty")

    plugins: list[PluginRef] = []
    seen_names: dict[str, int] = {}
    seen_dirs: dict[str, str] = {}

    for position, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise LibrarianError(
                _MSG_MANIFEST_SHAPE,
                detail=f"entry {position} is {type(entry).__name__}, not an object",
            )

        name = _plugin_name(entry, position)
        if name in seen_names:
            raise LibrarianError(
                f'This library lists two different collections both called "{name}", so I cannot '
                "tell them apart. " + _REPAIR_HINT,
                detail=f"duplicate plugin name at entries {seen_names[name]} and {position}",
            )

        plugin_dir = _plugin_dir_from_source(name, entry.get("source"))
        if plugin_dir in seen_dirs:
            raise LibrarianError(
                f'The collections called "{seen_dirs[plugin_dir]}" and "{name}" are both stored in '
                "the same place, so I cannot tell them apart. " + _REPAIR_HINT,
                detail=f"duplicate plugin source {plugin_dir!r}",
            )

        seen_names[name] = position
        seen_dirs[plugin_dir] = name
        plugins.append(
            PluginRef(
                name=name,
                plugin_dir=plugin_dir,
                version=_entry_version(entry, name),
                manifest_path=f"{plugin_dir}/{PLUGIN_MANIFEST_SUFFIX}",
            )
        )

    return plugins


def _load_manifest_json(text: str) -> dict[str, Any]:
    if not isinstance(text, str):
        raise LibrarianError(
            _MSG_MANIFEST_UNREADABLE, detail=f"manifest was {type(text).__name__}, not text"
        )
    if not text.strip():
        raise LibrarianError(_MSG_MANIFEST_UNREADABLE, detail="the manifest file is empty")
    try:
        data = json.loads(text)
    except ValueError as exc:
        # The reason it will not parse is a maintainer's problem, so it goes to the log only.
        raise LibrarianError(
            _MSG_MANIFEST_UNREADABLE, detail=f"manifest is not valid JSON: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise LibrarianError(
            _MSG_MANIFEST_SHAPE,
            detail=f"the manifest holds {type(data).__name__}, not an object",
        )
    return data


def _plugin_name(entry: dict[str, Any], position: int) -> str:
    raw = entry.get("name")
    if not isinstance(raw, str) or not raw.strip():
        raise LibrarianError(
            _MSG_MANIFEST_SHAPE,
            detail=f"entry {position} has no usable name",
        )
    name = raw.strip()
    if len(name) > _MAX_PLUGIN_NAME_LENGTH:
        raise LibrarianError(
            _MSG_MANIFEST_SHAPE, detail=f"entry {position} has an unreasonably long name"
        )
    if not name.isascii() or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in name):
        raise LibrarianError(
            _MSG_MANIFEST_SHAPE,
            detail=f"entry {position} has a name with characters I will not repeat back",
        )
    return name


def _entry_version(entry: dict[str, Any], plugin_name: str) -> str:
    """The version on the manifest entry, or empty when the entry does not carry one.

    An absent version is not repaired with a made up default here. The publish path reads the
    collection's own manifest, which is the file that actually gates delivery, and refuses
    there if no usable version exists.
    """
    raw = entry.get("version")
    if raw is None:
        return ""
    if not isinstance(raw, str) or not raw.strip():
        raise LibrarianError(
            f'The collection called "{plugin_name}" has a version number I cannot make sense of, '
            "so I stopped rather than guess. " + _REPAIR_HINT,
            detail=f"version for {plugin_name!r} is {raw!r}",
        )
    return raw.strip()


# ==============================================================================================
# Where a collection is allowed to live
# ==============================================================================================


def _unsupported_source_message(plugin_name: str) -> str:
    return (
        f'The collection of skills called "{plugin_name}" is kept somewhere outside this library, '
        "and I can only work with collections stored inside the library itself. I have stopped "
        "rather than pretend that collection is not there. " + _REPAIR_HINT
    )


def _outside_source_message(plugin_name: str) -> str:
    return (
        f'The collection of skills called "{plugin_name}" points to a place outside this library, '
        "so I will not read it. Nothing has been changed. " + _REPAIR_HINT
    )


def _wrong_source_shape_message(plugin_name: str) -> str:
    """For a folder inside the library that is simply not written the way it has to be."""
    return (
        f'The place this library gives for the collection of skills called "{plugin_name}" is not '
        "written the way I can read. A collection has to be given as a folder inside this library, "
        'written starting with "./". I have stopped rather than pretend that collection is not '
        "there. " + _REPAIR_HINT
    )


def _plugin_dir_from_source(plugin_name: str, source: Any) -> str:
    """Return the repository relative folder for a collection, or refuse the source.

    Only a relative path beginning with ``./`` is supported. Every other kind of source, such
    as another repository, a web address, or a package name, is reported as unsupported and
    named, because quietly ignoring it would hide a whole collection of skills.
    """
    if source is None:
        raise LibrarianError(
            _MSG_MANIFEST_SHAPE, detail=f"the entry for {plugin_name!r} has no source"
        )
    if not isinstance(source, str):
        # A source written as an object is one of the other kinds, such as another repository.
        raise LibrarianError(
            _unsupported_source_message(plugin_name),
            detail=f"the source for {plugin_name!r} is {type(source).__name__}, not a path",
        )

    raw = source
    if not raw.strip():
        raise LibrarianError(
            _MSG_MANIFEST_SHAPE, detail=f"the source for {plugin_name!r} is empty"
        )
    if raw != raw.strip():
        # Padding is refused rather than trimmed. This value decides which folder gets read
        # and later written, so it is taken exactly as written or not at all.
        raise UnsafePath(
            _outside_source_message(plugin_name),
            detail=f"the source for {plugin_name!r} has spaces around it",
        )
    if len(raw) > _MAX_SOURCE_LENGTH:
        raise LibrarianError(
            _MSG_MANIFEST_SHAPE,
            detail=f"the source for {plugin_name!r} is longer than {_MAX_SOURCE_LENGTH} characters",
        )
    if "\x00" in raw or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
        raise UnsafePath(
            _outside_source_message(plugin_name),
            detail=f"the source for {plugin_name!r} has a control character in it",
        )
    if not raw.isascii() or unicodedata.normalize("NFKC", raw) != raw:
        raise UnsafePath(
            _outside_source_message(plugin_name),
            detail=f"the source for {plugin_name!r} has characters outside plain ASCII",
        )
    if "\\" in raw or "%" in raw:
        raise UnsafePath(
            _outside_source_message(plugin_name),
            detail=f"the source for {plugin_name!r} is not a plain relative path: {raw!r}",
        )

    # A step of ".." is checked before the "./" rule so the person is told the real problem.
    if any(part == ".." for part in raw.split("/")):
        raise UnsafePath(
            _outside_source_message(plugin_name),
            detail=f"the source for {plugin_name!r} steps outside the repository: {raw!r}",
        )
    if raw.startswith("/") or posixpath.isabs(raw):
        raise UnsafePath(
            _outside_source_message(plugin_name),
            detail=f"the source for {plugin_name!r} starts at the top of a disk: {raw!r}",
        )
    if not raw.startswith("./"):
        # A web address, another repository, or a package name is a kind of source this service
        # cannot honour at all. A plain folder name is merely written the wrong way. Saying which
        # of the two it is saves the person a guess.
        if ":" in raw or "@" in raw:
            raise LibrarianError(
                _unsupported_source_message(plugin_name),
                detail=f"the source for {plugin_name!r} is not a folder in this repository: {raw!r}",
            )
        raise LibrarianError(
            _wrong_source_shape_message(plugin_name),
            detail=f"the source for {plugin_name!r} does not start with ./: {raw!r}",
        )

    body = raw[2:].rstrip("/")
    if not body:
        raise LibrarianError(
            _MSG_MANIFEST_SHAPE,
            detail=f"the source for {plugin_name!r} points at the whole repository",
        )

    parts = body.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            raise UnsafePath(
                _outside_source_message(plugin_name),
                detail=f"the source for {plugin_name!r} has an empty or relative step: {raw!r}",
            )
        if part != part.strip() or part.startswith(".") or part.endswith("."):
            raise UnsafePath(
                _outside_source_message(plugin_name),
                detail=f"the source for {plugin_name!r} has an unusable folder name: {raw!r}",
            )

    tidied = posixpath.normpath(body)
    if tidied != body:
        raise UnsafePath(
            _outside_source_message(plugin_name),
            detail=f"the source for {plugin_name!r} changes when tidied up: {raw!r}",
        )
    return tidied


# ==============================================================================================
# Finding skills in the repository
# ==============================================================================================


def list_all_skills(gh: GitHubClient, cfg: Config, ref: str) -> list[SkillRef]:
    """Every skill in every collection this library holds.

    A skill that appears in two collections appears twice here, once for each collection, so
    that a person can see the clash for themselves.

    A collection the manifest lists but the repository does not actually contain is reported
    by name, not passed over. A collection that is there but has no skills in it yet is simply
    empty, which is the normal state of a new collection.

    This looks inside each skill folder to confirm it really holds an instructions file, so a
    listing costs one lookup per skill. That is deliberate: a folder that only looks like a
    skill would otherwise be offered to someone who then could not open it.
    """
    skills, _damaged = _everything_in(gh, cfg, ref)
    return skills


def resolve_skill(gh: GitHubClient, cfg: Config, skill_name: str, ref: str) -> SkillRef:
    """Find the one collection that holds this skill.

    Raises :class:`SkillNotFound` when no collection holds it, and refuses outright when two
    collections both do, naming each of them.
    """
    wanted = validate_skill_name(skill_name)
    everything, damaged = _everything_in(gh, cfg, ref)
    matches = [skill for skill in everything if skill.skill_name == wanted]

    if len(matches) == 1:
        return matches[0]

    if len(matches) > 1:
        collections = [match.plugin.name for match in matches]
        raise LibrarianError(
            f'There is more than one skill called "{wanted}" in this library: one in the '
            f"{_join_names(collections)}. I have stopped rather than guess which one you mean. "
            "Tell me which collection it is in, or ask for one of them to be renamed.",
            detail=f"skill {wanted!r} found in {collections}",
        )

    # A folder with the right name but no instructions file inside it is repository damage. The
    # person asking is the one it lands on, so they are told what is actually wrong rather than
    # being told the skill does not exist.
    broken = [plugin_name for plugin_name, folder in damaged if folder == wanted]
    if broken:
        raise SkillNotFound(
            f'There is a folder called "{wanted}" in the {_join_names(broken)}, but it has no '
            "instructions in it, so there is no skill there for me to open. " + _REPAIR_HINT,
            detail=f"folder {wanted!r} has no {SKILL_FILE_NAME} in {broken}",
        )

    raise SkillNotFound(
        _not_found_message(wanted, everything),
        detail=f"skill {wanted!r} is in none of the collections",
    )


def skill_file_path(plugin_dir: str, skill_name: str) -> str:
    """The instructions file for one skill, relative to the top of the repository."""
    return f"{plugin_dir}/{SKILLS_DIR_NAME}/{skill_name}/{SKILL_FILE_NAME}"


def _read_plugins(gh: GitHubClient, cfg: Config, ref: str) -> list[PluginRef]:
    at = _ref_or_default(cfg, ref)
    try:
        text, _blob_sha = gh.get_file(MARKETPLACE_MANIFEST, at)
    except SkillNotFound as exc:
        raise LibrarianError(
            _MSG_MANIFEST_MISSING, detail=f"{MARKETPLACE_MANIFEST} is not in the repository"
        ) from exc
    except LibrarianError:
        raise
    except Exception as exc:  # noqa: BLE001 - turned into a plain English message
        raise LibrarianError(
            _MSG_MANIFEST_MISSING, detail=f"could not read {MARKETPLACE_MANIFEST}: {exc}"
        ) from exc
    return parse_marketplace(text)


def _everything_in(
    gh: GitHubClient, cfg: Config, ref: str
) -> tuple[list[SkillRef], list[tuple[str, str]]]:
    """Walk every collection once, returning the real skills and the damaged folders.

    The damaged folders are carried alongside rather than thrown away, so that someone who
    asks for one by name is told what is actually wrong with it.
    """
    plugins = _read_plugins(gh, cfg, ref)
    at = _ref_or_default(cfg, ref)

    skills: list[SkillRef] = []
    damaged: list[tuple[str, str]] = []
    for plugin in plugins:
        usable, broken = _skills_in(gh, plugin, at)
        for skill_name in usable:
            skills.append(
                SkillRef(
                    skill_name=skill_name,
                    plugin=plugin,
                    skill_path=skill_file_path(plugin.plugin_dir, skill_name),
                )
            )
        damaged.extend((plugin.name, folder) for folder in broken)

    skills.sort(key=lambda item: (item.skill_name, item.plugin.name))
    return skills, damaged


def _skills_in(gh: GitHubClient, plugin: PluginRef, ref: str) -> tuple[list[str], list[str]]:
    """The skills really present in one collection, and the folders that only look like skills.

    A folder counts as a skill when it is named the way a skill has to be named and it holds an
    instructions file. A folder that is named like a skill but holds no instructions is damage,
    and is reported separately so a listing stays usable while the person who asks for that one
    folder still gets a straight answer.
    """
    entries = _collection_entries(gh, plugin, ref)
    skills_entry = _entry_of_any_kind(entries, SKILLS_DIR_NAME)
    if skills_entry is None:
        return [], []
    if skills_entry != "dir":
        raise LibrarianError(
            _damaged_collection_message(plugin.name),
            detail=f"{SKILLS_DIR_NAME} in {plugin.plugin_dir!r} is a {skills_entry}, not a folder",
        )

    skills_dir = f"{plugin.plugin_dir}/{SKILLS_DIR_NAME}"
    listing = _folder_listing(gh, skills_dir, ref, plugin.name, allow_missing=True)

    usable: list[str] = []
    broken: list[str] = []
    for entry in listing:
        raw = _entry_name(entry, "dir")
        if raw is None:
            continue
        if raw != raw.strip() or _usable_skill_name(raw) is None:
            # Nobody could ask for this by name, so it is not a skill and not damage either.
            _LOG.warning(
                "ignoring folder %r in %s because it cannot be a skill name", raw, skills_dir
            )
            continue
        if _holds_skill_file(gh, skills_dir, raw, ref):
            usable.append(raw)
        else:
            _LOG.warning(
                "folder %r in %s has no %s in it, so it is not offered as a skill",
                raw,
                skills_dir,
                SKILL_FILE_NAME,
            )
            broken.append(raw)
    return sorted(set(usable)), sorted(set(broken))


def _collection_entries(gh: GitHubClient, plugin: PluginRef, ref: str) -> list[Any]:
    """What is inside a collection folder, refusing if the collection is not really there.

    A collection the manifest promises but the repository does not hold has to be reported.
    Returning nothing instead would make a whole collection of skills look as though it had
    simply never existed.

    The collection's own settings folder has to be there too, because that folder carries the
    version number that decides whether a published change ever reaches anybody. Its contents
    are checked when a change is published, which is the moment the version actually matters.
    """
    entries = _folder_listing(gh, plugin.plugin_dir, ref, plugin.name, allow_missing=False)
    if _entry_of_any_kind(entries, PLUGIN_SETTINGS_DIR_NAME) != "dir":
        raise LibrarianError(
            _damaged_collection_message(plugin.name),
            detail=f"{plugin.plugin_dir!r} has no {PLUGIN_SETTINGS_DIR_NAME} folder in it",
        )
    return entries


def _folder_listing(
    gh: GitHubClient, path: str, ref: str, plugin_name: str, *, allow_missing: bool
) -> list[Any]:
    """List a folder, telling apart a folder that is absent from one that is not a folder.

    Asking GitHub for the contents of a file answers with the file itself rather than an error,
    so a listing that only describes the thing that was asked for means the path is not a
    folder at all.
    """
    try:
        entries = gh.list_dir(path, ref)
    except SkillNotFound as exc:
        if allow_missing:
            return []
        raise LibrarianError(
            _missing_collection_message(plugin_name),
            detail=f"the folder {path!r} is not in the repository",
        ) from exc
    if not entries:
        if allow_missing:
            return []
        raise LibrarianError(
            _missing_collection_message(plugin_name),
            detail=f"the folder {path!r} is empty",
        )
    if any(isinstance(entry, dict) and entry.get("path") == path for entry in entries):
        raise LibrarianError(
            _damaged_collection_message(plugin_name),
            detail=f"{path!r} is a file, not a folder",
        )
    return entries


def _missing_collection_message(plugin_name: str) -> str:
    return (
        f'This library says it holds a collection of skills called "{plugin_name}", but that '
        "collection is not actually there. I have stopped rather than pretend it never existed. "
        + _REPAIR_HINT
    )


def _damaged_collection_message(plugin_name: str) -> str:
    return (
        f'The collection of skills called "{plugin_name}" is not arranged the way I expect, so I '
        "cannot tell what is in it. Nothing has been changed. " + _REPAIR_HINT
    )


def _collection_holds(entries: list[Any], name: str, kind: str) -> bool:
    return any(_entry_name(entry, kind) == name for entry in entries)


def _entry_of_any_kind(entries: list[Any], name: str) -> str | None:
    """What kind of thing a folder listing holds under this name, or nothing if it holds none."""
    for entry in entries:
        if isinstance(entry, dict) and entry.get("name") == name:
            kind = entry.get("type")
            return kind if isinstance(kind, str) else ""
    return None


def _entry_name(entry: Any, kind: str) -> str | None:
    """The name of a folder listing entry of the kind asked for, or nothing."""
    if not isinstance(entry, dict) or entry.get("type") != kind:
        return None
    raw = entry.get("name")
    return raw if isinstance(raw, str) else None


def _usable_skill_name(raw: str) -> str | None:
    try:
        return validate_skill_name(raw)
    except UnsafePath:
        return None


def _holds_skill_file(gh: GitHubClient, skills_dir: str, skill_name: str, ref: str) -> bool:
    """Whether a folder really holds a skill's instructions file."""
    try:
        entries = gh.list_dir(f"{skills_dir}/{skill_name}", ref)
    except SkillNotFound:
        return False
    return _collection_holds(entries, SKILL_FILE_NAME, "file")


def _ref_or_default(cfg: Config, ref: str) -> str:
    """Fall back to the shared branch when no particular version was asked for."""
    if isinstance(ref, str) and ref.strip():
        return ref.strip()
    return cfg.default_branch


def _not_found_message(wanted: str, everything: list[SkillRef]) -> str:
    available = sorted({skill.skill_name for skill in everything})
    if not available:
        return (
            f'I could not find a skill called "{wanted}", and this library does not have any '
            "skills in it yet."
        )
    shown = ", ".join(available[:20])
    if len(available) > 20:
        shown = shown + ", and others"
    return (
        f'I could not find a skill called "{wanted}". The skills I can see are: {shown}. Please '
        "pick one of those, or ask me to add a new one."
    )


def _join_names(names: list[str]) -> str:
    """Join collection names into a phrase a person can read out loud."""
    quoted = [f'"{name}" collection' for name in names]
    if len(quoted) == 1:
        return quoted[0]
    if len(quoted) == 2:
        return f"{quoted[0]} and one in the {quoted[1]}"
    head = ", one in the ".join(quoted[:-1])
    return f"{head}, and one in the {quoted[-1]}"
