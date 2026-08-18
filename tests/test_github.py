"""Tests for the GitHub layer.

The point of this module is attribution: the commit has to say that the human
asked for the change, while the librarian app is only the one that typed it in.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from typing import Any, Callable

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from librarian.errors import LibrarianError, NotAuthorized, PublishFailed, SkillNotFound
from librarian.github import GitHubAppClient, GitHubClient, TokenClient

from .fakes import FakeGitHubClient

OWNER = "atlantic-labs"
REPO = "people-skills"
FAKE_TOKEN = "ghs_installation_token_do_not_log"

JARGON = [
    "traceback",
    "http",
    "status code",
    "stack",
    "exception",
    "json",
    "none",
    "null",
    "api",
    "--",
    "—",
]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rsa_key() -> tuple[str, Any]:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    return pem, private.public_key()


class RecordingAPI:
    """A stand-in for the GitHub REST endpoints, backed by httpx's mock transport."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.handler = handler
        self.requests: list[httpx.Request] = []
        self.bodies: list[Any] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        raw = request.content
        self.bodies.append(json.loads(raw) if raw else None)
        return self.handler(request)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]

    def calls(self) -> list[tuple[str, str]]:
        return [(r.method, r.url.path) for r in self.requests]

    def body_for(self, method: str, path_fragment: str) -> Any:
        for request, body in zip(self.requests, self.bodies):
            if request.method == method and path_fragment in request.url.path:
                return body
        raise AssertionError(f"no {method} request touching {path_fragment}")

    def count(self, path_fragment: str) -> int:
        return sum(1 for r in self.requests if path_fragment in r.url.path)


def json_response(payload: Any, status: int = 200, headers: dict[str, str] | None = None):
    return httpx.Response(status, json=payload, headers=headers or {})


