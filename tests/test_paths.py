"""Tests for the write boundary.

Every name used here is invented for the test. Nothing about any real team, repository or
collection of skills appears, because the point of this module is that it knows none of
those things in advance: the collection folder is handed in as an argument that came from
the library's own manifest, and it is checked as hostile input just like a file path.

The tables below are the contract. A value in a hostile table must be refused. A value in
a good table must be accepted and must come back tidied.
"""

from __future__ import annotations

import dataclasses
import pathlib
from typing import Any

import pytest

from librarian.config import Config
from librarian.errors import UnsafePath
from librarian.paths import (
    assert_safe_repo_path,
    skill_dir,
    validate_plugin_dir,
    validate_skill_name,
)

#: A collection folder used as the anchor for most of the path tests.
PLUGIN_DIR = "plugins/alpha"
#: A second collection in the same repository, to prove one cannot reach into the other.
OTHER_PLUGIN_DIR = "plugins/beta"
#: A skill name that is fine, so that a refusal can only be about the rest of the path.
SKILL = "weekly-report"


# ==============================================================================================
# Skill names
# ==============================================================================================

GOOD_SKILL_NAMES: list[tuple[str, str]] = [
    ("weekly-report", "weekly-report"),
    ("a1", "a1"),
    ("notes-for-new-people", "notes-for-new-people"),
    ("2026-planning", "2026-planning"),
    ("  report-notes  ", "report-notes"),
    ("a" * 64, "a" * 64),
]

HOSTILE_SKILL_NAMES: list[str] = [
    "",
    "   ",
    ".",
    "..",
    "../escape",
    "../../etc/passwd",
    "a/b",
    "a\\b",
    "Weekly-Report",
    "WEEKLY",
    "-leading-hyphen",
    "trailing-hyphen-",
    "a",
    "with space",
    "with\ttab",
    "under_score",
    "skill.md",
    "skill/",
    "/skill",
    "~/skill",
    "skill\x00name",
    "skill\nname",
    "%2e%2e",
    "café-notes",
    "\u0455kill-notes",
    "\uff53kill-notes",
    "a" * 65,
]


@pytest.mark.parametrize(("given", "expected"), GOOD_SKILL_NAMES)
def test_good_skill_names_are_accepted(given: str, expected: str) -> None:
    assert validate_skill_name(given) == expected


@pytest.mark.parametrize("given", HOSTILE_SKILL_NAMES)
def test_hostile_skill_names_are_refused(given: str) -> None:
    with pytest.raises(UnsafePath):
        validate_skill_name(given)


@pytest.mark.parametrize("given", [None, 7, 1.5, b"weekly-report", ["weekly-report"], {}])
def test_skill_names_that_are_not_text_are_refused(given: Any) -> None:
    with pytest.raises(UnsafePath):
        validate_skill_name(given)


# ==============================================================================================
# Collection folders, which arrive from the manifest and are never trusted
# ==============================================================================================

GOOD_PLUGIN_DIRS: list[tuple[str, str]] = [
    ("plugins/alpha", "plugins/alpha"),
    ("./plugins/alpha", "plugins/alpha"),
    ("plugins/alpha/", "plugins/alpha"),
    ("./plugins/alpha/", "plugins/alpha"),
    ("collection", "collection"),
    ("a/b/c/d", "a/b/c/d"),
    ("shared-library/team-notes", "shared-library/team-notes"),
    ("skills-2026", "skills-2026"),
]

HOSTILE_PLUGIN_DIRS: list[str] = [
    "",
    "   ",
    ".",
    "./",
    "..",
    "../",
    "../plugins",
    "../../etc",
    "plugins/../../etc",
    "plugins/alpha/../beta",
    "./..",
    "././plugins",
    "/",
    "/plugins/alpha",
    "//plugins/alpha",
    "///",
    "C:/plugins/alpha",
    "c:\\plugins\\alpha",
    "plugins\\alpha",
    "plugins//alpha",
    "plugins/ alpha",
    "plugins/alpha /beta",
    "plugins/alpha.",
    "plugins/.hidden",
    ".github",
    ".github/workflows",
    ".claude-plugin",
    "plugins/.claude-plugin",
    "~",
    "~/plugins",
    "plugins/alpha\x00",
    "plugins/al\npha",
    "plugins/al\x7fpha",
    "plugins/al%2fpha",
    "http://evil.example/plugins",
    "git@example.com:owner/repo",
    "plugins/alpha->beta",
    "plugins/\u0430lpha",
    "plugins/\uff41lpha",
    "a" * 201,
    "/".join(["a"] * 13),
    # Spare space around the value is refused rather than trimmed away.
    "  ./plugins/alpha  ",
    " plugins/alpha",
    "plugins/alpha ",
    "\tplugins/alpha",
    # Characters nobody needs in a folder name, which could otherwise be carried into a
    # web address built from the folder later on.
    "plugins/alpha?ref=other",
    "plugins/alpha#fragment",
    "plugins/alpha&beta",
    "plugins/alpha=beta",
    "plugins/al pha",
    "plugins/alpha;beta",
    "plugins/alpha|beta",
    "plugins/alpha*",
    "plugins/alpha$beta",
    "plugins/alpha(beta)",
    "plugins/alpha'beta",
    'plugins/alpha"beta',
    "plugins/alpha,beta",
    "plugins/alpha+beta",
    "plugins/alpha!beta",
]


