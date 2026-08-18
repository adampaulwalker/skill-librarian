"""Draft changes waiting for a person to approve them.

A proposal is the exact set of file contents that somebody was shown, together
with a fingerprint of that content called ``diff_hash``. When the person says
"yes, publish it", the fingerprint is checked again. If a single byte moved in
between, the fingerprint no longer matches and the publish is refused. That is
what stops somebody approving one thing and a different thing being shipped.

Proposals live in memory only and are deliberately short lived. An approval that
arrives long after the draft was shown is refused rather than guessed at.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

from librarian.errors import ProposalExpired, ProposalNotFound

if TYPE_CHECKING:  # pragma: no cover - import used for type checking only
    from librarian.config import Config

__all__ = [
    "Proposal",
    "ProposalStore",
    "canonical_proposal_bytes",
    "compute_diff_hash",
    "recompute_diff_hash",
    "diff_hash_matches",
    "make_proposal",
    "new_proposal_id",
    "DEFAULT_TTL_SECONDS",
]

DEFAULT_TTL_SECONDS = 900

# Bump this label if the canonical byte layout below ever changes, so that a
# fingerprint made by an older version can never be mistaken for a current one.
_HASH_FORMAT_LABEL = b"skill-librarian.proposal.hash.v1"

# Every field is written as tag, then an 8 byte big endian length, then the raw
# bytes. Because the length always comes first, the reader can tell exactly
# where each field stops. That is what makes the layout unambiguous: a file
# named "a" holding "bc" cannot produce the same bytes as a file named "ab"
# holding "c".
_LENGTH_BYTES = 8


def _framed(tag: bytes, value: bytes) -> bytes:
    """Write one tagged, length prefixed field."""
    return tag + b":" + len(value).to_bytes(_LENGTH_BYTES, "big") + value


def canonical_proposal_bytes(base_sha: str, files: Mapping[str, str]) -> bytes:
    """Lay out the commit point and the files as one exact, repeatable string of bytes.

    The layout does not depend on the order the files were added to the mapping,
    only on their sorted paths and their exact contents.
    """
    parts: list[bytes] = [
        _framed(b"format", _HASH_FORMAT_LABEL),
        _framed(b"base", base_sha.encode("utf-8")),
        _framed(b"filecount", str(len(files)).encode("ascii")),
    ]
    for path in sorted(files):
        parts.append(_framed(b"path", path.encode("utf-8")))
        parts.append(_framed(b"content", files[path].encode("utf-8")))
    return b"".join(parts)


def compute_diff_hash(base_sha: str, files: Mapping[str, str]) -> str:
    """Fingerprint the commit point plus every file path and its exact contents.

    The fingerprint changes if any byte of any file changes, if a file is added,
    if a file is removed, if a file is renamed, or if the commit point changes.
    """
    return hashlib.sha256(canonical_proposal_bytes(base_sha, files)).hexdigest()


def new_proposal_id() -> str:
    """A short, unguessable identifier for one draft change."""
    return secrets.token_hex(8)


@dataclass(frozen=True)
class Proposal:
    """One draft change, exactly as it was shown to the person approving it."""

    id: str
    skill_name: str
    requested_by: str
    base_sha: str
    files: dict[str, str] = field(default_factory=dict)
    diff_text: str = ""
    plain_summary: str = ""
    diff_hash: str = ""
    created_at: float = 0.0


def recompute_diff_hash(proposal: Proposal) -> str:
    """Work the fingerprint out again from the proposal's own contents."""
    return compute_diff_hash(proposal.base_sha, proposal.files)


def diff_hash_matches(proposal: Proposal, expected_hash: str | None = None) -> bool:
    """Check a proposal still fingerprints to what it claims.

    Pass ``expected_hash`` to also check it against the fingerprint the person
    was shown when they approved. Any mismatch means the content moved and the
    publish must not go ahead.
    """
    actual = recompute_diff_hash(proposal)
    if not hmac.compare_digest(actual, proposal.diff_hash):
        return False
    if expected_hash is None:
        return True
    return hmac.compare_digest(actual, expected_hash)


