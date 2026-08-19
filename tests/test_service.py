"""The six operations, run end to end against the in-memory repository.

These tests do not stub out the layers underneath. A call to ``approve`` goes all the way
down through the publish path, cuts a branch, commits, opens a pull request and merges it,
and the assertions are made against the state the repository is left in afterwards. That is
deliberate: the failures this service exists to prevent are ones where each layer works on
its own and the joins between them do not.

The library used throughout holds two collections, because a marketplace repository is
allowed to hold as many as an organization wants and nothing in this service may assume
otherwise.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from librarian import service
from librarian.config import Config
from librarian.errors import (
    DiffMismatch,
    InvalidSkill,
    LibrarianError,
    NotAuthorized,
    ProposalNotFound,
    PublishFailed,
    SkillNotFound,
    UnsafePath,
)
from librarian.proposals import ProposalStore
from librarian.service import ANONYMOUS, Actor
from tests.fakes import (
    APP_COMMITTER_EMAIL,
    APP_COMMITTER_NAME,
    MARKETPLACE_MANIFEST,
    FakeGitHubClient,
    PluginSpec,
    library,
    marketplace_manifest_text,
    plugin_manifest_text,
    skill_text,
)

ALPHA = PluginSpec(
    name="alpha-pack",
    plugin_dir="plugins/alpha-pack",
    version="1.4.2",
    skills=(
        ("how-we-write-briefs", "How to write a brief."),
        ("weekly-report", "How the weekly report is put together."),
    ),
)
BETA = PluginSpec(
    name="beta-pack",
    plugin_dir="library/beta-pack",
    version="2.0.0",
    skills=(("handover-notes", "What to write when handing work over."),),
)

BRIEFS = "plugins/alpha-pack/skills/how-we-write-briefs/SKILL.md"
BRIEFS_NOTES = "plugins/alpha-pack/skills/how-we-write-briefs/reference/tone.md"
HANDOVER = "library/beta-pack/skills/handover-notes/SKILL.md"
ALPHA_MANIFEST = "plugins/alpha-pack/.claude-plugin/plugin.json"
BETA_MANIFEST = "library/beta-pack/.claude-plugin/plugin.json"

A_PERSON = Actor(name="A Person", email="a.person@example.test")
ANOTHER_PERSON = Actor(name="Another Person", email="another@example.test")

UPDATED_BRIEFS = skill_text(
    "how-we-write-briefs", "How to write a brief.", "Start every brief with the client name."
)


@pytest.fixture
def cfg() -> Config:
    return Config(repo_owner="an-owner", repo_name="a-repo", default_branch="main")


@pytest.fixture
def gh() -> FakeGitHubClient:
    return library(ALPHA, BETA)


@pytest.fixture
def store() -> ProposalStore:
    return ProposalStore(ttl_seconds=900)


def version_of(gh: FakeGitHubClient, manifest_path: str, ref: str = "main") -> str:
    return json.loads(gh.files_on(ref)[manifest_path])["version"]


def marketplace_entries(gh: FakeGitHubClient, ref: str = "main") -> list[dict]:
    return json.loads(gh.files_on(ref)[MARKETPLACE_MANIFEST])["plugins"]


def entry_named(gh: FakeGitHubClient, name: str, ref: str = "main") -> dict:
    return next(entry for entry in marketplace_entries(gh, ref) if entry["name"] == name)


def publish_a_change(
    gh: FakeGitHubClient,
    cfg: Config,
    store: ProposalStore,
    actor: Actor = A_PERSON,
    files: dict[str, str] | None = None,
    skill_name: str = "how-we-write-briefs",
) -> str:
    """Propose then approve one change, the way a person would."""
    preview = service.propose_edit(
        actor, gh, cfg, store, skill_name, files or {BRIEFS: UPDATED_BRIEFS}, "Because I said so."
    )
    return service.approve(actor, gh, cfg, store, preview.proposal_id, preview.diff_hash)


# ==============================================================================================
# list_skills
# ==============================================================================================


def test_list_skills_spans_every_collection_in_the_library(
    gh: FakeGitHubClient, cfg: Config
) -> None:
    answer = service.list_skills(A_PERSON, gh, cfg)

    assert "how-we-write-briefs" in answer
    assert "weekly-report" in answer
    assert "handover-notes" in answer
    assert "alpha-pack" in answer
    assert "beta-pack" in answer


def test_list_skills_shows_each_skills_own_description(
    gh: FakeGitHubClient, cfg: Config
) -> None:
    answer = service.list_skills(A_PERSON, gh, cfg)

    assert "How to write a brief." in answer
    assert "What to write when handing work over." in answer


def test_list_skills_says_so_plainly_when_the_library_is_empty(cfg: Config) -> None:
    empty = library(PluginSpec(name="alpha-pack", plugin_dir="plugins/alpha-pack"))

    answer = service.list_skills(A_PERSON, empty, cfg)

    assert "no skills" in answer.lower()


def test_list_skills_needs_no_identity_because_it_changes_nothing(
    gh: FakeGitHubClient, cfg: Config
) -> None:
    assert "how-we-write-briefs" in service.list_skills(ANONYMOUS, gh, cfg)


# ==============================================================================================
# read_skill
# ==============================================================================================


def test_read_skill_finds_a_skill_in_whichever_collection_holds_it(
    gh: FakeGitHubClient, cfg: Config
) -> None:
    from_alpha = service.read_skill(A_PERSON, gh, cfg, "how-we-write-briefs")
    from_beta = service.read_skill(A_PERSON, gh, cfg, "handover-notes")

    assert "alpha-pack" in from_alpha
    assert "How to write a brief." in from_alpha
    assert "beta-pack" in from_beta
    assert "What to write when handing work over." in from_beta


def test_read_skill_lists_the_supporting_notes_kept_with_a_skill(cfg: Config) -> None:
    gh = library(ALPHA, BETA)
    gh.seed({BRIEFS_NOTES: "Keep it short.\n"})

    answer = service.read_skill(A_PERSON, gh, cfg, "how-we-write-briefs")

    assert "tone.md" in answer


def test_read_skill_names_the_skills_that_do_exist_when_one_does_not(
    gh: FakeGitHubClient, cfg: Config
) -> None:
    with pytest.raises(SkillNotFound) as refused:
        service.read_skill(A_PERSON, gh, cfg, "no-such-skill")

    assert "how-we-write-briefs" in refused.value.user_message


def test_read_skill_refuses_a_name_that_could_never_be_a_folder(
    gh: FakeGitHubClient, cfg: Config
) -> None:
    with pytest.raises(UnsafePath):
        service.read_skill(A_PERSON, gh, cfg, "../../etc/passwd")


# ==============================================================================================
# A name used in two collections is an ambiguity, never a guess
# ==============================================================================================


def duplicated_library() -> FakeGitHubClient:
    """Two collections that both hold a skill called ``handover-notes``."""
    return library(
        PluginSpec(
            name="alpha-pack",
            plugin_dir="plugins/alpha-pack",
            version="1.4.2",
            skills=(("handover-notes", "The alpha team's version."),),
        ),
        PluginSpec(
            name="beta-pack",
            plugin_dir="library/beta-pack",
            version="2.0.0",
            skills=(("handover-notes", "The beta team's version."),),
        ),
    )


def test_a_duplicate_skill_name_across_collections_is_refused_by_name(cfg: Config) -> None:
    gh = duplicated_library()

    with pytest.raises(LibrarianError) as refused:
        service.read_skill(A_PERSON, gh, cfg, "handover-notes")

    message = refused.value.user_message
    assert "alpha-pack" in message
    assert "beta-pack" in message
    assert "more than one" in message.lower()


def test_a_duplicate_skill_name_is_never_quietly_resolved_to_one_of_them(
    cfg: Config,
) -> None:
    """Picking the first would edit one team's skill while somebody meant the other's.

    The refusal has to come from the lookup itself, not from a caller that happens to be
    careful, because every operation goes through that one lookup.
    """
    from librarian.marketplace import resolve_skill

    gh = duplicated_library()

    with pytest.raises(LibrarianError) as refused:
        resolve_skill(gh, cfg, "handover-notes", "main")

    assert not isinstance(refused.value, SkillNotFound)
    assert "alpha-pack" in refused.value.user_message
    assert "beta-pack" in refused.value.user_message


def test_a_duplicate_name_stops_an_edit_before_anything_is_prepared(
    cfg: Config, store: ProposalStore
) -> None:
    gh = duplicated_library()
    before = gh.files_on("main")

    with pytest.raises(LibrarianError):
        service.propose_edit(
            A_PERSON,
            gh,
            cfg,
            store,
            "handover-notes",
            {"plugins/alpha-pack/skills/handover-notes/SKILL.md": UPDATED_BRIEFS},
        )

    assert gh.files_on("main") == before
    assert store.active_ids() == []


def test_list_skills_warns_about_a_name_that_is_used_twice(cfg: Config) -> None:
    gh = duplicated_library()

    answer = service.list_skills(A_PERSON, gh, cfg)

    assert "handover-notes" in answer
    assert "more than one collection" in answer.lower()


# ==============================================================================================
# propose_edit
# ==============================================================================================


def test_propose_edit_shows_the_change_and_publishes_nothing(
    gh: FakeGitHubClient, cfg: Config, store: ProposalStore
) -> None:
    before = gh.files_on("main")

    preview = service.propose_edit(
        A_PERSON, gh, cfg, store, "how-we-write-briefs", {BRIEFS: UPDATED_BRIEFS}, "Clearer."
    )

    assert preview.proposal_id
    assert preview.diff_hash
    assert "Start every brief with the client name." in preview.diff
    assert gh.files_on("main") == before
    assert store.active_ids() == [preview.proposal_id]


def test_propose_edit_records_the_person_who_asked_in_the_git_form(
    gh: FakeGitHubClient, cfg: Config, store: ProposalStore
) -> None:
    preview = service.propose_edit(
        A_PERSON, gh, cfg, store, "how-we-write-briefs", {BRIEFS: UPDATED_BRIEFS}
    )

    assert store.get(preview.proposal_id).requested_by == "A Person <a.person@example.test>"


def test_propose_edit_works_without_an_identity_but_says_so(
    gh: FakeGitHubClient, cfg: Config, store: ProposalStore
) -> None:
    """Preparing a change is safe; publishing one under nobody's name is not."""
    preview = service.propose_edit(
        ANONYMOUS, gh, cfg, store, "how-we-write-briefs", {BRIEFS: UPDATED_BRIEFS}
    )

    assert "sign in" in preview.message.lower()


