"""The single write path.

Everything that reaches the shared repository goes through :func:`publish`.

The failure this module exists to prevent is silent. If a change is committed without the plugin
version number moving forward, Claude keeps serving the copy people already have and nobody is told
anything is wrong. So the version bump is not a courtesy step here, it is a precondition: if the new
version is not different from the old one, nothing is written at all.

A library can hold several collections of skills at once, so nothing here assumes there is only one.
The collection that owns the skill being published is worked out first, from the library's own
manifest, and it is the only collection whose version number moves. Every other collection's entry
in the library manifest is left exactly as it was, byte for byte, because a version number that
moves on its own would push a change at people who never asked for one.

Two people can ask for a change to the same skills at the same time, and both of them can work the
new version number out from the same starting point. Whoever gets there second would then publish
content while the version number people see stays exactly where the first person left it, which is
the silent failure again. So the shared copy is read again before the change is put forward for
review, and if it has moved the version number is worked out again from what is there right then
and the working copy is started again from where the shared copy now stands.

Then, in the last moment before the change is merged, the shared copy is read once more and the
version number this change is about to ship is compared against it. If it is not higher, the merge
does not happen at all. That comparison is the guard, and refusing is the only thing it can do:
merging and then noticing would put the wording on the shared copy under a number that had already
gone out, and everyone holding that number would keep what they have forever.

Starting the working copy again is only safe while nobody else has touched the very files this
change writes. The agreed wording is the whole of each file rather than the few lines that changed
inside it, so writing it onto a newer starting point replaces whatever somebody else put there in
the meantime, with nothing to clash over and nothing anywhere to say their work ever existed. So
before starting again, every file this change writes is read as it stands now and compared with how
it stood on the copy the change was agreed against. If any one of them has moved, the publish
refuses and says so, and the person is asked to prepare the change again from the newer copy. Two
people's wording is never put together on their behalf.

After the merge the shared copy is read one final time. That last read is damage detection and
nothing more. It confirms what arrived and tells the person plainly when something is wrong, but it
comes too late to stop anything, so it is never the thing that keeps a bad publish from happening.
What the number that arrived is measured against is the copy the merge was really built on, read
from the merge itself, rather than a number sampled a moment before it. Another change can land in
that gap and take the shared copy to the very number this change was going to ship, and the older
sample would call that a success while everybody holding that number keeps the wording they have.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import Config
from .errors import LibrarianError, PublishFailed, SkillNotFound, UnsafePath
from .github import GitHubClient
from .marketplace import (
    MARKETPLACE_MANIFEST,
    PLUGIN_MANIFEST_SUFFIX,
    SKILLS_DIR_NAME,
    PluginRef,
    parse_marketplace,
    resolve_skill,
)
from .paths import validate_plugin_dir, validate_skill_name
from .proposals import Proposal
from .versioning import bump_version, parse_semver

__all__ = [
    "MARKETPLACE_MANIFEST_PATH",
    "PLUGIN_MANIFEST_SUFFIX",
    "PublishResult",
    "estimated_live_by",
    "plugin_manifest_path",
    "plugin_owning_paths",
    "publish",
]

#: The library manifest, always at the root of the repository. Never writable by an edit.
MARKETPLACE_MANIFEST_PATH = MARKETPLACE_MANIFEST

_BRANCH_PREFIX = "librarian/"
_IDENTITY_WITH_EMAIL = re.compile(r"^(?P<name>[^<>]*?)\s*<(?P<email>[^<>\s]+)>$")
_FALLBACK_EMAIL_DOMAIN = "users.noreply.github.com"
_REFERENCE_DIR_NAME = "reference"
_REFERENCE_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}\.md$")
_SKILL_FILE_NAME = "SKILL.md"

_NOTHING_PUBLISHED = "Nothing has been published."

#: How many times a publish will work its version number out again when somebody else keeps
#: getting there first. A busy library could hand out a new number forever, so this stops and
#: says so instead of going round in circles.
_MAX_VERSION_ATTEMPTS = 3


@dataclass(frozen=True)
class PublishResult:
    """What happened when a change was published."""

    commit_sha: str
    pr_number: int
    new_version: str
    estimated_live_by: str
    #: The collection the skill belongs to. Empty only if a caller builds this by hand.
    plugin_name: str = ""


def plugin_manifest_path(plugin: PluginRef) -> str:
    """Repo relative path of a collection's own manifest, the file that gates delivery."""
    return plugin.manifest_path


