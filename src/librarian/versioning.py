"""Work out the version number that makes a published change actually reach people.

The version number in a plugin's settings file is the gate. If the words of a skill change
but that number does not, everyone keeps the copy they already have and nobody is told
anything is wrong. So this module has one rule above all others: it never guesses.

A version number that is missing, empty, or written in a shape this module does not
understand raises an error. It is never quietly replaced with a starting value such as
0.0.0 or 1.0.0, because a made up starting value is how a change ships without a real
version bump, which is the exact silent failure this whole service exists to prevent.

What is accepted, and what is not:

- ``1.4.2`` is accepted. Three plain numbers separated by dots, and nothing else.
- Spaces or a line break around the number are trimmed before it is read, so ``" 1.4.2 "``
  is accepted. A space inside the number is refused.
- ``v1.4.2`` is refused. The number written in the settings file is compared letter for
  letter when a change is delivered, so a decorated form is corrected by a person once
  rather than silently rewritten here on every publish.
- ``01.4.2`` is refused. ``01`` and ``1`` read as the same number to a person but are two
  different pieces of text to the delivery check, and that difference is exactly the kind
  of near miss that makes a change look published when it is not.
- ``1.4`` and ``1.4.2.7`` are refused. Neither has three numbers, and inventing the
  missing one or dropping the extra one would be a guess.
- A number with more than eighteen digits is refused. Nothing that long is a version
  number, and reading it back would fail in a way nobody could act on. A version already
  sitting at the top of that range is refused too, rather than being moved to a number
  this module would then turn around and refuse.
- ``1.4.-2`` and ``1.four.2`` are refused. Only the digits 0 to 9 count as a number here,
  so digits written in other alphabets are refused as well.
- ``1.4.2-beta.1`` and ``1.4.2+build.7`` are refused. A trailing label like that carries
  meaning this module cannot keep hold of, and dropping it would change what the settings
  file says without anybody asking for that.

Every refusal is written for someone who does not write software, and every one of them
says what to do next.
"""

from __future__ import annotations

import re

from .errors import PublishFailed

__all__ = [
    "MAJOR",
    "MINOR",
    "PATCH",
    "VERSION_KINDS",
    "bump_major",
    "bump_minor",
    "bump_patch",
    "bump_version",
    "parse_semver",
]

#: The three sizes of step a version number can take.
PATCH = "patch"
MINOR = "minor"
MAJOR = "major"

#: Listed smallest step first, which is the order they are offered to a reader.
VERSION_KINDS: tuple[str, ...] = (PATCH, MINOR, MAJOR)

#: Three runs of digits separated by dots, and nothing else at all. Written out as the
#: digits 0 to 9 on purpose: a looser check would let through digits from other alphabets,
#: which read as numbers to a person but not to the delivery check.
_SHAPE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")

_EXAMPLE = "1.4.2"

#: Long enough for any real version number, including one built from a date such as
#: 20260818. Short enough that the digits can always be read back as a number.
_MAX_DIGITS_PER_PART = 18

_MISSING_MESSAGE = (
    "There is no version number recorded for these skills, so there is nothing to move "
    "forward from and nothing has been changed. Someone with access to the repository "
    f"needs to add one, written as three numbers separated by dots, like {_EXAMPLE}."
)

_NOT_TEXT_MESSAGE = (
    "The version number recorded for these skills is not written as words I can read, so "
    "nothing has been changed. It needs to be three plain numbers separated by dots, like "
    f"{_EXAMPLE}, written in quotation marks so that it reads as text. Someone with access "
    "to the repository needs to correct it."
)

_SHAPE_MESSAGE = (
    "The version number recorded for these skills is not written in a way I can work "
    "with, so nothing has been changed. It needs to be three plain numbers separated by "
    f"dots, like {_EXAMPLE}, with nothing before them and nothing after them. Someone "
    "with access to the repository needs to correct it."
)

_LEADING_ZERO_MESSAGE = (
    "The version number recorded for these skills has an extra zero at the front of one "
    "of its numbers, so nothing has been changed. Please write it as "
    f"{_EXAMPLE} rather than 01.04.02, so that the next version is easy to tell apart "
    "from this one. Someone with access to the repository needs to correct it."
)

_TOO_LONG_MESSAGE = (
    "One of the numbers in the version number recorded for these skills has far more "
    "digits than a version number ever has, so nothing has been changed. Please write it "
    f"as something like {_EXAMPLE}. Someone with access to the repository needs to correct "
    "it."
)

