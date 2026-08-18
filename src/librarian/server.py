"""The remote Model Context Protocol server, spoken over plain HTTP.

This is what gets added to a Claude organization as a custom connector. It exposes the
six librarian operations as tools.

Identity is taken from the authenticated session and from nowhere else. A tool argument
that claims to be a person is refused outright, because accepting one would let anybody
publish a change under somebody else's name.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from . import service
from .config import Config, load_config
from .errors import LibrarianError
from .github import GitHubClient
from .proposals import ProposalStore
from .service import ANONYMOUS, Actor, ProposalPreview

SERVER_NAME = "skill-librarian"
SERVER_VERSION = "0.1.0"
DEFAULT_PROTOCOL_VERSION = "2025-06-18"

# Argument names that would let a caller claim to be somebody. None of these are ever
# read; a request carrying one is refused so the attempt is visible rather than silent.
IDENTITY_ARGUMENT_NAMES = frozenset(
    {
        "actor",
        "actor_name",
        "actor_email",
        "author",
        "author_name",
        "author_email",
        "requested_by",
        "requester",
        "on_behalf_of",
        "user",
        "user_name",
        "user_email",
        "username",
        "email",
        "identity",
        "committer",
        "committer_name",
        "committer_email",
    }
)

SPOOFING_REFUSAL = (
    "I cannot take a person's name or email address as part of a request. I use the "
    "account you are signed in with, so that every change carries the name of the "
    "person who actually asked for it. Please try again without that detail."
)

GENERIC_FAILURE = (
    "Something went wrong on my side and nothing was changed. Please try again, and if "
    "it keeps happening let whoever looks after this connector know."
)


@dataclass(frozen=True)
class _SessionDirectory:
    """Maps an access token from the connector to the person it belongs to.

    Tokens are held as hashes so the raw values are not kept in memory once loaded.
    """

    by_token_hash: Mapping[str, Actor]

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @classmethod
    def from_mapping(cls, sessions: Mapping[str, Mapping[str, str]] | None) -> "_SessionDirectory":
        table: dict[str, Actor] = {}
        for token, person in (sessions or {}).items():
            name = str(person.get("name", "")).strip()
            email = str(person.get("email", "")).strip()
            table[cls._hash(str(token))] = Actor(name=name, email=email)
        return cls(by_token_hash=table)

    @classmethod
    def from_env(cls) -> "_SessionDirectory":
        raw = os.environ.get("LIBRARIAN_SESSIONS", "").strip()
        if not raw:
            return cls(by_token_hash={})
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError:
            return cls(by_token_hash={})
        if not isinstance(loaded, Mapping):
            return cls(by_token_hash={})
        return cls.from_mapping(loaded)

    def actor_for_token(self, token: str) -> Actor:
        return self.by_token_hash.get(self._hash(token), ANONYMOUS)


def _bearer_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    prefix = "bearer "
    if header[: len(prefix)].lower() == prefix:
        return header[len(prefix) :].strip()
    return ""


class _Runtime:
    """Holds the pieces the tools need, built on first use so importing is cheap."""

    def __init__(
        self,
        gh: GitHubClient | None = None,
        cfg: Config | None = None,
        store: ProposalStore | None = None,
        sessions: _SessionDirectory | None = None,
    ) -> None:
        self._gh = gh
        self._cfg = cfg
        self._store = store
        self._sessions = sessions

    @property
    def cfg(self) -> Config:
        if self._cfg is None:
            self._cfg = load_config()
        return self._cfg

    @property
    def gh(self) -> GitHubClient:
        if self._gh is None:
            self._gh = _build_github_client(self.cfg)
        return self._gh

    @property
    def store(self) -> ProposalStore:
        if self._store is None:
            self._store = _build_proposal_store(self.cfg)
        return self._store

    @property
    def sessions(self) -> _SessionDirectory:
        if self._sessions is None:
            self._sessions = _SessionDirectory.from_env()
        return self._sessions

    def actor_for(self, request: Request) -> Actor:
        token = _bearer_token(request)
        if not token:
            return ANONYMOUS
        return self.sessions.actor_for_token(token)


#: The GitHub App this service signs in as. Everything here is read from the environment,
#: never from a chat message, and never from a default that names anybody's repository.
GITHUB_APP_ID_VAR = "LIBRARIAN_GITHUB_APP_ID"
GITHUB_PRIVATE_KEY_VAR = "LIBRARIAN_GITHUB_PRIVATE_KEY"
GITHUB_PRIVATE_KEY_PATH_VAR = "LIBRARIAN_GITHUB_PRIVATE_KEY_PATH"
GITHUB_INSTALLATION_ID_VAR = "LIBRARIAN_GITHUB_INSTALLATION_ID"

_SETUP_UNFINISHED = (
    "This connector is not finished being set up, so it cannot reach the library of "
    "skills yet. Whoever installed it needs to finish the setup. Nothing was changed."
)


def _github_private_key() -> str:
    """The app's private key, given either directly or as the file that holds it."""
    inline = os.environ.get(GITHUB_PRIVATE_KEY_VAR, "").strip()
    if inline:
        # A key pasted into an environment variable often arrives with the line breaks
        # written out rather than actually broken, so put them back.
        return inline.replace("\\n", "\n")
    path = os.environ.get(GITHUB_PRIVATE_KEY_PATH_VAR, "").strip()
    if not path:
        raise LibrarianError(_SETUP_UNFINISHED, detail=f"neither {GITHUB_PRIVATE_KEY_VAR} nor {GITHUB_PRIVATE_KEY_PATH_VAR} is set")
    try:
        return pathlib.Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise LibrarianError(
            _SETUP_UNFINISHED, detail=f"could not read the private key file: {exc}"
        ) from exc


