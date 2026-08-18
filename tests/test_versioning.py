"""Tests for reading and moving forward a version number.

The version number is the gate that decides whether a published change reaches anyone, so
the behaviour under test here is mostly about what is refused. Every refusal has to be a
refusal, never a quiet fallback to a starting value such as 0.0.0 or 1.0.0.
"""

from __future__ import annotations

import pytest

from librarian.errors import LibrarianError, PublishFailed
from librarian.versioning import (
    VERSION_KINDS,
    bump_major,
    bump_minor,
    bump_patch,
    bump_version,
    parse_semver,
)

# Words a person who does not write software should never have to meet in a refusal.
JARGON = (
    "semver",
    "semantic version",
    "traceback",
    "exception",
    "stack",
    "parse",
    "regex",
    "string",
    "int(",
    "integer",
    "tuple",
    "none",
    "null",
    "typeerror",
    "valueerror",
)

# Everything this module has to refuse, with a short note on why that decision was made.
REFUSED_VERSIONS = [
    pytest.param(None, id="missing-entirely"),
    pytest.param("", id="empty"),
    pytest.param("   ", id="only-spaces"),
    pytest.param("\n", id="only-a-line-break"),
    pytest.param(142, id="a-number-rather-than-text"),
    pytest.param(1.42, id="a-decimal-rather-than-text"),
    pytest.param(["1", "4", "2"], id="a-list-rather-than-text"),
    pytest.param("v1.4.2", id="leading-v"),
    pytest.param("V1.4.2", id="leading-capital-v"),
    pytest.param("version 1.4.2", id="a-word-in-front"),
    pytest.param("01.4.2", id="leading-zero-first-part"),
    pytest.param("1.04.2", id="leading-zero-middle-part"),
    pytest.param("1.4.02", id="leading-zero-last-part"),
    pytest.param("000.0.0", id="all-zeros-padded"),
    pytest.param("-1.4.2", id="negative-first-part"),
    pytest.param("1.-4.2", id="negative-middle-part"),
    pytest.param("1.4.-2", id="negative-last-part"),
    pytest.param("+1.4.2", id="explicit-plus-sign"),
    pytest.param("1.4", id="only-two-parts"),
    pytest.param("1", id="only-one-part"),
    pytest.param("1.4.2.7", id="four-parts"),
    pytest.param("1.4.2.", id="trailing-dot"),
    pytest.param(".1.4.2", id="leading-dot"),
    pytest.param("1..2", id="empty-middle-part"),
    pytest.param("1.four.2", id="a-word-for-a-number"),
    pytest.param("1.4.x", id="a-letter-for-a-number"),
    pytest.param("1, 4, 2", id="commas-not-dots"),
    pytest.param("1.4 .2", id="a-space-inside"),
    pytest.param("1 . 4 . 2", id="spaces-around-every-dot"),
    pytest.param("١.٤.٢", id="digits-from-another-alphabet"),
    pytest.param("１.４.２", id="wide-digits"),
    pytest.param("1.4.2-beta", id="a-pre-release-label"),
    pytest.param("1.4.2-rc.1", id="a-numbered-pre-release-label"),
    pytest.param("1.4.2+build.7", id="a-build-label"),
    pytest.param("1.4.2-beta+build.7", id="both-labels"),
    pytest.param("1.4.2\x00", id="a-hidden-null-character"),
    pytest.param("1." + "9" * 5000 + ".2", id="a-number-thousands-of-digits-long"),
]


def user_message_of(error: LibrarianError) -> str:
    message = getattr(error, "user_message", "")
    assert isinstance(message, str) and message.strip(), (
        "every refusal has to carry a plain English explanation"
    )
    return message


# ==================================================================================
# Reading a version number
# ==================================================================================


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0.0.0", (0, 0, 0)),
        ("0.1.0", (0, 1, 0)),
        ("1.4.2", (1, 4, 2)),
        ("10.20.30", (10, 20, 30)),
        ("2026.8.18", (2026, 8, 18)),
        ("  1.4.2  ", (1, 4, 2)),
        ("1.4.2\n", (1, 4, 2)),
        ("\t1.4.2\r\n", (1, 4, 2)),
    ],
)
def test_reads_a_plain_three_part_version(text: str, expected: tuple[int, int, int]) -> None:
    assert parse_semver(text) == expected


