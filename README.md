# Skill Librarian

Change a shared Claude skill by describing the change in chat and approving it.

## The problem

A skill in Claude has exactly one editor, whoever created it. Share it, publish it to the company
directory, or bundle it in a plugin, and everyone else is read-only. There is also no version history
and no undo. Checked against Anthropic's own documentation on 2026-08-18: no fix is announced, and
the missing history is not mentioned anywhere at all.

The one route with both organization-wide reach and more than one editor is a plugin marketplace
synced from a private GitHub repository. That route is a developer workflow, and using it by hand
costs seven steps per edit. One of those steps sinks it for anyone non-technical: a change only
reaches people when a merged pull request also raises a version number. Miss it and nothing syncs,
with no error and no warning. You see your change sitting in GitHub, open Claude, find the old
behavior, and reasonably conclude the whole thing is broken.

This collapses those seven steps into: describe the change, approve it, done.

## What it is

A remote Model Context Protocol server. Added once to a Claude organization as a custom connector, it
appears as tools inside an ordinary chat. Six things it does: list the skills, read one, propose a
change, publish an approved change, show a skill's history, and put a skill back to how it was.

Nobody needs a GitHub account, and nobody opens a terminal. Every change records the name of the
person who asked for it, because a commit's author and its committer are separate things: the service
writes the change while recording the person as its author.

## Setup

### 1. A private repository for your skills

It must be **private**. Anthropic rejects a public repository as an organization marketplace, which is
the reverse of the developer tool's behavior.

```
your-skills-repo/
  .claude-plugin/marketplace.json          the list Claude reads
  plugins/<collection>/
    .claude-plugin/plugin.json             carries the version that gates delivery
    skills/<skill-name>/SKILL.md           one folder per skill
    skills/<skill-name>/reference/*.md     optional supporting files
```

`marketplace.json` needs `name`, `owner.name`, and a `plugins` array. Each entry needs `name` and a
`source` that is a **relative path** beginning with `./`. Other source types are only supported
against public repositories, so a private setup cannot use them.

A repository may hold as many collections as you like, each holding as many skills as you like. The
layout is discovered at runtime, never assumed. Two skills sharing a name across collections is an
error the service reports rather than guessing between.

### 2. A GitHub App for the librarian

The librarian needs its own write credential. Anthropic's GitHub App handles Claude's side of the
sync and is not available to your code.

Register a GitHub App with **Contents: read and write** and **Pull requests: read and write**, install
it on that one repository, and keep its private key in your secret manager. It mints short-lived
tokens rather than holding a permanent one, and it can reach nothing else.

### 3. Run the service

```bash
uv venv && uv pip install -e ".[dev]"

export LIBRARIAN_REPO_OWNER=your-org
export LIBRARIAN_REPO_NAME=your-skills-repo
export LIBRARIAN_GITHUB_APP_ID=...
export LIBRARIAN_GITHUB_PRIVATE_KEY_PATH=/path/to/key.pem
export LIBRARIAN_GITHUB_INSTALLATION_ID=...

uv run uvicorn librarian.server:app --host 0.0.0.0 --port 8000
```

Anthropic reaches the server from the public internet, outbound from `160.79.104.0/21`, so anything
behind a VPN will not connect. `/health` answers for a load balancer.

For local development only, set `LIBRARIAN_DEV_TOKEN` to a GitHub token instead of the app settings.
The service warns loudly when it starts that way.

### 4. Connect it to Claude

An organization Owner does both of these once.

**The marketplace.** Organization settings, then Plugins, then Add plugin, then GitHub, then the
repository in `owner/repo` form. Cowork and Skills must both be enabled for the organization first or
the Plugins area does not work at all. If the repository does not appear, the Claude GitHub App is not
installed on it.

**The librarian.** Organization settings, then Connectors, then Add, then Custom, then the server URL.
Each person then connects it once from Customize, then Connectors.

### 5. Confirm before trusting it

Publish one change and watch it arrive. A sync can take up to half an hour and each person picks it up
the next time they start a chat. No polling interval is published, so the service always expresses
timing as a window and offers to confirm a change landed rather than quoting a number.

## What protects you

- **A publish cannot ship without the version moving.** Enforced before the merge, not detected after,
  and proven by removing the guard and watching the bad merge happen.
- **A publish never writes to the default branch.** Refused in the client itself.
- **A publish never overwrites somebody else's change.** Every approved file is re-read before merging
  and the publish refuses if any of them moved, so nobody's work disappears quietly.
- **Approval is bound to the exact text shown.** A fingerprint covers the content and the commit it
  was computed against, and it is rechecked before anything is published.
- **The service can touch nothing but skill files.** No arbitrary paths, no commands, no other
  repository. Enforced in code, not requested in an instruction.
- **The history is honest.** The person who asked and the person who approved are both recorded, and
  differ where they differ.

### One thing it cannot do

The fingerprint proves the text being published is the text that was shown. It does **not** prove a
person agreed to it, because both the fingerprint and the proposal reference appear in the
conversation, and anything that can read the conversation can repeat them. Real approval has to be
collected by whatever hosts the connector. The tool description says so plainly rather than implying
otherwise.

## Development

```bash
uv run pytest -q
```

`SPEC.md` is the build contract. `docs/marketplace-mechanics.md` records what was verified against
Anthropic's documentation, with quotes and sources, including what remains unverified.

`tests/test_generality.py` fails if a customer, organization, or person's name appears anywhere in the
source or the tests. This is a general tool and that test is what keeps it one.