def test_propose_edit_refuses_a_file_outside_the_skill_being_changed(
    gh: FakeGitHubClient, cfg: Config, store: ProposalStore
) -> None:
    with pytest.raises(UnsafePath):
        service.propose_edit(
            A_PERSON,
            gh,
            cfg,
            store,
            "how-we-write-briefs",
            {"plugins/alpha-pack/skills/weekly-report/SKILL.md": UPDATED_BRIEFS},
        )


def test_propose_edit_refuses_a_write_to_another_collection(
    gh: FakeGitHubClient, cfg: Config, store: ProposalStore
) -> None:
    with pytest.raises(UnsafePath):
        service.propose_edit(
            A_PERSON,
            gh,
            cfg,
            store,
            "how-we-write-briefs",
            {"library/beta-pack/skills/how-we-write-briefs/SKILL.md": UPDATED_BRIEFS},
        )


def test_propose_edit_refuses_a_write_to_the_manifest_that_gates_delivery(
    gh: FakeGitHubClient, cfg: Config, store: ProposalStore
) -> None:
    with pytest.raises(UnsafePath):
        service.propose_edit(
            A_PERSON, gh, cfg, store, "how-we-write-briefs", {ALPHA_MANIFEST: "{}"}
        )


def test_propose_edit_refuses_a_step_outside_the_repository(
    gh: FakeGitHubClient, cfg: Config, store: ProposalStore
) -> None:
    with pytest.raises(UnsafePath):
        service.propose_edit(
            A_PERSON,
            gh,
            cfg,
            store,
            "how-we-write-briefs",
            {"plugins/alpha-pack/skills/how-we-write-briefs/../../../../etc/passwd": "x"},
        )


def test_propose_edit_refuses_content_that_is_not_a_usable_skill(
    gh: FakeGitHubClient, cfg: Config, store: ProposalStore
) -> None:
    with pytest.raises(InvalidSkill):
        service.propose_edit(
            A_PERSON,
            gh,
            cfg,
            store,
            "how-we-write-briefs",
            {BRIEFS: "---\nname: how-we-write-briefs\nnonsense: yes\n---\n\nBody.\n"},
        )


