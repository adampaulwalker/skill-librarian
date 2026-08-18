"""The write boundary.

Every file this service is allowed to change has to come through
:func:`assert_safe_repo_path`. The rule is an allow list, not a block list: a path is
refused unless it is exactly one of

    <plugin_dir>/skills/<skill-name>/SKILL.md
    <plugin_dir>/skills/<skill-name>/reference/<file>.md

Everything else is refused, including the two manifest files, anything under ``.github``,
any workflow file, and anything at all outside that one collection folder. The two
manifest files carry the version number, and only the publish step is allowed to touch
them, so an edit that arrives through a tool can never reach them.

``plugin_dir`` is the folder of one collection of skills. It is not a setting and it is
never hardcoded: it is discovered at runtime by reading the library's own manifest, which
means it is repository content rather than something the operator typed. Repository
content can be edited by anyone who can open a pull request, so it is treated as hostile
input in exactly the same way a path is, and it is checked by :func:`validate_plugin_dir`
before anything is measured against it. A collection folder is what every safe path is
anchored to, so a bad value there would weaken every other check in this module.

Input is treated as hostile throughout. A path is checked before and after it is tidied
up, so a value like ``a/../../etc/x`` cannot pass by being cleaned into something
harmless, and web encoded or lookalike characters are refused outright rather than decoded
and trusted. Folder steps are compared one at a time rather than as text, so a collection
called ``alpha`` can never be reached by a path that begins ``alpha-two``.
"""

from __future__ import annotations

import posixpath
import re
import unicodedata

from .errors import UnsafePath

__all__ = [
    "MARKETPLACE_MANIFEST",
    "PLUGIN_MANIFEST_NAME",
    "SKILL_FILE_NAME",
    "REFERENCE_DIR_NAME",
    "SKILLS_DIR_NAME",
    "assert_safe_repo_path",
    "skill_dir",
    "validate_plugin_dir",
    "validate_skill_name",
]

#: The marketplace manifest, at the root of the repository. Never writable by an edit.
MARKETPLACE_MANIFEST = ".claude-plugin/marketplace.json"
#: The plugin manifest, inside the plugin folder. Never writable by an edit.
PLUGIN_MANIFEST_NAME = ".claude-plugin/plugin.json"

SKILL_FILE_NAME = "SKILL.md"
REFERENCE_DIR_NAME = "reference"
SKILLS_DIR_NAME = "skills"

_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$")
_REFERENCE_FILE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}\.md$", re.IGNORECASE)
_DRIVE_LETTER_RE = re.compile(r"^[A-Za-z]:")
#: One step of a collection folder. An allow list, so a character nobody needs in a folder
#: name, such as a question mark or a hash, can never reach a web address built from it.
_FOLDER_STEP_RE = re.compile(r"^[A-Za-z0-9._-]+$")

_MAX_PATH_LENGTH = 400
_MAX_PLUGIN_DIR_LENGTH = 200
_MAX_PLUGIN_DIR_DEPTH = 12

_NAME_HELP = (
    "A skill name has to be lowercase, and can only use letters, numbers and single "
    "hyphens, for example \"weekly-report\". Please pick a name in that shape."
)
_OUTSIDE_HELP = (
    "That file is outside the shelf of skills I look after, so I will not change it."
)
_MANIFEST_HELP = (
    "That file is the one that carries the version number. I update it myself whenever I "
    "publish a change, so it is not something I edit on request."
)
_SETTINGS_HELP = (
    "That file is part of the library's own setup rather than a skill, so I do not change it."
)
_SHAPE_HELP = (
    "Inside a skill I can only change its instructions file, called SKILL.md, or a notes "
    "file kept in its reference folder, which has to end in .md."
)
_COLLECTION_HELP = (
    "This library says one of its collections of skills is kept somewhere I am not allowed "
    "to look, so I have stopped rather than guess. Nothing has been changed. Someone with "
    "access to the library needs to check how it is set up."
)