def make_proposal(
    skill_name: str,
    requested_by: str,
    base_sha: str,
    files: Mapping[str, str],
    diff_text: str,
    plain_summary: str,
    proposal_id: str | None = None,
    clock: Callable[[], float] = time.time,
) -> Proposal:
    """Build a proposal, taking its own copy of the files and fingerprinting them."""
    copied = dict(files)
    return Proposal(
        id=proposal_id or new_proposal_id(),
        skill_name=skill_name,
        requested_by=requested_by,
        base_sha=base_sha,
        files=copied,
        diff_text=diff_text,
        plain_summary=plain_summary,
        diff_hash=compute_diff_hash(base_sha, copied),
        created_at=clock(),
    )


class ProposalStore:
    """Holds draft changes in memory until they are approved, dropped, or age out.

    The clock is handed in so that the ageing out can be tested without waiting.
    """

    def __init__(
        self,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("The time to live for a draft change must be more than zero seconds.")
        self._ttl_seconds = int(ttl_seconds)
        self._clock = clock
        self._live: dict[str, Proposal] = {}
        # Identifiers of drafts that were held here and then aged out, with the
        # time they were created. Keeping them lets the store tell the person
        # "that draft got too old" instead of the less helpful "never heard of it".
        self._aged_out: dict[str, float] = {}
        self._lock = threading.RLock()

    @classmethod
    def from_config(
        cls,
        cfg: "Config",
        clock: Callable[[], float] = time.time,
    ) -> "ProposalStore":
        """Build a store using the time to live from the loaded configuration."""
        return cls(ttl_seconds=cfg.proposal_ttl_seconds, clock=clock)

    @property
    def ttl_seconds(self) -> int:
        """How many seconds a draft change stays usable."""
        return self._ttl_seconds

    def _has_aged_out(self, created_at: float) -> bool:
        return (self._clock() - created_at) >= self._ttl_seconds

    def _forget_old_records(self) -> None:
        """Drop the notes about drafts that aged out a long time ago."""
        cutoff = self._clock() - (self._ttl_seconds * 2)
        for pid in [pid for pid, created in self._aged_out.items() if created <= cutoff]:
            del self._aged_out[pid]

    def put(self, p: Proposal) -> None:
        """Hold a draft change so it can be looked up when the person approves."""
        with self._lock:
            self._forget_old_records()
            self._aged_out.pop(p.id, None)
            self._live[p.id] = p

    def get(self, pid: str) -> Proposal:
        """Fetch a draft change.

        Raises ``ProposalExpired`` if it was here but has grown too old, and
        ``ProposalNotFound`` if it was never here or has already been used.
        An out of date draft is never handed back.
        """
        with self._lock:
            proposal = self._live.get(pid)
            if proposal is not None:
                if self._has_aged_out(proposal.created_at):
                    del self._live[pid]
                    self._aged_out[pid] = proposal.created_at
                    raise ProposalExpired(_expired_message())
                return proposal
            if pid in self._aged_out:
                raise ProposalExpired(_expired_message())
            raise ProposalNotFound(_not_found_message())

    def delete(self, pid: str) -> None:
        """Forget a draft change completely. Doing this twice is not an error."""
        with self._lock:
            self._live.pop(pid, None)
            self._aged_out.pop(pid, None)

    def purge_expired(self) -> int:
        """Clear out drafts that have grown too old. Returns how many were cleared."""
        with self._lock:
            stale = [pid for pid, p in self._live.items() if self._has_aged_out(p.created_at)]
            for pid in stale:
                self._aged_out[pid] = self._live[pid].created_at
                del self._live[pid]
            self._forget_old_records()
            return len(stale)

    def active_ids(self) -> list[str]:
        """The identifiers of every draft change that is still usable right now."""
        with self._lock:
            return sorted(
                pid for pid, p in self._live.items() if not self._has_aged_out(p.created_at)
            )

    def __len__(self) -> int:
        """How many draft changes are still usable right now."""
        return len(self.active_ids())


def _expired_message() -> str:
    return (
        "That draft change has been waiting too long, so it was set aside for safety. "
        "Nothing was published. Please ask for the change again and approve the fresh version."
    )


def _not_found_message() -> str:
    return (
        "I could not find that draft change. It may have already been published, or cancelled. "
        "Please ask for the change again."
    )
