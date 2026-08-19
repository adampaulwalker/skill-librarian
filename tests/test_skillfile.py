"""Tests for reading, writing, and checking a SKILL.md file."""

from __future__ import annotations

import pathlib

import pytest
import yaml

from librarian.errors import InvalidSkill
from librarian.skillfile import (
    ALLOWED_KEYS,
    Skill,
    parse_skill,
    render_skill,
    validate_frontmatter,
)

SIMPLE = """---
name: meeting-notes
description: Write up the notes from a client meeting.
---
# Meeting notes

Start with the decision, then the reasons.
"""

# Words a reader should never have to meet in a refusal.
JARGON = (
    "yaml",
    "frontmatter",
    "traceback",
    "exception",
    "stack",
    "parse",
    "serialis",
    "serializ",
    "dict",
    "none",
    "null",
)


def user_message_of(error: InvalidSkill) -> str:
    message = getattr(error, "user_message", "")
    assert isinstance(message, str) and message.strip(), (
        "every refusal has to carry a plain English explanation"
    )
    return message


def assert_reads_plainly(error: InvalidSkill) -> str:
    message = user_message_of(error)
    lowered = message.lower()
    for word in JARGON:
        assert word not in lowered, f"refusal uses jargon {word!r}: {message}"
    # The literal line of three dashes is file syntax, not prose, so it is
    # allowed to appear; nothing else may run two hyphens together.
    assert "--" not in message.replace("(---)", ""), (
        f"refusal uses a double hyphen: {message}"
    )
    return message


# ===
# Reading a file
# ===


def test_parse_reads_settings_and_body() -> None:
    skill = parse_skill(SIMPLE)
    assert skill.name == "meeting-notes"
    assert skill.description == "Write up the notes from a client meeting."
    assert skill.frontmatter == {
        "name": "meeting-notes",
        "description": "Write up the notes from a client meeting.",
    }
    assert skill.body == (
        "# Meeting notes\n\nStart with the decision, then the reasons.\n"
    )


def test_parse_keeps_the_body_exactly_as_written() -> None:
    body = "One\n\n\nTwo   \n\ttabbed\n\n"
    skill = parse_skill(f"---\ndescription: Keep the text.\n---\n{body}")
    assert skill.body == body


def test_parse_allows_a_skill_with_no_name() -> None:
    skill = parse_skill("---\ndescription: No name here.\n---\nBody\n")
    assert skill.name == ""
    assert "name" not in skill.frontmatter


def test_parse_keeps_the_order_of_the_settings() -> None:
    text = (
        "---\n"
        "description: Ordered on purpose.\n"
        "license: MIT\n"
        "name: ordered\n"
        "allowed-tools:\n"
        "  - Read\n"
        "---\n"
        "Body\n"
    )
    skill = parse_skill(text)
    assert list(skill.frontmatter) == [
        "description",
        "license",
        "name",
        "allowed-tools",
    ]


def test_parse_accepts_every_allowed_setting() -> None:
    text = (
        "---\n"
        "name: everything\n"
        "description: Uses every setting a skill may have.\n"
        "license: MIT\n"
        "compatibility: claude-code\n"
        "metadata:\n"
        "  owner: Robin\n"
        "allowed-tools:\n"
        "  - Read\n"
        "  - Write\n"
        "---\n"
        "Body\n"
    )
    skill = parse_skill(text)
    assert list(skill.frontmatter) == list(ALLOWED_KEYS)


def test_parse_ignores_an_invisible_marker_at_the_start_of_the_file() -> None:
    skill = parse_skill("﻿" + SIMPLE)
    assert skill.name == "meeting-notes"


# ===
# The body is never damaged
# ===


def test_a_line_of_three_dashes_in_the_body_survives() -> None:
    body = "Before the line.\n\n---\n\nAfter the line.\n"
    text = f"---\ndescription: Has a divider in the text.\n---\n{body}"
    skill = parse_skill(text)
    assert skill.body == body
    assert parse_skill(render_skill(skill)) == skill


def test_a_body_that_starts_with_three_dashes_survives() -> None:
    body = "---\nstill the body\n"
    text = f"---\ndescription: Starts with a divider.\n---\n{body}"
    skill = parse_skill(text)
    assert skill.body == body
    assert parse_skill(render_skill(skill)).body == body


def test_a_code_fence_holding_three_dashes_survives() -> None:
    body = (
        "Example file:\n\n"
        "```markdown\n"
        "---\n"
        "name: example\n"
        "description: An example skill.\n"
        "---\n"
        "Body of the example.\n"
        "```\n\n"
        "That is the whole example.\n"
    )
    text = f"---\ndescription: Shows an example file.\n---\n{body}"
    skill = parse_skill(text)
    assert skill.body == body
    assert parse_skill(render_skill(skill)) == skill


