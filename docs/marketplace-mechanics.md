# Verdict Sheet: Private GitHub repo → org plugin marketplace, Claude Team plan (7 non-technical recruiters)

## Evidence quality, up front

All five findings ultimately rest on **one** Anthropic support article: `support.claude.com/en/articles/13837433-manage-plugins-for-your-organization` (HTML dateModified 2026-08-06), plus the developer docs at `code.claude.com/docs/en/plugin-marketplaces` and `/plugins-reference` (dateModified 2026-08-17). That is a single-source dependency. If that article is wrong, stale, or describes an unreleased state, four of the five answers move at once.

Below, **QUOTED** means the exact string appears in the finding's evidence array. **INFERRED** means an agent reasoned from the absence of a restriction, or carried a rule from one surface (Claude Code command line) to a different surface (claude.ai org sync) without a quote covering that surface.

---

## 1. Is the route available on a TEAM plan?

**VERIFIED yes on plan eligibility. UNVERIFIED on whether the GitHub source option is actually rendered for this org today.**

Split it into two claims, because the findings blur them.

| Claim | Status | Basis |
|---|---|---|
| Owners/Primary Owners on **Team** can manage org plugins at Organization settings > Plugins | **QUOTED** | "Owners and Primary Owners of Team and Enterprise plans can manage organization plugins in Organization settings > Plugins." |
| Marketplaces are for "Team and Enterprise plan owners" | **QUOTED** | Same article, opening line |
| Cowork + Skills must both be enabled first | **QUOTED** | "Cowork and Skills must both be enabled for your organization before you can use plugin marketplaces." |
| GitHub source is offered alongside zip upload | **QUOTED** | "Click 'Add plugin' and select 'GitHub' as the source." |
| **GitHub source is available on Team specifically** | **INFERRED** | No quote gates it by plan, and no quote grants it to Team either. Finding 1 inferred availability from silence. |
| Group-level plugin access is Enterprise-only | **QUOTED** | Does not block this route |

### Findings 1 and 3 disagree. Finding 3 is right.

Finding 1 asserts flatly that "nothing in the article restricts the GitHub source to Enterprise" and calls the route supported. Finding 3 surfaces the contradicting evidence Finding 1 missed: the Anthropic blog of February 24, 2026 says admins get "private GitHub repositories as plugin sources **(in private beta)**."

Finding 3 has the better evidence because it holds both documents and names the conflict rather than resolving it by silence. The reconciliation: the help article is newer (2026-08-06 vs 2026-02-24) and documents the flow as a normal procedure with no beta label, which is weak evidence the beta ended. It is not proof. The failure mode is not a refusal message, it is the "GitHub" option simply not appearing in the Add plugin dialog.

**Practical read:** treat plan eligibility as settled and treat feature visibility as a 30-second thing a human confirms by opening the dialog.

---

## 2. What authenticates Claude to the private repo, and who holds the credential?

**VERIFIED, two layers, both quoted.**

| Layer | Credential | Held by | When used |
|---|---|---|---|
| Access check | The connecting person's own GitHub connection ("your personal GitHub token") | The Owner or Primary Owner doing the connecting | Once, at connect time, to prove that person can see the repo |
| Ongoing sync | The **Claude GitHub App installation token** | Anthropic's GitHub App, installed on the repo | Every sync thereafter |
| Auto-sync webhook | The toggler's personal GitHub connection, needing **admin-level** repo access, plus the App's **Webhooks (Read and Write)** permission approved | The person flipping "Sync automatically" | Once, to create the webhook |

QUOTED: "Your personal GitHub token is verified to confirm you have access, then Cowork uses its GitHub App installation token for sync operations." And: "Can't see your repo? Make sure the Claude GitHub App is installed in that repository."

Also QUOTED and non-negotiable in the other direction: the repo **must be private or internal**. "Public repos aren't allowed for organization marketplaces." This is the reverse of the Claude Code command-line behavior, where public is fine. Getting this backwards is an easy mistake.

**INFERRED, not quoted:** that there is no personal access token field, no deploy key, and no separate connector step. Finding 2 states this as fact. It is an argument from absence: those things are not described, so they were assumed not to exist. Finding 2 does honestly admit the related gap, that the article "never states literally where the 'personal GitHub token' is entered."

---

## 3. Is a version bump required? Is failure silent?

**VERIFIED yes to both, and this is the sharpest operational risk in the whole design.**

Two independent gates, each with the same symptom (nothing happens) and no error:

**Gate A, org sync trigger.** QUOTED: "automatic sync runs when a pull request that includes a plugin version bump is merged to the repository's default branch. Direct pushes to the default branch don't trigger a sync. You can always trigger a sync manually by clicking 'Update' on the marketplace."

So a direct commit to `main` does nothing at all. The change must arrive as a merged pull request that carries a version bump.

**Gate B, each user's plugin cache.** QUOTED: "If you declare `\"version\": \"1.0.0\"` in `plugin.json` and push new commits without changing that string, existing users of those sources keep the cached copy, because Claude Code sees the same version."

**Silence is documented, not just undocumented.** No user-visible error exists for changed-content-with-unchanged-version anywhere in the sources read. And for the closest analogous case the docs affirmatively promise silence: "Claude Code always uses the `plugin.json` value **without warning**, so a stale manifest version can mask a version you set in `marketplace.json`." The only version-mismatch warning that exists anywhere is in the author-run `claude plugin validate` command, and only for marketplace entries whose source is a **local path**.

### A conflict the findings did not resolve

Finding 3 (the sync-behavior finding) presents an escape hatch: omit the `version` field entirely for git-based sources, and the resolved commit SHA becomes the version, so users update on every commit. That is **QUOTED, but only from the Claude Code developer docs**, describing the Claude Code client.

Applying it to the org marketplace is **INFERRED and probably wrong**, because it collides head-on with Gate A: if no `version` field exists, there is no "plugin version bump" for a pull request to include, so org auto-sync plausibly never fires at all. Nobody verified this. Do not build on the omit-version trick for the org route.

**Timing, corrected.** Finding 1 loosely implies a 30-minute sync cadence. Finding 4 is more careful and is correct: there is **no documented polling interval** for org sync. The two numbers that exist mean different things.

| Number | What it actually is |
|---|---|
| Up to 30 minutes | Duration of one org sync operation once it starts (also the per-operation timeout) |
| Up to 10 minutes | Random delay after a Claude Code **session start** before the client checks for plugin updates |
| No figure | How often the org marketplace polls the repo. None published. Do not quote one to the client. |

Once a sync does run it is commit-based, not version-based: QUOTED, it "compares the latest commit in your repo against the last-synced commit... and replaces all plugins in the marketplace with the current state of the repo." The version bump is the **trigger**, not the payload.

Finding 3 also lists GitHub issues (43763, 41885, 61854, 72089, 72616) alleging real-world propagation failures and honestly labels them unread pointers, not evidence. Treat them that way.

---

## 4. Exact repo layout and manifest format

**VERIFIED against code.claude.com docs dated 2026-08-17 and 2026-08-18. This is the best-evidenced finding of the five.**

```
<repo root>/                      (private or internal on GitHub)
├── .claude-plugin/
│   └── marketplace.json          REQUIRED, this exact path
└── plugins/
    └── recruiter-skills/         one folder per plugin
        ├── .claude-plugin/
        │   └── plugin.json       technically optional; effectively REQUIRED here (see below)
        └── skills/
            └── screen-resume/
                └── SKILL.md      plus optional reference.md, scripts/
```

**marketplace.json** — required: `name` (kebab-case), `owner` (object with required `name`), `plugins` (array). Optional: `$schema`, `description`, `version`, `metadata.pluginRoot`, `allowCrossMarketplaceDependenciesOn`, `renames`.

Each entry in `plugins` requires only `name` and `source`.

```json
{
  "name": "recruiting-tools",
  "owner": { "name": "Ops" },
  "plugins": [
    { "name": "recruiter-skills", "source": "./plugins/recruiter-skills", "version": "1.0.0" }
  ]
}
```

**plugin.json** — QUOTED: "If you include a manifest, `name` is the only required field." Everything else optional: `displayName`, `version`, `description`, `author`, `homepage`, `repository`, `license`, `keywords`, `metadata`, `defaultEnabled`, and component paths (`skills`, `commands`, `agents`, `workflows`, `hooks`, `mcpServers`, `outputStyles`, `lspServers`, `experimental.*`, `userConfig`, `channels`, `dependencies`).

Caveat the findings did not connect: plugin.json is documented as optional, but since **the auto-sync trigger is a version bump**, and since **plugin.json's version overrides the marketplace entry's version without warning**, the librarian design needs plugin.json present with a `version` it bumps on every change. Optional in the schema, mandatory in this architecture.

**SKILL.md frontmatter** — QUOTED: "All fields are optional. Only `description` is recommended so Claude knows when to use the skill."

Hard constraints, all QUOTED:

- Source types for a private org marketplace: **relative paths only** (`"./plugins/x"`, must start with `./`, `../` disallowed). `github`, `url`, and `git-subdir` work only against **public** targets. `npm` and `pip` are unsupported. Private plugin code in a second repo must be copied in (submodule, subtree, or a CI step).
- Relative paths resolve against the **marketplace root** (the directory containing `.claude-plugin/`), not against `.claude-plugin/` itself.
- Reserved marketplace names are blocked and re-checked on every load, not just at add time: `claude-code-marketplace`, `claude-code-plugins`, `claude-plugins-official`, `claude-plugins-community`, `claude-community`, `anthropic-marketplace`, `anthropic-plugins`, `agent-skills`, `anthropic-agent-skills`, `knowledge-work-plugins`, `life-sciences`, `claude-for-legal`, `claude-for-financial-services`, `financial-services-plugins`, `first-party-plugins`, `healthcare`.
- One marketplace per name per user. Adding a second with the same name replaces the first.
- Plugins are copied into `~/.claude/plugins/cache`, so a plugin cannot reference files outside its own directory.
- Validation exists: `claude plugin validate .`, with `--strict` for continuous integration.

**INFERRED, worth flagging:** Finding 4 advises restricting SKILL.md frontmatter to the six fields (`name`, `description`, `license`, `compatibility`, `metadata`, `allowed-tools`) accepted by claude.ai uploads, the Skills API, and `package_skill.py`. That six-field limit is QUOTED, but only for those three surfaces. Whether **org plugin sync via Cowork** enforces the same allowlist is not stated anywhere. Sticking to the six is cheap insurance, not a verified requirement.

---

## 5. Remote MCP connector on Team

**VERIFIED available. The org-authorize-once story is where the finding correctly refuses to overclaim.**

QUOTED: custom connectors using remote MCP are available on Free, Pro, Max, **Team**, and Enterprise. "Only Owners can add them to Team and Enterprise plans." Path: Organization settings > Connectors > Add > Custom > Web > paste the remote MCP server URL. Members then go to Customize > Connectors and click Connect, then enable per conversation via the "+" button.

Six auth types are documented: `oauth_dcr` and `oauth_cimd` (supported out of the box), `oauth_anthropic_creds` (by request), custom connection credentials (by request), `static_headers` (fixed API key or bearer token, **beta**), and `none`.

| Auth model | Status for a Team org today |
|---|---|
| Per-member OAuth 2.0 sign-in | **Default and available.** QUOTED: users individually connect "so that Claude can only access tools and data that the individual user has access to." Requires PKCE with S256; redirect URI `https://claude.ai/api/mcp/auth_callback` |
| Authless (`none`) | Available |
| One org-wide credential via `static_headers` | **Beta, gated.** QUOTED: "being slowly rolled out to customers; contact Anthropic for early access." Max four headers from a reviewed allowlist (`authorization`, `x-api-key`, `x-auth-token`, etc.) |
| Enterprise-managed auth (authorize once org-wide) | **Beta by application, Okta only, and the listed connectors are Asana, Atlassian, Canva, Figma, Granola, Linear, Supabase.** Nothing says a self-hosted custom connector can be enrolled. Treat as UNVERIFIED for custom connectors |
| Machine-to-machine `client_credentials` | **QUOTED as not supported.** "Every connection requires user consent" |

Infrastructure constraint on any plan: Anthropic reaches the server from the public internet, outbound from `160.79.104.0/21`. Anything behind a VPN or corporate firewall will not connect.

No numeric per-plan cap on custom connectors is documented for Team. Only Free is capped, at one. Absence of a stated cap is not a stated absence of a cap.

---

## 6. What a human must check by opening the product

Each of these is under two minutes. Run them in this order, because 1 through 3 gate everything after.