def _github_installation_id() -> int | None:
    """The installation to mint tokens for, or nothing so the client discovers it."""
    raw = os.environ.get(GITHUB_INSTALLATION_ID_VAR, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise LibrarianError(
            _SETUP_UNFINISHED,
            detail=f"{GITHUB_INSTALLATION_ID_VAR} must be a whole number, got {raw!r}",
        ) from exc


def _build_github_client(cfg: Config) -> GitHubClient:
    """The client that does the writing, built from the environment and nothing else.

    Which repository it works in comes from the settings. Which credentials it signs in
    with come from the environment. Neither is ever taken from a tool argument.
    """
    from .github import GitHubAppClient, TokenClient

    if os.environ.get("LIBRARIAN_DEV_TOKEN", "").strip():
        # Development only. The client itself warns loudly that this is not for production.
        return TokenClient(
            cfg.repo_owner,
            cfg.repo_name,
            default_branch=cfg.default_branch,
        )

    return GitHubAppClient(
        app_id=os.environ.get(GITHUB_APP_ID_VAR, "").strip(),
        private_key=_github_private_key(),
        owner=cfg.repo_owner,
        repo=cfg.repo_name,
        installation_id=_github_installation_id(),
        default_branch=cfg.default_branch,
    )


def _build_proposal_store(cfg: Config) -> ProposalStore:
    """Drafts waiting for approval, held for as long as the settings say."""
    return ProposalStore(ttl_seconds=cfg.proposal_ttl_seconds)


TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_skills",
        "description": "List every shared skill, with the short description of each one.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "read_skill",
        "description": "Show the full current text of one skill.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "skill_name": {"type": "string", "description": "The name of the skill."}
            },
            "required": ["skill_name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "propose_edit",
        "description": (
            "Prepare a change to a skill and show it for checking. Send the complete "
            "new text of each file, not a patch. Nothing is published by this tool."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "skill_name": {"type": "string", "description": "The skill to change."},
                "files": {
                    "type": "object",
                    "description": (
                        "Each file to write, as its path in the repository mapped to "
                        "the full new text of that file."
                    ),
                    "additionalProperties": {"type": "string"},
                },
                "note": {
                    "type": "string",
                    "description": "Why the change is being made, in the person's own words.",
                },
            },
            "required": ["skill_name", "files"],
            "additionalProperties": False,
        },
    },
    {
        "name": "approve",
        "description": (
            "Publish a change that was already prepared. Show the person the full "
            "difference from propose_edit and get their explicit yes before calling "
            "this. Pass back the same proposal_id and diff_hash they were shown."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "proposal_id": {"type": "string"},
                "diff_hash": {
                    "type": "string",
                    "description": "The diff_hash of the exact change the person saw.",
                },
                "bump": {
                    "type": "string",
                    "enum": ["patch", "minor"],
                    "description": "How much of a change this is. Defaults to patch.",
                },
            },
            "required": ["proposal_id", "diff_hash"],
            "additionalProperties": False,
        },
    },
    {
        "name": "history",
        "description": "Show recent changes to a skill: who asked for each one, when, and what changed.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "skill_name": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            "required": ["skill_name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "revert",
        "description": (
            "Prepare a change that puts a skill back to how it read at an earlier "
            "point, using a reference from history. Shows the difference for checking; "
            "nothing is published until it is approved."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "skill_name": {"type": "string"},
                "commit_sha": {
                    "type": "string",
                    "description": "The reference shown next to the entry in history.",
                },
                "note": {"type": "string"},
            },
            "required": ["skill_name", "commit_sha"],
            "additionalProperties": False,
        },
    },
]