def test_propose_edit_refuses_a_change_that_changes_nothing(
    gh: FakeGitHubClient, cfg: Config, store: ProposalStore
) -> None:
    unchanged = gh.files_on("main")[BRIEFS]

    with pytest.raises(InvalidSkill):
        service.propose_edit(
            A_PERSON, gh, cfg, store, "how-we-write-briefs", {BRIEFS: unchanged}
        )


# ==============================================================================================
# approve
# ==============================================================================================


def test_approve_publishes_the_change_and_moves_the_version(
    gh: FakeGitHubClient, cfg: Config, store: ProposalStore
) -> None:
    answer = publish_a_change(gh, cfg, store)

    assert gh.files_on("main")[BRIEFS] == UPDATED_BRIEFS
    assert version_of(gh, ALPHA_MANIFEST) == "1.4.3"
    assert "1.4.3" in answer


def test_approve_keeps_both_manifests_carrying_the_same_version(
    gh: FakeGitHubClient, cfg: Config, store: ProposalStore
) -> None:
    publish_a_change(gh, cfg, store)

    assert version_of(gh, ALPHA_MANIFEST) == entry_named(gh, "alpha-pack")["version"]


def test_approve_leaves_every_other_collection_exactly_as_it_was(
    gh: FakeGitHubClient, cfg: Config, store: ProposalStore
) -> None:
    """A version that moves on its own would push a change at people who never asked."""
    before = entry_named(gh, "beta-pack")
    before_manifest = gh.files_on("main")[BETA_MANIFEST]

    publish_a_change(gh, cfg, store)

    assert entry_named(gh, "beta-pack") == before
    assert gh.files_on("main")[BETA_MANIFEST] == before_manifest


def test_approve_goes_through_a_pull_request_and_a_merge(
    gh: FakeGitHubClient, cfg: Config, store: ProposalStore
) -> None:
    publish_a_change(gh, cfg, store)

    assert len(gh.pull_requests) == 1
    pull = gh.pull_requests[1]
    assert pull.merged
    assert pull.base == "main"
    assert pull.head.startswith("librarian/")


def test_approve_never_commits_straight_to_the_shared_branch(
    gh: FakeGitHubClient, cfg: Config, store: ProposalStore
) -> None:
    publish_a_change(gh, cfg, store)

    committed_branches = [
        arguments[0] for name, arguments in gh.calls if name == "commit_files"
    ]
    assert committed_branches
    assert all(branch != "main" for branch in committed_branches)


def test_approve_records_the_person_as_author_and_the_app_as_committer(
    gh: FakeGitHubClient, cfg: Config, store: ProposalStore
) -> None:
    publish_a_change(gh, cfg, store)

    written = [c for c in gh.commit_list() if BRIEFS in c.changed and c.branch != "main"]
    assert written
    for commit in written:
        assert commit.author_name == "A Person"
        assert commit.author_email == "a.person@example.test"
        assert commit.committer_name == APP_COMMITTER_NAME
        assert commit.committer_email == APP_COMMITTER_EMAIL
        assert "Requested-By: A Person <a.person@example.test>" in commit.message


def test_approve_refuses_a_diff_hash_that_does_not_match_what_was_shown(
    gh: FakeGitHubClient, cfg: Config, store: ProposalStore
) -> None:
    preview = service.propose_edit(
        A_PERSON, gh, cfg, store, "how-we-write-briefs", {BRIEFS: UPDATED_BRIEFS}
    )
    before = gh.files_on("main")

    with pytest.raises(DiffMismatch):
        service.approve(A_PERSON, gh, cfg, store, preview.proposal_id, "b" * 64)

    assert gh.files_on("main") == before
    assert gh.pull_requests == {}


def test_approve_refuses_an_empty_diff_hash(
    gh: FakeGitHubClient, cfg: Config, store: ProposalStore
) -> None:
    preview = service.propose_edit(
        A_PERSON, gh, cfg, store, "how-we-write-briefs", {BRIEFS: UPDATED_BRIEFS}
    )

    with pytest.raises(DiffMismatch):
        service.approve(A_PERSON, gh, cfg, store, preview.proposal_id, "   ")


def test_approve_refuses_the_diff_hash_of_a_different_change(
    gh: FakeGitHubClient, cfg: Config, store: ProposalStore
) -> None:
    """An approval for one change must not carry over to another one."""
    first = service.propose_edit(
        A_PERSON, gh, cfg, store, "how-we-write-briefs", {BRIEFS: UPDATED_BRIEFS}
    )
    other_text = skill_text("weekly-report", "How the weekly report is put together.", "New.")
    second = service.propose_edit(
        A_PERSON,
        gh,
        cfg,
        store,
        "weekly-report",
        {"plugins/alpha-pack/skills/weekly-report/SKILL.md": other_text},
    )

    with pytest.raises(DiffMismatch):
        service.approve(A_PERSON, gh, cfg, store, second.proposal_id, first.diff_hash)


def test_approve_refuses_when_the_stored_change_was_altered_after_it_was_shown(
    gh: FakeGitHubClient, cfg: Config, store: ProposalStore
) -> None:
    preview = service.propose_edit(
        A_PERSON, gh, cfg, store, "how-we-write-briefs", {BRIEFS: UPDATED_BRIEFS}
    )
    held = store.get(preview.proposal_id)
    held.files[BRIEFS] = skill_text(
        "how-we-write-briefs", "How to write a brief.", "Something nobody approved."
    )
    before = gh.files_on("main")

    with pytest.raises(DiffMismatch):
        service.approve(A_PERSON, gh, cfg, store, preview.proposal_id, preview.diff_hash)

    assert gh.files_on("main") == before


def test_approve_refuses_when_nobody_knows_who_is_asking(
    gh: FakeGitHubClient, cfg: Config, store: ProposalStore
) -> None:
    """An unattributed publish is worse than no publish, so this one never happens."""
    preview = service.propose_edit(
        A_PERSON, gh, cfg, store, "how-we-write-briefs", {BRIEFS: UPDATED_BRIEFS}
    )
    before = gh.files_on("main")

    with pytest.raises(NotAuthorized) as refused:
        service.approve(ANONYMOUS, gh, cfg, store, preview.proposal_id, preview.diff_hash)

    assert "who you are" in refused.value.user_message.lower()
    assert gh.files_on("main") == before
    assert gh.pull_requests == {}


