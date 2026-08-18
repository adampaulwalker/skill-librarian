"""Errors raised by the skill librarian.

Every error carries a ``user_message`` written for someone who does not write software.
The user message never contains a file path, a stack trace, a status code, or any other
technical detail. Anything technical goes in ``detail``, which is for the server log only.
"""

from __future__ import annotations

__all__ = [
    "LibrarianError",
    "SkillNotFound",
    "UnsafePath",
    "InvalidSkill",
    "ProposalNotFound",
    "ProposalExpired",
    "DiffMismatch",
    "PublishFailed",
    "NotAuthorized",
]


class LibrarianError(Exception):
    """Base class for every error this service raises on purpose.

    ``user_message`` is what a person reads. ``detail`` is what a maintainer reads in the
    server log, and it is deliberately kept out of the user message.
    """

    default_user_message: str = (
        "Something went wrong and the change was not made. Please try again, and if it "
        "keeps happening let the person who set this up know."
    )

    def __init__(self, user_message: str | None = None, *, detail: str | None = None) -> None:
        message = user_message if user_message else self.default_user_message
        self.user_message: str = message
        self.detail: str | None = detail
        super().__init__(message)

    def __str__(self) -> str:
        return self.user_message


class SkillNotFound(LibrarianError):
    """Asked for a skill that is not in the library."""

    default_user_message = (
        "I could not find a skill with that name. Ask me to list the skills and I will "
        "show you the exact names you can use."
    )


class UnsafePath(LibrarianError):
    """A file location was outside the small set of files this service may change."""

    default_user_message = (
        "That file is not one I am allowed to change. I can only edit a skill's own "
        "instructions file and the extra notes stored with it."
    )


class InvalidSkill(LibrarianError):
    """The skill content itself was missing something required, or had something extra."""

    default_user_message = (
        "That skill content is not in a shape I can save. A skill needs a name and a short "
        "description, and it cannot carry settings I do not recognise."
    )


class ProposalNotFound(LibrarianError):
    """The pending change being approved is not on file."""

    default_user_message = (
        "I no longer have that pending change on file. Ask for the edit again and I will "
        "show you a fresh version to look over."
    )


class ProposalExpired(LibrarianError):
    """The pending change sat unapproved for too long."""

    default_user_message = (
        "That pending change sat unapproved for too long, so I let it go. Ask for the edit "
        "again and I will show you a fresh version to approve."
    )


class DiffMismatch(LibrarianError):
    """The content being published is not the content the person was shown."""

    default_user_message = (
        "The change I was about to publish is not the one you looked at, so I stopped. "
        "Ask for the edit again and approve the new version I show you."
    )


class PublishFailed(LibrarianError):
    """The publish path refused to finish, so nothing reached anyone."""

    default_user_message = (
        "I could not publish that change, so nothing has changed for anyone. Please try "
        "again, and if it keeps happening let the person who set this up know."
    )


class NotAuthorized(LibrarianError):
    """The person is not allowed to do this, or their identity is not known."""

    default_user_message = (
        "I cannot do that for you because I do not know for certain who you are, and every "
        "published change has to be recorded under a real person's name."
    )