def _text_result(text: str, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _string_argument(arguments: Mapping[str, Any], key: str, default: str = "") -> str:
    value = arguments.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise LookupError(key)
    return value


def _run_tool(
    runtime: _Runtime, actor: Actor, name: str, arguments: Mapping[str, Any]
) -> dict[str, Any]:
    offending = sorted(set(arguments) & IDENTITY_ARGUMENT_NAMES)
    if offending:
        return _text_result(SPOOFING_REFUSAL, is_error=True)

    gh = runtime.gh
    cfg = runtime.cfg
    store = runtime.store
    try:
        if name == "list_skills":
            return _text_result(service.list_skills(actor, gh, cfg))
        if name == "read_skill":
            return _text_result(
                service.read_skill(actor, gh, cfg, _string_argument(arguments, "skill_name"))
            )
        if name == "propose_edit":
            files = arguments.get("files")
            if not isinstance(files, Mapping):
                return _text_result(
                    "I need the full new text of each file to save, and none arrived.",
                    is_error=True,
                )
            preview = service.propose_edit(
                actor,
                gh,
                cfg,
                store,
                _string_argument(arguments, "skill_name"),
                {str(path): content for path, content in files.items()},
                _string_argument(arguments, "note"),
            )
            return _text_result(_preview_text(preview))
        if name == "approve":
            return _text_result(
                service.approve(
                    actor,
                    gh,
                    cfg,
                    store,
                    _string_argument(arguments, "proposal_id"),
                    _string_argument(arguments, "diff_hash"),
                    _string_argument(arguments, "bump", "patch") or "patch",
                )
            )
        if name == "history":
            limit = arguments.get("limit", 10)
            if not isinstance(limit, int) or isinstance(limit, bool):
                limit = 10
            return _text_result(
                service.history(
                    actor, gh, cfg, _string_argument(arguments, "skill_name"), limit
                )
            )
        if name == "revert":
            preview = service.revert(
                actor,
                gh,
                cfg,
                store,
                _string_argument(arguments, "skill_name"),
                _string_argument(arguments, "commit_sha"),
                _string_argument(arguments, "note"),
            )
            return _text_result(_preview_text(preview))
    except LookupError as bad_argument:
        return _text_result(
            f"The value given for {bad_argument.args[0]} was not text, so I have not "
            "done anything.",
            is_error=True,
        )
    except LibrarianError as known:
        message = getattr(known, "user_message", "") or GENERIC_FAILURE
        return _text_result(message, is_error=True)
    except Exception:
        return _text_result(GENERIC_FAILURE, is_error=True)

    return _text_result(f"There is no tool called {name}.", is_error=True)


def _preview_text(preview: ProposalPreview) -> str:
    return (
        preview.message
        + "\n\n"
        + "When you are ready, approve it with these two references:\n"
        + f"- proposal_id: {preview.proposal_id}\n"
        + f"- diff_hash: {preview.diff_hash}"
    )


def _jsonrpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _handle_message(
    runtime: _Runtime, actor: Actor, message: Mapping[str, Any], protocol_version: str
) -> dict[str, Any] | None:
    if not isinstance(message, Mapping) or message.get("jsonrpc") != "2.0":
        return _jsonrpc_error(None, -32600, "That request was not in the expected form.")

    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params")
    if not isinstance(params, Mapping):
        params = {}

    if method == "initialize":
        asked_for = params.get("protocolVersion")
        version = asked_for if isinstance(asked_for, str) and asked_for else protocol_version
        result: dict[str, Any] = {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": (
                "Tools for reading and changing the shared Claude skills. Always show "
                "the person the full difference from propose_edit and wait for their "
                "yes before calling approve."
            ),
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments")
        if not isinstance(arguments, Mapping):
            arguments = {}
        if not isinstance(name, str):
            return _jsonrpc_error(request_id, -32602, "No tool name was given.")
        result = _run_tool(runtime, actor, name, arguments)
    elif isinstance(method, str) and method.startswith("notifications/"):
        return None
    else:
        if request_id is None:
            return None
        return _jsonrpc_error(request_id, -32601, f"There is no method called {method}.")

    if request_id is None:
        return None
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def create_app(
    gh: GitHubClient | None = None,
    cfg: Config | None = None,
    store: ProposalStore | None = None,
    sessions: Mapping[str, Mapping[str, str]] | None = None,
) -> FastAPI:
    runtime = _Runtime(
        gh=gh,
        cfg=cfg,
        store=store,
        sessions=_SessionDirectory.from_mapping(sessions) if sessions is not None else None,
    )
    app = FastAPI(title="Skill Librarian", version=SERVER_VERSION)
    app.state.runtime = runtime

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": SERVER_NAME, "version": SERVER_VERSION}

    @app.get("/mcp")
    def mcp_get() -> Response:
        return JSONResponse(
            status_code=405,
            content={
                "error": "This connector answers on POST only.",
            },
            headers={"Allow": "POST"},
        )

    @app.post("/mcp")
    async def mcp_post(request: Request) -> Response:
        try:
            payload = json.loads(await request.body() or b"")
        except json.JSONDecodeError:
            return JSONResponse(
                status_code=400,
                content=_jsonrpc_error(None, -32700, "That request was not readable."),
            )

        actor = runtime.actor_for(request)
        if isinstance(payload, list):
            replies = [
                reply
                for message in payload
                if (reply := _handle_message(runtime, actor, message, DEFAULT_PROTOCOL_VERSION))
                is not None
            ]
            if not replies:
                return Response(status_code=202)
            return JSONResponse(content=replies)

        if not isinstance(payload, Mapping):
            return JSONResponse(
                status_code=400,
                content=_jsonrpc_error(None, -32600, "That request was not in the expected form."),
            )

        reply = _handle_message(runtime, actor, payload, DEFAULT_PROTOCOL_VERSION)
        if reply is None:
            return Response(status_code=202)
        return JSONResponse(content=reply)

    return app


app = create_app()
