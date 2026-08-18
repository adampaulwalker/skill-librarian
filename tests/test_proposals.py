"""Tests for the draft changes that wait for a person to approve them.

Two things are load bearing here.

The fingerprint, ``diff_hash``, is what binds an approval to the exact content somebody
was shown. If it can be made to stay the same while the content moves, then a person can
approve one thing and a different thing can be published, and nobody would ever be told.
So the tests below try to move a byte, a file name, a file count and the commit point the
change was measured against, and insist the fingerprint moves every single time.

The ageing out is the other one. A draft that has sat unapproved for too long has to be
refused as too old, not as never seen, because the two mean completely different things to
the person reading the answer. The clock is handed into the store so that can be proved
without any test ever sleeping.
"""

from __future__ import annotations

import pytest

from librarian.errors import ProposalExpired, ProposalNotFound
from librarian.proposals import (
    Proposal,
    ProposalStore,
    canonical_proposal_bytes,
    compute_diff_hash,
    diff_hash_matches,
    make_proposal,
    new_proposal_id,
    recompute_diff_hash,
)

BASE_SHA = "0" * 40
OTHER_SHA = "1" * 40
SKILL_FILE = "plugins/alpha/skills/one/SKILL.md"
NOTES_FILE = "plugins/alpha/skills/one/reference/notes.md"

FILES = {SKILL_FILE: "---\nname: one\ndescription: First.\n---\n\nBody.\n"}


class ManualClock:
    """A clock a test moves by hand, so nothing ever has to wait in real time."""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def a_proposal(
    files: dict[str, str] | None = None,
    base_sha: str = BASE_SHA,
    proposal_id: str = "prop-one",
    clock: ManualClock | None = None,
) -> Proposal:
    return make_proposal(
        skill_name="one",
        requested_by="A Person <person@example.test>",
        base_sha=base_sha,
        files=FILES if files is None else files,
        diff_text="--- a\n+++ b\n",
        plain_summary="One line changed.",
        proposal_id=proposal_id,
        clock=clock or ManualClock(),
    )


# ==============================================================================================
# The fingerprint moves whenever the content does
# ==============================================================================================


def test_diff_hash_changes_when_a_single_byte_of_a_file_changes() -> None:
    original = compute_diff_hash(BASE_SHA, {SKILL_FILE: "Body.\n"})
    one_byte_later = compute_diff_hash(BASE_SHA, {SKILL_FILE: "Bodz.\n"})

    assert original != one_byte_later


def test_diff_hash_changes_for_whitespace_nobody_would_notice_by_eye() -> None:
    """A trailing space is invisible to a reader, so the fingerprint has to catch it."""
    assert compute_diff_hash(BASE_SHA, {SKILL_FILE: "Body.\n"}) != compute_diff_hash(
        BASE_SHA, {SKILL_FILE: "Body. \n"}
    )


def test_diff_hash_changes_when_a_file_is_added() -> None:
    one_file = compute_diff_hash(BASE_SHA, {SKILL_FILE: "Body.\n"})
    two_files = compute_diff_hash(BASE_SHA, {SKILL_FILE: "Body.\n", NOTES_FILE: "Notes.\n"})

    assert one_file != two_files


def test_diff_hash_changes_when_a_file_is_removed() -> None:
    two_files = compute_diff_hash(BASE_SHA, {SKILL_FILE: "Body.\n", NOTES_FILE: "Notes.\n"})
    one_file = compute_diff_hash(BASE_SHA, {NOTES_FILE: "Notes.\n"})

    assert two_files != one_file


def test_diff_hash_changes_when_a_file_is_renamed_but_its_text_is_not() -> None:
    here = compute_diff_hash(BASE_SHA, {SKILL_FILE: "Body.\n"})
    there = compute_diff_hash(BASE_SHA, {NOTES_FILE: "Body.\n"})

    assert here != there


def test_diff_hash_changes_when_the_commit_it_was_measured_against_changes() -> None:
    """Same words, different starting point, so it is a different change."""
    assert compute_diff_hash(BASE_SHA, FILES) != compute_diff_hash(OTHER_SHA, FILES)