@pytest.mark.parametrize(("given", "expected"), GOOD_PLUGIN_DIRS)
def test_good_collection_folders_are_accepted(given: str, expected: str) -> None:
    assert validate_plugin_dir(given) == expected


@pytest.mark.parametrize("given", HOSTILE_PLUGIN_DIRS)
def test_hostile_collection_folders_are_refused(given: str) -> None:
    with pytest.raises(UnsafePath):
        validate_plugin_dir(given)


@pytest.mark.parametrize("given", [None, 7, 1.5, b"plugins/alpha", ["plugins", "alpha"], {}])
def test_collection_folders_that_are_not_text_are_refused(given: Any) -> None:
    with pytest.raises(UnsafePath):
        validate_plugin_dir(given)


def test_validated_collection_folder_survives_a_second_pass() -> None:
    """Whatever comes back has to be accepted again unchanged, or the value is not settled."""
    for given, expected in GOOD_PLUGIN_DIRS:
        once = validate_plugin_dir(given)
        assert once == expected
        assert validate_plugin_dir(once) == once


# ==============================================================================================
# Where a skill lives
# ==============================================================================================


def test_skill_dir_is_built_from_the_collection_it_was_given() -> None:
    assert skill_dir(PLUGIN_DIR, SKILL) == "plugins/alpha/skills/weekly-report"
    assert skill_dir(OTHER_PLUGIN_DIR, SKILL) == "plugins/beta/skills/weekly-report"


def test_skill_dir_tidies_the_collection_folder() -> None:
    assert skill_dir("./team-library/", "notes-a") == "team-library/skills/notes-a"


def test_skill_dir_refuses_a_hostile_collection_folder() -> None:
    for hostile in HOSTILE_PLUGIN_DIRS:
        with pytest.raises(UnsafePath):
            skill_dir(hostile, SKILL)


def test_skill_dir_refuses_a_hostile_skill_name() -> None:
    for hostile in HOSTILE_SKILL_NAMES:
        with pytest.raises(UnsafePath):
            skill_dir(PLUGIN_DIR, hostile)


def test_the_same_skill_name_in_two_collections_gives_two_places() -> None:
    """Two collections may hold a skill with the same name, and they stay separate."""
    assert skill_dir(PLUGIN_DIR, SKILL) != skill_dir(OTHER_PLUGIN_DIR, SKILL)


# ==============================================================================================
# Paths this service is allowed to write
# ==============================================================================================

GOOD_PATHS: list[tuple[str, str]] = [
    (PLUGIN_DIR, f"{PLUGIN_DIR}/skills/{SKILL}/SKILL.md"),
    (PLUGIN_DIR, f"{PLUGIN_DIR}/skills/{SKILL}/reference/notes.md"),
    (PLUGIN_DIR, f"{PLUGIN_DIR}/skills/{SKILL}/reference/how-we-do-it.md"),
    (PLUGIN_DIR, f"{PLUGIN_DIR}/skills/{SKILL}/reference/Notes.MD"),
    (PLUGIN_DIR, f"{PLUGIN_DIR}/skills/{SKILL}/reference/notes.v2.md"),
    (OTHER_PLUGIN_DIR, f"{OTHER_PLUGIN_DIR}/skills/{SKILL}/SKILL.md"),
    ("collection", f"collection/skills/{SKILL}/SKILL.md"),
    ("a/b/c/d", f"a/b/c/d/skills/{SKILL}/reference/notes.md"),
    ("./plugins/alpha/", f"{PLUGIN_DIR}/skills/{SKILL}/SKILL.md"),
]