def test_trailing_newlines_in_the_body_survive() -> None:
    for body in ("", "Body", "Body\n", "Body\n\n", "Body\n\n\n\n"):
        text = f"---\ndescription: Keeps the ending.\n---\n{body}"
        skill = parse_skill(text)
        assert skill.body == body
        assert parse_skill(render_skill(skill)).body == body


def test_non_english_characters_survive() -> None:
    body = "Grüße, 世界. Émile paid 5 € for a café. 🎉\n"
    text = f"---\ndescription: Grüße aus München - 世界 🎉\n---\n{body}"
    skill = parse_skill(text)
    assert skill.body == body
    assert skill.description == "Grüße aus München - 世界 🎉"
    assert parse_skill(render_skill(skill)) == skill


def test_windows_line_endings_leave_the_body_alone() -> None:
    text = "---\r\ndescription: Saved on Windows.\r\n---\r\nBody line\r\n"
    skill = parse_skill(text)
    assert skill.description == "Saved on Windows."
    assert skill.body == "Body line\r\n"


# ===
# Writing a file back out
# ===


def test_render_round_trips_exactly() -> None:
    skill = parse_skill(SIMPLE)
    assert parse_skill(render_skill(skill)) == skill


def test_render_round_trips_a_skill_carrying_every_setting() -> None:
    skill = Skill(
        name="everything",
        description="Uses every setting a skill may have.",
        frontmatter={
            "name": "everything",
            "description": "Uses every setting a skill may have.",
            "license": "MIT",
            "compatibility": "claude-code",
            "metadata": {"owner": "Robin", "reviewed": True},
            "allowed-tools": ["Read", "Write"],
        },
        body="Body\n",
    )
    assert parse_skill(render_skill(skill)) == skill


def test_render_round_trips_a_description_holding_a_line_of_three_dashes() -> None:
    skill = Skill(
        name="",
        description="First part\n---\nsecond part",
        frontmatter={"description": "First part\n---\nsecond part"},
        body="Body\n",
    )
    rendered = render_skill(skill)
    assert parse_skill(rendered) == skill


def test_render_keeps_the_order_of_the_settings() -> None:
    frontmatter = {
        "description": "Ordered on purpose.",
        "license": "MIT",
        "name": "ordered",
        "compatibility": "claude-code",
    }
    skill = Skill(
        name="ordered",
        description="Ordered on purpose.",
        frontmatter=frontmatter,
        body="Body\n",
    )
    rendered = render_skill(skill)
    settings_lines = rendered.split("---\n")[1].splitlines()
    assert [line.split(":")[0] for line in settings_lines] == list(frontmatter)
    assert list(parse_skill(rendered).frontmatter) == list(frontmatter)


def test_render_starts_and_closes_the_settings_block() -> None:
    rendered = render_skill(parse_skill(SIMPLE))
    assert rendered.startswith("---\n")
    assert "\n---\n" in rendered


def test_render_refuses_a_skill_whose_description_drifted_from_its_settings() -> None:
    skill = Skill(
        name="",
        description="What the caller thinks it says.",
        frontmatter={"description": "What the file actually says."},
        body="Body\n",
    )
    with pytest.raises(InvalidSkill) as caught:
        render_skill(skill)
    assert_reads_plainly(caught.value)


def test_render_refuses_a_skill_whose_name_drifted_from_its_settings() -> None:
    skill = Skill(
        name="one-name",
        description="A description.",
        frontmatter={"name": "another-name", "description": "A description."},
        body="Body\n",
    )
    with pytest.raises(InvalidSkill) as caught:
        render_skill(skill)
    assert_reads_plainly(caught.value)


# ===
# Settings that are refused
# ===


def test_an_unknown_setting_is_refused_and_named() -> None:
    text = "---\ndescription: Fine.\nauthor: Robin\n---\nBody\n"
    with pytest.raises(InvalidSkill) as caught:
        parse_skill(text)
    message = assert_reads_plainly(caught.value)
    assert "author" in message
    for key in ALLOWED_KEYS:
        assert key in message


@pytest.mark.parametrize("key", ["Description", "tools", "version", "hooks", "model"])
def test_settings_outside_the_allowed_list_are_refused(key: str) -> None:
    with pytest.raises(InvalidSkill) as caught:
        validate_frontmatter({"description": "Fine.", key: "value"})
    assert key in assert_reads_plainly(caught.value)


def test_a_missing_description_is_refused() -> None:
    with pytest.raises(InvalidSkill) as caught:
        parse_skill("---\nname: no-description\n---\nBody\n")
    assert_reads_plainly(caught.value)