def test_approve_refuses_an_actor_with_a_name_but_no_email(
    gh: FakeGitHubClient, cfg: Config, store: ProposalStore
) -> None:
    half_known = Actor(name="A Person", email="")
    preview = service.propose_edit(
        A_PERSON, gh, cfg, store, "how-we-write-briefs", {BRIEFS: UPDATED_BRIEFS}
    )

    with pytest.raises(NotAuthorized):
        service.approve(half_known, gh, cfg, store, preview.proposal_id, preview.diff_hash)


def test_approve_refuses_a_change_prepared_by_nobody(
    gh: FakeGitHubClient, cfg: Config, store: ProposalStore
) -> None:
    """Signing in only at the moment of approval must not launder an anonymous draft."""
    preview = service.propose_edit(
        ANONYMOUS, gh, cfg, store, "how-we-write-briefs", {BRIEFS: UPDATED_BRIEFS}
    )
    before = gh.files_on("main")

    with pytest.raises(NotAuthorized):
        service.approve(A_PERSON, gh, cfg, store, preview.proposal_id, preview.diff_hash)

    assert gh.files_on("main") == before


def test_a_change_can_only_be_approved_once(
    gh: FakeGitHubClient, cfg: Config, store: ProposalStore
) -> None:
    preview = service.propose_edit(
        A_PERSON, gh, cfg, store, "how-we-write-briefs", {BRIEFS: UPDATED_BRIEFS}
    )
    service.approve(A_PERSON, gh, cfg, store, preview.proposal_id, preview.diff_hash)

    with pytest.raises(ProposalNotFound):
        service.approve(A_PERSON, gh, cfg, store, preview.proposal_id, preview.diff_hash)


def test_approve_publishes_into_the_collection_that_owns_the_skill(
    gh: FakeGitHubClient, cfg: Config, store: ProposalStore
) -> None:
    updated = skill_text(
        "handover-notes", "What to write when handing work over.", "Name the next owner."
    )
    answer = publish_a_change(
        gh, cfg, store, files={HANDOVER: updated}, skill_name="handover-notes"
    )

    assert gh.files_on("main")[HANDOVER] == updated
    assert version_of(gh, BETA_MANIFEST) == "2.0.1"
    assert version_of(gh, ALPHA_MANIFEST) == "1.4.2"
    assert "beta-pack" in answer


def test_approve_never_promises_a_precise_moment_for_delivery(
    gh: FakeGitHubClient, cfg: Config, store: ProposalStore
) -> None:
    answer = publish_a_change(gh, cfg, store)

    lowered = answer.lower()
    assert "about" in lowered or "around" in lowered
    assert "can take longer" in lowered or "sometimes longer" in lowered
    for over_promise in ("exactly", "guaranteed", "immediately", "instantly"):
        assert over_promise not in lowered
    # The sentence must read cleanly, not double up its own hedging.
    assert "around usually" not in lowered
    assert lowered.count("sometimes longer") <= 1


def test_two_people_can_each_publish_and_each_gets_their_own_credit(
    gh: FakeGitHubClient, cfg: Config, store: ProposalStore
) -> None:
    publish_a_change(gh, cfg, store, actor=A_PERSON)
    second = skill_text(
        "how-we-write-briefs", "How to write a brief.", "And say who signed it off."
    )
    publish_a_change(gh, cfg, store, actor=ANOTHER_PERSON, files={BRIEFS: second})

    authors = {c.author_name for c in gh.commit_list() if BRIEFS in c.changed}
    assert {"A Person", "Another Person"} <= authors
    assert version_of(gh, ALPHA_MANIFEST) == "1.4.4"


# ==============================================================================================
# Who asked and who approved are two separate facts
# ==============================================================================================


def published_commit(gh: FakeGitHubClient, path: str = BRIEFS):
    """The commit that carried one change onto its own branch."""
    written = [c for c in gh.commit_list() if path in c.changed and c.branch != "main"]
    assert written, "nothing was committed for that change"
    return written[-1]


def trailer_lines(message: str, label: str) -> list[str]:
    return [line.strip() for line in message.splitlines() if line.strip().startswith(label)]


def test_one_person_can_approve_a_change_another_person_asked_for(
    gh: FakeGitHubClient, cfg: Config, store: ProposalStore
) -> None:
    """A second pair of eyes is a good way to work, so it is allowed."""
    preview = service.propose_edit(
        A_PERSON, gh, cfg, store, "how-we-write-briefs", {BRIEFS: UPDATED_BRIEFS}
    )

    answer = service.approve(
        ANOTHER_PERSON, gh, cfg, store, preview.proposal_id, preview.diff_hash
    )

    assert gh.files_on("main")[BRIEFS] == UPDATED_BRIEFS
    assert "1.4.3" in answer


def test_a_change_approved_by_someone_else_records_both_people(
    gh: FakeGitHubClient, cfg: Config, store: ProposalStore
) -> None:
    """The history must never say a publish was let through by somebody who did not do it."""
    preview = service.propose_edit(
        A_PERSON, gh, cfg, store, "how-we-write-briefs", {BRIEFS: UPDATED_BRIEFS}
    )

    service.approve(ANOTHER_PERSON, gh, cfg, store, preview.proposal_id, preview.diff_hash)

    commit = published_commit(gh)
    assert trailer_lines(commit.message, "Requested-By:") == [
        "Requested-By: A Person <a.person@example.test>"
    ]
    assert trailer_lines(commit.message, "Approved-By:") == [
        "Approved-By: Another Person <another@example.test>"
    ]
    assert "A Person" in commit.message
    assert "Another Person" in commit.message


def test_a_change_approved_by_the_person_who_asked_records_one_name(
    gh: FakeGitHubClient, cfg: Config, store: ProposalStore
) -> None:
    """One person doing both needs one line saying so, not two saying the same thing."""
    preview = service.propose_edit(
        A_PERSON, gh, cfg, store, "how-we-write-briefs", {BRIEFS: UPDATED_BRIEFS}
    )

    service.approve(A_PERSON, gh, cfg, store, preview.proposal_id, preview.diff_hash)

    commit = published_commit(gh)
    assert trailer_lines(commit.message, "Requested-By:") == [
        "Requested-By: A Person <a.person@example.test>"
    ]
    assert trailer_lines(commit.message, "Approved-By:") == []
    assert "Another Person" not in commit.message


