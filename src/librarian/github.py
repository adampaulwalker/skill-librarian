"""Talking to GitHub on behalf of a person who does not have a GitHub account.

Two things in here matter more than the plumbing:

1. Attribution. Every commit records the human who asked for the change as the git
   AUTHOR, while the librarian app is the COMMITTER. That is why the repository
   history can name the person who asked for a change even though they never open
   GitHub. It is done with the git data API (blobs, tree, commit, ref) because the simpler
   contents API does not let the author and the committer be set independently.
2. Secrecy. The GitHub App private key and the installation access token are never
   written to a log, never put into an error message, and never shown in a repr.
3. Only what was approved gets published. A merge names the exact commit that was
   approved, so GitHub refuses the merge if anyone moved the branch in the meantime,
   and a commit is never written straight onto the branch everyone reads from,
   because a change written there reaches nobody.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
import threading
import time
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

import httpx
import jwt

from .errors import LibrarianError, NotAuthorized, PublishFailed, SkillNotFound

__all__ = [
    "GITHUB_API_URL",
    "GitHubClient",
    "GitHubAppClient",
    "TokenClient",
]

_LOG = logging.getLogger(__name__)

GITHUB_API_URL = "https://api.github.com"

# A GitHub App JWT may live at most ten minutes. Nine is used so that a slow
# request still arrives with time to spare.
_JWT_LIFETIME_SECONDS = 9 * 60
# GitHub tells apps to backdate the issued-at claim to survive clock skew.
_JWT_BACKDATE_SECONDS = 60
# Refresh an installation token this long before it actually expires.
_TOKEN_REFRESH_MARGIN_SECONDS = 5 * 60

_FILE_MODE = "100644"
_TRAILER_LINE = re.compile(r"^[A-Za-z][A-Za-z0-9-]*: .+$")

_MSG_UNREACHABLE = (
    "I could not reach GitHub just now. Nothing was changed. "
    "Please try again in a few minutes."
)
_MSG_RATE_LIMITED = (
    "GitHub has asked me to slow down for a short while. Nothing was changed. "
    "Please try again in a few minutes."
)
_MSG_SIGN_IN = (
    "I could not sign in to the skills library. Whoever set this up will need to "
    "check the connection details. Nothing was changed."
)
_MSG_NOT_ALLOWED = (
    "I am connected to the skills library but I am not allowed to do that. "
    "Someone who manages the library will need to give me permission. "
    "Nothing was changed."
)
_MSG_GITHUB_TROUBLE = (
    "GitHub is having trouble at the moment. Nothing was changed. "
    "Please try again in a few minutes."
)
_MSG_READ_MISSING = "I could not find that in the skills library."
_MSG_WRITE_MISSING = (
    "I could not find the part of the skills library I needed to change. "
    "Nothing was published."
)
_MSG_WRITE_REJECTED = (
    "GitHub would not accept the change, most likely because something else was "
    "changed at the same time. Nothing was published. Please try again."
)
_MSG_MERGE_REFUSED = (
    "The change could not be merged into the shared library. Nothing has been "
    "published, and someone who manages the library will need to take a look."
)
_MSG_MERGE_HEAD_MOVED = (
    "The change was edited after it was approved, so nothing was published. "
    "Please ask for the change again, read it through, and approve the new one."
)
_MSG_MERGE_NO_PIN = (
    "I was not told which approved version of the change to publish, so nothing "
    "was published. Please ask for the change again."
)
_MSG_NO_DIRECT_TO_SHARED = (
    "A change has to be reviewed before it becomes part of the shared library, so "
    "I will not write it in directly. Nothing was published."
)


def _error(
    kind: type[LibrarianError], user_message: str, detail: str | None = None
) -> LibrarianError:
    """Build one of the shared error types.

    The plain English part is what a person reads. Anything technical goes in the
    detail, which is for the server log only, and never carries a token or a key.
    """
    return kind(user_message, detail=detail)


@runtime_checkable
class GitHubClient(Protocol):
    """Everything the librarian needs from the repository that holds the skills."""

    def get_ref_sha(self, branch: str) -> str: ...

    def get_file(self, path: str, ref: str) -> tuple[str, str]: ...

    def list_dir(self, path: str, ref: str) -> list[dict]: ...

    def create_branch(self, name: str, from_sha: str) -> None: ...

    def commit_files(
        self,
        branch: str,
        files: dict[str, str],
        message: str,
        author_name: str,
        author_email: str,
    ) -> str: ...

    def open_pr(self, head: str, base: str, title: str, body: str) -> int: ...

    # expected_head_sha is the commit that was approved. The merge must be refused
    # if the branch no longer points at it, so that only approved content is published.
    def merge_pr(self, number: int, commit_title: str, expected_head_sha: str) -> str: ...

    def list_commits(self, path: str, limit: int) -> list[dict]: ...


def _now() -> float:
    return time.time()


def _iso_now() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean_identity_part(value: str, field: str) -> str:
    text = (value or "").strip()
    if not text or any(ch in text for ch in "\r\n<>"):
        raise _error(
            NotAuthorized,
            "I do not have a usable "
            + field
            + " for the person asking for this change, so I cannot record who "
            "made it. Nothing was changed.",
        )
    return text


def _message_with_trailer(message: str, author_name: str, author_email: str) -> str:
    """Append the Requested-By trailer, so the message names the human too."""
    trailer = f"Requested-By: {author_name} <{author_email}>"
    text = (message or "").strip("\n")
    if not text:
        text = "Update skill"
    lines = text.splitlines()
    if trailer in lines:
        return text + "\n"
    if len(lines) > 1 and _TRAILER_LINE.match(lines[-1]):
        return text + "\n" + trailer + "\n"
    return text + "\n\n" + trailer + "\n"


def _is_rate_limited(response: httpx.Response) -> bool:
    if response.status_code == 429:
        return True
    if response.status_code != 403:
        return False
    if response.headers.get("x-ratelimit-remaining") == "0":
        return True
    if response.headers.get("retry-after"):
        return True
    body = ""
    try:
        body = response.text.lower()
    except Exception:  # pragma: no cover - a body that cannot be decoded
        body = ""
    return "rate limit" in body or "secondary rate" in body or "abuse detection" in body


def _head_moved(response: httpx.Response) -> bool:
    """True when GitHub refused the merge because the branch is no longer where it was.

    The merge names the approved commit, so GitHub answers with a conflict and says the
    head branch was modified when someone has moved the branch since the approval. A
    plain merge conflict comes back with the same conflict answer, which is why the
    wording is read rather than the code alone.
    """
    try:
        body = response.text.lower()
    except Exception:  # pragma: no cover - a body that cannot be decoded
        return False
    return "head branch was modified" in body or "did not match" in body


class _RestClient:
    """Shared REST plumbing. The two public clients differ only in how they sign in."""

    def __init__(
        self,
        owner: str,
        repo: str,
        *,
        default_branch: str = "main",
        committer_name: str = "Skill Librarian",
        committer_email: str = "skill-librarian@users.noreply.github.com",
        merge_method: str = "merge",
        api_url: str = GITHUB_API_URL,
        http_client: httpx.Client | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.owner = owner
        self.repo = repo
        self.default_branch = default_branch
        self.committer_name = committer_name
        self.committer_email = committer_email
        # A real merge commit keeps the human's authored commit intact in history,
        # which is the whole point of this module.
        self.merge_method = merge_method
        self.api_url = api_url.rstrip("/")
        self._owns_client = http_client is None
        self._http = http_client or httpx.Client(timeout=timeout)

    # -- signing in -----------------------------------------------------------

    def _auth_header(self) -> str:
        raise NotImplementedError

    def _base_headers(self) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "skill-librarian",
        }

    # -- request helpers ------------------------------------------------------

    def _url(self, suffix: str) -> str:
        return f"{self.api_url}{suffix}"

    def _repo_url(self, suffix: str) -> str:
        return self._url(f"/repos/{self.owner}/{self.repo}{suffix}")

    def _send(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json_body: Any | None = None,
        params: dict[str, Any] | None = None,
        writing: bool,
    ) -> httpx.Response:
        try:
            response = self._http.request(
                method, url, headers=headers, json=json_body, params=params
            )
        except httpx.RequestError:
            # The exception text can carry request detail, so it is not reused.
            raise _error(PublishFailed if writing else LibrarianError, _MSG_UNREACHABLE) from None
        return response

    def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: Any | None = None,
        params: dict[str, Any] | None = None,
        writing: bool = False,
        missing_message: str | None = None,
    ) -> httpx.Response:
        headers = self._base_headers()
        headers["Authorization"] = self._auth_header()
        response = self._send(
            method, url, headers=headers, json_body=json_body, params=params, writing=writing
        )
        self._raise_for_status(response, writing=writing, missing_message=missing_message)
        return response

    def _raise_for_status(
        self,
        response: httpx.Response,
        *,
        writing: bool,
        missing_message: str | None = None,
    ) -> None:
        status = response.status_code
        if 200 <= status < 300:
            return

        self._log_failure(response)
        detail = self._detail(response)

        if _is_rate_limited(response):
            raise _error(PublishFailed if writing else LibrarianError, _MSG_RATE_LIMITED, detail)
        if status == 401:
            raise _error(NotAuthorized, _MSG_SIGN_IN, detail)
        if status == 403:
            raise _error(NotAuthorized, _MSG_NOT_ALLOWED, detail)
        if status == 404:
            if missing_message is not None:
                raise _error(
                    SkillNotFound if not writing else PublishFailed, missing_message, detail
                )
            raise _error(
                PublishFailed if writing else SkillNotFound,
                _MSG_WRITE_MISSING if writing else _MSG_READ_MISSING,
                detail,
            )
        if status >= 500:
            raise _error(PublishFailed if writing else LibrarianError, _MSG_GITHUB_TROUBLE, detail)
        if writing:
            raise _error(PublishFailed, _MSG_WRITE_REJECTED, detail)
        raise _error(
            LibrarianError,
            "GitHub could not give me that part of the skills library. Nothing was changed.",
            detail,
        )

    @staticmethod
    def _detail(response: httpx.Response) -> str:
        """A short technical note for the server log. Never carries a token or a key."""
        method = response.request.method if response.request else "?"
        path = response.request.url.path if response.request else "?"
        return f"github {method} {path} returned {response.status_code}"

    def _log_failure(self, response: httpx.Response) -> None:
        """Log enough to debug, and nothing that could contain a secret.

        A 404 is a normal answer here, because callers ask for optional files and folders
        that may simply not exist. Logging that as a failure trains people to ignore the
        log, so it goes to debug and only real problems go to warning.
        """
        level = _LOG.debug if response.status_code == 404 else _LOG.warning
        level(
            "GitHub request failed: %s %s -> %s",
            response.request.method if response.request else "?",
            response.request.url.path if response.request else "?",
            response.status_code,
        )

    @staticmethod
    def _json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except (json.JSONDecodeError, ValueError):
            raise _error(
                LibrarianError,
                "GitHub sent back something I could not read. Nothing was changed.",
            ) from None

    # -- reading --------------------------------------------------------------

    def get_ref_sha(self, branch: str) -> str:
        response = self._request(
            "GET",
            self._repo_url(f"/git/ref/heads/{branch}"),
            missing_message=(
                "I could not find the "
                + branch
                + " branch in the skills library. Nothing was changed."
            ),
        )
        payload = self._json(response)
        sha = (payload.get("object") or {}).get("sha")
        if not isinstance(sha, str) or not sha:
            raise _error(
                LibrarianError,
                "GitHub did not tell me where the "
                + branch
                + " branch is pointing. Nothing was changed.",
            )
        return sha

    def get_file(self, path: str, ref: str) -> tuple[str, str]:
        response = self._request(
            "GET",
            self._repo_url(f"/contents/{path}"),
            params={"ref": ref},
            missing_message="I could not find " + path + " in the skills library.",
        )
        payload = self._json(response)
        if isinstance(payload, list):
            raise _error(
                SkillNotFound,
                "I looked for the file " + path + " and found a folder with that name instead.",
            )
        blob_sha = payload.get("sha") or ""
        encoded = payload.get("content")
        if payload.get("encoding") == "base64" and encoded:
            return self._decode_base64(encoded, path), blob_sha
        # GitHub leaves the content out for large files and points at the blob.
        if blob_sha:
            blob = self._json(
                self._request(
                    "GET",
                    self._repo_url(f"/git/blobs/{blob_sha}"),
                    missing_message="I could not find " + path + " in the skills library.",
                )
            )
            return self._decode_base64(blob.get("content") or "", path), blob_sha
        raise _error(SkillNotFound, "I could not read " + path + " from the skills library.")

    @staticmethod
    def _decode_base64(encoded: str, path: str) -> str:
        try:
            raw = base64.b64decode(encoded)
            return raw.decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            raise _error(
                LibrarianError,
                "I could not read " + path + " as text. It may not be a text file.",
            ) from None

    def list_dir(self, path: str, ref: str) -> list[dict]:
        cleaned = path.strip("/")
        response = self._request(
            "GET",
            self._repo_url(f"/contents/{cleaned}"),
            params={"ref": ref},
            missing_message="I could not find " + (cleaned or "that folder") + " in the skills library.",
        )
        payload = self._json(response)
        if isinstance(payload, dict):
            payload = [payload]
        entries: list[dict] = []
        for item in payload:
            entries.append(
                {
                    "name": item.get("name", ""),
                    "path": item.get("path", ""),
                    "type": item.get("type", ""),
                    "sha": item.get("sha", ""),
                    "size": item.get("size", 0),
                }
            )
        return entries

    def list_commits(self, path: str, limit: int) -> list[dict]:
        capped = max(1, min(int(limit), 100))
        response = self._request(
            "GET",
            self._repo_url("/commits"),
            params={"path": path, "per_page": capped, "sha": self.default_branch},
            missing_message="I could not find any history for " + path + ".",
        )
        payload = self._json(response)
        if not isinstance(payload, list):
            return []
        history: list[dict] = []
        for item in payload[:capped]:
            commit = item.get("commit") or {}
            author = commit.get("author") or {}
            committer = commit.get("committer") or {}
            history.append(
                {
                    "sha": item.get("sha", ""),
                    "message": commit.get("message", ""),
                    "author_name": author.get("name", ""),
                    "author_email": author.get("email", ""),
                    "committer_name": committer.get("name", ""),
                    "date": author.get("date", ""),
                }
            )
        return history

    # -- writing --------------------------------------------------------------

    def _is_shared_branch(self, branch: str) -> bool:
        """True when the name points at the branch the whole organization reads from.

        The comparison ignores surrounding spaces, an optional full reference prefix,
        and letter case, so that none of those spellings can slip a direct write past
        the check.
        """
        cleaned = (branch or "").strip()
        if cleaned.startswith("refs/heads/"):
            cleaned = cleaned[len("refs/heads/") :]
        return cleaned.casefold() == (self.default_branch or "").strip().casefold()

    def create_branch(self, name: str, from_sha: str) -> None:
        self._request(
            "POST",
            self._repo_url("/git/refs"),
            json_body={"ref": f"refs/heads/{name}", "sha": from_sha},
            writing=True,
        )

    def commit_files(
        self,
        branch: str,
        files: dict[str, str],
        message: str,
        author_name: str,
        author_email: str,
    ) -> str:
        """Commit whole files, recording the human as the author of the change."""
        # A commit written straight onto the branch everyone reads from reaches nobody,
        # because people only pick up a change when a reviewed change is merged into it.
        # There is no reason for this service to ever do that, so the door is nailed shut
        # here rather than only in the code that calls it.
        if self._is_shared_branch(branch):
            raise _error(PublishFailed, _MSG_NO_DIRECT_TO_SHARED)
        if not files:
            raise _error(
                PublishFailed,
                "There was nothing to save, so no change was made to the skills library.",
            )
        name = _clean_identity_part(author_name, "name")
        email = _clean_identity_part(author_email, "email address")
        if "@" not in email:
            raise _error(
                NotAuthorized,
                "The email address for the person asking for this change does not look "
                "like an email address, so I cannot record who made it. Nothing was changed.",
            )

        head_sha = self.get_ref_sha(branch)
        head_commit = self._json(
            self._request("GET", self._repo_url(f"/git/commits/{head_sha}"), writing=True)
        )
        base_tree = (head_commit.get("tree") or {}).get("sha")
        if not base_tree:
            raise _error(PublishFailed, _MSG_WRITE_REJECTED)

        tree_entries: list[dict[str, str]] = []
        for path, content in files.items():
            blob = self._json(
                self._request(
                    "POST",
                    self._repo_url("/git/blobs"),
                    json_body={"content": content, "encoding": "utf-8"},
                    writing=True,
                )
            )
            blob_sha = blob.get("sha")
            if not blob_sha:
                raise _error(PublishFailed, _MSG_WRITE_REJECTED)
            tree_entries.append(
                {"path": path, "mode": _FILE_MODE, "type": "blob", "sha": blob_sha}
            )

        tree = self._json(
            self._request(
                "POST",
                self._repo_url("/git/trees"),
                json_body={"base_tree": base_tree, "tree": tree_entries},
                writing=True,
            )
        )
        tree_sha = tree.get("sha")
        if not tree_sha:
            raise _error(PublishFailed, _MSG_WRITE_REJECTED)

        stamp = _iso_now()
        commit = self._json(
            self._request(
                "POST",
                self._repo_url("/git/commits"),
                json_body={
                    "message": _message_with_trailer(message, name, email),
                    "tree": tree_sha,
                    "parents": [head_sha],
                    # The person who asked is the author. The librarian is the committer.
                    "author": {"name": name, "email": email, "date": stamp},
                    "committer": {
                        "name": self.committer_name,
                        "email": self.committer_email,
                        "date": stamp,
                    },
                },
                writing=True,
            )
        )
        commit_sha = commit.get("sha")
        if not commit_sha:
            raise _error(PublishFailed, _MSG_WRITE_REJECTED)

        self._request(
            "PATCH",
            self._repo_url(f"/git/refs/heads/{branch}"),
            json_body={"sha": commit_sha, "force": False},
            writing=True,
        )
        return commit_sha

    def open_pr(self, head: str, base: str, title: str, body: str) -> int:
        payload = self._json(
            self._request(
                "POST",
                self._repo_url("/pulls"),
                json_body={"head": head, "base": base, "title": title, "body": body},
                writing=True,
            )
        )
        number = payload.get("number")
        if not isinstance(number, int):
            raise _error(
                PublishFailed,
                "GitHub did not confirm that the change was opened for review. "
                "Nothing was published.",
            )
        return number

    def merge_pr(self, number: int, commit_title: str, expected_head_sha: str) -> str:
        """Merge once, and only the commit that was approved.

        `expected_head_sha` is the commit the person was shown and agreed to. It is
        sent to GitHub, which then refuses the merge if the branch has been moved to
        anything else since. Without it, anyone able to write to the repository could
        swap the content between approval and publication.

        A merge is never retried, because a blind retry can double up.
        """
        pinned = (expected_head_sha or "").strip()
        if not pinned:
            raise _error(PublishFailed, _MSG_MERGE_NO_PIN)
        headers = self._base_headers()
        headers["Authorization"] = self._auth_header()
        response = self._send(
            "PUT",
            self._repo_url(f"/pulls/{number}/merge"),
            headers=headers,
            json_body={
                "commit_title": commit_title,
                "merge_method": self.merge_method,
                # GitHub merges only if the branch still points here.
                "sha": pinned,
            },
            writing=True,
        )
        if response.status_code == 409:
            self._log_failure(response)
            raise _error(
                PublishFailed,
                _MSG_MERGE_HEAD_MOVED if _head_moved(response) else _MSG_MERGE_REFUSED,
                self._detail(response),
            )
        if response.status_code in (405, 422):
            self._log_failure(response)
            raise _error(PublishFailed, _MSG_MERGE_REFUSED, self._detail(response))
        self._raise_for_status(response, writing=True)
        payload = self._json(response)
        if not payload.get("merged"):
            raise _error(PublishFailed, _MSG_MERGE_REFUSED)
        sha = payload.get("sha")
        if not isinstance(sha, str) or not sha:
            raise _error(
                PublishFailed,
                "The change was merged but GitHub did not tell me where it landed. "
                "Someone who manages the library should check it.",
            )
        return sha

    # -- housekeeping ---------------------------------------------------------

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def __repr__(self) -> str:
        return f"{type(self).__name__}(owner={self.owner!r}, repo={self.repo!r})"


@dataclass(frozen=True)
class _InstallationToken:
    value: str
    expires_at: float

    def usable_at(self, when: float) -> bool:
        return when < self.expires_at - _TOKEN_REFRESH_MARGIN_SECONDS


class GitHubAppClient(_RestClient):
    """Production client. Signs in as a GitHub App installed on one repository.

    A short lived JWT proves the app's identity, and is exchanged for an
    installation access token that is limited to this one repository. The token is
    cached in memory and replaced before it expires. Neither the private key nor
    the token is ever logged or shown.
    """

    def __init__(
        self,
        app_id: str,
        private_key: str,
        owner: str,
        repo: str,
        *,
        installation_id: int | None = None,
        default_branch: str = "main",
        committer_name: str = "Skill Librarian",
        committer_email: str = "skill-librarian@users.noreply.github.com",
        merge_method: str = "merge",
        api_url: str = GITHUB_API_URL,
        http_client: httpx.Client | None = None,
        timeout: float = 30.0,
    ) -> None:
        super().__init__(
            owner,
            repo,
            default_branch=default_branch,
            committer_name=committer_name,
            committer_email=committer_email,
            merge_method=merge_method,
            api_url=api_url,
            http_client=http_client,
            timeout=timeout,
        )
        if not str(app_id).strip():
            raise _error(
                LibrarianError,
                "The skills librarian has not been told which GitHub app to use, so it "
                "cannot reach the library. Whoever set this up will need to finish the setup.",
            )
        if not (private_key or "").strip():
            raise _error(
                LibrarianError,
                "The skills librarian is missing its private sign-in key, so it cannot "
                "reach the library. Whoever set this up will need to finish the setup.",
            )
        self.app_id = str(app_id).strip()
        self._private_key = private_key
        self._installation_id = installation_id
        self._token: _InstallationToken | None = None
        self._lock = threading.Lock()

    # -- app level sign in ----------------------------------------------------

    def _build_jwt(self) -> str:
        issued_at = int(_now()) - _JWT_BACKDATE_SECONDS
        claims = {
            "iat": issued_at,
            "exp": issued_at + _JWT_BACKDATE_SECONDS + _JWT_LIFETIME_SECONDS,
            "iss": self.app_id,
        }
        try:
            return jwt.encode(claims, self._private_key, algorithm="RS256")
        except Exception:
            # The exception text can quote the key material, so it is not reused.
            raise _error(
                NotAuthorized,
                "The private sign-in key for the skills librarian could not be used. "
                "Whoever set this up will need to check it. Nothing was changed.",
            ) from None

    def _app_request(
        self, method: str, url: str, *, json_body: Any | None = None
    ) -> httpx.Response:
        headers = self._base_headers()
        headers["Authorization"] = f"Bearer {self._build_jwt()}"
        response = self._send(method, url, headers=headers, json_body=json_body, writing=False)
        if response.status_code == 404:
            raise _error(
                NotAuthorized,
                "The skills librarian does not appear to be installed on that repository. "
                "Someone who manages the library will need to install it. Nothing was changed.",
            )
        self._raise_for_status(response, writing=False)
        return response

    def _resolve_installation_id(self) -> int:
        if self._installation_id is not None:
            return self._installation_id
        payload = self._json(
            self._app_request("GET", self._repo_url("/installation"))
        )
        installation_id = payload.get("id")
        if not isinstance(installation_id, int):
            raise _error(
                NotAuthorized,
                "GitHub did not confirm that the skills librarian is installed on the "
                "library. Someone who manages the library will need to check it.",
            )
        self._installation_id = installation_id
        return installation_id

    def _mint_token(self) -> _InstallationToken:
        installation_id = self._resolve_installation_id()
        payload = self._json(
            self._app_request(
                "POST",
                self._url(f"/app/installations/{installation_id}/access_tokens"),
                json_body={
                    # Scoped to the single repository this librarian looks after.
                    "repositories": [self.repo],
                    "permissions": {
                        "contents": "write",
                        "pull_requests": "write",
                        "metadata": "read",
                    },
                },
            )
        )
        token = payload.get("token")
        if not isinstance(token, str) or not token:
            raise _error(
                NotAuthorized,
                "GitHub did not give the skills librarian permission to work on the "
                "library. Nothing was changed.",
            )
        return _InstallationToken(value=token, expires_at=_parse_expiry(payload.get("expires_at")))

    def _current_token(self) -> str:
        with self._lock:
            now = _now()
            if self._token is not None and self._token.usable_at(now):
                return self._token.value
            self._token = self._mint_token()
            _LOG.info("Renewed the GitHub installation token for %s/%s", self.owner, self.repo)
            return self._token.value

    def _auth_header(self) -> str:
        return f"token {self._current_token()}"

    def __repr__(self) -> str:
        return (
            f"GitHubAppClient(app_id={self.app_id!r}, owner={self.owner!r}, "
            f"repo={self.repo!r}, key=hidden, token=hidden)"
        )


def _parse_expiry(raw: Any) -> float:
    if isinstance(raw, str) and raw:
        text = raw.replace("Z", "+00:00")
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            moment = None
        if moment is not None:
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            return moment.timestamp()
    # GitHub installation tokens last an hour. Fall back to that.
    return _now() + 3600.0


class TokenClient(_RestClient):
    """Development only client. Signs in with a personal access token from the environment."""

    ENV_VAR = "LIBRARIAN_DEV_TOKEN"

    WARNING = (
        "The skills librarian is running with a personal developer token from "
        f"{ENV_VAR}. This is for local development only and must not be used in "
        "production, where the GitHub app sign-in is required."
    )

    def __init__(
        self,
        owner: str,
        repo: str,
        *,
        token: str | None = None,
        default_branch: str = "main",
        committer_name: str = "Skill Librarian (development)",
        committer_email: str = "skill-librarian@users.noreply.github.com",
        merge_method: str = "merge",
        api_url: str = GITHUB_API_URL,
        http_client: httpx.Client | None = None,
        timeout: float = 30.0,
    ) -> None:
        super().__init__(
            owner,
            repo,
            default_branch=default_branch,
            committer_name=committer_name,
            committer_email=committer_email,
            merge_method=merge_method,
            api_url=api_url,
            http_client=http_client,
            timeout=timeout,
        )
        resolved = token if token is not None else os.environ.get(self.ENV_VAR, "")
        if not resolved.strip():
            raise _error(
                NotAuthorized,
                "The skills librarian has no way to sign in to GitHub. Whoever set this "
                "up will need to finish the setup before anything can be changed.",
            )
        self._token = resolved.strip()
        _LOG.warning(self.WARNING)
        warnings.warn(self.WARNING, RuntimeWarning, stacklevel=2)

    def _auth_header(self) -> str:
        return f"token {self._token}"

    def __repr__(self) -> str:
        return f"TokenClient(owner={self.owner!r}, repo={self.repo!r}, token=hidden)"
