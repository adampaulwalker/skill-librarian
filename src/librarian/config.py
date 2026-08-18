"""Server settings, read once from the environment.

Nothing here comes from a chat message. Which repository to work in, and which branch
everyone shares, are decided by whoever runs the server, never by the model and never by
the person typing.

There is deliberately no setting for where the skills sit inside the repository. A
repository can hold several collections of skills at once, and the arrangement is read at
runtime from the repository's own manifest. Freezing a folder name here would quietly
hide every collection except one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .errors import LibrarianError

__all__ = ["Config", "load_config"]

_MISSING_SETTING = (
    "This service is not set up yet. The person who runs it still needs to tell it which "
    "shared library of skills to work with."
)
_BAD_SETTING = (
    "This service is not set up correctly. The person who runs it needs to check its "
    "settings before it can make any changes."
)


@dataclass(frozen=True)
class Config:
    repo_owner: str
    repo_name: str
    default_branch: str = "main"
    proposal_ttl_seconds: int = 900
    sync_estimate_minutes: int = 30


def load_config() -> Config:
    """Build the settings from environment variables.

    Reads LIBRARIAN_REPO_OWNER, LIBRARIAN_REPO_NAME, LIBRARIAN_DEFAULT_BRANCH,
    LIBRARIAN_PROPOSAL_TTL_SECONDS and LIBRARIAN_SYNC_ESTIMATE_MINUTES. The first two are
    required, because without them the service does not know which library it looks after.
    """
    owner = _required("LIBRARIAN_REPO_OWNER")
    name = _required("LIBRARIAN_REPO_NAME")
    branch = _optional_text("LIBRARIAN_DEFAULT_BRANCH", "main")
    ttl = _positive_int("LIBRARIAN_PROPOSAL_TTL_SECONDS", 900)
    sync_minutes = _positive_int("LIBRARIAN_SYNC_ESTIMATE_MINUTES", 30)
    return Config(
        repo_owner=owner,
        repo_name=name,
        default_branch=branch,
        proposal_ttl_seconds=ttl,
        sync_estimate_minutes=sync_minutes,
    )


def _required(key: str) -> str:
    value = (os.environ.get(key) or "").strip()
    if not value:
        raise LibrarianError(_MISSING_SETTING, detail=f"environment variable {key} is not set")
    return value


def _optional_text(key: str, fallback: str) -> str:
    value = (os.environ.get(key) or "").strip()
    return value if value else fallback


def _positive_int(key: str, fallback: int) -> int:
    raw = (os.environ.get(key) or "").strip()
    if not raw:
        return fallback
    try:
        value = int(raw)
    except ValueError as exc:
        raise LibrarianError(
            _BAD_SETTING, detail=f"environment variable {key} must be a whole number, got {raw!r}"
        ) from exc
    if value <= 0:
        raise LibrarianError(
            _BAD_SETTING,
            detail=f"environment variable {key} must be greater than zero, got {value}",
        )
    return value