def publish(gh: GitHubClient, cfg: Config, proposal: Proposal, bump: str = "patch") -> PublishResult:
    """Publish an approved proposal, in the fixed order that makes the change actually arrive.

    Raises :class:`PublishFailed` if the version number would not move, and
    :class:`UnsafePath` if the proposal tries to write a file it is not allowed to write.
    """
    skill_name = _skill_name_of(proposal)
    cleaned = _cleaned_paths(proposal)

    # Step 1. Work out which collection owns this skill, then read that collection's manifest and
    # the library manifest at the commit the change was prepared against.
    plugin = _owning_plugin(gh, cfg, proposal, skill_name, cleaned)
    files_to_write = _checked_proposal_files(plugin, skill_name, cleaned)

    base_plugin_manifest = _read_manifest(gh, plugin.manifest_path, proposal.base_sha)
    old_version = _current_version(base_plugin_manifest, plugin)

    # The person was shown exactly what would change, measured against one exact copy of the
    # library. If the shared copy has moved on since then, for any reason at all, then what would
    # be published now is not what was agreed to, so it is refused instead. A version number that
    # did not move is only one of the ways the shared copy can change underneath a waiting change.
    head_sha = _shared_copy_head(gh, cfg)
    if head_sha != proposal.base_sha:
        raise PublishFailed(
            "These skills changed after this change was written, so what would be published now is "
            "not what was agreed to. Nothing has been changed for anyone. Please ask for the change "
            "to be prepared again, and it will start from the latest copy.",
            detail="the shared copy moved off the starting point the change was built on",
        )

    # Step 2. Work out the new version and put it into both manifests, kept in step. The shared
    # copy stands exactly where the change was prepared from, so these are the same two files
    # either way.
    new_version = _bumped_version(old_version, bump)
    _assert_version_moved(new_version, old_version)

    plugin_text, marketplace_text = _manifests_for(
        gh, plugin, proposal.base_sha, base_plugin_manifest, new_version
    )

    # Work out who this commit belongs to before anything is written. A change is only published
    # when it can be recorded under the name of the person who asked for it.
    author_name, author_email = _split_identity(proposal.requested_by)

    # Step 3. Cut a branch. The shared branch is never written to directly, because a change pushed
    # straight to it does not trigger any delivery at all.
    branch = _branch_name(cfg, proposal)
    # A start that fails is deliberately not tidied up. The usual reason it fails is that a
    # working copy under that name is already there, and taking that one away could throw away
    # somebody else's work that is still going on. A stray working copy nobody is using costs
    # nothing; somebody's afternoon does.
    _create_working_copy(gh, branch, head_sha)

    # Every working copy this publish makes, so that a failure takes all of them away again. The
    # version guard below can start the working copy over on a newer starting point, which makes
    # a second one.
    working_copies: list[str] = [branch]

    # Every change this publish puts forward for review. If the publish then falls over, each one
    # is withdrawn, because a change waiting for review whose working copy has just been taken
    # away is a proposal to merge something nobody meant to leave behind.
    changes_put_forward: list[int] = []
    the_change_was_merged = False

    # From here on a working copy exists, so every way out of this function that is not a finished
    # publish takes that working copy away again. Otherwise a second attempt at the same change
    # runs into the leftovers of the first one and cannot even start.
    try:
        # Step 4. One commit carries the edited files and both manifests together, so there is no
        # state where the content moved and the version did not.
        commit_sha = _save_working_copy(
            gh,
            branch,
            _payload(files_to_write, plugin, plugin_text, marketplace_text),
            _commit_message(proposal, skill_name, new_version, author_name, author_email),
            author_name,
            author_email,
        )

        # Step 5. Look at the shared copy again, before this change is put forward for review at
        # all. If someone else published while this change was being prepared, the version number
        # worked out earlier is already in use, and shipping under it would leave the version
        # people see exactly where it already was. That is the silent failure, so the number is
        # worked out again from what the shared copy says right now.
        #
        # The working copy is started again from where the shared copy now stands, rather than
        # having a new number written on top of a copy that was cut from an older starting point.
        # A copy cut from an older starting point holds one answer for the two settings files
        # while the shared copy now holds another, and putting the two together is the kind of
        # clash a person has to sort out by hand before anything can be merged. Starting again
        # from where the shared copy really is leaves nothing to sort out.
        #
        # This all happens before the change is put forward rather than after, so the wording
        # people read on the change itself names the version number that actually goes out. A
        # change whose wording promises one version while another one ships is a change nobody
        # can check by reading it.
        attempts = 0
        while True:
            shared_sha, shared_plugin_manifest = _shared_copy_manifest(gh, cfg, plugin)
            shared_version = _current_version(shared_plugin_manifest, plugin)
            if _is_higher(new_version, shared_version):
                break

            # THE GUARD ON SOMEBODY ELSE'S WORDING. Starting again from where the shared copy
            # now stands means writing the agreed wording on top of a newer starting point, and
            # the agreed wording is the whole file rather than the few lines that changed in it.
            # So if somebody else edited one of these very files while this change was waiting,
            # writing it now would replace their wording with this one. Nothing would clash,
            # nothing would fail, and their work would simply be gone, while the person who took
            # it away was told the publish worked. Refusing is the only honest answer, and
            # putting two people's wording together is not something to attempt on their behalf.
            _assert_nobody_else_changed_these_files(
                gh, files_to_write, proposal.base_sha, shared_sha
            )

            attempts += 1
            if attempts > _MAX_VERSION_ATTEMPTS:
                # Somebody keeps getting there first. Trying forever against a busy library is a
                # problem of its own, so this stops and says so rather than going round in circles.
                raise PublishFailed(
                    "Several people are changing these skills at the same time, so this change "
                    "could not be given a version number ahead of the ones already going out. It "
                    "has not been published and nothing has been changed for anyone. Please try "
                    "again in a few minutes.",
                    detail=(
                        f"gave up after {_MAX_VERSION_ATTEMPTS} attempts to get past the shared "
                        f"version {shared_version}"
                    ),
                )

            new_version = _bumped_version(shared_version, bump)
            _assert_version_moved(new_version, shared_version)
            plugin_text, marketplace_text = _manifests_for(
                gh, plugin, shared_sha, shared_plugin_manifest, new_version
            )

            if _remove_working_copy(gh, branch):
                working_copies.remove(branch)
            branch = _branch_name(cfg, proposal, attempts)
            # Only a working copy this publish really did start is written down as one to tidy
            # up afterwards. A name that was already taken belongs to somebody else's work, and
            # tidying that away would throw their afternoon out with it.
            _create_working_copy(gh, branch, shared_sha)
            working_copies.append(branch)
            commit_sha = _save_working_copy(
                gh,
                branch,
                _payload(files_to_write, plugin, plugin_text, marketplace_text),
                _commit_message(proposal, skill_name, new_version, author_name, author_email),
                author_name,
                author_email,
            )

        # Step 6. Put the change forward for merging. Both the wording and the version number it
        # names are the ones settled on just above, so the change reads as exactly what will ship.
        title = _publish_title(skill_name, new_version)
        try:
            pr_number = gh.open_pr(
                branch,
                cfg.default_branch,
                title,
                _pull_request_body(
                    cfg, proposal, skill_name, plugin, new_version, author_name, author_email,
                    files_to_write,
                ),
            )
        except LibrarianError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PublishFailed(
                "The change was saved on its own working copy but could not be put forward for "
                "merging, so nothing has been changed for anyone yet. Please try again.",
                detail=f"open_pr failed: {exc}",
            ) from exc
        changes_put_forward.append(pr_number)

        # Step 6b. THE GUARD. Look at the shared copy one final time, in the last moment before
        # the merge, and compare the number this change is about to ship against the number that
        # is really there right now. This is read here rather than reused from a moment ago
        # because the shared copy can move again while the change is being put up for review.
        #
        # If a further change landed in that gap and took the version to the very number this
        # change was going to ship under, then merging now would leave the shared copy standing
        # exactly where it already stood. The wording would be there and nobody would ever fetch
        # it, because the number that tells Claude to fetch it did not move. So the merge does not
        # happen at all. Nothing at this point can start the working copy over, because the change
        # has already been put forward for review from it, so the only honest answer is to refuse.
        shared_copy_now = _shared_copy_head(gh, cfg)

        # THE GUARD ON SOMEBODY ELSE'S WORDING, ASKED ONE LAST TIME. The version number is not
        # the only thing that can move. Somebody can change the very files this change writes
        # without touching the version number at all, and then nothing above would have looked.
        #
        # It matters because merging is not the same as replacing. Where the two of them changed
        # different parts of the same file, a merge puts both sets of words into it and calls
        # that a success, and what lands is a paragraph of theirs next to a paragraph of this
        # one: wording nobody wrote, nobody read and nobody agreed to, going out under this
        # change's version number. Where they changed the same part, the merge cannot be done at
        # all and somebody is told to try again, over and over, because trying again does exactly
        # the same thing. Both of those are refused here instead, in words that say what actually
        # happened and what to do about it.
        _assert_nobody_else_changed_these_files(
            gh, files_to_write, proposal.base_sha, shared_copy_now
        )

        version_just_before_merge = _current_version(
            _read_manifest(gh, plugin.manifest_path, shared_copy_now), plugin
        )
        if not _is_higher(new_version, version_just_before_merge):
            raise PublishFailed(
                "Someone else published a change to these skills a moment ago, and the version "
                "number this change was going to use is no longer ahead of the one that has "
                "already gone out. Putting this change in now would not reach everyone, because "
                "anyone who already has that version would keep the wording they have and would "
                "never see this one. So it was not published, and nothing has been changed for "
                "anyone. Please ask for the change again, and it will start from the latest copy.",
                detail=(
                    f"planned {new_version} is not higher than {version_just_before_merge} on "
                    f"{cfg.default_branch}"
                ),
            )

        # Step 6c. Merge it, naming the exact saved change that is being published. Only a merge
        # into the shared copy makes a change reach people, and naming the change means a working
        # copy that was altered after it was agreed to is refused rather than published.
        try:
            merge_sha = gh.merge_pr(pr_number, title, commit_sha)
        except LibrarianError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PublishFailed(
                "The change is ready and waiting but it could not be merged, so it has not reached "
                "anyone yet. Nothing in the shared skills has changed. Please try again.",
                detail=f"merge_pr failed: {exc}",
            ) from exc
        the_change_was_merged = True

        # Step 6d. Read the shared copy back. THIS IS DAMAGE DETECTION, NOT THE GUARD. By now the
        # wording is already on the copy everyone reads from, so nothing here can prevent a bad
        # publish; it can only notice one and say so. The refusal in step 6b is what keeps a merge
        # from happening under a number that did not move, and this must never be mistaken for it.
        #
        # What is read back is this merge itself, and what it is measured against is the copy it
        # was really put on top of, which is the first copy the merge was built on. Neither of
        # those can move afterwards. Reading whatever the shared copy holds by now instead would
        # be looking at somebody else's change: one that landed in the gap can hide damage this
        # merge did, by supplying a version number this merge dropped, and can equally raise a
        # false alarm about a merge that was perfect, by revising the very wording just published.
        built_on_sha, version_this_was_put_on_top_of = _what_the_merge_was_built_on(
            gh, plugin, merge_sha
        )
        _assert_the_change_reached_everyone(
            gh,
            cfg,
            plugin,
            files_to_write,
            merge_sha,
            built_on_sha,
            version_this_was_put_on_top_of,
        )

    except BaseException:
        # The reason this publish failed is what the person needs to hear, so tidying up is never
        # allowed to replace it with a different error.
        #
        # The change is withdrawn from review first and the working copies are taken away second.
        # Doing it the other way round leaves a change sitting there for review that points at a
        # working copy that is already gone.
        if not the_change_was_merged:
            for waiting in changes_put_forward:
                _withdraw_from_review(gh, waiting)
        for leftover in working_copies:
            _remove_working_copy(gh, leftover)
        raise

    # Step 7. The working copies are taken away, including the one that was just merged. Its
    # wording is on the shared copy now, so the copy it was written on has nothing left to say,
    # and leaving it behind would fill the library with working copies nobody is using. This
    # happens after the publish worked, so a working copy that will not go away is not worth
    # troubling anybody about.
    for leftover in working_copies:
        _remove_working_copy(gh, leftover)

    return PublishResult(
        commit_sha=commit_sha,
        pr_number=pr_number,
        new_version=new_version,
        estimated_live_by=estimated_live_by(cfg),
        plugin_name=plugin.name,
    )