def test_diff_hash_does_not_depend_on_the_order_files_were_added() -> None:
    one_way = compute_diff_hash(BASE_SHA, {SKILL_FILE: "a\n", NOTES_FILE: "b\n"})
    other_way = compute_diff_hash(BASE_SHA, {NOTES_FILE: "b\n", SKILL_FILE: "a\n"})

    assert one_way == other_way


def test_a_file_name_cannot_be_shuffled_into_a_file_body() -> None:
    """The layout is length prefixed, so where a name stops is never a matter of opinion.

    Without that, a file called "a" holding "bc" and a file called "ab" holding "c" would
    lay out as the same run of bytes, and one could be swapped for the other after approval.
    """
    first = compute_diff_hash(BASE_SHA, {"a": "bc"})
    second = compute_diff_hash(BASE_SHA, {"ab": "c"})

    assert first != second
    assert canonical_proposal_bytes(BASE_SHA, {"a": "bc"}) != canonical_proposal_bytes(
        BASE_SHA, {"ab": "c"}
    )


def test_the_same_content_always_fingerprints_the_same_way() -> None:
    assert compute_diff_hash(BASE_SHA, FILES) == compute_diff_hash(BASE_SHA, dict(FILES))


def test_a_proposal_carries_the_fingerprint_of_its_own_contents() -> None:
    proposal = a_proposal()

    assert proposal.diff_hash == compute_diff_hash(proposal.base_sha, proposal.files)
    assert recompute_diff_hash(proposal) == proposal.diff_hash
    assert diff_hash_matches(proposal)
    assert diff_hash_matches(proposal, proposal.diff_hash)


def test_a_proposal_whose_files_were_swapped_no_longer_matches_its_fingerprint() -> None:
    """The stored fingerprint is checked against the stored files, not trusted on sight."""
    tampered = Proposal(
        id="prop-one",
        skill_name="one",
        requested_by="A Person <person@example.test>",
        base_sha=BASE_SHA,
        files={SKILL_FILE: "Something else entirely.\n"},
        diff_text="",
        plain_summary="",
        diff_hash=compute_diff_hash(BASE_SHA, FILES),
        created_at=0.0,
    )

    assert not diff_hash_matches(tampered)


def test_an_older_approval_is_not_accepted_for_a_newer_draft() -> None:
    first = a_proposal(proposal_id="prop-one")
    second = a_proposal(files={SKILL_FILE: "Different.\n"}, proposal_id="prop-two")

    assert not diff_hash_matches(second, first.diff_hash)


def test_a_proposal_takes_its_own_copy_of_the_files_it_was_given() -> None:
    """Changing the caller's dictionary afterwards must not change what was approved."""
    handed_in = {SKILL_FILE: "Body.\n"}
    proposal = a_proposal(files=handed_in)
    handed_in[SKILL_FILE] = "Rewritten after the fact.\n"

    assert proposal.files[SKILL_FILE] == "Body.\n"
    assert diff_hash_matches(proposal)


# ==============================================================================================
# Two proposals never collide
# ==============================================================================================


def test_two_identical_drafts_still_get_different_identifiers() -> None:
    first = make_proposal("one", "A <a@example.test>", BASE_SHA, FILES, "", "")
    second = make_proposal("one", "A <a@example.test>", BASE_SHA, FILES, "", "")

    assert first.id != second.id


def test_a_thousand_identifiers_are_all_different() -> None:
    minted = {new_proposal_id() for _ in range(1000)}

    assert len(minted) == 1000


def test_two_drafts_held_at_once_do_not_overwrite_each_other() -> None:
    clock = ManualClock()
    store = ProposalStore(ttl_seconds=900, clock=clock)
    first = a_proposal(proposal_id="prop-one", clock=clock)
    second = a_proposal(
        files={SKILL_FILE: "Different.\n"}, proposal_id="prop-two", clock=clock
    )

    store.put(first)
    store.put(second)

    assert store.get("prop-one").files == first.files
    assert store.get("prop-two").files == second.files
    assert store.active_ids() == ["prop-one", "prop-two"]