def test_the_author_of_a_change_stays_the_person_who_asked_for_it(
    gh: FakeGitHubClient, cfg: Config, store: ProposalStore
) -> None:
    """Approving somebody else's change must not take the credit for asking for it."""
    preview = service.propose_edit(
        A_PERSON, gh, cfg, store, "how-we-write-briefs", {BRIEFS: UPDATED_BRIEFS}
    )

    service.approve(ANOTHER_PERSON, gh, cfg, store, preview.proposal_id, preview.diff_hash)

    commit = published_commit(gh)
    assert commit.author_name == "A Person"
    assert commit.author_email == "a.person@example.test"
    assert commit.committer_name == APP_COMMITTER_NAME
    assert commit.committer_email == APP_COMMITTER_EMAIL


def test_the_confirmation_names_both_people_when_they_are_not_the_same_person(
    gh: FakeGitHubClient, cfg: Config, store: ProposalStore
) -> None:
    preview = service.propose_edit(
        A_PERSON, gh, cfg, store, "how-we-write-briefs", {BRIEFS: UPDATED_BRIEFS}
    )

    answer = service.approve(
        ANOTHER_PERSON, gh, cfg, store, preview.proposal_id, preview.diff_hash
    )

    assert "Another Person approved a change requested by A Person." in answer
    assert "your name is on it" not in answer


def test_the_confirmation_does_not_invent_a_second_person(
    gh: FakeGitHubClient, cfg: Config, store: ProposalStore
) -> None:
    preview = service.propose_edit(
        A_PERSON, gh, cfg, store, "how-we-write-briefs", {BRIEFS: UPDATED_BRIEFS}
    )

    answer = service.approve(A_PERSON, gh, cfg, store, preview.proposal_id, preview.diff_hash)

    assert "approved a change requested by" not in answer
    assert "your name is on it" in answer


def test_the_written_record_says_in_words_who_approved_whose_change(
    gh: FakeGitHubClient, cfg: Config, store: ProposalStore
) -> None:
    """Somebody reading the history later should not have to decode a label to follow it."""
    preview = service.propose_edit(
        A_PERSON, gh, cfg, store, "how-we-write-briefs", {BRIEFS: UPDATED_BRIEFS}
    )

    service.approve(ANOTHER_PERSON, gh, cfg, store, preview.proposal_id, preview.diff_hash)

    sentence = "Another Person approved a change requested by A Person."
    assert sentence in published_commit(gh).message
    assert sentence in gh.pull_requests[1].body


def test_the_same_person_under_a_different_spelling_is_still_one_person(
    gh: FakeGitHubClient, cfg: Config, store: ProposalStore
) -> None:
    """The email settles it, so a nickname does not turn one person into two."""
    preview = service.propose_edit(
        A_PERSON, gh, cfg, store, "how-we-write-briefs", {BRIEFS: UPDATED_BRIEFS}
    )
    same_person_different_spelling = Actor(name="A. Person", email="A.Person@Example.test")

    answer = service.approve(
        same_person_different_spelling, gh, cfg, store, preview.proposal_id, preview.diff_hash
    )

    assert "approved a change requested by" not in answer
    assert trailer_lines(published_commit(gh).message, "Approved-By:") == []


def test_someone_else_cannot_approve_without_being_signed_in(
    gh: FakeGitHubClient, cfg: Config, store: ProposalStore
) -> None:
    """Allowing a second pair of eyes must not open a door for nobody at all."""
    preview = service.propose_edit(
        A_PERSON, gh, cfg, store, "how-we-write-briefs", {BRIEFS: UPDATED_BRIEFS}
    )
    before = gh.files_on("main")

    with pytest.raises(NotAuthorized):
        service.approve(ANONYMOUS, gh, cfg, store, preview.proposal_id, preview.diff_hash)

    assert gh.files_on("main") == before
    assert gh.pull_requests == {}


def test_someone_else_cannot_approve_with_the_wrong_fingerprint(
    gh: FakeGitHubClient, cfg: Config, store: ProposalStore
) -> None:
    preview = service.propose_edit(
        A_PERSON, gh, cfg, store, "how-we-write-briefs", {BRIEFS: UPDATED_BRIEFS}
    )
    before = gh.files_on("main")

    with pytest.raises(DiffMismatch):
        service.approve(
            ANOTHER_PERSON, gh, cfg, store, preview.proposal_id, "0" * 64
        )

    assert gh.files_on("main") == before
    assert gh.pull_requests == {}


def test_the_approve_tool_description_is_honest_about_what_it_checks(
    gh: FakeGitHubClient, cfg: Config, store: ProposalStore
) -> None:
    """Anyone wiring this up must not read the fingerprint as proof a person agreed.

    The fingerprint proves the text going out is the text that was shown. It cannot
    prove a person was involved, because the two references travel in the conversation
    itself. The only place a real approval can be collected is the thing hosting the
    tools, so the description has to say so.
    """
    description = service.APPROVE_TOOL_DESCRIPTION.lower()

    assert "do not prove" in description
    assert "shown" in description
    assert "said yes" in description or "confirmation" in description
    assert "outside this tool" in description


def test_the_module_says_plainly_that_the_fingerprint_is_not_an_approval(
    gh: FakeGitHubClient, cfg: Config, store: ProposalStore
) -> None:
    text = (service.__doc__ or "").lower()

    assert "does not prove that a person approved" in text
    assert "conversation" in text


# ==============================================================================================
# history
# ==============================================================================================


def test_history_says_so_plainly_when_a_skill_has_never_changed(
    gh: FakeGitHubClient, cfg: Config
) -> None:
    """An empty history is an answer, not a silence."""

    class NoRecordedHistory(FakeGitHubClient):
        def list_commits(self, path: str, limit: int) -> list[dict]:
            return []

    quiet = NoRecordedHistory()
    quiet.seed(gh.files_on("main"))

    answer = service.history(A_PERSON, quiet, cfg, "how-we-write-briefs")

    assert "nothing to show" in answer.lower()


def test_history_starts_from_the_moment_the_skill_was_added(
    gh: FakeGitHubClient, cfg: Config
) -> None:
    answer = service.history(A_PERSON, gh, cfg, "how-we-write-briefs")

    assert "how-we-write-briefs" in answer
    assert "alpha-pack" in answer


def test_history_names_the_person_who_asked_for_each_change(
    gh: FakeGitHubClient, cfg: Config, store: ProposalStore
) -> None:
    publish_a_change(gh, cfg, store)

    answer = service.history(A_PERSON, gh, cfg, "how-we-write-briefs")

    assert "A Person" in answer
    assert "alpha-pack" in answer