_TOO_HIGH_MESSAGE = (
    "The version number recorded for these skills has already reached the highest number "
    "I can count up to, so it cannot be moved forward and nothing has been changed. "
    f"Someone with access to the repository needs to set a smaller one, like {_EXAMPLE}."
)

_UNKNOWN_KIND_MESSAGE = (
    "I was asked to move the version number forward by a size of step I do not "
    "recognise, so nothing has been changed. I can move it forward by a patch step for a "
    "small fix, a minor step for a new ability, or a major step for a big change."
)

_NOT_FORWARD_MESSAGE = (
    "The new version number did not come out higher than the one already recorded, so "
    "nothing has been changed. A change only reaches people when its version number goes "
    "up, so publishing this would have looked successful while reaching nobody."
)


def parse_semver(v: str) -> tuple[int, int, int]:
    """Read a version number as its three parts, or refuse and say why.

    Returns the three numbers in order: the major number, the minor number, and the patch
    number. Raises :class:`~librarian.errors.PublishFailed` for anything it cannot read,
    and never falls back to a starting value of its own.
    """
    if v is None:
        raise PublishFailed(_MISSING_MESSAGE, detail="version was absent")

    if not isinstance(v, str):
        raise PublishFailed(_NOT_TEXT_MESSAGE, detail=f"version was {type(v).__name__}, not text")

    cleaned = v.strip()
    if not cleaned:
        raise PublishFailed(_MISSING_MESSAGE, detail="version was empty or only spaces")

    if not _SHAPE.match(cleaned):
        raise PublishFailed(_SHAPE_MESSAGE, detail=f"version {cleaned!r} is not three dotted numbers")

    parts = cleaned.split(".")
    for part in parts:
        # Python refuses to read a run of digits this long back as a number, and it would
        # do so with an error nobody could act on, so it is caught here instead.
        if len(part) > _MAX_DIGITS_PER_PART:
            raise PublishFailed(
                _TOO_LONG_MESSAGE, detail=f"a part of version {cleaned!r} has {len(part)} digits"
            )
        if len(part) > 1 and part.startswith("0"):
            raise PublishFailed(
                _LEADING_ZERO_MESSAGE, detail=f"version {cleaned!r} has a leading zero"
            )

    major, minor, patch = (int(part) for part in parts)
    return major, minor, patch


def bump_patch(v: str) -> str:
    """The next patch version, for a small fix: 1.4.2 becomes 1.4.3."""
    major, minor, patch = parse_semver(v)
    return _finish(v, (major, minor, patch + 1))


def bump_minor(v: str) -> str:
    """The next minor version, for a new ability: 1.4.2 becomes 1.5.0."""
    major, minor, _patch = parse_semver(v)
    return _finish(v, (major, minor + 1, 0))


def bump_major(v: str) -> str:
    """The next major version, for a big change: 1.4.2 becomes 2.0.0."""
    major, _minor, _patch = parse_semver(v)
    return _finish(v, (major + 1, 0, 0))


def bump_version(current: str, kind: str = "patch") -> str:
    """Move a version number forward by the named size of step.

    ``kind`` is one of ``patch``, ``minor``, or ``major``. Anything else is refused rather
    than quietly treated as a patch, because a step nobody asked for is still a guess.
    """
    if not isinstance(kind, str):
        raise PublishFailed(_UNKNOWN_KIND_MESSAGE, detail=f"kind was {type(kind).__name__}, not text")

    wanted = kind.strip().lower()
    if wanted == PATCH:
        return bump_patch(current)
    if wanted == MINOR:
        return bump_minor(current)
    if wanted == MAJOR:
        return bump_major(current)

    raise PublishFailed(_UNKNOWN_KIND_MESSAGE, detail=f"kind {kind!r} is not one of {VERSION_KINDS}")


def _finish(current: str, parts: tuple[int, int, int]) -> str:
    """Write the three parts back out, having checked the result really is a step forward.

    The check costs nothing and guards the one promise this module makes: what comes back
    is always higher than what went in.
    """
    rendered = ".".join(str(part) for part in parts)

    # A version sitting at the very top of the range this module reads would step up to a
    # number it would then refuse. Saying so plainly beats handing back a version that
    # cannot be read the next time round.
    if any(len(str(part)) > _MAX_DIGITS_PER_PART for part in parts):
        raise PublishFailed(_TOO_HIGH_MESSAGE, detail=f"stepping up from {current!r} overflows")

    if parse_semver(rendered) <= parse_semver(current):
        raise PublishFailed(
            _NOT_FORWARD_MESSAGE, detail=f"bumping {current!r} produced {rendered!r}"
        )
    return rendered