@pytest.mark.parametrize(("plugin_dir", "path"), GOOD_PATHS)
def test_good_paths_are_accepted_unchanged(plugin_dir: str, path: str) -> None:
    assert assert_safe_repo_path(plugin_dir, path) == path


HOSTILE_PATHS: list[str] = [
    # Nothing at all.
    "",
    "   ",
    # Stepping up and out of the collection.
    f"{PLUGIN_DIR}/skills/{SKILL}/../../../../etc/passwd",
    f"{PLUGIN_DIR}/skills/{SKILL}/../{SKILL}/SKILL.md",
    f"{PLUGIN_DIR}/skills/../skills/{SKILL}/SKILL.md",
    f"{PLUGIN_DIR}/skills/{SKILL}/reference/../SKILL.md",
    f"{PLUGIN_DIR}/skills/{SKILL}/reference/../../../../.github/workflows/deploy.yml",
    f"{PLUGIN_DIR}/./skills/{SKILL}/SKILL.md",
    "../" + f"{PLUGIN_DIR}/skills/{SKILL}/SKILL.md",
    # Starting somewhere other than inside this repository.
    f"/{PLUGIN_DIR}/skills/{SKILL}/SKILL.md",
    f"//{PLUGIN_DIR}/skills/{SKILL}/SKILL.md",
    f"C:/{PLUGIN_DIR}/skills/{SKILL}/SKILL.md",
    f"~/{PLUGIN_DIR}/skills/{SKILL}/SKILL.md",
    "https://evil.example/SKILL.md",
    "file:///etc/passwd",
    # Characters that hide where a path really points.
    f"{PLUGIN_DIR}\\skills\\{SKILL}\\SKILL.md",
    f"{PLUGIN_DIR}/skills/{SKILL}/SKILL.md\x00.txt",
    f"{PLUGIN_DIR}/skills/{SKILL}/SKI\nLL.md",
    f"{PLUGIN_DIR}/skills/{SKILL}/SKI\x7fLL.md",
    f"{PLUGIN_DIR}/skills/{SKILL}/%2e%2e/SKILL.md",
    f"{PLUGIN_DIR}/skills/{SKILL}/SKILL.md ",
    f" {PLUGIN_DIR}/skills/{SKILL}/SKILL.md",
    f"{PLUGIN_DIR}/skills/ {SKILL}/SKILL.md",
    f"{PLUGIN_DIR}//skills/{SKILL}/SKILL.md",
    f"{PLUGIN_DIR}/skills/{SKILL}/SKILL.md -> /etc/passwd",
    f"{PLUGIN_DIR}/skills/\u0455kill/SKILL.md",
    f"{PLUGIN_DIR}/skills/{SKILL}/\uff33KILL.md",
    # The files that carry the version number, which only the publish step may touch.
    ".claude-plugin/marketplace.json",
    f"{PLUGIN_DIR}/.claude-plugin/plugin.json",
    f"{OTHER_PLUGIN_DIR}/.claude-plugin/plugin.json",
    f"{PLUGIN_DIR}/skills/{SKILL}/.claude-plugin/plugin.json",
    # The repository's own setup.
    ".github/workflows/deploy.yml",
    ".github/CODEOWNERS",
    ".gitignore",
    f"{PLUGIN_DIR}/.gitignore",
    f"{PLUGIN_DIR}/skills/{SKILL}/.hidden",
    "workflows/deploy.yml",
    # Inside the right skill, but not a file this service may write.
    f"{PLUGIN_DIR}/skills/{SKILL}",
    f"{PLUGIN_DIR}/skills/{SKILL}/",
    f"{PLUGIN_DIR}/skills/{SKILL}/SKILL.md/extra.md",
    f"{PLUGIN_DIR}/skills/{SKILL}/notes.md",
    f"{PLUGIN_DIR}/skills/{SKILL}/skill.md",
    f"{PLUGIN_DIR}/skills/{SKILL}/reference",
    f"{PLUGIN_DIR}/skills/{SKILL}/reference/notes.txt",
    f"{PLUGIN_DIR}/skills/{SKILL}/reference/notes",
    f"{PLUGIN_DIR}/skills/{SKILL}/reference/deeper/notes.md",
    f"{PLUGIN_DIR}/skills/{SKILL}/reference/.hidden.md",
    f"{PLUGIN_DIR}/skills/{SKILL}/reference/notes.md.exe",
    f"{PLUGIN_DIR}/skills/{SKILL}/reference/{'n' * 80}.md",
    f"{PLUGIN_DIR}/skills/{SKILL}/settings.yml",
    f"{PLUGIN_DIR}/skills/{SKILL}/settings.yaml",
    # Inside the collection, but not inside a skill.
    f"{PLUGIN_DIR}/SKILL.md",
    f"{PLUGIN_DIR}/skills/SKILL.md",
    f"{PLUGIN_DIR}/skills",
    f"{PLUGIN_DIR}/README.md",
    # Nowhere near the collection.
    "SKILL.md",
    "README.md",
    "skills/{0}/SKILL.md".format(SKILL),
    "etc/passwd",
    # A skill folder that is not a legal skill name.
    f"{PLUGIN_DIR}/skills/Weekly-Report/SKILL.md",
    f"{PLUGIN_DIR}/skills/a/SKILL.md",
    f"{PLUGIN_DIR}/skills/-leading/SKILL.md",
    # Absurdly long.
    f"{PLUGIN_DIR}/skills/{SKILL}/reference/" + ("a/" * 200) + "notes.md",
]


