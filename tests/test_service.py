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