def test_a_single_zero_in_a_part_is_fine() -> None:
    """A zero on its own is a real number. Only a zero padding another digit is refused."""
    assert parse_semver("0.0.1") == (0, 0, 1)
    assert parse_semver("1.0.0") == (1, 0, 0)


@pytest.mark.parametrize("text", REFUSED_VERSIONS)
def test_a_version_it_cannot_read_is_refused(text: object) -> None:
    """The refusal is one of this service's own errors, so it carries a readable message.

    PublishFailed is the one that fits: a version number nobody can read means the change
    does not get published, and that is what the person needs to hear.
    """
    with pytest.raises(PublishFailed):
        parse_semver(text)  # type: ignore[arg-type]

    assert issubclass(PublishFailed, LibrarianError)


@pytest.mark.parametrize("text", REFUSED_VERSIONS)
def test_a_refusal_never_becomes_a_made_up_starting_version(text: object) -> None:
    """The failure this whole service exists to prevent starts with a silent default.

    If a version that cannot be read were quietly treated as 0.0.0 or 1.0.0, a change
    would be committed, the version would look like it moved, and nobody would ever
    receive the change. So nothing here may return a value for input it cannot read.
    """
    for call in (parse_semver, bump_patch, bump_minor, bump_major, bump_version):
        with pytest.raises(PublishFailed):
            result = call(text)  # type: ignore[arg-type]
            pytest.fail(f"{call.__name__} quietly returned {result!r} instead of refusing")


@pytest.mark.parametrize("text", REFUSED_VERSIONS)
def test_a_refusal_is_readable_by_someone_who_does_not_write_software(text: object) -> None:
    with pytest.raises(PublishFailed) as caught:
        parse_semver(text)  # type: ignore[arg-type]

    message = user_message_of(caught.value)
    lowered = message.lower()
    for word in JARGON:
        assert word not in lowered, f"the refusal uses the word {word!r}: {message}"
    assert "--" not in message
    assert "—" not in message
    assert message.strip().endswith("."), "a refusal reads as a finished sentence"
    assert "1.4.2" in message, "a refusal shows the reader what a version number looks like"


def test_a_missing_version_says_one_needs_adding() -> None:
    for absent in (None, "", "   "):
        with pytest.raises(PublishFailed) as caught:
            parse_semver(absent)  # type: ignore[arg-type]
        assert "no version number" in user_message_of(caught.value).lower()


def test_a_version_that_is_there_but_not_text_is_not_called_missing() -> None:
    """Telling someone to add a version they can plainly see would send them in circles."""
    with pytest.raises(PublishFailed) as caught:
        parse_semver(1.42)  # type: ignore[arg-type]
    message = user_message_of(caught.value).lower()
    assert "no version number" not in message
    assert "quotation marks" in message


def test_a_number_far_longer_than_any_version_is_refused_readably() -> None:
    with pytest.raises(PublishFailed) as caught:
        parse_semver("1.4." + "2" * 5000)
    assert "more digits" in user_message_of(caught.value).lower()


def test_a_number_just_over_the_limit_is_refused_with_the_same_wording() -> None:
    """The wording has to be true at the edge, not only for an absurdly long number."""
    with pytest.raises(PublishFailed) as caught:
        parse_semver("1.4." + "9" * 19)
    assert "more digits" in user_message_of(caught.value).lower()


def test_a_version_at_the_top_of_the_range_is_refused_rather_than_moved() -> None:
    """Stepping up would land on a number this module refuses, so it says so instead.

    The important part is that it refuses. It never hands back a version that cannot be
    read the next time a change is published.
    """
    at_the_top = "1.4." + "9" * 18
    assert parse_semver(at_the_top)
    with pytest.raises(PublishFailed) as caught:
        bump_patch(at_the_top)
    message = user_message_of(caught.value).lower()
    assert "nothing has been changed" in message
    assert "highest number" in message


def test_a_long_but_believable_version_still_works() -> None:
    """A version built from a date is unusual but real, so it must not be refused."""
    assert parse_semver("20260818.1.0") == (20260818, 1, 0)
    assert bump_patch("20260818.1.0") == "20260818.1.1"