def test_history_refuses_a_skill_name_that_is_in_no_collection(
    gh: FakeGitHubClient, cfg: Config
) -> None:
    with pytest.raises(SkillNotFound):
        service.history(A_PERSON, gh, cfg, "no-such-skill")


# ==============================================================================================
# revert
# ==============================================================================================


def test_revert_prepares_a_change_back_to_an_earlier_reading(
    gh: FakeGitHubClient, cfg: Config, store: ProposalStore
) -> None:
    original = gh.files_on("main")[BRIEFS]
    starting_point = gh.get_ref_sha("main")
    publish_a_change(gh, cfg, store)

    preview = service.revert(
        A_PERSON, gh, cfg, store, "how-we-write-briefs", starting_point
    )

    assert store.get(preview.proposal_id).files[BRIEFS] == original
    assert "nothing will be erased" in preview.message.lower()


def test_revert_publishes_a_new_forward_change_rather_than_undoing_history(
    gh: FakeGitHubClient, cfg: Config, store: ProposalStore
) -> None:
    original = gh.files_on("main")[BRIEFS]
    starting_point = gh.get_ref_sha("main")
    publish_a_change(gh, cfg, store)
    commits_before = len(gh.commit_list())

    preview = service.revert(A_PERSON, gh, cfg, store, "how-we-write-briefs", starting_point)
    service.approve(A_PERSON, gh, cfg, store, preview.proposal_id, preview.diff_hash)

    assert gh.files_on("main")[BRIEFS] == original
    assert len(gh.commit_list()) > commits_before
    assert version_of(gh, ALPHA_MANIFEST) == "1.4.4"
    assert len(gh.pull_requests) == 2


def test_revert_refuses_without_a_reference_to_go_back_to(
    gh: FakeGitHubClient, cfg: Config, store: ProposalStore
) -> None:
    with pytest.raises(SkillNotFound):
        service.revert(A_PERSON, gh, cfg, store, "how-we-write-briefs", "   ")


# ==============================================================================================
# Adding a skill that is not there yet
# ==============================================================================================


def test_a_new_skill_is_added_to_the_collection_its_file_names_point_at(
    gh: FakeGitHubClient, cfg: Config, store: ProposalStore
) -> None:
    """A skill that does not exist yet cannot be looked up, so the file names decide."""
    new_file = "library/beta-pack/skills/brand-new/SKILL.md"
    text = skill_text("brand-new", "Something nobody has written down before.")

    answer = publish_a_change(
        gh, cfg, store, files={new_file: text}, skill_name="brand-new"
    )

    assert gh.files_on("main")[new_file] == text
    assert version_of(gh, BETA_MANIFEST) == "2.0.1"
    assert version_of(gh, ALPHA_MANIFEST) == "1.4.2"
    assert "beta-pack" in answer


def test_a_new_skill_whose_files_name_no_collection_asks_rather_than_guesses(
    gh: FakeGitHubClient, cfg: Config, store: ProposalStore
) -> None:
    with pytest.raises(LibrarianError) as refused:
        service.propose_edit(
            A_PERSON,
            gh,
            cfg,
            store,
            "brand-new",
            {"skills/brand-new/SKILL.md": skill_text("brand-new", "Nowhere in particular.")},
        )

    message = refused.value.user_message
    assert "alpha-pack" in message
    assert "beta-pack" in message
    assert store.active_ids() == []


# ==============================================================================================
# The last gate before a write
# ==============================================================================================


def test_the_write_gate_refuses_a_collection_folder_the_manifest_smuggled_in(
    cfg: Config, store: ProposalStore
) -> None:
    """The folder every safe path is measured against is repository content, not a setting.

    Anybody who can open a pull request can change it, so the last gate before a write
    checks it as hard as the first gate did rather than trusting it because it arrived
    from inside the repository.
    """
    from librarian import publisher
    from librarian.proposals import make_proposal

    odd = PluginSpec(
        name="odd-pack",
        plugin_dir="plugins/odd pack",
        version="1.0.0",
        skills=(("a-skill", "Something."),),
    )
    gh = library(odd)
    skill_file = "plugins/odd pack/skills/a-skill/SKILL.md"
    proposal = make_proposal(
        skill_name="a-skill",
        requested_by="A Person <a.person@example.test>",
        base_sha=gh.get_ref_sha("main"),
        files={skill_file: skill_text("a-skill", "Something else.")},
        diff_text="",
        plain_summary="A change.",
    )
    before = gh.files_on("main")

    with pytest.raises(UnsafePath):
        publisher.publish(gh, cfg, proposal)

    assert gh.files_on("main") == before
    assert gh.pull_requests == {}


# ==============================================================================================
# Two people publishing at the same time
# ==============================================================================================


WEEKLY = "plugins/alpha-pack/skills/weekly-report/SKILL.md"