def validate_skill_name(name: str) -> str:
    """Return the skill name if it is safe to use, otherwise refuse.

    A skill name becomes a folder name, so it is the first thing an attacker would reach
    for. Only plain lowercase names pass, and the value is compared against its own
    unicode normal form first so that lookalike characters cannot slip through.
    """
    if not isinstance(name, str):
        raise UnsafePath(_NAME_HELP, detail=f"skill name was {type(name).__name__}, not text")
    candidate = name.strip()
    if not candidate:
        raise UnsafePath(
            "I need the name of the skill you want me to work on.", detail="empty skill name"
        )
    if not candidate.isascii():
        raise UnsafePath(_NAME_HELP, detail="skill name has characters outside plain ASCII")
    if unicodedata.normalize("NFKC", candidate) != candidate:
        raise UnsafePath(_NAME_HELP, detail="skill name changes when unicode is normalised")
    if not _SKILL_NAME_RE.fullmatch(candidate):
        raise UnsafePath(_NAME_HELP, detail=f"skill name failed the name rule: {name!r}")
    return candidate


def validate_plugin_dir(plugin_dir: str) -> str:
    """Return the collection folder if paths may be anchored to it, otherwise refuse.

    The value arrives from the library's own manifest, so it is repository content and not
    a trusted setting. It has to be a plain relative folder inside the repository: no step
    up with ``..``, no leading slash, no drive letter or other absolute form, no
    backslashes, no null bytes, and no hidden folder such as ``.github`` or the folder that
    holds a manifest.

    Each step of the folder has to be made of plain letters, numbers, dots, underscores and
    hyphens. That is an allow list rather than a list of things to watch out for, so a
    character nobody needs in a folder name, such as a question mark, a hash or a space,
    is refused instead of being carried into a web address built from the folder later.

    Two shapes are accepted and tidied, because both mean exactly the same folder and
    neither can hide anything: a leading ``./``, which is how the manifest writes a source,
    and a trailing slash. Everything else is refused rather than repaired, spare space
    around the value included, because repairing a path means guessing what the sender
    meant.
    """
    if not isinstance(plugin_dir, str):
        raise UnsafePath(
            _COLLECTION_HELP,
            detail=f"the collection folder was {type(plugin_dir).__name__}, not text",
        )
    value = plugin_dir
    if not value:
        raise UnsafePath(_COLLECTION_HELP, detail="the collection folder is empty")
    if value != value.strip():
        raise UnsafePath(_COLLECTION_HELP, detail="the collection folder has spaces around it")
    if len(value) > _MAX_PLUGIN_DIR_LENGTH:
        raise UnsafePath(
            _COLLECTION_HELP,
            detail=f"the collection folder is longer than {_MAX_PLUGIN_DIR_LENGTH} characters",
        )
    _reject_hostile_folder_characters(value)

    if value.startswith("/") or posixpath.isabs(value):
        raise UnsafePath(
            _COLLECTION_HELP,
            detail=f"the collection folder starts at the top of a disk: {plugin_dir!r}",
        )
    if _DRIVE_LETTER_RE.match(value):
        raise UnsafePath(
            _COLLECTION_HELP,
            detail=f"the collection folder starts with a drive letter: {plugin_dir!r}",
        )

    if value.startswith("./"):
        value = value[2:]
    value = value.rstrip("/")
    if not value:
        raise UnsafePath(
            _COLLECTION_HELP,
            detail=f"the collection folder names the whole repository: {plugin_dir!r}",
        )

    # The tidied value is checked again from scratch. Trimming a leading "./" or a trailing
    # slash cannot introduce anything, but checking twice costs nothing and means no later
    # rule depends on the order the trimming happened in.
    _reject_hostile_folder_characters(value)

    parts = value.split("/")
    if len(parts) > _MAX_PLUGIN_DIR_DEPTH:
        raise UnsafePath(
            _COLLECTION_HELP,
            detail=f"the collection folder is more than {_MAX_PLUGIN_DIR_DEPTH} folders deep",
        )
    for part in parts:
        problem = _folder_step_problem(part)
        if problem is not None:
            raise UnsafePath(
                _COLLECTION_HELP,
                detail=f"the collection folder {problem}: {plugin_dir!r}",
            )

    if posixpath.normpath(value) != value:
        raise UnsafePath(
            _COLLECTION_HELP,
            detail=f"the collection folder changes when tidied up: {plugin_dir!r}",
        )
    return value


