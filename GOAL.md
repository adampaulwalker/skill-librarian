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
- [ ] B4: Codex review round ends approved - running
- [x] B5: Proven end to end against the live testbed repository: list, ambiguity refusal, propose,
      bad-fingerprint refusal, anonymous-publish refusal, publish, history, revert
- [ ] B6: Deployment path and setup instructions for a new team
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