# ==============================================================================================
# Ageing out, proved without waiting
# ==============================================================================================


def test_a_draft_is_returned_while_it_is_still_fresh() -> None:
    clock = ManualClock()
    store = ProposalStore(ttl_seconds=900, clock=clock)
    store.put(a_proposal(clock=clock))

    clock.advance(899)

    assert store.get("prop-one").id == "prop-one"


def test_a_draft_that_sat_too_long_is_refused_as_too_old_not_as_unknown() -> None:
    """The two answers mean different things to the reader, so the right one has to come back."""
    clock = ManualClock()
    store = ProposalStore(ttl_seconds=900, clock=clock)
    store.put(a_proposal(clock=clock))

    clock.advance(901)

    with pytest.raises(ProposalExpired) as refused:
        store.get("prop-one")

    assert not isinstance(refused.value, ProposalNotFound)
    assert "too long" in refused.value.user_message.lower()


def test_a_draft_expires_exactly_on_its_deadline_rather_than_a_moment_after() -> None:
    clock = ManualClock()
    store = ProposalStore(ttl_seconds=900, clock=clock)
    store.put(a_proposal(clock=clock))

    clock.advance(900)

    with pytest.raises(ProposalExpired):
        store.get("prop-one")


def test_a_draft_that_was_never_held_is_refused_as_unknown() -> None:
    store = ProposalStore(ttl_seconds=900, clock=ManualClock())

    with pytest.raises(ProposalNotFound):
        store.get("never-seen")


def test_expiry_is_remembered_so_a_second_attempt_still_says_too_old() -> None:
    clock = ManualClock()
    store = ProposalStore(ttl_seconds=900, clock=clock)
    store.put(a_proposal(clock=clock))
    clock.advance(901)

    with pytest.raises(ProposalExpired):
        store.get("prop-one")
    with pytest.raises(ProposalExpired):
        store.get("prop-one")


def test_an_expired_draft_is_never_handed_back_even_once() -> None:
    clock = ManualClock()
    store = ProposalStore(ttl_seconds=900, clock=clock)
    store.put(a_proposal(clock=clock))
    clock.advance(5_000)

    assert store.active_ids() == []
    assert len(store) == 0
    with pytest.raises(ProposalExpired):
        store.get("prop-one")


def test_purging_clears_out_the_drafts_that_have_aged_and_leaves_the_rest() -> None:
    clock = ManualClock()
    store = ProposalStore(ttl_seconds=900, clock=clock)
    store.put(a_proposal(proposal_id="old", clock=clock))
    clock.advance(901)
    store.put(a_proposal(proposal_id="fresh", clock=clock))

    assert store.purge_expired() == 1
    assert store.active_ids() == ["fresh"]


def test_a_deleted_draft_is_unknown_rather_than_too_old() -> None:
    clock = ManualClock()
    store = ProposalStore(ttl_seconds=900, clock=clock)
    store.put(a_proposal(clock=clock))

    store.delete("prop-one")

    with pytest.raises(ProposalNotFound):
        store.get("prop-one")


def test_deleting_a_draft_twice_is_not_an_error() -> None:
    store = ProposalStore(ttl_seconds=900, clock=ManualClock())
    store.delete("never-seen")
    store.delete("never-seen")


def test_the_store_refuses_a_time_to_live_of_nothing() -> None:
    with pytest.raises(ValueError):
        ProposalStore(ttl_seconds=0, clock=ManualClock())


def test_the_store_takes_its_time_to_live_from_the_settings() -> None:
    from librarian.config import Config

    clock = ManualClock()
    store = ProposalStore.from_config(
        Config(repo_owner="an-owner", repo_name="a-repo", proposal_ttl_seconds=60), clock=clock
    )
    store.put(a_proposal(clock=clock))

    clock.advance(61)

    assert store.ttl_seconds == 60
    with pytest.raises(ProposalExpired):
        store.get("prop-one")
