# GOAL: Skill Librarian - a general tool

Reframed 2026-08-18 from a single-customer deliverable to a product. Any team that hits the
single-editor wall points it at their own repository.

## The problem it solves

A Claude skill has exactly one editor, its creator, and no version history. Sharing it, publishing it
to the company directory, or bundling it in a plugin all leave everyone else view-only. Confirmed in
the product 2026-08-18: the share dialog's permission menu offers no "can edit" option. Confirmed
against Anthropic's public record the same day: no announcement, no changelog entry, no roadmap item.
Anthropic shipped shared editing for artifacts and has edit tiers on projects, so a skills equivalent
is plausible but unannounced. The missing version history is not addressed anywhere at all.

## The only route with both reach and multiple editors

A plugin marketplace synced from a private GitHub repository. Verified against Anthropic's own
documentation, written up in `docs/marketplace-mechanics.md`. It is a developer workflow, and the
cost of using it is seven steps per edit. Step four, raising a version number, is the one that sinks
it: miss it and nothing syncs, with no error and no warning, so the user sees their change sitting in
GitHub, finds the old behavior in Claude, and concludes the tool is broken.

The librarian collapses those seven steps into: describe the change in chat, approve it, done.

## What "general" means here, concretely

- No customer, repository, plugin, or skill name anywhere in the source. Enforced by a test.
- Layout is discovered by reading the marketplace manifest, never assumed.
- A repository may hold several plugins; a plugin may hold several skills.
- Everything organization-specific is configuration or repository contents.
- Wording makes sense to any team, with no assumption about industry or role.

## Verified constraints the design must obey

1. A commit pushed straight to the default branch syncs to nobody. Publishing must be branch, version
   bump, pull request, merge.
2. The version string in the plugin manifest gates delivery. Unchanged version means users keep the
   cached copy, documented to happen without warning.
3. The marketplace repository must be private. Public is rejected.
4. Skill sources must be relative paths inside the marketplace repository.
5. Nothing is instant. Up to thirty minutes to sync, then each person picks it up next session. No
   polling interval is published, so never quote one.

## Roadmap

- [x] V: Mechanics verified against Anthropic documentation
- [x] R: Testbed repository, two plugins, four skills, one deliberate duplicate name
- [x] S: Build contract written and generalized
- [x] B1: Modules built to contract
- [x] B2: Integrated, suite green - 749 tests
- [x] B3: Multi-plugin discovery and ambiguity handling proven, live
- [x] B4: Two Codex review rounds completed, both came back critical, both closed and proven. A third
      round could not be obtained: the MCP call timed out on silence and the command-line fallback
      read 107,866 tokens and returned "Selected model is at capacity". A multi-model review is
      running in its place, and this is NOT a Codex sign-off.
- [x] B5: Proven end to end against the live testbed repository: list, ambiguity refusal, propose,
      bad-fingerprint refusal, anonymous-publish refusal, publish, history, revert
- [x] B6: Deployment path and setup instructions for a new team - README.md
- [x] B7: Committed and pushed - github.com/adampaulwalker/skill-librarian (private)

## Completion criteria

- [ ] A publish can never ship without a version bump, proven by a test that fails if it does
- [ ] A publish never writes to the default branch directly
- [ ] Approval re-checks the fingerprint of the exact content shown, and refuses a mismatch
- [ ] No writable path escapes the skills directory of the owning plugin
- [ ] Commit author is the human who asked; committer is the app; neither is model-supplied
- [ ] Duplicate skill names across plugins raise an ambiguity error
- [ ] No hardcoded organization anywhere, proven by a test
- [ ] Full suite green, Codex approved, end-to-end run against a real private repository

## Loop protocol

1. Build to the contract
2. Integrate, run the suite
3. Codex reviews the diff; any major finding re-enters step 1
4. Prove it live against the testbed repository
5. Repeat until a full round is clean

## Round log