| # | Check | Where | What answers it |
|---|---|---|---|
| 1 | Are **Cowork** and **Skills** both on for the org? Skills needs "Code execution and file creation" plus "Skills" toggled | claude.ai > Organization settings > Skills, and the Cowork setting | Both toggles read as enabled. If either is off, the Plugins area does not work at all |
| 2 | Does **Organization settings > Plugins** load for the Owner account? | `claude.ai/admin-settings/plugins` | Page renders with an "Add plugin" button |
| 3 | Click **Add plugin**. Is **GitHub** listed as a source? | Same screen | Three options expected: Browse Anthropic sources, Upload a file, GitHub. **If GitHub is missing, the private-beta gate is real for this org and the design changes.** This is the single highest-value check on the list |
| 4 | Type `owner/repo`. Does the repo resolve? | Same dialog | If it does not appear, the Claude GitHub App is not installed on that repo. Install it, retry |
| 5 | Confirm the repo is **private or internal** on GitHub | github.com repo settings | Public is rejected outright |
| 6 | Flip **Sync automatically**. Does it succeed or throw? | Marketplace detail screen | Proves the toggler holds GitHub admin on the repo and that the App's Webhooks (Read and Write) permission is approved |
| 7 | Watch what happens at connect time: does a GitHub sign-in or authorize prompt appear, and under whose account? | The connect flow | The docs never say where the "personal GitHub token" is established. This is the one step nobody could document |
| 8 | After a manual **Update**, does a recruiter see the plugin, and how long did it take? | One recruiter's Claude session | Establishes real end-to-end latency, since no poll interval is published |
| 9 | Does the org see any cap on marketplaces or plugin count? | Plugins screen after adding one | No published limit either way |
| 10 | In Add custom connector, does a **Request headers** section appear? | Organization settings > Connectors > Add > Custom > Web | Determines whether `static_headers` beta is enabled for this org, which decides whether recruiters each sign in or share one credential |

**The one decisive test that is not two minutes** (budget 45 minutes, run it before quoting the client any behavior): merge a pull request that changes SKILL.md content **without** bumping `version`, then observe whether an installed recruiter ever receives it. Then repeat **with** a bump. That single experiment settles Gate A, Gate B, real latency, and whether any warning surfaces, in one pass, and it replaces every inference in section 3 with observation.

---

## Does anything kill the "skill librarian" design?

**No hard blocker was found. But two things reshape it, and one gap sits entirely outside what the five findings investigated.**

### Not blockers, but binding constraints

1. **The librarian cannot push to `main`.** Direct pushes trigger nothing. Every edit must become a branch, a `version` bump in `plugin.json`, a pull request, and a merge. That is the mechanism, quoted. A librarian that commits straight to the default branch produces a perfect no-op with no error, forever.
2. **Skill sources must live inside the marketplace repo** and be referenced by relative path. No pulling skills from a second private repo.
3. **Nothing is instant.** Sync can take up to 30 minutes, then each recruiter picks it up on their next session or plugin refresh. Seven non-technical people who expect a saved edit to appear immediately will read normal latency as breakage. Build a visible "published, live for everyone by ~HH:MM" confirmation, or they will file the tool as broken.
4. **The Owner must complete the one-time connect and the auto-sync toggle**, and must hold GitHub admin on the repo to do it.

### The real gap nobody verified

Every finding covers how **Claude reads** the repo. **None covers how the librarian writes to it.** The Claude GitHub App installation token is Anthropic's, used for sync, and is not available to your code. So the librarian needs its own separate GitHub write credential (a machine user with a fine-grained personal access token, or your own GitHub App) with contents-write and pull-request-write on that one repo. That is ordinary work, not a blocker, but it is unscoped in the current findings and it is the piece that actually has to be built.

Related and equally unverified: **there appears to be no API for the org marketplace itself.** Every documented action (add, connect, Update, toggle) is a claude.ai admin screen. So GitHub is the only programmatic surface the librarian has. Design accordingly, and do not promise the client any admin-side automation.

### The one thing that would kill it, and the fallback

**If check #3 comes back negative** and a Team org cannot see the GitHub source option, the repo-sync route is dead for now. Ranked alternatives:

1. **Zip upload with a human in the loop.** QUOTED as available: "Upload a file" accepts a valid .zip under 50 MB. The librarian still edits the repo and continuous integration still builds the zip, but an Owner clicks upload. Costs about two minutes of one person's time per release and keeps the entire authoring pipeline intact. This is the nearest workable alternative and the one I would plan around.
2. **Organization settings > Skills provisioning**, skipping plugins entirely. QUOTED: "Organization-wide skill management is available to Team and Enterprise plans." For seven non-technical recruiters who need skills and nothing else, this is arguably the better-fitting product surface regardless. Unverified: whether it has any API, or is also upload-only. Worth ten minutes of checking before the plugin route gets more investment.
3. **Per-user `/plugin marketplace add owner/repo` in Claude Code.** Works, and supports private repos via the user's own git credentials, but it requires each recruiter to run a command-line tool. For this audience, discard it.

**Bottom line:** the design survives, conditional on check #3. The thing most likely to make it fail quietly in production is not a plan restriction, it is a librarian that edits files without bumping `version` and merging through a pull request.