def version_parts(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def someone_else_publishes(gh: FakeGitHubClient, version: str, weekly_text: str) -> None:
    """Another person's change lands in the shared copy, complete with its own version move.

    This is what a real competing publish leaves behind: their wording, their collection's
    version number moved on, and the library's own list of collections moved with it.
    """
    gh.move_default_branch(
        {
            WEEKLY: weekly_text,
            ALPHA_MANIFEST: plugin_manifest_text("alpha-pack", version),
            MARKETPLACE_MANIFEST: marketplace_manifest_text(
                dataclasses.replace(ALPHA, version=version), BETA
            ),
        }
    )


def test_a_change_overtaken_mid_publish_still_moves_the_version_past_the_other_one(
    gh: FakeGitHubClient, cfg: Config, store: ProposalStore
) -> None:
    """Two people publish at once, and the second one does not go out silently.

    Both start from version 1.4.2 and both work out 1.4.3. The other person gets there
    first, so by the time this change is ready to merge the shared copy already says 1.4.3.
    Merging on that number would put this wording into the repository under a version number
    that has already been delivered, and Claude would keep serving the copy people already
    have. Nobody would see the new wording and nobody would be told anything was wrong.

    So the version number is worked out again from the shared copy in the moment before the
    change is put forward, and this change goes out as 1.4.4 instead. That is the guard this
    test exists for: take it away and the wording lands under a number that never moved past
    the other person's.
    """
    other_persons_weekly = skill_text(
        "weekly-report", "How the weekly report is put together.", "Somebody else's new wording."
    )
    preview = service.propose_edit(
        A_PERSON, gh, cfg, store, "how-we-write-briefs", {BRIEFS: UPDATED_BRIEFS}, "Because."
    )

    # The other person's change lands the moment this one saves its work, which is exactly
    # when a real race arrives: both started from the same copy and one of them got there first.
    gh.after_next(
        "commit_files", lambda client: someone_else_publishes(client, "1.4.3", other_persons_weekly)
    )

    answer = service.approve(A_PERSON, gh, cfg, store, preview.proposal_id, preview.diff_hash)

    assert gh.hooks_fired == ["commit_files"], "the race never happened, so this proves nothing"

    landed = version_of(gh, ALPHA_MANIFEST)
    assert landed == "1.4.4"
    assert version_parts(landed) > version_parts("1.4.3"), (
        "the wording went out under a version number that had already been delivered"
    )

    # Both settings files moved together, or the change reaches nobody.
    assert entry_named(gh, "alpha-pack")["version"] == landed
    assert landed in answer

    # This change arrived, and the other person's change was not quietly undone by it.
    assert gh.files_on("main")[BRIEFS] == UPDATED_BRIEFS
    assert gh.files_on("main")[WEEKLY] == other_persons_weekly

    # Nobody else's collection was dragged along.
    assert version_of(gh, BETA_MANIFEST) == "2.0.0"
    assert entry_named(gh, "beta-pack")["version"] == "2.0.0"

    # The change people read before it was merged names the version that really went out,
    # rather than the number that was worked out before the other person got there first.
    pull_request = gh.pull_requests[1]
    assert landed in pull_request.title
    assert landed in pull_request.body
    assert "1.4.3" not in pull_request.title


def test_the_change_overtaken_mid_publish_is_still_merged_only_as_approved(
    gh: FakeGitHubClient, cfg: Config, store: ProposalStore
) -> None:
    """Working out a new version number is still a merge of exactly the change that was saved."""
    preview = service.propose_edit(
        A_PERSON, gh, cfg, store, "how-we-write-briefs", {BRIEFS: UPDATED_BRIEFS}, "Because."
    )
    gh.after_next(
        "commit_files",
        lambda client: someone_else_publishes(
            client, "1.4.3", skill_text("weekly-report", "How the weekly report is put together.")
        ),
    )

    service.approve(A_PERSON, gh, cfg, store, preview.proposal_id, preview.diff_hash)

    # One merge, and it published the saved change that carries the recomputed version number.
    assert gh.merge_attempts == 1
    merged = [pull for pull in gh.pull_requests.values() if pull.merged]
    assert len(merged) == 1
    assert merged[0].base == "main"
    # The commit that was merged is the last one made on the working copy, not an earlier one.
    # The working copy itself is taken away once the publish has worked, so what was merged is
    # proved from the record of the merge rather than from a branch that is no longer there.
    saved_on_the_working_copy = [
        commit.sha for commit in gh.commit_list() if commit.branch == merged[0].head
    ]
    assert saved_on_the_working_copy, "nothing was ever saved on the working copy"
    assert merged[0].merged_head_sha == saved_on_the_working_copy[-1]
    assert merged[0].merged_head_sha == gh.commits[gh.branches["main"]].parents[-1]
    assert merged[0].head not in gh.branches, "the working copy was left behind after publishing"


SOMEONE_ELSES_BRIEFS = skill_text(
    "how-we-write-briefs",
    "How to write a brief.",
    "Open with the client name and the date they asked.",
)


def someone_else_publishes_the_same_skill(
    gh: FakeGitHubClient, version: str, briefs_text: str
) -> None:
    """Another person's change lands, and it is to the very skill this change is editing.

    This is the case that destroys work rather than the case that merely gets in the way. The
    wording that was agreed to is the whole of the file, so putting it on top of theirs replaces
    what they wrote instead of joining it. Their version move comes with it, exactly as a real
    publish would, so the change in flight has to work its own number out again and start from
    where the shared copy now stands.
    """
    gh.move_default_branch(
        {
            BRIEFS: briefs_text,
            ALPHA_MANIFEST: plugin_manifest_text("alpha-pack", version),
            MARKETPLACE_MANIFEST: marketplace_manifest_text(
                dataclasses.replace(ALPHA, version=version), BETA
            ),
        }
    )


def test_a_publish_never_writes_over_someone_elses_wording_for_the_same_skill(
    gh: FakeGitHubClient, cfg: Config, store: ProposalStore
) -> None:
    """Two people edit the same skill at once, and neither one's work disappears in silence.

    Somebody else publishes their own wording for this very skill while this change is saving
    its work, and moves the version number with it, so this change has to start again from where
    the shared copy now stands. Starting again means writing the agreed wording, which is the
    whole file, straight over theirs. Nothing would clash and nothing would fail: their words
    would simply be gone, and the person who took them away would be told the publish worked.

    So it does not happen at all. The publish stops before anything is merged, their wording is
    still the wording in the shared skills, and the person is told plainly what happened and that
    asking again will start from the latest copy.
    """
    preview = service.propose_edit(
        A_PERSON, gh, cfg, store, "how-we-write-briefs", {BRIEFS: UPDATED_BRIEFS}, "Because."
    )
    gh.after_next(
        "commit_files",
        lambda client: someone_else_publishes_the_same_skill(
            client, "1.4.3", SOMEONE_ELSES_BRIEFS
        ),
    )

    with pytest.raises(PublishFailed) as refused:
        service.approve(A_PERSON, gh, cfg, store, preview.proposal_id, preview.diff_hash)

    assert gh.hooks_fired == ["commit_files"], "the race never happened, so this proves nothing"

    # The whole point. Their words are still the words everybody reads.
    assert gh.files_on("main")[BRIEFS] == SOMEONE_ELSES_BRIEFS
    assert gh.files_on("main")[BRIEFS] != UPDATED_BRIEFS

    # And nothing was merged on the way to finding that out, so there was never a moment where
    # their wording was off the shared copy.
    assert gh.merge_attempts == 0
    assert [pull for pull in gh.pull_requests.values() if pull.merged] == []
    assert version_of(gh, ALPHA_MANIFEST) == "1.4.3"
    assert entry_named(gh, "alpha-pack")["version"] == "1.4.3"

    # The refusal comes before this change is ever put up for review, so there is nothing waiting
    # for anybody. Said as an emptiness rather than as "every one of them is withdrawn", because
    # that phrasing is quietly true of no changes at all and would prove nothing.
    assert gh.pull_requests == {}
    assert gh.withdrawn_pull_requests == []
    # And no working copy is left behind, so asking again starts from nothing.
    assert [name for name in gh.branches if name.startswith("librarian/")] == []

    message = refused.value.user_message
    assert "somebody else changed this skill" in message.lower()
    assert "nothing has been changed" in message.lower()
    assert "ask for the change again" in message.lower()


def test_a_file_this_change_adds_is_not_written_over_when_somebody_else_added_it_first(
    gh: FakeGitHubClient, cfg: Config, store: ProposalStore
) -> None:
    """A file this change creates can be created by somebody else in the very same gap.

    There is nothing to compare it against at the starting point, because it was not there then.
    That is exactly why it matters: writing it now would replace what they just published rather
    than add anything, and the whole thing would read as a perfectly clean publish.
    """
    theirs = "Keep the tone warm and plain.\n"
    preview = service.propose_edit(
        A_PERSON,
        gh,
        cfg,
        store,
        "how-we-write-briefs",
        {BRIEFS_NOTES: "The note this change would add.\n"},
        "Because.",
    )
    gh.after_next(
        "commit_files",
        lambda client: client.move_default_branch(
            {
                BRIEFS_NOTES: theirs,
                ALPHA_MANIFEST: plugin_manifest_text("alpha-pack", "1.4.3"),
                MARKETPLACE_MANIFEST: marketplace_manifest_text(
                    dataclasses.replace(ALPHA, version="1.4.3"), BETA
                ),
            }
        ),
    )

    with pytest.raises(PublishFailed) as refused:
        service.approve(A_PERSON, gh, cfg, store, preview.proposal_id, preview.diff_hash)

    assert gh.hooks_fired == ["commit_files"], "the race never happened, so this proves nothing"
    assert gh.files_on("main")[BRIEFS_NOTES] == theirs
    assert gh.merge_attempts == 0
    assert "somebody else changed this skill" in refused.value.user_message.lower()


# ==============================================================================================
# One answer describes one moment
# ==============================================================================================


def test_reading_a_skill_describes_one_moment_even_when_a_publish_lands_partway_through(
    gh: FakeGitHubClient, cfg: Config, store: ProposalStore
) -> None:
    """Reading a skill takes several looks at the library, and they have to agree with each other.

    Somebody else publishes while this reading is under way. If each look asked for the library
    by name it would be answered from wherever the library happened to stand at that instant, and
    the reply would be stitched together from two different moments: the wording from before the
    publish and the list of supporting notes from after it. Nothing in the reply would say so, and
    the person would be told about a note that does not go with the wording they were just shown.
    """
    a_note = "plugins/alpha-pack/skills/how-we-write-briefs/reference/tone.md"
    theirs = skill_text(
        "how-we-write-briefs", "How to write a brief.", "Their newer wording entirely."
    )
    gh.after_next(
        "get_file",
        lambda client: client.move_default_branch({BRIEFS: theirs, a_note: "Keep it warm.\n"}),
    )

    answer = service.read_skill(A_PERSON, gh, cfg, "how-we-write-briefs")

    assert gh.hooks_fired == ["get_file"], "the publish never landed, so this proves nothing"

    # Their publish really is on the shared copy, so there were two moments to choose between.
    assert gh.files_on("main")[BRIEFS] == theirs
    assert a_note in gh.files_on("main")

    # And the answer is entirely one of them: the wording from before their publish, and no
    # mention of the note that only exists after it.
    assert "Their newer wording entirely." not in answer
    assert "tone" not in answer
    assert "supporting notes" not in answer


def test_listing_skills_describes_one_moment_even_when_a_publish_lands_partway_through(
    gh: FakeGitHubClient, cfg: Config, store: ProposalStore
) -> None:
    """The list and the descriptions in it come from the same moment, or they disagree.

    Somebody else changes a description while the list is being put together. Asking for the
    library by name for each description would answer some of them from before their publish and
    some from after it, and the list would quietly describe a library that never existed.
    """
    gh.after_next(
        "list_dir",
        lambda client: client.move_default_branch(
            {BRIEFS: skill_text("how-we-write-briefs", "A description they published instead.")}
        ),
    )

    answer = service.list_skills(A_PERSON, gh, cfg)

    assert gh.hooks_fired == ["list_dir"], "the publish never landed, so this proves nothing"

    # Their change really is on the shared copy, so there were two moments to choose between.
    assert "A description they published instead." in gh.files_on("main")[BRIEFS]

    # And the list describes one of them throughout.
    assert "A description they published instead." not in answer
    assert "How to write a brief." in answer


def test_a_third_publish_landing_between_review_and_merge_cannot_ship_a_used_version(
    gh: FakeGitHubClient, cfg: Config, store: ProposalStore
) -> None:
    """The version number is checked against where the shared copy really was, not a stale note.

    Two other people publish while this change is being prepared, one after the other. The
    first arrives while this change is saving its work, so this change moves itself on to
    1.4.4 to sit past their 1.4.3. The second arrives later, in the gap between this change
    being put up for review and being merged, and takes the shared copy to 1.4.4 as well.

    By the time this change merges, 1.4.4 has already been delivered to everyone. Merging
    now leaves the shared copy still saying 1.4.4, so every person who already fetched 1.4.4
    keeps the wording they have and never sees this change. That is the silent failure this
    whole module exists to prevent, so it has to be reported rather than reported as success.

    The trap it guards against is comparing what landed with a version number noted down
    earlier. Measured against the stale 1.4.3 the merge looks like a step forward. Measured
    against what the shared copy actually held in the moment before the merge, it is not.
    """
    weekly = skill_text("weekly-report", "How the weekly report is put together.")
    preview = service.propose_edit(
        A_PERSON, gh, cfg, store, "how-we-write-briefs", {BRIEFS: UPDATED_BRIEFS}, "Because."
    )

    # Somebody else gets there first, so this change moves itself on to 1.4.4.
    gh.after_next("commit_files", lambda client: someone_else_publishes(client, "1.4.3", weekly))
    # And a third person lands 1.4.4 in the gap before this one is merged.
    gh.after_next("open_pr", lambda client: someone_else_publishes(client, "1.4.4", weekly))

    with pytest.raises(PublishFailed) as refused:
        service.approve(A_PERSON, gh, cfg, store, preview.proposal_id, preview.diff_hash)

    assert gh.hooks_fired == ["commit_files", "open_pr"], (
        "the race never happened, so this proves nothing"
    )
    assert "reach everyone" in refused.value.user_message
