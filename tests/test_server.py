"""The connector itself: identity at the door, and the pieces it builds on start-up.

Two things are checked here that nothing else in the suite can check.

The first is identity. It comes from the authenticated session and from nowhere else, so a
tool call that carries a name or an email address is refused outright rather than quietly
ignored. And when nobody is signed in, preparing a change still works while approving one
does not, because an unattributed publish is worse than no publish.

The second is the wiring. The server builds its GitHub client and its store of drafts from
the settings and the environment on first use. If those calls do not match the shapes the
other modules actually offer, every single tool fails at run time while every unit test
still passes.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from librarian import service
from librarian.config import Config
from librarian.errors import LibrarianError
from librarian.proposals import ProposalStore
from librarian.server import (
    GITHUB_APP_ID_VAR,
    GITHUB_INSTALLATION_ID_VAR,
    GITHUB_PRIVATE_KEY_PATH_VAR,
    GITHUB_PRIVATE_KEY_VAR,
    _build_github_client,
    _build_proposal_store,
    create_app,
)
from tests.fakes import FakeGitHubClient, PluginSpec, library, skill_text

ALPHA = PluginSpec(
    name="alpha-pack",
    plugin_dir="plugins/alpha-pack",
    version="1.4.2",
    skills=(("how-we-write-briefs", "How to write a brief."),),
)
BETA = PluginSpec(
    name="beta-pack",
    plugin_dir="library/beta-pack",
    version="2.0.0",
    skills=(("handover-notes", "What to write when handing work over."),),
)

BRIEFS = "plugins/alpha-pack/skills/how-we-write-briefs/SKILL.md"
UPDATED_BRIEFS = skill_text(
    "how-we-write-briefs", "How to write a brief.", "Start every brief with the client name."
)

A_TOKEN = "a-session-token"
SESSIONS = {A_TOKEN: {"name": "A Person", "email": "a.person@example.test"}}


@pytest.fixture
def gh() -> FakeGitHubClient:
    return library(ALPHA, BETA)


@pytest.fixture
def client(gh: FakeGitHubClient) -> TestClient:
    app = create_app(
        gh=gh,
        cfg=Config(repo_owner="an-owner", repo_name="a-repo", default_branch="main"),
        store=ProposalStore(ttl_seconds=900),
        sessions=SESSIONS,
    )
    return TestClient(app)


def call(client: TestClient, name: str, arguments: dict[str, Any], token: str | None) -> dict:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        headers=headers,
    )
    assert response.status_code == 200
    return response.json()["result"]


def text_of(result: dict) -> str:
    return "\n".join(block["text"] for block in result["content"])


# ==============================================================================================
# The connector answers the way a connector has to
# ==============================================================================================


def test_the_health_check_answers(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_the_six_operations_are_offered_as_tools(client: TestClient) -> None:
    response = client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    )

    offered = {tool["name"] for tool in response.json()["result"]["tools"]}
    assert offered == {
        "list_skills",
        "read_skill",
        "propose_edit",
        "approve",
        "history",
        "revert",
    }


def test_a_tool_nobody_has_heard_of_is_answered_in_plain_english(client: TestClient) -> None:
    result = call(client, "make-me-a-sandwich", {}, A_TOKEN)

    assert result["isError"] is True
    assert "no tool called" in text_of(result).lower()


# ==============================================================================================
# Identity comes from the session and from nowhere else
# ==============================================================================================


@pytest.mark.parametrize(
    "argument", ["author_name", "requested_by", "user_email", "actor", "committer"]
)
def test_a_tool_call_that_claims_to_be_somebody_is_refused(
    client: TestClient, argument: str
) -> None:
    """Accepting one of these would let anybody publish under somebody else's name."""
    result = call(client, "list_skills", {argument: "Someone Else"}, A_TOKEN)

    assert result["isError"] is True
    assert "signed in" in text_of(result).lower()


def test_reading_the_library_works_without_anybody_signed_in(client: TestClient) -> None:
    result = call(client, "list_skills", {}, token=None)

    assert result["isError"] is False
    assert "how-we-write-briefs" in text_of(result)


def test_preparing_a_change_works_without_anybody_signed_in(client: TestClient) -> None:
    result = call(
        client,
        "propose_edit",
        {"skill_name": "how-we-write-briefs", "files": {BRIEFS: UPDATED_BRIEFS}},
        token=None,
    )

    assert result["isError"] is False
    assert "proposal_id" in text_of(result)