@pytest.mark.parametrize("path", HOSTILE_PATHS)
def test_hostile_paths_are_refused(path: str) -> None:
    with pytest.raises(UnsafePath):
        assert_safe_repo_path(PLUGIN_DIR, path)


@pytest.mark.parametrize("path", [None, 7, 1.5, b"SKILL.md", ["SKILL.md"], {}])
def test_paths_that_are_not_text_are_refused(path: Any) -> None:
    with pytest.raises(UnsafePath):
        assert_safe_repo_path(PLUGIN_DIR, path)


# ==============================================================================================
# One collection can never reach into another
# ==============================================================================================

CROSS_COLLECTION_ESCAPES: list[tuple[str, str]] = [
    # The plain version: a path into the other collection in the same repository.
    (PLUGIN_DIR, f"{OTHER_PLUGIN_DIR}/skills/{SKILL}/SKILL.md"),
    (OTHER_PLUGIN_DIR, f"{PLUGIN_DIR}/skills/{SKILL}/SKILL.md"),
    # Stepping up out of one collection and back down into another.
    (PLUGIN_DIR, f"{PLUGIN_DIR}/skills/{SKILL}/../../../beta/skills/{SKILL}/SKILL.md"),
    (PLUGIN_DIR, f"{PLUGIN_DIR}/../beta/skills/{SKILL}/SKILL.md"),
    # A collection whose name merely starts the same way. Folder steps are compared one at
    # a time, so "alpha" must not match "alpha-two" or "alphabet".
    (PLUGIN_DIR, f"plugins/alpha-two/skills/{SKILL}/SKILL.md"),
    (PLUGIN_DIR, f"plugins/alphabet/skills/{SKILL}/SKILL.md"),
    ("plugins/alpha-two", f"{PLUGIN_DIR}/skills/{SKILL}/SKILL.md"),
    # A deeper collection that sits inside another one.
    ("plugins/alpha", f"plugins/alpha/nested/skills/{SKILL}/SKILL.md"),
    ("plugins/alpha/nested", f"{PLUGIN_DIR}/skills/{SKILL}/SKILL.md"),
    # The other collection's version file, which nothing may write through an edit.
    (PLUGIN_DIR, f"{OTHER_PLUGIN_DIR}/.claude-plugin/plugin.json"),
]


@pytest.mark.parametrize(("plugin_dir", "path"), CROSS_COLLECTION_ESCAPES)
def test_one_collection_cannot_reach_into_another(plugin_dir: str, path: str) -> None:
    with pytest.raises(UnsafePath):
        assert_safe_repo_path(plugin_dir, path)


def test_a_collection_rooted_inside_another_cannot_touch_its_skill_files() -> None:
    """A collection folder that sits inside another collection's skills tree is contained.

    Nothing stops a library's manifest from recording one collection inside another. When
    that happens the inner anchor still cannot reach any file the outer collection actually
    holds, because every path allowed under the inner anchor has to carry its own skills
    step, and no real skill file of the outer collection has one.
    """
    inner = f"{PLUGIN_DIR}/skills/{SKILL}"
    for path in (
        f"{PLUGIN_DIR}/skills/{SKILL}/SKILL.md",
        f"{PLUGIN_DIR}/skills/{SKILL}/reference/notes.md",
        f"{PLUGIN_DIR}/skills/other-skill/SKILL.md",
        f"{PLUGIN_DIR}/.claude-plugin/plugin.json",
    ):
        with pytest.raises(UnsafePath):
            assert_safe_repo_path(inner, path)