def encode(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def git_data_handler(
    *,
    head_sha: str = "headsha",
    tree_sha: str = "basetree",
    new_commit_sha: str = "newcommitsha",
    merge_sha: str = "mergesha",
    merge_status: int = 200,
    merge_message: str = "not mergeable",
    branch_head_now: str | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Answers the whole branch, commit, pull request and merge conversation.

    When `branch_head_now` is set, the merge endpoint behaves the way GitHub does when
    a merge names the commit it expects: it refuses with a conflict unless the branch
    still points at that exact commit.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/access_tokens"):
            return json_response({"token": FAKE_TOKEN, "expires_at": "2099-01-01T00:00:00Z"})
        if path.endswith("/installation"):
            return json_response({"id": 4242})
        if "/git/ref/heads/" in path:
            return json_response({"object": {"sha": head_sha}})
        if re.search(r"/git/commits/[^/]+$", path) and request.method == "GET":
            return json_response({"sha": head_sha, "tree": {"sha": tree_sha}})
        if path.endswith("/git/blobs") and request.method == "POST":
            return json_response({"sha": "blob-" + str(len(request.content))}, status=201)
        if path.endswith("/git/trees"):
            return json_response({"sha": "newtree"}, status=201)
        if path.endswith("/git/commits") and request.method == "POST":
            return json_response({"sha": new_commit_sha}, status=201)
        if "/git/refs/heads/" in path and request.method == "PATCH":
            return json_response({"object": {"sha": new_commit_sha}})
        if path.endswith("/git/refs") and request.method == "POST":
            return json_response({"ref": "refs/heads/new"}, status=201)
        if path.endswith("/commits") and request.method == "GET":
            return json_response(
                [
                    {
                        "sha": "commitsha",
                        "commit": {
                            "message": "Update a skill\n\nRequested-By: A Person <a@example.test>",
                            "author": {
                                "name": "A Person",
                                "email": "a@example.test",
                                "date": "2026-08-18T12:00:00Z",
                            },
                            "committer": {"name": "Skill Librarian"},
                        },
                    }
                ]
            )
        if path.endswith("/pulls") and request.method == "POST":
            return json_response({"number": 17}, status=201)
        if path.endswith("/merge") and request.method == "PUT":
            if merge_status != 200:
                return json_response({"message": merge_message}, status=merge_status)
            if branch_head_now is not None:
                sent = json.loads(request.content or b"{}").get("sha")
                if sent != branch_head_now:
                    return json_response(
                        {"message": "Head branch was modified. Review and try the merge again."},
                        status=409,
                    )
            return json_response({"merged": True, "sha": merge_sha})
        return json_response({"message": "not found"}, status=404)

    return handler


def app_client(api: RecordingAPI, rsa_key: tuple[str, Any], **kwargs: Any) -> GitHubAppClient:
    pem, _ = rsa_key
    return GitHubAppClient(
        app_id="123456",
        private_key=pem,
        owner=OWNER,
        repo=REPO,
        installation_id=kwargs.pop("installation_id", 4242),
        http_client=api.client(),
        **kwargs,
    )


def assert_plain_english(message: str) -> None:
    assert message, "every error needs something a person can read"
    lowered = message.lower()
    for word in JARGON:
        assert word not in lowered, f"error message should not contain {word!r}: {message}"
    assert message[0].isupper()


# ---------------------------------------------------------------------------
# signing in as the app
# ---------------------------------------------------------------------------


def test_jwt_is_short_lived_backdated_and_rs256(rsa_key: tuple[str, Any]) -> None:
    _, public = rsa_key
    api = RecordingAPI(git_data_handler())
    client = app_client(api, rsa_key)

    client.get_ref_sha("main")

    token_request = next(r for r in api.requests if r.url.path.endswith("/access_tokens"))
    raw = token_request.headers["Authorization"].removeprefix("Bearer ")
    header = jwt.get_unverified_header(raw)
    assert header["alg"] == "RS256"

    claims = jwt.decode(raw, public, algorithms=["RS256"], options={"verify_exp": False})
    assert claims["iss"] == "123456"
    lifetime = claims["exp"] - claims["iat"]
    assert lifetime <= 10 * 60, "a GitHub app token may not live longer than ten minutes"
    now = __import__("time").time()
    assert claims["iat"] <= now - 59, "issued-at is backdated to survive clock differences"


def test_installation_token_is_scoped_to_the_single_repository(rsa_key: tuple[str, Any]) -> None:
    api = RecordingAPI(git_data_handler())
    client = app_client(api, rsa_key)

    client.get_ref_sha("main")

    body = api.body_for("POST", "/access_tokens")
    assert body["repositories"] == [REPO]
    assert body["permissions"]["contents"] == "write"


def test_installation_token_is_cached_between_calls(rsa_key: tuple[str, Any]) -> None:
    api = RecordingAPI(git_data_handler())
    client = app_client(api, rsa_key)

    client.get_ref_sha("main")
    client.get_ref_sha("main")
    client.list_commits("plugins/people-project/skills/onboarding/SKILL.md", 5)

    assert api.count("/access_tokens") == 1


def test_installation_token_is_replaced_before_it_expires(
    rsa_key: tuple[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def near_expiry(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/access_tokens"):
            # Already inside the refresh margin, so it must not be reused.
            return json_response({"token": FAKE_TOKEN, "expires_at": "2000-01-01T00:00:00Z"})
        return git_data_handler()(request)

    api = RecordingAPI(near_expiry)
    client = app_client(api, rsa_key)

    client.get_ref_sha("main")
    client.get_ref_sha("main")

    assert api.count("/access_tokens") == 2


def test_installation_id_is_discovered_when_not_given(rsa_key: tuple[str, Any]) -> None:
    api = RecordingAPI(git_data_handler())
    client = app_client(api, rsa_key, installation_id=None)

    client.get_ref_sha("main")

    assert any(p.endswith(f"/repos/{OWNER}/{REPO}/installation") for p in api.paths())
    assert any("/app/installations/4242/access_tokens" in p for p in api.paths())


def test_token_and_private_key_never_reach_the_logs_or_a_repr(
    rsa_key: tuple[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    pem, _ = rsa_key
    api = RecordingAPI(git_data_handler())
    client = app_client(api, rsa_key)

    with caplog.at_level(logging.DEBUG):
        client.get_ref_sha("main")
        with pytest.raises(LibrarianError) as failure:
            client.get_file("missing.md", "main")

    written = "\n".join(record.getMessage() for record in caplog.records)
    assert FAKE_TOKEN not in written
    assert "PRIVATE KEY" not in written
    assert FAKE_TOKEN not in repr(client)
    assert "PRIVATE KEY" not in repr(client)
    assert FAKE_TOKEN not in str(failure.value)
    assert FAKE_TOKEN not in failure.value.user_message


def test_app_client_refuses_to_start_without_a_key(rsa_key: tuple[str, Any]) -> None:
    api = RecordingAPI(git_data_handler())
    with pytest.raises(LibrarianError) as failure:
        GitHubAppClient(
            app_id="123456", private_key="  ", owner=OWNER, repo=REPO, http_client=api.client()
        )
    assert_plain_english(failure.value.user_message)


# ---------------------------------------------------------------------------
# the development token client
# ---------------------------------------------------------------------------


def test_token_client_reads_the_environment_and_warns_loudly(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("LIBRARIAN_DEV_TOKEN", "dev-token-value")
    api = RecordingAPI(git_data_handler())

    with caplog.at_level(logging.WARNING):
        with pytest.warns(RuntimeWarning, match="must not be used in production"):
            client = TokenClient(OWNER, REPO, http_client=api.client())

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "must not be used in production" in logged
    assert "dev-token-value" not in repr(client)

    client.get_ref_sha("main")
    assert api.requests[0].headers["Authorization"] == "token dev-token-value"


def test_token_client_without_a_token_says_so_plainly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LIBRARIAN_DEV_TOKEN", raising=False)
    with pytest.raises(NotAuthorized) as failure:
        TokenClient(OWNER, REPO)
    assert_plain_english(failure.value.user_message)


def test_both_clients_satisfy_the_protocol(
    rsa_key: tuple[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LIBRARIAN_DEV_TOKEN", "dev-token-value")
    api = RecordingAPI(git_data_handler())
    with pytest.warns(RuntimeWarning):
        assert isinstance(TokenClient(OWNER, REPO, http_client=api.client()), GitHubClient)
    assert isinstance(app_client(api, rsa_key), GitHubClient)
    assert isinstance(FakeGitHubClient(), GitHubClient)


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------


def test_get_file_returns_the_text_and_the_blob_id(rsa_key: tuple[str, Any]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "/contents/" in request.url.path:
            return json_response(
                {"sha": "blobsha", "encoding": "base64", "content": encode("hello skills")}
            )
        return git_data_handler()(request)

    api = RecordingAPI(handler)
    text, blob_sha = app_client(api, rsa_key).get_file("plugins/p/skills/s/SKILL.md", "main")

    assert text == "hello skills"
    assert blob_sha == "blobsha"


def test_get_file_falls_back_to_the_blob_for_a_large_file(rsa_key: tuple[str, Any]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "/contents/" in request.url.path:
            return json_response({"sha": "bigblob", "encoding": "none", "content": ""})
        if "/git/blobs/bigblob" in request.url.path:
            return json_response({"content": encode("a very large skill"), "encoding": "base64"})
        return git_data_handler()(request)

    api = RecordingAPI(handler)
    text, blob_sha = app_client(api, rsa_key).get_file("plugins/p/skills/s/SKILL.md", "main")

    assert text == "a very large skill"
    assert blob_sha == "bigblob"


def test_list_dir_returns_the_entries(rsa_key: tuple[str, Any]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "/contents/" in request.url.path:
            return json_response(
                [
                    {"name": "SKILL.md", "path": "s/SKILL.md", "type": "file", "sha": "a", "size": 3},
                    {"name": "reference", "path": "s/reference", "type": "dir", "sha": "b"},
                ]
            )
        return git_data_handler()(request)

    api = RecordingAPI(handler)
    entries = app_client(api, rsa_key).list_dir("plugins/p/skills", "main")

    assert [e["name"] for e in entries] == ["SKILL.md", "reference"]
    assert entries[0]["type"] == "file"


def test_list_commits_reports_who_asked(rsa_key: tuple[str, Any]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/commits") and request.method == "GET":
            return json_response(
                [
                    {
                        "sha": "c1",
                        "commit": {
                            "message": "Update onboarding\n\nRequested-By: Ellie Ward <ellie@example.com>",
                            "author": {
                                "name": "Ellie Ward",
                                "email": "ellie@example.com",
                                "date": "2026-08-18T10:00:00Z",
                            },
                            "committer": {"name": "Skill Librarian"},
                        },
                    }
                ]
            )
        return git_data_handler()(request)

    api = RecordingAPI(handler)
    history = app_client(api, rsa_key).list_commits("plugins/p/skills/s/SKILL.md", 5)

    assert history[0]["author_name"] == "Ellie Ward"
    assert history[0]["committer_name"] == "Skill Librarian"


def test_a_missing_file_is_reported_in_plain_english(rsa_key: tuple[str, Any]) -> None:
    api = RecordingAPI(lambda request: json_response({"message": "Not Found"}, status=404))
    client = GitHubAppClient(
        app_id="1",
        private_key=rsa_key[0],
        owner=OWNER,
        repo=REPO,
        installation_id=1,
        http_client=api.client(),
    )
    # The token call is the first thing to fail, so give it a working answer.
    api.handler = lambda request: (
        json_response({"token": FAKE_TOKEN, "expires_at": "2099-01-01T00:00:00Z"})
        if request.url.path.endswith("/access_tokens")
        else json_response({"message": "Not Found"}, status=404)
    )

    with pytest.raises(SkillNotFound) as failure:
        client.get_file("plugins/p/skills/nope/SKILL.md", "main")
    assert_plain_english(failure.value.user_message)


def test_rate_limiting_is_explained_and_not_hidden(rsa_key: tuple[str, Any]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/access_tokens"):
            return json_response({"token": FAKE_TOKEN, "expires_at": "2099-01-01T00:00:00Z"})
        return json_response(
            {"message": "API rate limit exceeded"},
            status=403,
            headers={"x-ratelimit-remaining": "0"},
        )

    api = RecordingAPI(handler)
    with pytest.raises(LibrarianError) as failure:
        app_client(api, rsa_key).get_ref_sha("main")

    message = failure.value.user_message
    assert "slow down" in message.lower()
    assert_plain_english(message)


def test_a_sign_in_problem_is_reported_as_not_authorized(rsa_key: tuple[str, Any]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/access_tokens"):
            return json_response({"token": FAKE_TOKEN, "expires_at": "2099-01-01T00:00:00Z"})
        return json_response({"message": "Bad credentials"}, status=401)

    api = RecordingAPI(handler)
    with pytest.raises(NotAuthorized) as failure:
        app_client(api, rsa_key).get_ref_sha("main")
    assert_plain_english(failure.value.user_message)


def test_github_being_down_is_reported_without_technical_detail(rsa_key: tuple[str, Any]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/access_tokens"):
            return json_response({"token": FAKE_TOKEN, "expires_at": "2099-01-01T00:00:00Z"})
        raise httpx.ConnectError("connection refused to api.github.com", request=request)

    api = RecordingAPI(handler)
    with pytest.raises(LibrarianError) as failure:
        app_client(api, rsa_key).get_ref_sha("main")

    assert "api.github.com" not in failure.value.user_message
    assert_plain_english(failure.value.user_message)


# ---------------------------------------------------------------------------
# writing, which is where attribution happens
# ---------------------------------------------------------------------------


def test_commit_sets_the_human_as_author_and_the_app_as_committer(
    rsa_key: tuple[str, Any]
) -> None:
    api = RecordingAPI(git_data_handler())
    client = app_client(api, rsa_key)

    sha = client.commit_files(
        "librarian/onboarding-ab12",
        {"plugins/p/skills/onboarding/SKILL.md": "new body"},
        "Update the onboarding skill",
        "Ellie Ward",
        "ellie@example.com",
    )

    assert sha == "newcommitsha"
    commit_body = api.body_for("POST", "/git/commits")
    assert commit_body["author"] == {
        "name": "Ellie Ward",
        "email": "ellie@example.com",
        "date": commit_body["author"]["date"],
    }
    assert commit_body["committer"]["name"] == "Skill Librarian"
    assert commit_body["committer"]["email"].endswith("users.noreply.github.com")
    assert commit_body["author"]["email"] != commit_body["committer"]["email"]


def test_commit_message_carries_the_requested_by_trailer(rsa_key: tuple[str, Any]) -> None:
    api = RecordingAPI(git_data_handler())
    app_client(api, rsa_key).commit_files(
        "librarian/onboarding-ab12",
        {"plugins/p/skills/onboarding/SKILL.md": "new body"},
        "Update the onboarding skill",
        "Ellie Ward",
        "ellie@example.com",
    )

    message = api.body_for("POST", "/git/commits")["message"]
    assert message.startswith("Update the onboarding skill")
    assert "Requested-By: Ellie Ward <ellie@example.com>" in message.splitlines()


def test_commit_uses_the_git_data_endpoints_not_the_contents_endpoint(
    rsa_key: tuple[str, Any]
) -> None:
    api = RecordingAPI(git_data_handler())
    app_client(api, rsa_key).commit_files(
        "librarian/onboarding-ab12",
        {"a/SKILL.md": "one", "a/reference/notes.md": "two"},
        "Update two files",
        "Ellie Ward",
        "ellie@example.com",
    )

    written = [call for call in api.calls() if call[0] in {"POST", "PATCH"}]
    endpoints = [path for method, path in written]
    assert not any("/contents/" in path for path in endpoints)
    assert sum(1 for path in endpoints if path.endswith("/git/blobs")) == 2
    assert any(path.endswith("/git/trees") for path in endpoints)
    assert any(path.endswith("/git/commits") for path in endpoints)
    assert any("/git/refs/heads/" in path for path in endpoints)

    tree_body = api.body_for("POST", "/git/trees")
    assert tree_body["base_tree"] == "basetree"
    assert {entry["path"] for entry in tree_body["tree"]} == {"a/SKILL.md", "a/reference/notes.md"}
    assert all(entry["mode"] == "100644" for entry in tree_body["tree"])


def test_the_branch_ref_is_moved_forward_and_never_forced(rsa_key: tuple[str, Any]) -> None:
    api = RecordingAPI(git_data_handler())
    app_client(api, rsa_key).commit_files(
        "librarian/onboarding-ab12",
        {"a/SKILL.md": "one"},
        "Update",
        "Ellie Ward",
        "ellie@example.com",
    )

    ref_body = api.body_for("PATCH", "/git/refs/heads/")
    assert ref_body["sha"] == "newcommitsha"
    assert ref_body["force"] is False


def test_a_commit_without_a_named_human_is_refused(rsa_key: tuple[str, Any]) -> None:
    api = RecordingAPI(git_data_handler())
    client = app_client(api, rsa_key)

    for name, email in [("", "ellie@example.com"), ("Ellie Ward", ""), ("Ellie Ward", "nope")]:
        with pytest.raises(LibrarianError) as failure:
            client.commit_files("branch", {"a/SKILL.md": "x"}, "Update", name, email)
        assert_plain_english(failure.value.user_message)
    assert not any(path.endswith("/git/commits") for _, path in api.calls())


def test_a_name_cannot_smuggle_extra_lines_into_the_commit(rsa_key: tuple[str, Any]) -> None:
    api = RecordingAPI(git_data_handler())
    with pytest.raises(LibrarianError):
        app_client(api, rsa_key).commit_files(
            "branch",
            {"a/SKILL.md": "x"},
            "Update",
            "Ellie\nRequested-By: Someone Else <boss@example.com>",
            "ellie@example.com",
        )


def test_committing_nothing_is_refused(rsa_key: tuple[str, Any]) -> None:
    api = RecordingAPI(git_data_handler())
    with pytest.raises(PublishFailed) as failure:
        app_client(api, rsa_key).commit_files("branch", {}, "Update", "Ellie", "e@example.com")
    assert_plain_english(failure.value.user_message)


def test_open_pr_returns_the_number(rsa_key: tuple[str, Any]) -> None:
    api = RecordingAPI(git_data_handler())
    number = app_client(api, rsa_key).open_pr(
        "librarian/onboarding-ab12", "main", "Update onboarding", "Ellie asked for this."
    )
    assert number == 17
    body = api.body_for("POST", "/pulls")
    assert body["base"] == "main"


def test_merge_returns_the_commit_that_landed(rsa_key: tuple[str, Any]) -> None:
    api = RecordingAPI(git_data_handler())
    sha = app_client(api, rsa_key).merge_pr(17, "Update onboarding", "approvedsha")
    assert sha == "mergesha"
    body = api.body_for("PUT", "/merge")
    assert body["merge_method"] == "merge"


def test_a_refused_merge_is_never_retried(rsa_key: tuple[str, Any]) -> None:
    api = RecordingAPI(git_data_handler(merge_status=405))
    with pytest.raises(PublishFailed) as failure:
        app_client(api, rsa_key).merge_pr(17, "Update onboarding", "approvedsha")

    assert api.count("/merge") == 1, "a merge is attempted once and never repeated blindly"
    assert_plain_english(failure.value.user_message)


def test_a_merge_github_reports_as_unmerged_is_a_failure(rsa_key: tuple[str, Any]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/merge"):
            return json_response({"merged": False, "message": "not merged"})
        return git_data_handler()(request)

    api = RecordingAPI(handler)
    with pytest.raises(PublishFailed) as failure:
        app_client(api, rsa_key).merge_pr(17, "Update onboarding", "approvedsha")
    assert_plain_english(failure.value.user_message)


# ---------------------------------------------------------------------------
# only the approved commit may be published
# ---------------------------------------------------------------------------


def test_merge_names_the_approved_commit_so_github_can_refuse_a_swap(
    rsa_key: tuple[str, Any]
) -> None:
    api = RecordingAPI(git_data_handler())
    app_client(api, rsa_key).merge_pr(17, "Update onboarding", "approvedsha")

    body = api.body_for("PUT", "/merge")
    assert body["sha"] == "approvedsha", (
        "the merge has to name the commit that was approved, or GitHub will merge "
        "whatever the branch points at when the merge runs"
    )


def test_a_merge_is_refused_when_someone_moved_the_branch_after_approval(
    rsa_key: tuple[str, Any]
) -> None:
    # GitHub answers with a conflict when the branch no longer points at the named commit.
    api = RecordingAPI(git_data_handler(branch_head_now="somebody-elses-commit"))

    with pytest.raises(PublishFailed) as failure:
        app_client(api, rsa_key).merge_pr(17, "Update onboarding", "approvedsha")

    message = failure.value.user_message
    assert "approved" in message.lower()
    assert "nothing was published" in message.lower()
    assert_plain_english(message)
    assert api.count("/merge") == 1, "a refused merge is never retried"


def test_the_same_merge_goes_through_when_the_branch_has_not_moved(
    rsa_key: tuple[str, Any]
) -> None:
    api = RecordingAPI(git_data_handler(branch_head_now="approvedsha"))
    sha = app_client(api, rsa_key).merge_pr(17, "Update onboarding", "approvedsha")
    assert sha == "mergesha"


def test_an_ordinary_clash_is_not_reported_as_someone_editing_the_change(
    rsa_key: tuple[str, Any]
) -> None:
    """A conflict that is not about the branch moving must not blame the approver."""
    api = RecordingAPI(git_data_handler(merge_status=409, merge_message="Merge conflict"))

    with pytest.raises(PublishFailed) as failure:
        app_client(api, rsa_key).merge_pr(17, "Update onboarding", "approvedsha")

    message = failure.value.user_message
    assert "approved" not in message.lower()
    assert_plain_english(message)


def test_a_merge_without_the_approved_commit_never_reaches_github(
    rsa_key: tuple[str, Any]
) -> None:
    api = RecordingAPI(git_data_handler())
    client = app_client(api, rsa_key)

    for missing in ["", "   "]:
        with pytest.raises(PublishFailed) as failure:
            client.merge_pr(17, "Update onboarding", missing)
        assert_plain_english(failure.value.user_message)

    assert api.count("/merge") == 0, (
        "without the approved commit there is nothing to pin the merge to, so nothing "
        "is sent at all"
    )


# ---------------------------------------------------------------------------
# a write straight into the shared library reaches nobody
# ---------------------------------------------------------------------------


def test_the_real_client_refuses_to_commit_onto_the_shared_branch(
    rsa_key: tuple[str, Any]
) -> None:
    api = RecordingAPI(git_data_handler())
    client = app_client(api, rsa_key, default_branch="main")

    with pytest.raises(PublishFailed) as failure:
        client.commit_files(
            "main",
            {"plugins/p/skills/onboarding/SKILL.md": "new body"},
            "Update the onboarding skill",
            "Ellie Ward",
            "ellie@example.com",
        )

    assert_plain_english(failure.value.user_message)
    assert not any(
        method in {"POST", "PATCH"} for method, _ in api.calls()
    ), "nothing at all may be written when the target is the shared branch"


def test_the_shared_branch_refusal_cannot_be_dodged_by_spelling_it_differently(
    rsa_key: tuple[str, Any]
) -> None:
    api = RecordingAPI(git_data_handler())
    client = app_client(api, rsa_key, default_branch="main")

    for spelling in ["main", " main ", "refs/heads/main", "Main", "MAIN"]:
        with pytest.raises(PublishFailed) as failure:
            client.commit_files(spelling, {"a/SKILL.md": "x"}, "Update", "Ellie", "e@example.com")
        assert_plain_english(failure.value.user_message)

    assert not any(method in {"POST", "PATCH"} for method, _ in api.calls())


def test_the_shared_branch_is_whichever_one_this_library_uses(rsa_key: tuple[str, Any]) -> None:
    """Nothing here assumes the shared branch is called main."""
    api = RecordingAPI(git_data_handler())
    client = app_client(api, rsa_key, default_branch="trunk")

    with pytest.raises(PublishFailed) as failure:
        client.commit_files("trunk", {"a/SKILL.md": "x"}, "Update", "Ellie", "e@example.com")
    assert_plain_english(failure.value.user_message)

    # A branch called main is an ordinary working branch for this library, so it is allowed.
    assert client.commit_files(
        "main", {"a/SKILL.md": "x"}, "Update", "Ellie", "e@example.com"
    ) == "newcommitsha"


def test_the_development_client_refuses_the_shared_branch_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LIBRARIAN_DEV_TOKEN", "dev-token-value")
    api = RecordingAPI(git_data_handler())
    with pytest.warns(RuntimeWarning):
        client = TokenClient(OWNER, REPO, http_client=api.client())

    with pytest.raises(PublishFailed) as failure:
        client.commit_files("main", {"a/SKILL.md": "x"}, "Update", "Ellie", "e@example.com")

    assert_plain_english(failure.value.user_message)
    assert not any(method in {"POST", "PATCH"} for method, _ in api.calls())


def test_create_branch_asks_for_the_right_reference(rsa_key: tuple[str, Any]) -> None:
    api = RecordingAPI(git_data_handler())
    app_client(api, rsa_key).create_branch("librarian/onboarding-ab12", "headsha")
    body = api.body_for("POST", "/git/refs")
    assert body == {"ref": "refs/heads/librarian/onboarding-ab12", "sha": "headsha"}


# ---------------------------------------------------------------------------
# the in-memory fake, which the rest of the suite relies on
# ---------------------------------------------------------------------------


def test_fake_records_the_human_as_author_and_the_app_as_committer() -> None:
    gh = FakeGitHubClient()
    gh.seed({"plugins/p/skills/onboarding/SKILL.md": "old"})
    gh.create_branch("librarian/onboarding-ab12", gh.get_ref_sha("main"))

    sha = gh.commit_files(
        "librarian/onboarding-ab12",
        {"plugins/p/skills/onboarding/SKILL.md": "new"},
        "Update onboarding",
        "Ellie Ward",
        "ellie@example.com",
    )

    commit = gh.commits[sha]
    assert commit.author_name == "Ellie Ward"
    assert commit.author_email == "ellie@example.com"
    assert commit.committer_name == "Skill Librarian"
    assert "Requested-By: Ellie Ward <ellie@example.com>" in commit.message.splitlines()


def test_fake_refuses_a_commit_straight_to_the_default_branch() -> None:
    gh = FakeGitHubClient()
    with pytest.raises(PublishFailed):
        gh.commit_files("main", {"a/SKILL.md": "x"}, "Update", "Ellie", "ellie@example.com")


def test_fake_merge_moves_the_default_branch_forward() -> None:
    gh = FakeGitHubClient()
    gh.seed({"plugins/p/skills/onboarding/SKILL.md": "old"})
    before = gh.get_ref_sha("main")
    gh.create_branch("librarian/onboarding-ab12", before)
    gh.commit_files(
        "librarian/onboarding-ab12",
        {"plugins/p/skills/onboarding/SKILL.md": "new"},
        "Update onboarding",
        "Ellie Ward",
        "ellie@example.com",
    )
    number = gh.open_pr("librarian/onboarding-ab12", "main", "Update onboarding", "body")
    merged = gh.merge_pr(number, "Update onboarding", gh.get_ref_sha("librarian/onboarding-ab12"))

    assert gh.get_ref_sha("main") == merged
    assert merged != before
    assert gh.files_on("main")["plugins/p/skills/onboarding/SKILL.md"] == "new"
    assert gh.pull_requests[number].merged is True


def test_fake_merging_twice_is_refused() -> None:
    gh = FakeGitHubClient()
    gh.create_branch("librarian/x", gh.get_ref_sha("main"))
    head = gh.commit_files(
        "librarian/x", {"a/SKILL.md": "x"}, "Update", "Ellie", "ellie@example.com"
    )
    number = gh.open_pr("librarian/x", "main", "t", "b")
    gh.merge_pr(number, "t", head)
    with pytest.raises(PublishFailed):
        gh.merge_pr(number, "t", head)


def test_fake_refuses_to_merge_content_that_was_never_approved() -> None:
    """The fake has to hold the same promise as the real client, or the suite proves nothing."""
    gh = FakeGitHubClient()
    gh.create_branch("librarian/x", gh.get_ref_sha("main"))
    approved = gh.commit_files(
        "librarian/x", {"a/SKILL.md": "approved"}, "Update", "Ellie", "ellie@example.com"
    )
    number = gh.open_pr("librarian/x", "main", "t", "b")

    # Somebody with write access adds another commit to the branch after the approval.
    gh.commit_files(
        "librarian/x", {"a/SKILL.md": "sneaked in"}, "Extra", "Someone Else", "else@example.com"
    )

    with pytest.raises(PublishFailed) as failure:
        gh.merge_pr(number, "t", approved)

    assert_plain_english(failure.value.user_message)
    assert gh.files_on("main").get("a/SKILL.md") is None, "nothing may reach the shared library"


def test_fake_reads_files_and_folders_back() -> None:
    gh = FakeGitHubClient()
    gh.seed(
        {
            "plugins/p/skills/onboarding/SKILL.md": "body",
            "plugins/p/skills/onboarding/reference/notes.md": "notes",
        }
    )

    text, blob = gh.get_file("plugins/p/skills/onboarding/SKILL.md", "main")
    assert text == "body"
    assert blob

    entries = gh.list_dir("plugins/p/skills/onboarding", "main")
    assert {(e["name"], e["type"]) for e in entries} == {
        ("SKILL.md", "file"),
        ("reference", "dir"),
    }

    with pytest.raises(SkillNotFound):
        gh.get_file("plugins/p/skills/missing/SKILL.md", "main")


def test_fake_history_lists_only_commits_touching_the_file() -> None:
    gh = FakeGitHubClient()
    gh.seed({"plugins/p/skills/onboarding/SKILL.md": "one"})
    gh.create_branch("librarian/x", gh.get_ref_sha("main"))
    head = gh.commit_files(
        "librarian/x",
        {"plugins/p/skills/onboarding/SKILL.md": "two"},
        "Second change",
        "Ellie Ward",
        "ellie@example.com",
    )
    gh.merge_pr(gh.open_pr("librarian/x", "main", "t", "b"), "Second change", head)

    history = gh.list_commits("plugins/p/skills/onboarding/SKILL.md", 10)
    assert len(history) >= 2
    assert history[0]["author_name"] == "Ellie Ward"