def _payload(
    files_to_write: dict[str, str],
    plugin: PluginRef,
    plugin_text: str,
    marketplace_text: str,
) -> dict[str, str]:
    """Everything one commit carries: the edited files and both settings files together.

    They travel together on purpose. There is no moment where the wording has moved and the
    version number that delivers it has not.
    """
    payload: dict[str, str] = dict(files_to_write)
    payload[plugin.manifest_path] = plugin_text
    payload[MARKETPLACE_MANIFEST] = marketplace_text
    return payload


def _create_working_copy(gh: GitHubClient, branch: str, from_sha: str) -> None:
    """Start a working copy of the library at an exact starting point."""
    try:
        gh.create_branch(branch, from_sha)
    except LibrarianError:
        raise
    except Exception as exc:  # noqa: BLE001 - turned into a plain English message below
        raise PublishFailed(
            "A working copy for this change could not be started, so nothing has been changed for "
            "anyone. Please try again.",
            detail=f"create_branch failed: {exc}",
        ) from exc


def _save_working_copy(
    gh: GitHubClient,
    branch: str,
    payload: dict[str, str],
    message: str,
    author_name: str,
    author_email: str,
) -> str:
    """Save everything into the working copy in one go, under the name of the person who asked."""
    try:
        return gh.commit_files(branch, payload, message, author_name, author_email)
    except LibrarianError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PublishFailed(
            "The change could not be saved, so nothing has been changed for anyone. Please try "
            "again.",
            detail=f"commit_files failed: {exc}",
        ) from exc


def _manifests_for(
    gh: GitHubClient,
    plugin: PluginRef,
    ref: str,
    plugin_manifest: dict[str, Any],
    new_version: str,
) -> tuple[str, str]:
    """Both settings files, carrying the new version, built from one exact copy of the library.

    Building them from the copy they will sit on top of is what keeps this simple. Every other
    collection's entry keeps the bytes that copy already had.
    """
    marketplace_before = _read_text(gh, MARKETPLACE_MANIFEST, ref)
    plugin_text = _render_json(_with_version(plugin_manifest, new_version))
    marketplace_text = _marketplace_text_with_version(marketplace_before, plugin, new_version)
    _assert_manifests_in_step(
        plugin_text, marketplace_text, marketplace_before, plugin, new_version
    )
    return plugin_text, marketplace_text


def estimated_live_by(cfg: Config, now: datetime | None = None) -> str:
    """A window, never a promise. Nothing about this delivery is instant or precisely timed."""
    minutes = max(1, int(cfg.sync_estimate_minutes))
    moment = now or datetime.now(timezone.utc)
    return (moment + timedelta(minutes=minutes)).strftime("%H:%M UTC on %d %B %Y")


# ==============================================================================================
# Reading the shared copy, and proving the change reached it
# ==============================================================================================


def _shared_copy_head(gh: GitHubClient, cfg: Config) -> str:
    """Where the copy everyone reads from stands right now."""
    try:
        sha = gh.get_ref_sha(cfg.default_branch)
    except LibrarianError as exc:
        raise _publish_failure(exc) from exc
    except Exception as exc:  # noqa: BLE001
        raise PublishFailed(
            "The shared copy of the skills could not be read just now, so nothing was published. "
            "Please try again.",
            detail=f"get_ref_sha({cfg.default_branch!r}) failed: {exc}",
        ) from exc

    if not isinstance(sha, str) or not sha.strip():
        raise PublishFailed(
            "The shared copy of the skills could not be read just now, so nothing was published. "
            "Please try again.",
            detail="the shared copy did not say where it stands",
        )
    return sha.strip()


def _shared_copy_manifest(
    gh: GitHubClient, cfg: Config, plugin: PluginRef
) -> tuple[str, dict[str, Any]]:
    """Where the shared copy stands, and what that copy says this collection's version is."""
    sha = _shared_copy_head(gh, cfg)
    return sha, _read_manifest(gh, plugin.manifest_path, sha)


def _file_as_it_stood(gh: GitHubClient, path: str, ref: str) -> str | None:
    """What one file held at one exact copy of the library, or nothing if it was not there yet.

    A file that is not there is an answer in its own right, because a change can add a file that
    nobody had written before. Anything else that goes wrong while reading is not turned into
    that answer: a file that could not be read is not a file that is known to be unchanged, and
    treating the two the same would let a publish walk straight over somebody else's wording on
    the strength of a network that was having a bad moment.
    """
    try:
        text, _blob_sha = gh.get_file(path, ref)
    except SkillNotFound:
        return None
    except FileNotFoundError:
        return None
    except LibrarianError as exc:
        raise _publish_failure(exc) from exc
    except Exception as exc:  # noqa: BLE001
        raise PublishFailed(
            "The latest copy of these skills could not be read, so nothing was published and "
            "nothing has been changed for anyone. Please try again.",
            detail=f"could not read {path} at {ref}: {exc}",
        ) from exc

    if not isinstance(text, str):
        raise PublishFailed(
            "The latest copy of these skills could not be read, so nothing was published and "
            "nothing has been changed for anyone. Please try again.",
            detail=f"{path} at {ref} was {type(text).__name__}, not text",
        )
    return text