@pytest.mark.parametrize("blank", ["", " ", "   \n  ", "\t"])
def test_a_blank_description_is_refused(blank: str) -> None:
    with pytest.raises(InvalidSkill) as caught:
        validate_frontmatter({"description": blank})
    assert_reads_plainly(caught.value)


def test_a_description_that_is_not_writing_is_refused() -> None:
    with pytest.raises(InvalidSkill) as caught:
        validate_frontmatter({"description": ["a", "list"]})
    assert_reads_plainly(caught.value)


def test_a_blank_name_is_refused() -> None:
    with pytest.raises(InvalidSkill) as caught:
        validate_frontmatter({"name": "  ", "description": "Fine."})
    assert_reads_plainly(caught.value)


def test_metadata_that_is_not_a_list_of_named_values_is_refused() -> None:
    with pytest.raises(InvalidSkill) as caught:
        validate_frontmatter({"description": "Fine.", "metadata": "owner"})
    assert_reads_plainly(caught.value)


def test_allowed_tools_that_is_not_a_list_of_names_is_refused() -> None:
    with pytest.raises(InvalidSkill) as caught:
        validate_frontmatter({"description": "Fine.", "allowed-tools": {"Read": True}})
    assert_reads_plainly(caught.value)


def test_allowed_tools_may_be_one_name_or_a_list() -> None:
    assert validate_frontmatter({"description": "Fine.", "allowed-tools": "Read"})
    assert validate_frontmatter({"description": "Fine.", "allowed-tools": ["Read"]})


def test_validate_returns_a_copy_and_keeps_the_order() -> None:
    original = {"description": "Fine.", "license": "MIT", "name": "kept"}
    checked = validate_frontmatter(original)
    assert checked == original
    assert list(checked) == list(original)
    checked["license"] = "Apache-2.0"
    assert original["license"] == "MIT"


# ===
# Files that are not shaped like a skill
# ===


def test_a_file_with_no_settings_block_is_refused() -> None:
    with pytest.raises(InvalidSkill) as caught:
        parse_skill("# Just markdown\n\nNo settings at all.\n")
    assert_reads_plainly(caught.value)


def test_a_settings_block_that_is_never_closed_is_refused() -> None:
    with pytest.raises(InvalidSkill) as caught:
        parse_skill("---\ndescription: Never closed.\n\nBody text\n")
    assert_reads_plainly(caught.value)


def test_an_empty_settings_block_is_refused() -> None:
    with pytest.raises(InvalidSkill) as caught:
        parse_skill("---\n---\nBody\n")
    assert_reads_plainly(caught.value)


def test_an_empty_file_is_refused() -> None:
    with pytest.raises(InvalidSkill) as caught:
        parse_skill("")
    assert_reads_plainly(caught.value)


@pytest.mark.parametrize(
    "settings",
    [
        "- one\n- two",
        "just a sentence",
        "42",
        "'text in quotes'",
    ],
)
def test_settings_that_are_not_a_list_of_named_values_are_refused(
    settings: str,
) -> None:
    with pytest.raises(InvalidSkill) as caught:
        parse_skill(f"---\n{settings}\n---\nBody\n")
    assert_reads_plainly(caught.value)


def test_settings_that_cannot_be_read_are_refused() -> None:
    with pytest.raises(InvalidSkill) as caught:
        parse_skill("---\ndescription: 'unbalanced quote\n---\nBody\n")
    assert_reads_plainly(caught.value)


# ===
# Safety
# ===


def test_the_module_never_uses_the_unsafe_reader() -> None:
    source = pathlib.Path(
        __file__
    ).resolve().parents[1].joinpath("src", "librarian", "skillfile.py").read_text(
        encoding="utf-8"
    )
    assert "yaml.load(" not in source
    assert "unsafe_load" not in source
    assert "full_load" not in source
    assert "yaml.safe_load(" in source


def test_settings_never_build_python_objects() -> None:
    dangerous = (
        "---\n"
        "description: Looks ordinary.\n"
        "metadata: !!python/object/apply:os.system ['echo unsafe']\n"
        "---\n"
        "Body\n"
    )
    with pytest.raises((InvalidSkill, yaml.YAMLError)):
        parse_skill(dangerous)


def test_a_very_long_description_is_written_on_one_line() -> None:
    description = "A long description that keeps going. " * 20
    skill = Skill(
        name="",
        description=description,
        frontmatter={"description": description},
        body="Body\n",
    )
    rendered = render_skill(skill)
    assert parse_skill(rendered) == skill
    assert rendered.count("\n") == 4
