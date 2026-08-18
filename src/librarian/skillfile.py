"""Read, write, and check a single SKILL.md file.

A SKILL.md file is a settings block at the very top, written between two lines
of three dashes, followed by the markdown text of the skill itself.

Two rules matter more than anything else here:

1. Reading a file and writing it straight back must give the same skill, with
   the settings in the same order and the markdown text untouched.
2. Only a small, fixed set of settings is allowed. Anything else is refused
   with a message a non-technical reader can act on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import yaml

from .errors import InvalidSkill

#: The only settings a skill file may carry, in the order they are listed to
#: the reader when something unexpected turns up.
ALLOWED_KEYS: tuple[str, ...] = (
    "name",
    "description",
    "license",
    "compatibility",
    "metadata",
    "allowed-tools",
)

#: The line that opens and closes the settings block.
DELIMITER = "---"

#: Wide enough that a long setting is never wrapped onto a second line when it
#: is written back out.
_MAX_LINE_WIDTH = 1_000_000


def _invalid(message: str) -> InvalidSkill:
    """Build the refusal a reader sees, carrying a plain English explanation."""
    try:
        error = InvalidSkill(message)
    except TypeError:
        error = InvalidSkill(message, message)
    if getattr(error, "user_message", None) != message:
        try:
            error.user_message = message
        except AttributeError:
            pass
    return error


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    frontmatter: dict[str, Any]
    body: str


def validate_frontmatter(fm: dict[str, Any]) -> dict[str, Any]:
    """Check the settings of a skill and hand back a copy in the same order."""
    if not isinstance(fm, dict):
        raise _invalid(
            "The settings at the top of this skill need to be a list of named "
            "settings, one per line, such as 'description: what this skill is for'."
        )

    checked: dict[str, Any] = {}
    for key, value in fm.items():
        if not isinstance(key, str):
            raise _invalid(
                "Every setting at the top of a skill needs a plain name, such "
                "as 'description'."
            )
        if key not in ALLOWED_KEYS:
            raise _invalid(
                f"'{key}' is not a setting a skill can have. A skill can use "
                f"only these: {', '.join(ALLOWED_KEYS)}."
            )
        checked[key] = value

    if "description" not in checked:
        raise _invalid(
            "This skill has no description. Add a line at the top that starts "
            "with 'description:' and says in one sentence what the skill is for."
        )

    description = checked["description"]
    if not isinstance(description, str):
        raise _invalid(
            "This skill's description needs to be written as a sentence of "
            "ordinary text."
        )
    if not description.strip():
        raise _invalid(
            "This skill's description is blank. Write one sentence saying what "
            "the skill is for."
        )

    if "name" in checked:
        name = checked["name"]
        if not isinstance(name, str):
            raise _invalid(
                "This skill's name needs to be written as ordinary text, such "
                "as 'meeting-notes'."
            )
        if not name.strip():
            raise _invalid(
                "This skill's name is blank. Give the skill a short name, or "
                "take the name line out altogether."
            )

    if "metadata" in checked and not isinstance(checked["metadata"], dict):
        raise _invalid(
            "The extra information under 'metadata' needs to be a list of "
            "named values, one per line, such as 'owner: the support team'."
        )

    if "allowed-tools" in checked:
        tools = checked["allowed-tools"]
        tools_are_text_list = isinstance(tools, list) and all(
            isinstance(tool, str) for tool in tools
        )
        if not isinstance(tools, str) and not tools_are_text_list:
            raise _invalid(
                "The 'allowed-tools' setting needs to be either one tool name "
                "or a list of tool names."
            )

    return checked


def parse_skill(text: str) -> Skill:
    """Turn the text of a SKILL.md file into a skill."""
    if not isinstance(text, str):
        raise _invalid("This skill file could not be read as text.")

    # A file saved by some editors starts with an invisible marker character.
    # Ignore it so the file is not refused for something nobody can see.
    lines = text.lstrip("\ufeff").split("\n")

    if lines[0].rstrip() != DELIMITER:
        raise _invalid(
            "This skill does not start with a settings block. The first line "
            "needs to be three dashes (---), then the settings, then another "
            "line of three dashes."
        )

    closing: int | None = None
    for index in range(1, len(lines)):
        if lines[index].rstrip() == DELIMITER:
            closing = index
            break

    if closing is None:
        raise _invalid(
            "The settings block at the top of this skill was never closed. Add "
            "a line of three dashes (---) after the settings and before the "
            "rest of the skill."
        )

    settings_text = "\n".join(lines[1:closing])
    body = "\n".join(lines[closing + 1 :])

    try:
        loaded = yaml.safe_load(settings_text)
    except yaml.YAMLError:
        raise _invalid(
            "The settings at the top of this skill could not be read. Each one "
            "needs to be on its own line, written as a name, then a colon, "
            "then the value."
        ) from None

    if loaded is None:
        raise _invalid(
            "The settings block at the top of this skill is empty. It needs at "
            "least a description."
        )

    if not isinstance(loaded, dict):
        raise _invalid(
            "The settings at the top of this skill are not a list of named "
            "settings. Each line needs a name, then a colon, then the value, "
            "such as 'description: what this skill is for'."
        )

    frontmatter = validate_frontmatter(loaded)
    return Skill(
        name=frontmatter.get("name", ""),
        description=frontmatter["description"],
        frontmatter=frontmatter,
        body=body,
    )


def render_skill(skill: Skill) -> str:
    """Turn a skill back into the text of a SKILL.md file."""
    if not isinstance(skill.body, str):
        raise _invalid("The text of this skill could not be read as writing.")

    frontmatter = validate_frontmatter(skill.frontmatter)

    if skill.name != frontmatter.get("name", ""):
        raise _invalid(
            "The name being held for this skill is not the same as the name "
            "written at the top of the file. Make the two match before saving."
        )
    if skill.description != frontmatter["description"]:
        raise _invalid(
            "The description being held for this skill is not the same as the "
            "description written at the top of the file. Make the two match "
            "before saving."
        )

    try:
        settings_text = yaml.safe_dump(
            frontmatter,
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
            width=_MAX_LINE_WIDTH,
        )
    except yaml.YAMLError:
        raise _invalid(
            "The settings for this skill could not be written out. Please keep "
            "them to ordinary text, numbers, lists, and simple yes or no values."
        ) from None

    if not settings_text.endswith("\n"):
        settings_text += "\n"

    return f"{DELIMITER}\n{settings_text}{DELIMITER}\n{skill.body}"