def test_approving_a_change_is_refused_when_nobody_is_signed_in(
    client: TestClient, gh: FakeGitHubClient
) -> None:
    prepared = text_of(
        call(
            client,
            "propose_edit",
            {"skill_name": "how-we-write-briefs", "files": {BRIEFS: UPDATED_BRIEFS}},
            token=None,
        )
    )
    proposal_id = prepared.split("proposal_id: ")[1].split("\n")[0].strip()
    diff_hash = prepared.split("diff_hash: ")[1].split("\n")[0].strip()
    before = gh.files_on("main")

    result = call(
        client, "approve", {"proposal_id": proposal_id, "diff_hash": diff_hash}, token=None
    )

    assert result["isError"] is True
    assert "who you are" in text_of(result).lower()
    assert gh.files_on("main") == before


def test_an_unrecognised_token_is_treated_as_nobody(
    client: TestClient, gh: FakeGitHubClient
) -> None:
    prepared = text_of(
        call(
            client,
            "propose_edit",
            {"skill_name": "how-we-write-briefs", "files": {BRIEFS: UPDATED_BRIEFS}},
            token="a-token-nobody-issued",
        )
    )
    proposal_id = prepared.split("proposal_id: ")[1].split("\n")[0].strip()
    diff_hash = prepared.split("diff_hash: ")[1].split("\n")[0].strip()

    result = call(
        client,
        "approve",
        {"proposal_id": proposal_id, "diff_hash": diff_hash},
        token="a-token-nobody-issued",
    )

    assert result["isError"] is True
    assert gh.files_on("main")[BRIEFS] != UPDATED_BRIEFS


# ==============================================================================================
# A whole change, from the connector's side of the wire
# ==============================================================================================


def test_a_signed_in_person_can_prepare_and_publish_a_change(
    client: TestClient, gh: FakeGitHubClient
) -> None:
    prepared = text_of(
        call(
            client,
            "propose_edit",
            {
                "skill_name": "how-we-write-briefs",
                "files": {BRIEFS: UPDATED_BRIEFS},
                "note": "Clients kept asking whose brief it was.",
            },
            A_TOKEN,
        )
    )
    proposal_id = prepared.split("proposal_id: ")[1].split("\n")[0].strip()
    diff_hash = prepared.split("diff_hash: ")[1].split("\n")[0].strip()

    published = text_of(
        call(client, "approve", {"proposal_id": proposal_id, "diff_hash": diff_hash}, A_TOKEN)
    )

    assert gh.files_on("main")[BRIEFS] == UPDATED_BRIEFS
    assert "1.4.3" in published
    assert json.loads(gh.files_on("main")[f"{ALPHA.plugin_dir}/.claude-plugin/plugin.json"])[
        "version"
    ] == "1.4.3"


def test_the_history_of_a_skill_comes_back_through_the_connector(
    client: TestClient, gh: FakeGitHubClient
) -> None:
    result = call(client, "history", {"skill_name": "how-we-write-briefs"}, A_TOKEN)

    assert result["isError"] is False


def test_a_skill_that_is_not_there_is_answered_without_jargon(client: TestClient) -> None:
    result = call(client, "read_skill", {"skill_name": "no-such-skill"}, A_TOKEN)

    message = text_of(result)
    assert result["isError"] is True
    for jargon in ("traceback", "exception", "404", "sha", "None"):
        assert jargon not in message


# ==============================================================================================
# What the server builds on start-up
# ==============================================================================================