def test_each_collection_still_accepts_its_own_file() -> None:
    """The cross checks above must not be passing simply because everything is refused."""
    first = f"{PLUGIN_DIR}/skills/{SKILL}/SKILL.md"
    second = f"{OTHER_PLUGIN_DIR}/skills/{SKILL}/SKILL.md"
    assert assert_safe_repo_path(PLUGIN_DIR, first) == first
    assert assert_safe_repo_path(OTHER_PLUGIN_DIR, second) == second


@pytest.mark.parametrize("hostile", HOSTILE_PLUGIN_DIRS)
def test_a_hostile_collection_folder_refuses_even_a_perfect_path(hostile: str) -> None:
    """The anchor is checked first, so a bad collection folder refuses everything."""
    with pytest.raises(UnsafePath):
        assert_safe_repo_path(hostile, f"{PLUGIN_DIR}/skills/{SKILL}/SKILL.md")


@pytest.mark.parametrize("hostile", [None, 7, b"plugins/alpha", ["plugins"]])
def test_a_collection_folder_that_is_not_text_refuses_everything(hostile: Any) -> None:
    with pytest.raises(UnsafePath):
        assert_safe_repo_path(hostile, f"{PLUGIN_DIR}/skills/{SKILL}/SKILL.md")


def test_a_collection_folder_pointing_at_the_repository_root_is_refused() -> None:
    """Anchoring at the root would make every skills folder in the repository writable."""
    for anchor in (".", "./", "/", ""):
        with pytest.raises(UnsafePath):
            assert_safe_repo_path(anchor, f"skills/{SKILL}/SKILL.md")


# ==============================================================================================
# What a person is told when something is refused
# ==============================================================================================


def _every_refusal() -> list[UnsafePath]:
    """Collect one refusal per hostile value in every table above."""
    caught: list[UnsafePath] = []
    for name in HOSTILE_SKILL_NAMES:
        with pytest.raises(UnsafePath) as info:
            validate_skill_name(name)
        caught.append(info.value)
    for folder in HOSTILE_PLUGIN_DIRS:
        with pytest.raises(UnsafePath) as info:
            validate_plugin_dir(folder)
        caught.append(info.value)
    for path in HOSTILE_PATHS:
        with pytest.raises(UnsafePath) as info:
            assert_safe_repo_path(PLUGIN_DIR, path)
        caught.append(info.value)
    for plugin_dir, path in CROSS_COLLECTION_ESCAPES:
        with pytest.raises(UnsafePath) as info:
            assert_safe_repo_path(plugin_dir, path)
        caught.append(info.value)
    return caught


def test_every_refusal_reads_as_plain_english() -> None:
    """No file path, no code, and nothing that only a programmer would understand."""
    banned = ("/", "\\", "..", "0x", "None", "Traceback", "Error", "raise", "_")
    for error in _every_refusal():
        message = error.user_message
        assert message, "a refusal came back with nothing to read"
        assert message[0].isupper(), f"message does not start like a sentence: {message!r}"
        assert message.endswith((".", "?")), f"message is not a finished sentence: {message!r}"
        for fragment in banned:
            assert fragment not in message, f"message leaks {fragment!r}: {message!r}"


def test_every_refusal_keeps_the_technical_reason_for_the_log() -> None:
    for error in _every_refusal():
        assert error.detail, f"nothing was recorded for the log: {error.user_message!r}"


# ==============================================================================================
# Nothing about one team is baked in
# ==============================================================================================


def test_the_collection_folder_is_not_a_setting() -> None:
    """It comes from the library's manifest at runtime, so it cannot live on Config."""
    fields = {field.name for field in dataclasses.fields(Config)}
    assert fields == {
        "repo_owner",
        "repo_name",
        "default_branch",
        "proposal_ttl_seconds",
        "sync_estimate_minutes",
    }
    assert not hasattr(Config("owner", "repo"), "plugin_dir")


def test_no_collection_folder_is_written_into_these_two_modules() -> None:
    """A default folder here would quietly hide every other collection in a repository."""
    here = pathlib.Path(__file__).resolve().parents[1] / "src" / "librarian"
    for module in ("config.py", "paths.py"):
        text = (here / module).read_text(encoding="utf-8")
        assert "plugins/" not in text, f"{module} names a particular folder of plugins"
        assert "LIBRARIAN_PLUGIN_DIR" not in text, f"{module} still reads a plugin folder setting"