def test_a_leading_zero_is_called_out_on_its_own_terms() -> None:
    with pytest.raises(PublishFailed) as caught:
        parse_semver("1.04.2")
    assert "zero" in user_message_of(caught.value).lower()


# ==================================================================================
# Moving a version number forward
# ==================================================================================


@pytest.mark.parametrize(
    ("current", "expected"),
    [("0.0.0", "0.0.1"), ("1.4.2", "1.4.3"), ("1.4.9", "1.4.10"), ("2026.8.18", "2026.8.19")],
)
def test_a_patch_step_moves_the_last_number(current: str, expected: str) -> None:
    assert bump_patch(current) == expected


@pytest.mark.parametrize(
    ("current", "expected"),
    [("0.0.0", "0.1.0"), ("1.4.2", "1.5.0"), ("1.9.9", "1.10.0")],
)
def test_a_minor_step_moves_the_middle_number_and_clears_the_last(
    current: str, expected: str
) -> None:
    assert bump_minor(current) == expected


@pytest.mark.parametrize(
    ("current", "expected"),
    [("0.0.0", "1.0.0"), ("1.4.2", "2.0.0"), ("9.9.9", "10.0.0")],
)
def test_a_major_step_moves_the_first_number_and_clears_the_rest(
    current: str, expected: str
) -> None:
    assert bump_major(current) == expected


def test_bump_version_defaults_to_the_smallest_step() -> None:
    assert bump_version("1.4.2") == "1.4.3"


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("patch", "1.4.3"),
        ("minor", "1.5.0"),
        ("major", "2.0.0"),
        ("PATCH", "1.4.3"),
        ("Minor", "1.5.0"),
        ("  major  ", "2.0.0"),
    ],
)
def test_bump_version_understands_each_named_step(kind: str, expected: str) -> None:
    assert bump_version("1.4.2", kind) == expected


def test_every_named_step_in_the_public_list_works() -> None:
    for kind in VERSION_KINDS:
        assert bump_version("1.4.2", kind) != "1.4.2"


@pytest.mark.parametrize(
    "kind",
    ["", "   ", "biggish", "pat", "PATCH!", "1", "release", None, 3],
)
def test_a_step_it_does_not_recognise_is_refused_rather_than_treated_as_a_patch(
    kind: object,
) -> None:
    with pytest.raises(PublishFailed) as caught:
        result = bump_version("1.4.2", kind)  # type: ignore[arg-type]
        pytest.fail(f"an unrecognised step quietly produced {result!r}")

    message = user_message_of(caught.value)
    assert "nothing has been changed" in message.lower()
    for word in ("patch", "minor", "major"):
        assert word in message.lower(), "the reader is told which steps do work"


def test_the_version_the_change_started_from_is_never_returned_unchanged() -> None:
    for current in ("0.0.0", "0.1.0", "1.4.2", "9.9.9", "2026.8.18"):
        for kind in VERSION_KINDS:
            assert bump_version(current, kind) != current


@pytest.mark.parametrize("kind", list(VERSION_KINDS))
@pytest.mark.parametrize(
    "current",
    ["0.0.0", "0.0.9", "0.1.0", "1.0.0", "1.4.2", "1.9.9", "9.9.9", "10.20.30", "2026.8.18"],
)
def test_a_step_always_lands_on_a_strictly_higher_version(current: str, kind: str) -> None:
    """The one promise this module makes. A step that is not higher reaches nobody."""
    result = bump_version(current, kind)
    assert parse_semver(result) > parse_semver(current)


def test_the_result_of_a_step_can_itself_be_read_back() -> None:
    """Whatever comes out has to be something this module would accept going in."""
    current = "1.4.2"
    for _ in range(5):
        current = bump_patch(current)
        assert parse_semver(current)
    assert current == "1.4.7"


def test_surrounding_spaces_do_not_survive_into_the_new_version() -> None:
    """The new number is written into a settings file, so it has to be clean text."""
    assert bump_patch("  1.4.2\n") == "1.4.3"
    assert bump_version(" 1.4.2 ", " patch ") == "1.4.3"


def test_steps_can_be_taken_one_after_another_across_the_kinds() -> None:
    version = "0.9.9"
    version = bump_patch(version)
    assert version == "0.9.10"
    version = bump_minor(version)
    assert version == "0.10.0"
    version = bump_major(version)
    assert version == "1.0.0"