def test_the_github_client_is_built_for_the_repository_in_the_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole connector is dead if this call does not match what the client offers."""
    monkeypatch.setenv("LIBRARIAN_DEV_TOKEN", "a-development-token")

    built = _build_github_client(
        Config(repo_owner="an-owner", repo_name="a-repo", default_branch="trunk")
    )

    assert built.owner == "an-owner"
    assert built.repo == "a-repo"
    assert built.default_branch == "trunk"


def test_the_github_app_client_is_built_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.delenv("LIBRARIAN_DEV_TOKEN", raising=False)
    key_file = tmp_path / "key.pem"
    key_file.write_text("-----BEGIN PRIVATE KEY-----\nnot a real key\n", encoding="utf-8")
    monkeypatch.setenv(GITHUB_APP_ID_VAR, "123456")
    monkeypatch.setenv(GITHUB_PRIVATE_KEY_PATH_VAR, str(key_file))
    monkeypatch.setenv(GITHUB_INSTALLATION_ID_VAR, "4242")
    monkeypatch.delenv(GITHUB_PRIVATE_KEY_VAR, raising=False)

    built = _build_github_client(Config(repo_owner="an-owner", repo_name="a-repo"))

    assert built.app_id == "123456"
    assert built.owner == "an-owner"
    assert built.repo == "a-repo"
    assert "hidden" in repr(built)
    assert "not a real key" not in repr(built)


def test_a_missing_private_key_is_reported_in_plain_english(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LIBRARIAN_DEV_TOKEN", raising=False)
    for name in (GITHUB_PRIVATE_KEY_VAR, GITHUB_PRIVATE_KEY_PATH_VAR):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(LibrarianError) as refused:
        _build_github_client(Config(repo_owner="an-owner", repo_name="a-repo"))

    assert "set up" in refused.value.user_message.lower()


def test_the_store_of_drafts_uses_the_time_to_live_from_the_settings() -> None:
    store = _build_proposal_store(
        Config(repo_owner="an-owner", repo_name="a-repo", proposal_ttl_seconds=120)
    )

    assert store.ttl_seconds == 120


# ==============================================================================================
# What the tool list actually tells a host, which is the only version anybody reads
# ==============================================================================================
#
# These go over the wire on purpose. The warning about what the two references prove lives
# in service.APPROVE_TOOL_DESCRIPTION, and a test that reads that constant passes happily
# while the connector hands hosts a shorter retelling with the warning cut out. That is
# exactly what happened, so everything below asks the running server what it publishes.


def tools_offered(client: TestClient) -> dict[str, dict[str, Any]]:
    """The tool list exactly as a host receives it."""
    response = client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    )
    assert response.status_code == 200
    return {tool["name"]: tool for tool in response.json()["result"]["tools"]}


def test_the_approve_tool_that_is_published_says_the_two_references_are_not_an_approval(
    client: TestClient,
) -> None:
    """A host that believes the fingerprint is the approval will publish with nobody there."""
    description = tools_offered(client)["approve"]["description"].lower()

    assert "do not prove that a person agreed" in description
    assert "collected outside this tool" in description
    assert "said yes to it in their own words" in description


def test_the_published_approve_tool_is_word_for_word_the_one_the_service_owns(
    client: TestClient,
) -> None:
    """One wording, in one place. A second copy is a copy that goes stale."""
    assert tools_offered(client)["approve"]["description"] == service.APPROVE_TOOL_DESCRIPTION


def published_wording(tool: dict[str, Any]) -> list[str]:
    """Every piece of writing a host reads for one tool, including the ones on arguments."""
    found: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "description" and isinstance(item, str):
                    found.append(item)
                else:
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(tool)
    return found


def test_nothing_else_the_server_publishes_retells_the_approval_wording(
    client: TestClient,
) -> None:
    """A shorter retelling anywhere in the list is the same failure in a new place.

    The rule is narrow on purpose: apart from the one authoritative wording, nothing the
    server publishes may tell a host what the two references mean for publishing. A
    passing mention of one reference is allowed, and so is telling a host to get a yes.
    Putting the two together is how the softened retelling gets written every time.
    """
    references = ("proposal_id", "diff_hash", "fingerprint")
    publishing = ("approv", "publish", "go ahead", "says yes", "said yes")

    for name, tool in tools_offered(client).items():
        for wording in published_wording(tool):
            if name == "approve" and wording == service.APPROVE_TOOL_DESCRIPTION:
                continue
            spoken = wording.lower()
            names_a_reference = any(word in spoken for word in references)
            talks_about_publishing = any(word in spoken for word in publishing)
            assert not (names_a_reference and talks_about_publishing), (
                f"the {name} tool tells a host what the references mean for publishing, "
                "in wording other than the one the service owns"
            )


def test_the_connector_repeats_the_same_warning_when_it_introduces_itself(
    client: TestClient,
) -> None:
    """The introduction is read before any tool is, so it must not soften the warning."""
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )

    instructions = response.json()["result"]["instructions"]
    assert service.APPROVE_TOOL_DESCRIPTION in instructions