def _assert_nobody_else_changed_these_files(
    gh: GitHubClient,
    files_to_write: dict[str, str],
    base_sha: str,
    shared_sha: str,
) -> None:
    """Refuse to write the agreed wording on top of somebody else's newer wording.

    The agreed wording is the whole of each file, not the handful of lines that changed inside
    it. So writing it onto a newer starting point does not merge with what somebody else wrote
    there in the meantime, it replaces it, and nothing anywhere would say that had happened.

    Every file this change writes is read as it stands on the newer starting point and compared
    with how it stood on the copy the change was agreed against. A file somebody else has since
    edited stops the publish. So does a file this change would add that somebody else has since
    added, because writing over it would throw their version of it away just the same.

    One thing does not stop it: a file somebody else has moved to exactly the wording this change
    was going to give it. Nothing of theirs is lost by writing what is already written, and
    stopping there would send somebody off to prepare a change with nothing left in it while the
    wording sat on the shared copy waiting for a version number that never came.
    """
    if base_sha == shared_sha:
        return

    for path, agreed in sorted(files_to_write.items()):
        was = _file_as_it_stood(gh, path, base_sha)
        now = _file_as_it_stood(gh, path, shared_sha)
        if now == was:
            continue
        if now == agreed:
            # Somebody else put this file into exactly the state this change was going to put it
            # into, word for word. There is nothing of theirs to lose by writing it, because what
            # would be written is what is already there. Refusing here would send somebody away
            # to prepare a change that has nothing left to change, and if the wording landed
            # without the version number moving it would sit there undelivered for good.
            continue
        raise PublishFailed(
            "Somebody else changed this skill while this change was waiting, so publishing now "
            "would quietly replace their wording with this one and their work would be lost. "
            "Nothing has been changed for anyone. Please ask for the change again: it will start "
            "from their version, so neither piece of work is lost.",
            detail=f"{path} is not what it was on the copy this change was agreed against",
        )