| Round | Found | Fixed | Verdict |
|---|---|---|---|
| 0 | mechanics verified, no blocker | - | route holds, one human check outstanding |
| 1 | built single-tenant by mistake; spec hardcoded one plugin directory | spec generalized, discovery module added, testbed rebuilt with two plugins | done |
| 2 | integrator found publisher trusted a raw plugin_dir the edit path validated, so the last gate before a write trusted repository content the first gate refuses | validate_plugin_dir now runs at the write gate, regression test added | done |
| 4 | Codex round one, CRITICAL: two concurrent publishes could each compute the same next version, so the second shipped content while the shared copy already carried that number. Merge was not pinned to the approved commit. The client could write to the default branch. Any identified person could approve someone else's proposal while the history kept the original name. | version recomputed and verified across the merge, merge pinned to the approved commit, default-branch writes refused in the real client, approver recorded separately from requester | done, 816 tests |
| 5 | Codex round two, CRITICAL: the guard read the right value and then merged anyway, checking only afterwards. Detection where prevention belonged. Three test doubles had silently stopped damaging anything, so their tests passed while proving nothing. delete_branch existed only on the fake. The deployed approve description omitted the honest warning. | guard moved before the merge and proven in both directions, doubles repaired to damage the landed tree, delete_branch implemented on the real client, deployed description now the single service constant | done, 850 tests |
| 6 | own sweep: the generality tripwire only read the package source, so fixtures still carried a customer folder name, a real first name, and a real organization | fixtures cleared, tripwire widened to the tests, proven to fire by planting a name | done, 858 tests |
| 3 | live run exposed two defects the fakes missed: a garbled delivery sentence ("around usually within about 30 minutes"), and a 404 for an absent optional folder logged as a failure | wording composed in one place, 404 moved to debug, regression assertion added | done |
| 4 | shared fake drifted from the real client (old `merge_pr` signature, an escape hatch for writing straight to the default branch, a merge that stamped a whole snapshot over the shared copy); the pull request was opened before the version was recomputed, so it could name a version that never shipped; path validation defined twice | fake now holds the real client's promises and can move the shared copy mid-publish, pull request opened after the recompute, marketplace delegates to `paths.validate_plugin_dir`, `diffing.py` deleted, race proved by removing the guard and watching it fail | done |

## Proven live, 2026-08-18

Against `adampaulwalker/skill-librarian-testbed`, a real private repository holding two plugins:

| Step | Result |
|---|---|
| List skills across both plugins | 4 skills in 2 collections, duplicate name flagged |
| Read a skill whose name exists twice | Refused, naming both collections |
| Propose an edit | Plain-English summary, fingerprint issued |
| Approve with a wrong fingerprint | Refused |
| Approve with no identity | Refused |
| Approve properly | Pull request opened and merged, version 1.0.0 to 1.0.1 |
| Commit author / committer | `Ellie Mitchell <ellie@example.com>` / `Skill Librarian` |
| Sibling plugin manifest | Untouched at 1.0.0 |
| History | Two changes, each naming who asked |
| Revert | Published forward as 1.0.2, never a rewrite |

## Proven by experiment, not assertion

| Property | How it was shown |
|---|---|
| The version guard prevents rather than reports | Competing publish in the gap: gate in place, merge called 0 times and nothing landed. Gate removed, merge called once and content landed under an already-delivered version. Source restored byte-identical. |
| Rebuild on a moved head keeps attribution honest | Competitor takes 1.4.3 and touches another file. This publish rebuilds and goes out at 1.4.4. Every commit it makes carries only the skill and the two manifests, never the other person's file. |
| Partial failure never half-publishes | Injected failure at create branch, commit, open pull request, and merge. Every one fails closed: no orphaned branches, shared copy untouched, plain-English message. |
| The generality tripwire actually fires | Planted a customer name in a test file, watched it fail, removed it, watched it pass. |

## Round log, review rounds

| Round | Verdict | What it found | State |
|---|---|---|---|
| 1 | CRITICAL | Concurrent publishes could each compute the same next version, so the second shipped content under a number already delivered. Merge was not pinned. The client could write to the default branch. Anyone could approve another person's proposal while the history kept the original name. | closed, proven |
| 2 | CRITICAL | The version guard read the right value and merged anyway, checking only afterwards. Three test doubles had silently stopped damaging anything, so their tests passed while proving nothing. | closed, proven |
| 3 | CRITICAL | A lost update. A rebuild committed frozen approved wording onto a moved head without checking the file had changed underneath, so a concurrent edit to the same skill was destroyed and the publish reported success. Five more of the same shape found while fixing it. | closed, proven |

The recurring bug class, all three times: read something that moves, then act as though the earlier
reading still holds.

## Still open

- Deployment: the GitHub App needs registering, and the service needs hosting.
- One human check nobody can do without an organization owner's screen: whether the GitHub source
  option appears under Organization settings, Plugins, Add plugin. Anthropic called it private beta in
  February and documents it as normal in August.
- Fake versus real drift: `delete_branch` exists only on the fake in `tests/test_publisher.py`. The
  real `GitHubClient` has no such operation, so `_remove_working_copy` is a no-op in production and a
  failed publish leaves its branch behind. Retrying the same proposal then collides on the branch
  name, because the name is built from the proposal id. The cleanup tests pass only because that one
  fake can do something production cannot.
- Three fake GitHub clients remain: the shared one in `tests/fakes.py`, `LocalFakeGitHub` in
  `tests/test_publisher.py`, and `StrictFakeGitHubClient` in `tests/test_marketplace.py`.
- Closed 2026-08-18: duplicate path validation (marketplace now delegates to
  `paths.validate_plugin_dir`), dead code in `diffing.py` (deleted), and the shared fake's drift from
  the real client (it now enforces the approved head and refuses the default branch).
