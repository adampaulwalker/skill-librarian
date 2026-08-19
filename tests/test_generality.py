"""The test that stops this becoming one customer's tool again.

This is a general product. Any organization that hits the single-editor wall should be
able to point it at their own repository and use it, which means nothing about a
particular organization, repository, collection of skills, or person may be written into
the code. Everything organization-specific arrives as configuration, or as the contents of
the repository being read at runtime.

That is easy to say and easy to lose. A stray example in a docstring, a person's name left
in a comment, a folder name that "everyone uses anyway" hardcoded as a fallback, and the
service quietly stops working for the second customer. So this file reads the package
source and fails on the names that must never be in it.

The list is deliberately small and specific. It is not a substitute for review; it is a
tripwire on the mistakes that have actually happened.
"""

from __future__ import annotations

import dataclasses
import pathlib
import re

import pytest

import librarian
from librarian.config import Config

PACKAGE_ROOT = pathlib.Path(librarian.__file__).resolve().parent

#: Names of an organization, a collection of skills, or a person. None of these belong in
#: code that any team is meant to be able to run against their own repository.
FORBIDDEN_NAMES: tuple[str, ...] = (
    "people-project",
    "peopleproject",
    "Jodi",
    "Ellie",
    "Alicia",
    "Marko",
)

#: A repository, an owner, or a folder inside one. A default here reads as harmless and is
#: not: the first organization to have a different layout gets an empty library and no error.
FORBIDDEN_DEFAULTS: tuple[str, ...] = (
    "atlantic-labs",
    "atlanticlabs",
    "plugins/",
    "./plugins",
)


def source_files() -> list[pathlib.Path]:
    return sorted(path for path in PACKAGE_ROOT.rglob("*.py") if "__pycache__" not in path.parts)


def test_the_package_source_is_actually_being_read() -> None:
    """A tripwire that reads nothing would pass forever while proving nothing."""
    files = source_files()

    assert len(files) >= 10, f"expected the whole package under {PACKAGE_ROOT}, found {files}"
    assert any(path.name == "config.py" for path in files)
    assert any(path.name == "publisher.py" for path in files)
    assert all(path.read_text(encoding="utf-8") is not None for path in files)


@pytest.mark.parametrize("forbidden", FORBIDDEN_NAMES)
def test_no_organization_or_person_is_named_in_the_package_source(forbidden: str) -> None:
    pattern = re.compile(re.escape(forbidden), re.IGNORECASE)
    offences: list[str] = []

    for path in source_files():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if pattern.search(line):
                offences.append(f"{path.relative_to(PACKAGE_ROOT)}:{number}: {line.strip()}")

    assert not offences, (
        f"{forbidden!r} is the name of a particular organization, collection, or person, and "
        "this service has to work for any team that points it at their own repository. "
        "Found in:\n" + "\n".join(offences)
    )


@pytest.mark.parametrize("forbidden", FORBIDDEN_DEFAULTS)
def test_no_repository_or_folder_is_hardcoded_in_the_package_source(forbidden: str) -> None:
    pattern = re.compile(re.escape(forbidden), re.IGNORECASE)
    offences: list[str] = []

    for path in source_files():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if pattern.search(line):
                offences.append(f"{path.relative_to(PACKAGE_ROOT)}:{number}: {line.strip()}")

    assert not offences, (
        f"{forbidden!r} names one team's repository or folder layout. The layout is read at "
        "runtime from the library's own manifest and is never assumed. Found in:\n"
        + "\n".join(offences)
    )


# ==============================================================================================
# The setting that must not come back
# ==============================================================================================


def test_config_has_no_plugin_dir_setting() -> None:
    """Where the skills sit is repository content, not an operator's setting.

    A single ``plugin_dir`` setting silently hides every collection except one, which looks
    exactly like a missing skill to the person asking.
    """
    assert not hasattr(Config, "plugin_dir")
    assert "plugin_dir" not in {field.name for field in dataclasses.fields(Config)}

    built = Config(repo_owner="an-owner", repo_name="a-repo")
    assert not hasattr(built, "plugin_dir")


def test_config_refuses_a_plugin_dir_even_if_someone_tries_to_pass_one() -> None:
    with pytest.raises(TypeError):
        Config(repo_owner="an-owner", repo_name="a-repo", plugin_dir="plugins/anything")


def test_config_carries_only_the_settings_the_contract_names() -> None:
    assert {field.name for field in dataclasses.fields(Config)} == {
        "repo_owner",
        "repo_name",
        "default_branch",
        "proposal_ttl_seconds",
        "sync_estimate_minutes",
    }


def test_the_repository_to_work_in_has_no_default_at_all() -> None:
    """A default repository is how a general tool quietly becomes one team's tool."""
    fields = {field.name: field for field in dataclasses.fields(Config)}

    for name in ("repo_owner", "repo_name"):
        field = fields[name]
        assert field.default is dataclasses.MISSING
        assert field.default_factory is dataclasses.MISSING


def test_loading_the_settings_without_a_repository_refuses_rather_than_guesses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from librarian.config import load_config
    from librarian.errors import LibrarianError

    for name in (
        "LIBRARIAN_REPO_OWNER",
        "LIBRARIAN_REPO_NAME",
        "LIBRARIAN_DEFAULT_BRANCH",
        "LIBRARIAN_PROPOSAL_TTL_SECONDS",
        "LIBRARIAN_SYNC_ESTIMATE_MINUTES",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(LibrarianError):
        load_config()


def test_nothing_in_the_package_reaches_for_a_plugin_dir_setting() -> None:
    """The name is gone from the settings, so no module may still be asking for it."""
    offences: list[str] = []
    pattern = re.compile(r"(cfg|config|settings|self\.cfg)\s*\.\s*plugin_dir")

    for path in source_files():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if pattern.search(line):
                offences.append(f"{path.relative_to(PACKAGE_ROOT)}:{number}: {line.strip()}")

    assert not offences, "the settings no longer carry a plugin_dir:\n" + "\n".join(offences)


# ==================================================================================================
# The same tripwire, pointed at the tests
# ==================================================================================================

TESTS_ROOT = pathlib.Path(__file__).resolve().parent


def test_files() -> list[pathlib.Path]:
    """Every test file except this one, which has to contain the names to check for them."""
    return sorted(
        path
        for path in TESTS_ROOT.rglob("*.py")
        if "__pycache__" not in path.parts and path.name != pathlib.Path(__file__).name
    )


def test_the_test_files_are_actually_being_read() -> None:
    """The same trap as above. A sweep that reads nothing passes forever and proves nothing."""
    files = test_files()

    assert len(files) >= 5, f"expected the test suite under {TESTS_ROOT}, found {files}"
    assert any(path.name == "test_publisher.py" for path in files)


@pytest.mark.parametrize("forbidden", FORBIDDEN_NAMES)
def test_no_customer_or_personal_name_is_used_as_test_data(forbidden: str) -> None:
    """Fixtures drift too, and a fixture is where the first one got in.

    The package source was swept from the start and the tests were not, so example data kept
    a customer's folder name and a real person's first name long after the code was clean.
    Nothing ships from here, so this is tidiness rather than a defect, but a suite full of one
    customer's vocabulary is how the next person learns the wrong shape of this thing.
    """
    pattern = re.compile(re.escape(forbidden), re.IGNORECASE)

    offenders = [
        f"{path.relative_to(TESTS_ROOT)}:{number}"
        for path in test_files()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if pattern.search(line)
    ]

    assert not offenders, (
        f"{forbidden!r} is used as test data in: {', '.join(offenders)}. "
        "Use a neutral example instead, so the suite does not read as one customer's tool."
    )