def _what_the_merge_was_built_on(
    gh: GitHubClient, plugin: PluginRef, merge_sha: str
) -> tuple[str, str]:
    """The copy the merge was really put on top of, and the version number that copy carried.

    A merge records what it was built on, first the copy that was merged into and then the
    copies that were merged in. The first of those is what this change actually landed on top
    of, so it is the only fair thing to measure the landed version number against. A number
    sampled a moment before the merge is not the same thing: another change can land in the gap
    and take the shared copy to the very number this change was going to ship, and measuring
    against the older sample would call that a success while everybody holding that number quietly
    keeps the wording they already have.
    """
    if not isinstance(merge_sha, str) or not merge_sha.strip():
        raise PublishFailed(
            _MAY_NOT_HAVE_ARRIVED,
            detail=f"merge_pr answered with {merge_sha!r}, which names nothing",
        )

    try:
        parents = gh.commit_parents(merge_sha.strip())
    except LibrarianError as exc:
        raise PublishFailed(
            _MAY_NOT_HAVE_ARRIVED,
            detail=f"could not read what the merge was built on: {exc.detail or exc}",
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise PublishFailed(
            _MAY_NOT_HAVE_ARRIVED,
            detail=f"could not read what the merge was built on: {exc}",
        ) from exc

    if not isinstance(parents, list) or not parents:
        raise PublishFailed(
            _MAY_NOT_HAVE_ARRIVED,
            detail="the merge did not say what it was built on",
        )
    first_parent = parents[0]
    if not isinstance(first_parent, str) or not first_parent.strip():
        raise PublishFailed(
            _MAY_NOT_HAVE_ARRIVED,
            detail=f"the merge named {first_parent!r} as what it was built on",
        )

    built_on = first_parent.strip()
    try:
        return built_on, _current_version(
            _read_manifest(gh, plugin.manifest_path, built_on), plugin
        )
    except LibrarianError as exc:
        raise PublishFailed(
            _MAY_NOT_HAVE_ARRIVED,
            detail=(
                "could not read the version on the copy the merge was built on: "
                f"{exc.detail or exc}"
            ),
        ) from exc


def _assert_version_moved(new_version: str, old_version: str) -> None:
    """Refuse a publish whose version number would stay where it is.

    This is the whole point of the module. Content that arrives without a new version number is
    served to nobody, and nobody is told, so it must never be written at all.
    """
    if new_version != old_version:
        return
    raise PublishFailed(
        "The version number did not move forward, so this change was not published. Without a new "
        "version number the change would reach the repository but never reach anyone in Claude, "
        "and nobody would be told. Nothing has been changed."
    )


def _publish_title(skill_name: str, new_version: str) -> str:
    return f"Update the {skill_name} skill (version {new_version})"


_MAY_NOT_HAVE_ARRIVED = (
    "The change was saved into the shared skills, but it could not be confirmed that it will "
    "reach everyone, so some people may still see the wording as it was before. Please ask for "
    "the change again."
)


#: A stop on how far back along the shared copy this will look for the merge it just made.
#: This is not what normally ends the walk. Every change that lands on the shared copy is put
#: on top of where the shared copy stood, so following that line back always arrives either at
#: this merge or at the copy this merge was built on, and both of those end it. The stop is
#: here only so that a history of an unexpected shape cannot turn a check into an endless one.
#: It is set high enough that no ordinary run of publishes could reach it, because a stop that
#: a busy library could hit would raise a false alarm about a merge that was perfectly fine.
_MAX_STEPS_LOOKING_FOR_THE_MERGE = 500


def _assert_the_merge_is_on_the_shared_copy(
    gh: GitHubClient, cfg: Config, merge_sha: str, built_on_sha: str
) -> None:
    """Prove this merge really is on the copy everyone reads from.

    Reading the merge itself is what makes the rest of the check honest, because nobody can move
    a saved change once it is made. It is also what makes this step necessary: a merge that
    answered with a saved change and never actually moved the shared copy would be read back in
    loving detail and found perfect, while not one person ever saw it.

    So the shared copy is asked where it stands. Nearly always it stands exactly on this merge and
    there is nothing more to do. If it has moved on, the changes that landed afterwards are walked
    back through, each one by the copy it was put on top of, until this merge is found. Walking by
    the first copy is what makes this work: every later change was put on top of the shared copy as
    it stood, so following that line back is following the shared copy's own history. Not finding
    it means it is not there, and a merge that is not there reached nobody.
    """
    where_it_stands = _shared_copy_head(gh, cfg)
    if where_it_stands == merge_sha:
        return

    if where_it_stands == built_on_sha:
        raise PublishFailed(
            _MAY_NOT_HAVE_ARRIVED,
            detail=(
                "the shared copy still stands exactly where the merge was built on, so the merge "
                "never reached it"
            ),
        )

    walked = where_it_stands
    for _step in range(_MAX_STEPS_LOOKING_FOR_THE_MERGE):
        try:
            parents = gh.commit_parents(walked)
        except LibrarianError as exc:
            raise PublishFailed(
                _MAY_NOT_HAVE_ARRIVED,
                detail=(
                    "could not follow the shared copy back to the merge: "
                    f"{exc.detail or exc}"
                ),
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise PublishFailed(
                _MAY_NOT_HAVE_ARRIVED,
                detail=f"could not follow the shared copy back to the merge: {exc}",
            ) from exc

        if not isinstance(parents, list) or not parents:
            raise PublishFailed(
                _MAY_NOT_HAVE_ARRIVED,
                detail="the shared copy's history ran out before the merge was found",
            )
        first_parent = parents[0]
        if not isinstance(first_parent, str) or not first_parent.strip():
            raise PublishFailed(
                _MAY_NOT_HAVE_ARRIVED,
                detail=f"the shared copy named {first_parent!r} as what it was built on",
            )
        walked = first_parent.strip()
        if walked == merge_sha:
            return
        if walked == built_on_sha:
            # The line the shared copy really followed goes straight past this merge to the copy
            # it was built on, so this merge is not on the shared copy at all.
            break

    raise PublishFailed(
        _MAY_NOT_HAVE_ARRIVED,
        detail="the merge is not on the shared copy",
    )


def _assert_the_change_reached_everyone(
    gh: GitHubClient,
    cfg: Config,
    plugin: PluginRef,
    files: dict[str, str],
    merge_sha: str,
    built_on_sha: str,
    version_the_merge_was_built_on: str,
) -> None:
    """Read this merge back and prove every part of a real delivery arrived.

    This runs after the merge, so it is damage detection and nothing more. It can say that
    something went wrong; it cannot stop it. What keeps a change from being merged under a
    version number that did not move is the refusal before the merge, not this.

    What is read is this merge, and what it is measured against is the copy the merge was put on
    top of. Both are fixed points that nobody can move afterwards. Reading whatever the shared
    copy holds by the time this runs would be reading somebody else's change instead, and that
    cuts both ways. A change landing in the gap can supply a version number this merge dropped,
    and the damage this check exists to notice would be reported as a success. It can equally
    revise the very wording just published, and a merge that was perfect would be reported to the
    person as one that may never have arrived.

    Four things have to be true. The merge has to be on the shared copy, because a merge that
    answered with a saved change and never reached the copy everyone reads from reached nobody,
    and reading that saved change on its own would describe it in loving detail regardless. The
    wording has to be there. The collection's own version number has to be higher than the number
    on the copy the merge was built on, because that number is the only thing that tells Claude to
    pick the wording up. And the library's list has to be carrying the same number for that
    collection, because the two are meant to stay in step and a check that only looks at one of
    them proves half of what it claims to prove.
    """
    _assert_the_merge_is_on_the_shared_copy(gh, cfg, merge_sha, built_on_sha)

    try:
        sha = merge_sha
        manifest = _read_manifest(gh, plugin.manifest_path, sha)
        landed_version = _current_version(manifest, plugin)
        landed_marketplace = _read_text(gh, MARKETPLACE_MANIFEST, sha)
        landed_entries = parse_marketplace(landed_marketplace)
        landed = {path: _read_text(gh, path, sha) for path in files}
    except LibrarianError as exc:
        raise PublishFailed(
            _MAY_NOT_HAVE_ARRIVED,
            detail=f"could not read the shared copy back after merging: {exc.detail or exc}",
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise PublishFailed(
            _MAY_NOT_HAVE_ARRIVED,
            detail=f"could not read the shared copy back after merging: {exc}",
        ) from exc

    if not _is_higher(landed_version, version_the_merge_was_built_on):
        raise PublishFailed(
            _MAY_NOT_HAVE_ARRIVED,
            detail=(
                f"{plugin.manifest_path} says {landed_version} after merging, which is not higher "
                f"than the {version_the_merge_was_built_on} the merge was built on"
            ),
        )

    listed_version = next(
        (
            entry.version
            for entry in landed_entries
            if entry.name == plugin.name and entry.plugin_dir == plugin.plugin_dir
        ),
        None,
    )
    if listed_version != landed_version:
        raise PublishFailed(
            _MAY_NOT_HAVE_ARRIVED,
            detail=(
                f"{MARKETPLACE_MANIFEST} says {listed_version!r} for {plugin.name!r} after "
                f"merging, not {landed_version}"
            ),
        )

    for path, content in files.items():
        if landed.get(path) != content:
            raise PublishFailed(
                _MAY_NOT_HAVE_ARRIVED,
                detail=f"{path} does not hold what was agreed to after merging",
            )


def _is_higher(candidate: str, previous: str) -> bool:
    """True only when the first version number is strictly further forward than the second."""
    try:
        return parse_semver(candidate) > parse_semver(previous)
    except LibrarianError:
        return False
    except Exception:  # noqa: BLE001 - an unreadable version proves nothing moved forward
        return False


def _remove_working_copy(gh: GitHubClient, branch: str) -> bool:
    """Take away a working copy this publish made, so a second attempt starts from nothing.

    Answers whether the working copy really did go away, so the caller knows whether there is
    still something to tidy up later. Whatever went wrong is the thing the person needs to hear
    about, so any problem while tidying up is swallowed rather than raised over the top of it,
    and the answer is simply no.
    """
    try:
        gh.delete_branch(branch)
    except Exception:  # noqa: BLE001 - the original failure is the one that matters
        return False
    return True


def _withdraw_from_review(gh: GitHubClient, number: int) -> None:
    """Take a change back out of review when it is not going to be published after all.

    This runs on a path where something has already gone wrong, and the working copy the change
    was written on is about to be taken away, so leaving the change sitting there would leave a
    proposal to merge something that no longer exists anywhere. Whatever went wrong first is the
    thing the person needs to hear about, so a problem while withdrawing is kept quiet rather
    than raised over the top of it.
    """
    try:
        gh.close_pr(number)
    except Exception:  # noqa: BLE001 - the original failure is the one that matters
        return


# ==============================================================================================
# Which collection owns the skill
# ==============================================================================================


def plugin_owning_paths(
    plugins: list[PluginRef], skill_name: str, paths: list[str]
) -> PluginRef | None:
    """The one collection whose skill folder holds every one of these paths, if there is one.

    This is how a skill that does not exist yet finds its home: the file names say which
    collection it is being added to. Nothing is guessed - if the paths do not all sit inside a
    single collection, this answers with nothing and the caller asks the person.
    """
    matches = [
        plugin
        for plugin in plugins
        if paths
        and all(path.startswith(skill_folder(plugin, skill_name)) for path in paths)
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def skill_folder(plugin: PluginRef, skill_name: str) -> str:
    """The folder one skill lives in, with a trailing slash so prefix checks are exact."""
    return f"{plugin.plugin_dir}/{SKILLS_DIR_NAME}/{skill_name}/"


def _owning_plugin(
    gh: GitHubClient, cfg: Config, proposal: Proposal, skill_name: str, paths: dict[str, str]
) -> PluginRef:
    """Find the collection this skill belongs to, or refuse to publish."""
    try:
        return resolve_skill(gh, cfg, skill_name, proposal.base_sha).plugin
    except SkillNotFound:
        # Not there yet, so this is a new skill and the file names say where it is going.
        pass
    except UnsafePath:
        # The library's own manifest points a collection somewhere this service will not go.
        # That is refused as what it is rather than reworded into a general publishing
        # problem, because the reason matters: it is the anchor every writable path is
        # measured against, and it already says in plain English that nothing was changed.
        raise
    except LibrarianError as exc:
        raise _publish_failure(exc) from exc

    plugins = _plugins_at(gh, cfg, proposal.base_sha)
    chosen = plugin_owning_paths(plugins, skill_name, sorted(paths))
    if chosen is not None:
        return chosen
    if len(plugins) == 1:
        # There is only one collection, so there is nothing to choose between. The file names are
        # checked next, and a name that does not belong is refused there with a clear reason.
        return plugins[0]
    names = ", ".join(sorted(plugin.name for plugin in plugins))
    raise PublishFailed(
        f'There is no skill called "{skill_name}" yet, and this library keeps more than one '
        f"collection of skills: {names}. Please say which collection the new skill belongs in, so "
        "it is added in the right place. " + _NOTHING_PUBLISHED,
        detail=f"new skill {skill_name!r} did not name a collection",
    )


def _plugins_at(gh: GitHubClient, cfg: Config, ref: str) -> list[PluginRef]:
    text = _read_text(gh, MARKETPLACE_MANIFEST, ref)
    try:
        return parse_marketplace(text)
    except UnsafePath:
        # See the note in _owning_plugin: an unsafe collection folder is reported as exactly
        # that, so nobody goes looking for a publishing problem that is not there.
        raise
    except LibrarianError as exc:
        raise _publish_failure(exc) from exc


def _publish_failure(exc: LibrarianError) -> PublishFailed:
    """Repeat a plain English reason back, making clear that nothing reached anyone."""
    message = (exc.user_message or "").strip()
    if not message:
        return PublishFailed(detail=exc.detail)
    if "nothing has been" not in message.lower():
        message = f"{message} {_NOTHING_PUBLISHED}"
    return PublishFailed(message, detail=exc.detail)


# ==============================================================================================
# Reading and writing the two manifests
# ==============================================================================================


def _read_text(gh: GitHubClient, path: str, ref: str) -> str:
    try:
        text, _blob_sha = gh.get_file(path, ref)
    except LibrarianError as exc:
        raise _publish_failure(exc) from exc
    except Exception as exc:  # noqa: BLE001
        raise PublishFailed(
            "One of the settings files that decides how these skills are delivered could not be "
            "read, so nothing was published. Someone with access to the library needs to check "
            "that the file is there.",
            detail=f"could not read {path} at {ref}: {exc}",
        ) from exc
    if not isinstance(text, str):
        raise PublishFailed(
            "One of the settings files that decides how these skills are delivered could not be "
            "read as text, so nothing was published. Someone with access to the library needs to "
            "check it.",
            detail=f"{path} at {ref} was {type(text).__name__}, not text",
        )
    return text


def _read_manifest(gh: GitHubClient, path: str, ref: str) -> dict[str, Any]:
    text = _read_text(gh, path, ref)
    return _loaded_manifest(text, path)


def _loaded_manifest(text: str, path: str) -> dict[str, Any]:
    try:
        loaded = json.loads(text)
    except ValueError as exc:
        raise PublishFailed(
            "One of the settings files that decides how these skills are delivered is damaged, so "
            "nothing was published. Someone with access to the library needs to fix that file "
            "first.",
            detail=f"{path} is not valid JSON: {exc}",
        ) from exc
    if not isinstance(loaded, dict):
        raise PublishFailed(
            "One of the settings files that decides how these skills are delivered is not in the "
            "shape this tool expects, so nothing was published. Someone with access to the library "
            "needs to check it.",
            detail=f"{path} holds {type(loaded).__name__}, not an object",
        )
    return loaded


def _current_version(manifest: dict[str, Any], plugin: PluginRef) -> str:
    version = manifest.get("version")
    if not isinstance(version, str) or not version.strip():
        raise PublishFailed(
            f'The collection of skills called "{plugin.name}" has no version number recorded, so '
            "there is nothing to move forward from and nothing was published. Someone with access "
            "to the library needs to add one.",
            detail=f"{plugin.manifest_path} has no usable version",
        )
    return version.strip()


def _bumped_version(current: str, bump: str) -> str:
    try:
        candidate = bump_version(current, bump)
    except LibrarianError as exc:
        raise _publish_failure(exc) from exc
    except Exception as exc:  # noqa: BLE001
        raise PublishFailed(
            "The next version number could not be worked out from the current one, so nothing was "
            "published. Someone with access to the library needs to check the version number.",
            detail=f"bump_version({current!r}, {bump!r}) failed: {exc}",
        ) from exc

    if not isinstance(candidate, str) or not candidate.strip():
        raise PublishFailed(
            "The next version number came back empty, so nothing was published. Nothing has been "
            "changed for anyone.",
            detail=f"bump_version({current!r}, {bump!r}) returned {candidate!r}",
        )
    return candidate.strip()


def _with_version(manifest: dict[str, Any], new_version: str) -> dict[str, Any]:
    updated = dict(manifest)
    updated["version"] = new_version
    return updated


def _marketplace_text_with_version(text: str, plugin: PluginRef, new_version: str) -> str:
    """Record the new version against one collection, and touch nothing else in the file.

    The entry for the collection being published is rewritten. Every other entry keeps the exact
    bytes it already had, including whatever spacing the team writes their manifest with, so a
    publish can never look like a change to somebody else's collection.
    """
    index = _entry_index(text, plugin)
    data = _loaded_manifest(text, MARKETPLACE_MANIFEST)
    entries = data.get("plugins")
    if not isinstance(entries, list) or index >= len(entries):
        raise PublishFailed(
            "The library's list of skill collections does not have an entry for these skills, so "
            "the new version could not be recorded there and nothing was published. Someone with "
            "access to the library needs to add that entry.",
            detail=f"no entry at position {index} for {plugin.name!r}",
        )
    entry = entries[index]
    if not isinstance(entry, dict):
        raise PublishFailed(
            "The library's list of skill collections is not in the shape this tool expects, so "
            "nothing was published. Someone with access to the library needs to check it.",
            detail=f"entry {index} is {type(entry).__name__}, not an object",
        )

    updated_entry = _with_version(entry, new_version)
    spliced = _replaced_entry_text(text, index, updated_entry)
    if spliced is not None:
        return spliced

    # The file could not be edited in place, so it is written out again from scratch. Every
    # entry still carries exactly the settings it had, only the spacing may be tidied.
    rewritten = dict(data)
    rewritten_entries = list(entries)
    rewritten_entries[index] = updated_entry
    rewritten["plugins"] = rewritten_entries
    return _render_json(rewritten)


def _entry_index(text: str, plugin: PluginRef) -> int:
    """Where this collection sits in the library manifest, read from the manifest itself."""
    try:
        listed = parse_marketplace(text)
    except LibrarianError as exc:
        raise _publish_failure(exc) from exc
    for index, candidate in enumerate(listed):
        if candidate.name == plugin.name and candidate.plugin_dir == plugin.plugin_dir:
            return index
    raise PublishFailed(
        f'The library\'s list of skill collections has no entry for "{plugin.name}", so the new '
        "version could not be recorded there and nothing was published. Someone with access to the "
        "library needs to add that entry.",
        detail=f"{plugin.name!r} at {plugin.plugin_dir!r} is not listed",
    )


def _assert_manifests_in_step(
    plugin_text: str,
    marketplace_text: str,
    marketplace_before: str,
    plugin: PluginRef,
    new_version: str,
) -> None:
    """Read back what is about to be committed and confirm it says what it has to say."""
    written_plugin = _loaded_manifest(plugin_text, plugin.manifest_path)
    if written_plugin.get("version") != new_version:
        raise PublishFailed(
            "The new version number was not written into this collection's settings, so nothing "
            "was published. Nothing has been changed for anyone.",
            detail=f"{plugin.manifest_path} did not take version {new_version}",
        )

    written = _loaded_manifest(marketplace_text, MARKETPLACE_MANIFEST)
    before = _loaded_manifest(marketplace_before, MARKETPLACE_MANIFEST)
    written_entries = written.get("plugins")
    before_entries = before.get("plugins")
    if not isinstance(written_entries, list) or not isinstance(before_entries, list):
        raise PublishFailed(
            "The library's list of skill collections is not in the shape this tool expects, so "
            "nothing was published. Someone with access to the library needs to check it.",
            detail="the plugins key is not a list",
        )
    if len(written_entries) != len(before_entries):
        raise PublishFailed(
            "Recording the new version would have added or removed a collection of skills, so "
            "nothing was published. Nothing has been changed for anyone.",
            detail=f"{len(before_entries)} entries became {len(written_entries)}",
        )

    index = _entry_index(marketplace_text, plugin)
    if not isinstance(written_entries[index], dict):
        raise PublishFailed(
            "The library's list of skill collections is not in the shape this tool expects, so "
            "nothing was published. Someone with access to the library needs to check it.",
            detail=f"entry {index} is not an object",
        )
    if written_entries[index].get("version") != new_version:
        raise PublishFailed(
            "The two settings files ended up with different version numbers, so nothing was "
            "published. Nothing has been changed for anyone.",
            detail=f"entry {index} did not take version {new_version}",
        )

    for position, (was, now) in enumerate(zip(before_entries, written_entries)):
        if position == index:
            continue
        if was != now:
            raise PublishFailed(
                "Recording the new version would have changed another collection of skills as "
                "well, so nothing was published. Nothing has been changed for anyone.",
                detail=f"entry {position} changed while publishing {plugin.name!r}",
            )


def _render_json(data: dict[str, Any]) -> str:
    """Two space indent and a trailing newline, so the change stays easy to read in the history."""
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


# ==============================================================================================
# Editing one entry of a JSON file without disturbing the rest of it
# ==============================================================================================


class _ScanFailed(Exception):
    """The manifest could not be edited in place, so it will be written out again instead."""


def _replaced_entry_text(text: str, index: int, entry: dict[str, Any]) -> str | None:
    """Swap one entry of the plugins list for a new one, leaving every other byte alone.

    Returns nothing if the file cannot be walked through confidently, in which case the caller
    writes the whole file out again rather than risk a careless edit.
    """
    try:
        spans = _entry_spans(text)
    except _ScanFailed:
        return None
    if index >= len(spans):
        return None

    start, end = spans[index]
    line_start = text.rfind("\n", 0, start) + 1
    indent = text[line_start:start]
    if indent.strip():
        indent = ""
    rendered = json.dumps(entry, indent=2, ensure_ascii=False).replace("\n", "\n" + indent)
    return text[:start] + rendered + text[end:]


def _entry_spans(text: str) -> list[tuple[int, int]]:
    """Where each entry of the plugins list starts and ends in the raw text of the manifest."""
    start, end = _root_member_span(text, "plugins")
    if text[start] != "[":
        raise _ScanFailed()

    spans: list[tuple[int, int]] = []
    index = _skip_space(text, start + 1)
    if index < end and text[index] == "]":
        return spans
    while True:
        value_end = _value_end(text, index)
        spans.append((index, value_end))
        index = _skip_space(text, value_end)
        if index >= len(text):
            raise _ScanFailed()
        if text[index] == ",":
            index = _skip_space(text, index + 1)
            continue
        if text[index] == "]":
            return spans
        raise _ScanFailed()


def _root_member_span(text: str, key: str) -> tuple[int, int]:
    """Where the value of one top level key starts and ends."""
    index = _skip_space(text, 0)
    if index >= len(text) or text[index] != "{":
        raise _ScanFailed()
    index = _skip_space(text, index + 1)
    while True:
        if index >= len(text):
            raise _ScanFailed()
        if text[index] != '"':
            raise _ScanFailed()
        name_end = _string_end(text, index)
        try:
            name = json.loads(text[index:name_end])
        except ValueError as exc:
            raise _ScanFailed() from exc
        index = _skip_space(text, name_end)
        if index >= len(text) or text[index] != ":":
            raise _ScanFailed()
        value_start = _skip_space(text, index + 1)
        value_end = _value_end(text, value_start)
        if name == key:
            return value_start, value_end
        index = _skip_space(text, value_end)
        if index < len(text) and text[index] == ",":
            index = _skip_space(text, index + 1)
            continue
        raise _ScanFailed()


def _skip_space(text: str, index: int) -> int:
    while index < len(text) and text[index] in " \t\r\n":
        index += 1
    return index


def _string_end(text: str, index: int) -> int:
    """The position just past a quoted string that starts at ``index``."""
    index += 1
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == '"':
            return index + 1
        index += 1
    raise _ScanFailed()


def _value_end(text: str, index: int) -> int:
    """The position just past the JSON value that starts at ``index``."""
    if index >= len(text):
        raise _ScanFailed()
    char = text[index]
    if char == '"':
        return _string_end(text, index)
    if char in "[{":
        depth = 0
        while index < len(text):
            here = text[index]
            if here == '"':
                index = _string_end(text, index)
                continue
            if here in "[{":
                depth += 1
            elif here in "]}":
                depth -= 1
                if depth == 0:
                    return index + 1
            index += 1
        raise _ScanFailed()
    end = index
    while end < len(text) and text[end] not in ",]} \t\r\n":
        end += 1
    if end == index:
        raise _ScanFailed()
    return end


# ==============================================================================================
# Paths that may be written
# ==============================================================================================


def _skill_name_of(proposal: Proposal) -> str:
    try:
        return validate_skill_name(proposal.skill_name)
    except LibrarianError as exc:
        raise _publish_failure(exc) from exc


def _cleaned_paths(proposal: Proposal) -> dict[str, str]:
    """Tidy every file name and refuse the ones that point somewhere they should not."""
    if not proposal.files:
        raise PublishFailed(
            "There is nothing in this change to publish, so nothing was changed.",
            detail="the proposal carried no files",
        )

    cleaned: dict[str, str] = {}
    for raw_path, content in proposal.files.items():
        path = _clean_path(raw_path)
        if not isinstance(content, str):
            raise PublishFailed(
                "The new contents of one of the files could not be read as text, so nothing was "
                "changed.",
                detail=f"{path} held {type(content).__name__}, not text",
            )
        cleaned[path] = content
    return cleaned


def _checked_proposal_files(
    plugin: PluginRef, skill_name: str, files: dict[str, str]
) -> dict[str, str]:
    """Every file in the proposal has to be part of this one skill, in this one collection.

    The manifests are deliberately not writable here. They are only ever written by the version
    bump above, so an edit can never quietly rewrite the files that control delivery.

    The collection folder is checked here as well as wherever the edit came from. It arrives
    from the library's own manifest, which is repository content that anybody able to open a
    pull request can change, so the last gate before a write gets no more trust in it than the
    first gate did. Every writable path is anchored to that folder, so a bad value there would
    weaken every other check in this function.
    """
    root = f"{validate_plugin_dir(plugin.plugin_dir)}/{SKILLS_DIR_NAME}/{skill_name}/"
    checked: dict[str, str] = {}

    for path, content in files.items():
        if path == MARKETPLACE_MANIFEST or path.endswith("/" + PLUGIN_MANIFEST_SUFFIX):
            raise UnsafePath(
                "This change tried to edit one of the settings files that control how the skills "
                "are delivered. Those are only ever updated by the publishing step itself, so the "
                "change was not made.",
                detail=f"{path} is a manifest",
            )
        if not path.startswith(root) or len(path) == len(root):
            raise UnsafePath(
                "This change tried to write a file that is not part of this skill, so it was not "
                "made. Only a skill's own instructions and the notes stored with it can be edited "
                "here.",
                detail=f"{path} is not inside {root}",
            )
        _assert_allowed_inside_skill(path, path[len(root) :])
        checked[path] = content

    return checked


def _assert_allowed_inside_skill(path: str, remainder: str) -> None:
    """Inside a skill folder, only the instructions file and reference notes may be written."""
    if remainder == _SKILL_FILE_NAME:
        return
    parts = remainder.split("/")
    if len(parts) == 2 and parts[0] == _REFERENCE_DIR_NAME and _REFERENCE_FILE.match(parts[1]):
        return
    raise UnsafePath(
        "This change tried to write a kind of file I do not handle. A skill is made of its "
        "instructions file and notes stored alongside it, and nothing else, so the change was not "
        "made.",
        detail=f"{path} is not an allowed file inside a skill",
    )


def _clean_path(raw_path: Any) -> str:
    if not isinstance(raw_path, str):
        raise UnsafePath(
            "One of the file names in this change is not readable, so it was not made.",
            detail=f"a file name was {type(raw_path).__name__}, not text",
        )

    path = raw_path.strip()
    while path.startswith("./"):
        path = path[2:]

    rejected = (
        not path
        or path.startswith("/")
        or path.startswith("~")
        or "\\" in path
        or "\x00" in path
        or "//" in path
        or ":" in path
        or any(part in ("", ".", "..") for part in path.split("/"))
    )
    if rejected:
        raise UnsafePath(
            "One of the file names in this change points somewhere it is not allowed to go, so the "
            "change was not made.",
            detail=f"refused the file name {raw_path!r}",
        )
    return path


# ==============================================================================================
# Branch, commit message and pull request wording
# ==============================================================================================


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", value.strip().lower()).strip("-")


def _branch_name(cfg: Config, proposal: Proposal, attempt: int = 0) -> str:
    """The name of the working copy for this change, worked out from the change itself.

    The same request always produces the same name, so a second try at the same change does not
    leave a trail of near duplicates behind it. When the change has to be started again on a
    newer starting point, the attempt number is added, which is still the same name every time
    for the same sequence of events.
    """
    skill = _slug(proposal.skill_name) or "skill"
    short_id = _slug(proposal.id)[:8] or "change"
    branch = f"{_BRANCH_PREFIX}{skill}-{short_id}"
    if attempt > 0:
        branch = f"{branch}-{attempt + 1}"
    if branch == cfg.default_branch:
        raise PublishFailed(
            "The change could not be given a working copy of its own, so nothing was published.",
            detail=f"the branch name collided with {cfg.default_branch!r}",
        )
    return branch


def _split_identity(requested_by: str) -> tuple[str, str]:
    """Turn the verified identity of the requester into a git author name and email."""
    if not isinstance(requested_by, str) or not requested_by.strip():
        raise PublishFailed(
            "It is not clear who asked for this change, so it was not published. A change is only "
            "published when it can be recorded under the name of the person who asked for it.",
            detail="the proposal carried no requester",
        )

    identity = requested_by.strip()
    match = _IDENTITY_WITH_EMAIL.match(identity)
    if match:
        email = match.group("email").strip()
        name = match.group("name").strip() or email
        return name, email

    if "@" in identity and " " not in identity:
        return identity, identity

    fallback = _slug(identity) or "requester"
    return identity, f"{fallback}@{_FALLBACK_EMAIL_DOMAIN}"


def _commit_message(
    proposal: Proposal,
    skill_name: str,
    new_version: str,
    author_name: str,
    author_email: str,
) -> str:
    summary = (proposal.plain_summary or "").strip() or "No summary was given."
    message = (
        f"Update the {skill_name} skill (version {new_version})\n"
        "\n"
        f"{summary}\n"
        "\n"
        f"Requested-By: {author_name} <{author_email}>\n"
    )
    approver = _approver_line(proposal)
    if approver:
        message = f"{message}{approver}\n"
    return message


def _approver_line(proposal: Proposal) -> str:
    """The person who let the change through, when that is somebody else.

    Who asked and who agreed are two separate facts, and the history has to be able to tell them
    apart. When one person did both there is only one fact, so only one line is written and the
    record does not read as though a second person looked at it.
    """
    approved_by = getattr(proposal, "approved_by", "")
    if not isinstance(approved_by, str) or not approved_by.strip():
        return ""

    approver_name, approver_email = _split_identity(approved_by)
    requester_name, requester_email = _split_identity(proposal.requested_by)
    if (approver_name, approver_email.casefold()) == (requester_name, requester_email.casefold()):
        return ""
    return f"Approved-By: {approver_name} <{approver_email}>"


def _pull_request_body(
    cfg: Config,
    proposal: Proposal,
    skill_name: str,
    plugin: PluginRef,
    new_version: str,
    author_name: str,
    author_email: str,
    files: dict[str, str],
) -> str:
    summary = (proposal.plain_summary or "").strip() or "No summary was given."
    file_lines = "\n".join(f"- {path}" for path in sorted(files))
    return (
        f"{author_name} asked for a change to the {skill_name} skill, which is part of the "
        f"{plugin.name} collection.\n"
        "\n"
        "What changed, in plain English:\n"
        f"{summary}\n"
        "\n"
        "Files updated:\n"
        f"{file_lines}\n"
        "\n"
        f"The version number of the {plugin.name} collection moves to {new_version}. That version "
        "number is what tells Claude to pick the change up. Without it the change would sit here "
        "and never reach anyone. No other collection in this library is touched.\n"
        "\n"
        f"Once this is merged the change is {estimated_live_by(cfg)}\n"
        "\n"
        f"Requested-By: {author_name} <{author_email}>\n"
    )