def skill_dir(plugin_dir: str, skill_name: str) -> str:
    """Where one skill lives, as a repository relative path with forward slashes."""
    folder = validate_plugin_dir(plugin_dir)
    name = validate_skill_name(skill_name)
    return "/".join([folder, SKILLS_DIR_NAME, name])


def assert_safe_repo_path(plugin_dir: str, path: str) -> str:
    """Return the tidied path if it is one this service may write, otherwise refuse.

    The order matters. The collection folder is checked first, so a bad anchor is refused
    whatever the path says. Then hostile characters are refused on the raw path text, the
    path is tidied, the same checks run again on the tidied result, and only then is it
    matched against the allow list.
    """
    plugin_parts = validate_plugin_dir(plugin_dir).split("/")

    raw = _reject_hostile_characters(path)
    tidied = posixpath.normpath(raw)
    if tidied != raw:
        # Nothing legitimate changes shape here, because dot steps and doubled slashes were
        # already refused. A change means the input was trying to hide where it pointed.
        raise UnsafePath(
            _OUTSIDE_HELP, detail=f"path changes when tidied up: {path!r} became {tidied!r}"
        )
    _reject_hostile_characters(tidied)

    parts = tidied.split("/")
    _reject_reserved_areas(parts, tidied)

    prefix = [*plugin_parts, SKILLS_DIR_NAME]
    # Compared one folder step at a time, never as text, so a collection called "alpha"
    # cannot be reached by a path that begins "alpha-two".
    if parts[: len(prefix)] != prefix or len(parts) < len(prefix) + 2:
        raise UnsafePath(
            _OUTSIDE_HELP, detail=f"path is not inside the skills folder: {tidied!r}"
        )

    validate_skill_name(parts[len(prefix)])
    remainder = parts[len(prefix) + 1 :]

    if remainder == [SKILL_FILE_NAME]:
        return tidied
    if (
        len(remainder) == 2
        and remainder[0] == REFERENCE_DIR_NAME
        and _REFERENCE_FILE_RE.fullmatch(remainder[1])
        and ".." not in remainder[1]
    ):
        return tidied
    raise UnsafePath(_SHAPE_HELP, detail=f"path is not an editable skill file: {tidied!r}")


def _reject_hostile_characters(path: str) -> str:
    """Refuse anything that is not a plain, relative, forward slash path."""
    if not isinstance(path, str):
        raise UnsafePath(_OUTSIDE_HELP, detail=f"path was {type(path).__name__}, not text")
    if not path:
        raise UnsafePath(
            "I need to know which file you want me to change.", detail="empty path"
        )
    if len(path) > _MAX_PATH_LENGTH:
        raise UnsafePath(_OUTSIDE_HELP, detail=f"path is longer than {_MAX_PATH_LENGTH} characters")
    if path != path.strip():
        raise UnsafePath(_OUTSIDE_HELP, detail="path has spaces around it")
    if "\x00" in path:
        raise UnsafePath(_OUTSIDE_HELP, detail="path contains a null byte")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in path):
        raise UnsafePath(_OUTSIDE_HELP, detail="path contains a control character")
    if not path.isascii():
        # Lookalike letters and full width slashes are refused rather than folded, because
        # folding them would mean guessing what the sender meant.
        raise UnsafePath(_OUTSIDE_HELP, detail="path contains characters outside plain ASCII")
    if unicodedata.normalize("NFKC", path) != path:
        raise UnsafePath(_OUTSIDE_HELP, detail="path changes when unicode is normalised")
    if "\\" in path:
        raise UnsafePath(_OUTSIDE_HELP, detail="path contains a backslash")
    if "%" in path:
        raise UnsafePath(_OUTSIDE_HELP, detail="path looks web encoded")
    if "://" in path or path.startswith("~"):
        raise UnsafePath(_OUTSIDE_HELP, detail="path points somewhere other than this repository")
    if "->" in path:
        raise UnsafePath(_OUTSIDE_HELP, detail="path looks like a shortcut to another file")
    if path.startswith("/") or posixpath.isabs(path):
        raise UnsafePath(_OUTSIDE_HELP, detail="path starts at the top of a disk")
    if _DRIVE_LETTER_RE.match(path):
        raise UnsafePath(_OUTSIDE_HELP, detail="path starts with a drive letter")
    if path.endswith("/"):
        raise UnsafePath(_SHAPE_HELP, detail="path names a folder, not a file")
    for part in path.split("/"):
        if part == "":
            raise UnsafePath(_OUTSIDE_HELP, detail="path has an empty step in it")
        if part == "." or part == "..":
            raise UnsafePath(_OUTSIDE_HELP, detail="path steps up or sideways with a dot step")
        if part != part.strip():
            raise UnsafePath(_OUTSIDE_HELP, detail="path has spaces around one of its steps")
        if part.endswith("."):
            raise UnsafePath(_OUTSIDE_HELP, detail="path has a step ending in a dot")
        if part.startswith("."):
            raise UnsafePath(_SETTINGS_HELP, detail=f"path has a hidden step: {part!r}")
    return path


