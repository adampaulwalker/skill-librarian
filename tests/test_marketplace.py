"""Tests for discovering what a library holds.

The names used here are invented for the tests. They are deliberately plain so that nothing
about one particular team's repository can leak into the code being tested.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from librarian.config import Config
from librarian.errors import LibrarianError, SkillNotFound, UnsafePath
from librarian.marketplace import (
    MARKETPLACE_MANIFEST,
    PluginRef,
    list_all_skills,
    parse_marketplace,
    resolve_skill,
)

from .fakes import FakeGitHubClient

DEFAULT_BRANCH = "main"


# ==============================================================================================
# Building test repositories
# ==============================================================================================


def marketplace_text(*entries: dict[str, Any], root: dict[str, Any] | None = None) -> str:
    manifest: dict[str, Any] = {
        "name": "example-library",
        "owner": {"name": "Example Owner"},
        "plugins": list(entries),
    }
    if root is not None:
        manifest.update(root)
    return json.dumps(manifest, indent=2) + "\n"


def entry(name: str, source: str, version: str | None = "1.0.0") -> dict[str, Any]:
    made: dict[str, Any] = {"name": name, "source": source}
    if version is not None:
        made["version"] = version
    return made


def collection_files(plugin_dir: str, *skill_names: str, version: str = "1.0.0") -> dict[str, str]:
    """A collection folder as it really looks: its own manifest, plus a folder per skill."""
    files: dict[str, str] = {
        f"{plugin_dir}/.claude-plugin/plugin.json": json.dumps(
            {"name": plugin_dir.rsplit("/", 1)[-1], "version": version}, indent=2
        )
        + "\n"
    }
    files.update(skill_files(plugin_dir, *skill_names))
    return files


def skill_files(plugin_dir: str, *skill_names: str) -> dict[str, str]:
    files: dict[str, str] = {}
    for skill_name in skill_names:
        files[f"{plugin_dir}/skills/{skill_name}/SKILL.md"] = (
            f"---\nname: {skill_name}\ndescription: A skill for testing.\n---\n\nBody.\n"
        )
    return files


@pytest.fixture
def cfg() -> Config:
    return Config(
        repo_owner="example-owner",
        repo_name="example-repo",
        default_branch=DEFAULT_BRANCH,
    )


def client_with(manifest: str, files: dict[str, str] | None = None) -> FakeGitHubClient:
    client = FakeGitHubClient(default_branch=DEFAULT_BRANCH)
    seeded = {MARKETPLACE_MANIFEST: manifest}
    seeded.update(files or {})
    client.seed(seeded)
    return client


def two_plugin_client() -> FakeGitHubClient:
    """A library with two collections, one skill name shared between them."""
    manifest = marketplace_text(
        entry("alpha-pack", "./plugins/alpha-pack", "1.2.3"),
        entry("beta-pack", "./collections/beta-pack", "0.4.0"),
    )
    files: dict[str, str] = {}
    files.update(collection_files("plugins/alpha-pack", "alpha-only", "shared-name"))
    files.update(collection_files("collections/beta-pack", "beta-only", "shared-name"))
    return client_with(manifest, files)


# ==============================================================================================
# Reading the manifest
# ==============================================================================================


def test_every_collection_in_the_manifest_is_discovered() -> None:
    plugins = parse_marketplace(
        marketplace_text(
            entry("alpha-pack", "./plugins/alpha-pack", "1.2.3"),
            entry("beta-pack", "./collections/nested/beta-pack", "0.4.0"),
            entry("gamma-pack", "./gamma", "2.0.0"),
        )
    )

    assert [plugin.name for plugin in plugins] == ["alpha-pack", "beta-pack", "gamma-pack"]
    assert [plugin.plugin_dir for plugin in plugins] == [
        "plugins/alpha-pack",
        "collections/nested/beta-pack",
        "gamma",
    ]
    assert [plugin.version for plugin in plugins] == ["1.2.3", "0.4.0", "2.0.0"]
    assert plugins[1].manifest_path == "collections/nested/beta-pack/.claude-plugin/plugin.json"


def test_the_layout_is_read_rather_than_assumed() -> None:
    """A collection is wherever the manifest says, including outside any plugins folder."""
    plugins = parse_marketplace(marketplace_text(entry("only-pack", "./anywhere/at/all")))

    assert plugins[0].plugin_dir == "anywhere/at/all"
    assert plugins[0].manifest_path == "anywhere/at/all/.claude-plugin/plugin.json"


def test_a_trailing_slash_on_a_source_is_tidied_away() -> None:
    plugins = parse_marketplace(marketplace_text(entry("only-pack", "./plugins/only-pack/")))

    assert plugins[0].plugin_dir == "plugins/only-pack"


def test_a_collection_without_a_version_is_carried_through_as_missing() -> None:
    plugins = parse_marketplace(
        marketplace_text(entry("only-pack", "./plugins/only-pack", version=None))
    )

    assert plugins[0].version == ""


def test_a_version_that_is_not_text_is_refused() -> None:
    manifest = marketplace_text({"name": "only-pack", "source": "./x", "version": 3})

    with pytest.raises(LibrarianError) as caught:
        parse_marketplace(manifest)

    assert "only-pack" in caught.value.user_message


def test_two_collections_with_the_same_name_are_refused() -> None:
    manifest = marketplace_text(
        entry("same-pack", "./plugins/one"), entry("same-pack", "./plugins/two")
    )

    with pytest.raises(LibrarianError) as caught:
        parse_marketplace(manifest)

    assert "same-pack" in caught.value.user_message


def test_two_collections_stored_in_the_same_place_are_refused() -> None:
    manifest = marketplace_text(
        entry("first-pack", "./plugins/one"), entry("second-pack", "./plugins/one")
    )

    with pytest.raises(LibrarianError) as caught:
        parse_marketplace(manifest)

    assert "first-pack" in caught.value.user_message
    assert "second-pack" in caught.value.user_message


# ==============================================================================================
# Sources this service cannot honour
# ==============================================================================================


@pytest.mark.parametrize(
    "source",
    [
        "github:example-owner/example-repo",
        "https://example.invalid/skills.git",
        "git@example.invalid:example-owner/example-repo.git",
        "npm:example-skills",
        "pip:example-skills",
    ],
)
def test_a_source_kept_somewhere_else_is_reported_not_skipped(source: str) -> None:
    manifest = marketplace_text(entry("outside-pack", source))

    with pytest.raises(LibrarianError) as caught:
        parse_marketplace(manifest)

    message = caught.value.user_message
    assert "outside-pack" in message, "the collection has to be named, not silently dropped"
    assert "outside this library" in message


@pytest.mark.parametrize("source", ["plugins/no-dot-slash", "plugins/no-dot-slash/"])
def test_a_folder_written_without_the_leading_marks_is_reported_not_skipped(source: str) -> None:
    """A folder inside the library, written the wrong way, is a different problem to explain."""
    manifest = marketplace_text(entry("mistyped-pack", source))

    with pytest.raises(LibrarianError) as caught:
        parse_marketplace(manifest)

    message = caught.value.user_message
    assert "mistyped-pack" in message, "the collection has to be named, not silently dropped"
    assert "./" in message
    assert "outside this library" not in message


def test_a_source_written_as_an_object_is_reported_as_unsupported() -> None:
    manifest = marketplace_text(
        {"name": "outside-pack", "source": {"source": "github", "repo": "example/repo"}}
    )

    with pytest.raises(LibrarianError) as caught:
        parse_marketplace(manifest)

    assert "outside-pack" in caught.value.user_message


def test_an_unsupported_source_stops_the_whole_read_rather_than_hiding_a_collection() -> None:
    """A collection this service cannot reach must never look like a collection that is absent."""
    manifest = marketplace_text(
        entry("good-pack", "./plugins/good-pack"),
        entry("outside-pack", "https://example.invalid/skills.git"),
    )

    with pytest.raises(LibrarianError):
        parse_marketplace(manifest)


@pytest.mark.parametrize(
    "source",
    [
        "./../outside",
        "./plugins/../../outside",
        "../outside",
        "/plugins/absolute",
        ".\\plugins\\windows",
        "./plugins/%2e%2e/outside",
    ],
)
def test_a_source_that_escapes_the_repository_is_refused(source: str) -> None:
    manifest = marketplace_text(entry("escaping-pack", source))

    with pytest.raises(LibrarianError) as caught:
        parse_marketplace(manifest)

    assert "escaping-pack" in caught.value.user_message


def test_a_source_that_steps_out_of_the_repository_is_an_unsafe_path() -> None:
    manifest = marketplace_text(entry("escaping-pack", "./plugins/../../outside"))

    with pytest.raises(UnsafePath):
        parse_marketplace(manifest)


def test_a_source_pointing_at_the_whole_repository_is_refused() -> None:
    with pytest.raises(LibrarianError):
        parse_marketplace(marketplace_text(entry("root-pack", "./")))


def test_a_source_pointing_at_a_hidden_folder_is_refused() -> None:
    with pytest.raises(UnsafePath):
        parse_marketplace(marketplace_text(entry("hidden-pack", "./.claude-plugin")))


# ==============================================================================================
# A manifest that cannot be read at all
# ==============================================================================================


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   \n",
        "{ not json at all",
        '{"plugins": [}',
        "[]",
        '"just a string"',
    ],
)
def test_a_damaged_manifest_gives_a_plain_english_message(text: str) -> None:
    with pytest.raises(LibrarianError) as caught:
        parse_marketplace(text)

    message = caught.value.user_message
    assert "library" in message
    _assert_reads_plainly(message)


def test_a_manifest_with_no_collections_listed_is_explained() -> None:
    for manifest in ('{"name": "example-library"}', '{"name": "example-library", "plugins": []}'):
        with pytest.raises(LibrarianError) as caught:
            parse_marketplace(manifest)
        _assert_reads_plainly(caught.value.user_message)


def test_a_collections_key_of_the_wrong_shape_is_explained() -> None:
    with pytest.raises(LibrarianError) as caught:
        parse_marketplace('{"plugins": {"alpha": "./plugins/alpha"}}')

    _assert_reads_plainly(caught.value.user_message)


def test_a_missing_manifest_file_is_explained_without_naming_a_file(cfg: Config) -> None:
    client = FakeGitHubClient(default_branch=DEFAULT_BRANCH)
    client.seed(skill_files("plugins/alpha-pack", "alpha-only"))

    with pytest.raises(LibrarianError) as caught:
        list_all_skills(client, cfg, DEFAULT_BRANCH)

    message = caught.value.user_message
    assert "collections of skills" in message
    _assert_reads_plainly(message)


def _assert_reads_plainly(message: str) -> None:
    """No jargon, no file paths, no stack detail, and no double hyphens."""
    assert message
    assert message[0].isupper()
    assert "--" not in message
    assert "—" not in message
    for jargon in (
        "json",
        "JSON",
        "Traceback",
        "marketplace.json",
        ".claude-plugin",
        "plugin",
        "repository",
        "parse",
        "None",
        "dict",
        "array",
    ):
        assert jargon not in message, f"{jargon!r} is not language to put in front of a reader"


# ==============================================================================================
# Finding a skill across several collections
# ==============================================================================================


def test_a_skill_resolves_to_the_collection_that_holds_it(cfg: Config) -> None:
    client = two_plugin_client()

    alpha = resolve_skill(client, cfg, "alpha-only", DEFAULT_BRANCH)
    beta = resolve_skill(client, cfg, "beta-only", DEFAULT_BRANCH)

    assert alpha.plugin.name == "alpha-pack"
    assert alpha.skill_path == "plugins/alpha-pack/skills/alpha-only/SKILL.md"
    assert alpha.plugin.manifest_path == "plugins/alpha-pack/.claude-plugin/plugin.json"
    assert alpha.plugin.version == "1.2.3"

    assert beta.plugin.name == "beta-pack"
    assert beta.skill_path == "collections/beta-pack/skills/beta-only/SKILL.md"
    assert beta.plugin.version == "0.4.0"


def test_every_collection_is_searched_not_just_the_first(cfg: Config) -> None:
    """The skill lives in the last collection listed, so a first-only search would miss it."""
    manifest = marketplace_text(
        entry("first-pack", "./plugins/first-pack"),
        entry("second-pack", "./plugins/second-pack"),
        entry("third-pack", "./plugins/third-pack"),
    )
    files = collection_files("plugins/first-pack", "one-skill")
    files.update(collection_files("plugins/second-pack"))
    files.update(collection_files("plugins/third-pack", "buried-skill"))
    client = client_with(manifest, files)

    found = resolve_skill(client, cfg, "buried-skill", DEFAULT_BRANCH)

    assert found.plugin.name == "third-pack"


def test_a_skill_name_in_two_collections_is_refused_and_names_both(cfg: Config) -> None:
    client = two_plugin_client()

    with pytest.raises(LibrarianError) as caught:
        resolve_skill(client, cfg, "shared-name", DEFAULT_BRANCH)

    message = caught.value.user_message
    assert "alpha-pack" in message
    assert "beta-pack" in message
    assert "shared-name" in message
    _assert_reads_plainly(message)


def test_an_ambiguous_skill_is_never_quietly_resolved(cfg: Config) -> None:
    client = two_plugin_client()

    with pytest.raises(LibrarianError):
        resolve_skill(client, cfg, "shared-name", DEFAULT_BRANCH)


def test_a_skill_name_in_three_collections_names_all_three(cfg: Config) -> None:
    manifest = marketplace_text(
        entry("one-pack", "./plugins/one-pack"),
        entry("two-pack", "./plugins/two-pack"),
        entry("three-pack", "./plugins/three-pack"),
    )
    files: dict[str, str] = {}
    for folder in ("plugins/one-pack", "plugins/two-pack", "plugins/three-pack"):
        files.update(collection_files(folder, "shared-name"))
    client = client_with(manifest, files)

    with pytest.raises(LibrarianError) as caught:
        resolve_skill(client, cfg, "shared-name", DEFAULT_BRANCH)

    for name in ("one-pack", "two-pack", "three-pack"):
        assert name in caught.value.user_message


def test_a_missing_skill_lists_the_ones_that_do_exist(cfg: Config) -> None:
    client = two_plugin_client()

    with pytest.raises(SkillNotFound) as caught:
        resolve_skill(client, cfg, "not-here", DEFAULT_BRANCH)

    message = caught.value.user_message
    assert "not-here" in message
    assert "alpha-only" in message
    assert "beta-only" in message
    assert "shared-name" in message


def test_a_library_with_no_skills_yet_says_so(cfg: Config) -> None:
    client = client_with(
        marketplace_text(entry("empty-pack", "./plugins/empty-pack")),
        collection_files("plugins/empty-pack"),
    )

    with pytest.raises(SkillNotFound) as caught:
        resolve_skill(client, cfg, "anything-at-all", DEFAULT_BRANCH)

    assert "does not have any skills in it yet" in caught.value.user_message


def test_an_unusable_skill_name_is_refused_before_anything_is_read(cfg: Config) -> None:
    client = two_plugin_client()
    before = len(client.calls)

    with pytest.raises(UnsafePath):
        resolve_skill(client, cfg, "../../etc/passwd", DEFAULT_BRANCH)

    assert len(client.calls) == before


# ==============================================================================================
# Listing everything
# ==============================================================================================


def test_listing_covers_every_collection(cfg: Config) -> None:
    client = two_plugin_client()

    skills = list_all_skills(client, cfg, DEFAULT_BRANCH)

    assert [(skill.skill_name, skill.plugin.name) for skill in skills] == [
        ("alpha-only", "alpha-pack"),
        ("beta-only", "beta-pack"),
        ("shared-name", "alpha-pack"),
        ("shared-name", "beta-pack"),
    ]


def test_listing_shows_a_shared_name_once_per_collection(cfg: Config) -> None:
    client = two_plugin_client()

    shared = [
        skill for skill in list_all_skills(client, cfg, DEFAULT_BRANCH)
        if skill.skill_name == "shared-name"
    ]

    assert {skill.plugin.plugin_dir for skill in shared} == {
        "plugins/alpha-pack",
        "collections/beta-pack",
    }


def test_a_collection_with_no_skills_folder_is_simply_empty(cfg: Config) -> None:
    manifest = marketplace_text(
        entry("full-pack", "./plugins/full-pack"), entry("new-pack", "./plugins/new-pack")
    )
    files = collection_files("plugins/full-pack", "one-skill")
    files.update(collection_files("plugins/new-pack"))
    client = client_with(manifest, files)

    skills = list_all_skills(client, cfg, DEFAULT_BRANCH)

    assert [skill.skill_name for skill in skills] == ["one-skill"]


def test_a_folder_that_could_never_be_a_skill_name_is_left_out(cfg: Config) -> None:
    manifest = marketplace_text(entry("only-pack", "./plugins/only-pack"))
    files = collection_files("plugins/only-pack", "good-skill")
    files["plugins/only-pack/skills/Not A Skill/SKILL.md"] = "---\nname: x\n---\n"
    files["plugins/only-pack/skills/README.md"] = "Not a skill folder.\n"
    client = client_with(manifest, files)

    skills = list_all_skills(client, cfg, DEFAULT_BRANCH)

    assert [skill.skill_name for skill in skills] == ["good-skill"]


def test_listing_reads_the_version_of_the_commit_it_was_asked_for(cfg: Config) -> None:
    client = two_plugin_client()
    first_sha = client.get_ref_sha(DEFAULT_BRANCH)
    client.seed(skill_files("plugins/alpha-pack", "added-later"))

    at_first = [skill.skill_name for skill in list_all_skills(client, cfg, first_sha)]
    at_head = [skill.skill_name for skill in list_all_skills(client, cfg, DEFAULT_BRANCH)]

    assert "added-later" not in at_first
    assert "added-later" in at_head


def test_no_version_asked_for_means_the_shared_branch(cfg: Config) -> None:
    client = two_plugin_client()

    skills = list_all_skills(client, cfg, "")

    assert [skill.skill_name for skill in skills] == [
        "alpha-only",
        "beta-only",
        "shared-name",
        "shared-name",
    ]


# ==============================================================================================
# Nothing here is tied to one team's repository
# ==============================================================================================


def test_the_same_code_serves_a_completely_different_layout(cfg: Config) -> None:
    """A single collection at an unusual place, with names sharing nothing with the other tests."""
    manifest = marketplace_text(entry("field-notes", "./library/field-notes", "9.9.9"))
    client = client_with(manifest, collection_files("library/field-notes", "site-visit-report"))

    found = resolve_skill(client, cfg, "site-visit-report", DEFAULT_BRANCH)

    assert found.plugin == PluginRef(
        name="field-notes",
        plugin_dir="library/field-notes",
        version="9.9.9",
        manifest_path="library/field-notes/.claude-plugin/plugin.json",
    )
    assert found.skill_path == "library/field-notes/skills/site-visit-report/SKILL.md"


def test_a_collection_that_is_listed_but_not_there_is_reported_by_name(cfg: Config) -> None:
    """A promised collection that is missing must never look like a collection nobody asked for."""
    manifest = marketplace_text(
        entry("real-pack", "./plugins/real-pack"), entry("ghost-pack", "./plugins/ghost-pack")
    )
    client = client_with(manifest, collection_files("plugins/real-pack", "one-skill"))

    with pytest.raises(LibrarianError) as caught:
        list_all_skills(client, cfg, DEFAULT_BRANCH)

    assert "ghost-pack" in caught.value.user_message


def test_a_folder_with_no_instructions_file_is_not_offered_as_a_skill(cfg: Config) -> None:
    manifest = marketplace_text(entry("only-pack", "./plugins/only-pack"))
    files = collection_files("plugins/only-pack", "good-skill")
    files["plugins/only-pack/skills/empty-skill/reference/notes.md"] = "Notes with no skill.\n"
    client = client_with(manifest, files)

    skills = list_all_skills(client, cfg, DEFAULT_BRANCH)

    assert [skill.skill_name for skill in skills] == ["good-skill"]


def test_a_folder_with_no_instructions_file_does_not_block_the_real_skill(cfg: Config) -> None:
    """A folder holding no instructions is damage, and must not stand in for a real skill."""
    manifest = marketplace_text(
        entry("real-pack", "./plugins/real-pack"), entry("shell-pack", "./plugins/shell-pack")
    )
    files = collection_files("plugins/real-pack", "one-skill")
    files.update(collection_files("plugins/shell-pack"))
    files["plugins/shell-pack/skills/one-skill/reference/notes.md"] = "Notes with no skill.\n"
    client = client_with(manifest, files)

    found = resolve_skill(client, cfg, "one-skill", DEFAULT_BRANCH)

    assert found.plugin.name == "real-pack"


def test_a_padded_source_is_refused_rather_than_trimmed() -> None:
    with pytest.raises(UnsafePath) as caught:
        parse_marketplace(marketplace_text(entry("padded-pack", "  ./plugins/padded-pack  ")))

    assert "padded-pack" in caught.value.user_message


# ==============================================================================================
# Behaving the way the real GitHub client does
# ==============================================================================================


class StrictFakeGitHubClient(FakeGitHubClient):
    """A fake that answers a folder listing the way the real client does.

    The plain fake returns an empty listing both for a folder that is not there and for a path
    that is really a file. GitHub answers with a not found for the first and with the file
    itself for the second, and those have to lead to different outcomes, so the tests below run
    against this closer stand-in.
    """

    def list_dir(self, path: str, ref: str) -> list[dict]:
        cleaned = path.strip("/")
        stored = self.files_on(ref)
        if cleaned in stored:
            return [
                {
                    "name": cleaned.rsplit("/", 1)[-1],
                    "path": cleaned,
                    "type": "file",
                    "sha": "",
                    "size": len(stored[cleaned].encode("utf-8")),
                }
            ]
        entries = super().list_dir(path, ref)
        if not entries:
            raise SkillNotFound("I could not find that folder in the skills library.")
        return entries


def strict_client_with(manifest: str, files: dict[str, str] | None = None) -> FakeGitHubClient:
    client = StrictFakeGitHubClient(default_branch=DEFAULT_BRANCH)
    seeded = {MARKETPLACE_MANIFEST: manifest}
    seeded.update(files or {})
    client.seed(seeded)
    return client


def test_a_collection_with_no_skills_folder_is_empty_against_a_strict_client(cfg: Config) -> None:
    manifest = marketplace_text(
        entry("full-pack", "./plugins/full-pack"), entry("new-pack", "./plugins/new-pack")
    )
    files = collection_files("plugins/full-pack", "one-skill")
    files.update(collection_files("plugins/new-pack"))
    client = strict_client_with(manifest, files)

    skills = list_all_skills(client, cfg, DEFAULT_BRANCH)

    assert [skill.skill_name for skill in skills] == ["one-skill"]


def test_a_missing_collection_is_reported_against_a_strict_client(cfg: Config) -> None:
    manifest = marketplace_text(
        entry("real-pack", "./plugins/real-pack"), entry("ghost-pack", "./plugins/ghost-pack")
    )
    client = strict_client_with(manifest, collection_files("plugins/real-pack", "one-skill"))

    with pytest.raises(LibrarianError) as caught:
        list_all_skills(client, cfg, DEFAULT_BRANCH)

    assert "ghost-pack" in caught.value.user_message


def test_a_folder_with_no_instructions_file_is_left_out_against_a_strict_client(
    cfg: Config,
) -> None:
    manifest = marketplace_text(entry("only-pack", "./plugins/only-pack"))
    files = collection_files("plugins/only-pack", "good-skill")
    files["plugins/only-pack/skills/empty-skill/reference/notes.md"] = "Notes with no skill.\n"
    client = strict_client_with(manifest, files)

    skills = list_all_skills(client, cfg, DEFAULT_BRANCH)

    assert [skill.skill_name for skill in skills] == ["good-skill"]


def test_asking_for_a_folder_with_no_instructions_file_says_what_is_wrong(cfg: Config) -> None:
    manifest = marketplace_text(entry("only-pack", "./plugins/only-pack"))
    files = collection_files("plugins/only-pack", "good-skill")
    files["plugins/only-pack/skills/hollow-skill/reference/notes.md"] = "Notes with no skill.\n"
    client = client_with(manifest, files)

    with pytest.raises(SkillNotFound) as caught:
        resolve_skill(client, cfg, "hollow-skill", DEFAULT_BRANCH)

    message = caught.value.user_message
    assert 'There is a folder called "hollow-skill" in the "only-pack" collection' in message
    assert "no instructions in it" in message
    assert ", and one in the" not in message, "one collection must not read like a list"


def test_a_collection_with_no_settings_folder_is_reported(cfg: Config) -> None:
    """A folder of skills with no settings folder has no version, so it could never be published."""
    manifest = marketplace_text(entry("bare-pack", "./plugins/bare-pack"))
    client = client_with(manifest, skill_files("plugins/bare-pack", "one-skill"))

    with pytest.raises(LibrarianError) as caught:
        list_all_skills(client, cfg, DEFAULT_BRANCH)

    assert "bare-pack" in caught.value.user_message


def test_a_skills_folder_that_is_really_a_file_is_reported(cfg: Config) -> None:
    manifest = marketplace_text(entry("odd-pack", "./plugins/odd-pack"))
    files = collection_files("plugins/odd-pack")
    files["plugins/odd-pack/skills"] = "This is a file where a folder should be.\n"
    client = client_with(manifest, files)

    with pytest.raises(LibrarianError) as caught:
        list_all_skills(client, cfg, DEFAULT_BRANCH)

    assert "odd-pack" in caught.value.user_message


def test_a_collection_that_is_really_a_file_is_reported_against_a_strict_client(
    cfg: Config,
) -> None:
    """Asking GitHub to list a file answers with the file, which must not look like a collection."""
    manifest = marketplace_text(entry("file-pack", "./plugins/file-pack"))
    client = strict_client_with(
        manifest, {"plugins/file-pack": "This is a file where a collection should be.\n"}
    )

    with pytest.raises(LibrarianError) as caught:
        list_all_skills(client, cfg, DEFAULT_BRANCH)

    assert "file-pack" in caught.value.user_message
