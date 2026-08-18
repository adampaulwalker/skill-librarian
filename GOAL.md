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
- [ ] B1: Modules built to contract
- [ ] B2: Integrated, suite green
- [ ] B3: Multi-plugin discovery and ambiguity handling proven
- [ ] B4: Codex review round ends approved
- [ ] B5: Proven end to end against the live testbed repository: propose, publish, confirm, revert
- [ ] B6: Deployment path and setup instructions for a new team
- [ ] B7: Committed and pushed

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
| 1 | built single-tenant by mistake; spec hardcoded one plugin directory | spec generalized, discovery module added, testbed rebuilt with two plugins | in progress |