def _reject_hostile_folder_characters(value: str) -> None:
    """Refuse a folder value that is not plain, relative and written with forward slashes.

    This runs on the collection folder rather than on a file path, so it says nothing about
    file names. It is deliberately the same set of refusals as for a path: the collection
    folder arrives from repository content, which anyone able to open a pull request can
    change, so it gets no more trust than a path typed into a tool.
    """
    if "\x00" in value:
        raise UnsafePath(_COLLECTION_HELP, detail="the collection folder contains a null byte")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise UnsafePath(
            _COLLECTION_HELP, detail="the collection folder contains a control character"
        )
    if not value.isascii():
        raise UnsafePath(
            _COLLECTION_HELP,
            detail="the collection folder contains characters outside plain ASCII",
        )
    if unicodedata.normalize("NFKC", value) != value:
        raise UnsafePath(
            _COLLECTION_HELP, detail="the collection folder changes when unicode is normalised"
        )
    if "\\" in value:
        raise UnsafePath(_COLLECTION_HELP, detail="the collection folder contains a backslash")
    if "%" in value:
        raise UnsafePath(_COLLECTION_HELP, detail="the collection folder looks web encoded")
    if "://" in value:
        raise UnsafePath(
            _COLLECTION_HELP, detail="the collection folder points outside this repository"
        )
    if ":" in value or "@" in value:
        # A folder inside this repository never needs either character, while an address of
        # another repository, such as one written for secure shell, always has one of them.
        raise UnsafePath(
            _COLLECTION_HELP, detail="the collection folder names another repository"
        )
    if value.startswith("~"):
        raise UnsafePath(_COLLECTION_HELP, detail="the collection folder points at a home folder")
    if "->" in value:
        raise UnsafePath(
            _COLLECTION_HELP, detail="the collection folder looks like a shortcut elsewhere"
        )


def _folder_step_problem(part: str) -> str | None:
    """Why one step of a collection folder cannot be used, or nothing when it is fine."""
    if part == "":
        return "has an empty step in it"
    if part == "." or part == "..":
        return "steps up or sideways with a dot step"
    if part != part.strip():
        return "has spaces around one of its steps"
    if part.startswith("."):
        return "has a hidden step in it"
    if part.endswith("."):
        return "has a step ending in a dot"
    if not _FOLDER_STEP_RE.fullmatch(part):
        return "has a step with a character I will not use in a folder name"
    return None


def _reject_reserved_areas(parts: list[str], tidied: str) -> None:
    """Refuse the places that must never be written, whatever else the path says."""
    lowered = [part.lower() for part in parts]
    if tidied.lower() == MARKETPLACE_MANIFEST.lower():
        raise UnsafePath(_MANIFEST_HELP, detail="path is the marketplace manifest")
    if "/".join(lowered[-2:]) == PLUGIN_MANIFEST_NAME.lower():
        raise UnsafePath(_MANIFEST_HELP, detail="path is a plugin manifest")
    if ".claude-plugin" in lowered:
        raise UnsafePath(_MANIFEST_HELP, detail="path is inside a manifest folder")
    if ".github" in lowered or "workflows" in lowered:
        raise UnsafePath(_SETTINGS_HELP, detail="path is inside the repository's own settings")
    if lowered[-1].endswith((".yml", ".yaml")):
        raise UnsafePath(_SETTINGS_HELP, detail="path is a workflow style settings file")
