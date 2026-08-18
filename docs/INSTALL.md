# Installation and configuration

This document lists the files you must touch when first setting up
reasona-dev, and why. Its main purpose is to separate those files from
auto-generated artifacts -- hand-editing an auto-generated artifact gets it
overwritten on the next `compile-plan`, and conversely, mistaking a
required config file for an auto-generated artifact and leaving it empty
makes the first run fail.

## 0. Prerequisites

| Item | Requirement | Check |
|---|---|---|
| Python | **>= 3.12** | `python3 --version` |
| Bernstein | >= 3.15.1 | `bernstein --version` |
| Adapter auth | login for whichever CLI adapter you use | `bernstein doctor` |
| `gh` CLI | only for the merge tail (`--ship`/`--merge`) | `gh auth status` |

The Python 3.12 floor is Bernstein's own requirement. Go lower and `uv`
cannot resolve the dependency (`No solution found ... bernstein cannot be
used`).

`gh` is optional. Using `--ship` without being authenticated makes the
merge tail block at that point with a named error, rather than silently
skipping it.

```bash
uv tool install bernstein
uv pip install -e .        # or the published reasona-dev
```

## 1. Execution model -- not an always-on daemon

Something you need to know before looking at the config files: a single
`run-plan` invocation spins up its own Bernstein instance and tears it down
when it finishes.

```
run-plan
  |- bernstein serve   +  bernstein worker     <- startup
  |- per-unit review -> scan -> ship (-> merge tail)
  \- both terminate                            <- shutdown
```

The processes persist **only within one run** (the whole plan shares a
single server, and the worker blocks until SIGTERM). Nothing survives
between runs. Bernstein does support an always-on central node with remote
workers (`BERNSTEIN_BIND_HOST=0.0.0.0` + `BERNSTEIN_CLUSTER_ENABLED=1`,
`bernstein worker --server URL --token`), but reasona-dev has no wiring for
it yet -- `start_server` always both starts and stops it itself.

## 2. Required config -- three global files

All of these live under `~/.reasona/`. This repository's own `.reasona/`
is a working example, so the fastest start is to copy it.

```bash
mkdir -p ~/.reasona
cp -r <reasona-dev-repo>/.reasona/prompts            ~/.reasona/prompts
cp    <reasona-dev-repo>/.reasona/bernstein-template.yaml ~/.reasona/
cp    <reasona-dev-repo>/.reasona/reasona.yaml       ~/.reasona/reasona.yaml
```

### 2.1 `~/.reasona/reasona.yaml` -- per-role models

```yaml
dev-models:
  dev: claude:sonnet:high
  review: claude:opus:high
  recheck: claude:sonnet:high
  bugbot: claude:opus:high
  verify: claude:sonnet:high
  final_audit: claude:opus:high
  dev_escalation: claude:opus:high
```

The format is `tool:model:effort`, and the flag, the env var, and this file
all accept it identically. A bare model name (`opus`) is also valid --
adapter and effort then fall back to that role's defaults.

**The pipeline still runs without this file, but that isn't recommended.**
Here is what actually gets resolved when the file is absent.

| Role | Default | Note |
|---|---|---|
| `dev` | `claude:sonnet:high` | |
| `review` | `claude:opus:high` | |
| `recheck` | `claude:opus:high` | falls back to `review` -- bounded recheck's cost savings disappear |
| `verify` | `claude:sonnet:high` | used by the compliance role |
| `bugbot` | **`kilo:deepseek-v4-pro:high`** | fails in any environment without kilo auth |
| `final_audit` | `claude:opus:high` | |
| `dev_escalation` | `claude:opus:high` | |

The fact that `bugbot`'s default adapter is `kilo` matters in particular --
any environment not using kilo must override it. Leaving `recheck`
unspecified also makes it equal to `review`, nullifying the savings path
where a fix that stays inside the finding's scope is confirmed with a
cheap model instead.

**`effort` also sets the turn budget** -- the coupling most easily missed
in configuration. reasona-dev dispatches every role at `scope: large`, and
Bernstein's claude adapter computes the budget as
`effort_base_turns[effort] x scope_multipliers[scope]`.

| effort | turn budget (scope=large) |
|---|---|
| `max` | 200 |
| `high` | **100** |
| `medium` | 60 |
| `normal` | 50 |
| `low` | **30** |

The review prompt enumerates every checklist item and named symbol,
greps the diff, and writes its report only as **the last action**. So when
the budget runs out mid-exploration, the result isn't truncated -- it
disappears entirely, and the role is treated as producing no output
(ERROR). In practice a reviewer set to `low` failed this way twice.

**`review` and `final_audit` should stay at `high` or above.** Lowering
them to `low` to cut cost doesn't just make the model cheaper -- it stops
the review from completing at all.

### 2.2 `~/.reasona/prompts/<profile>/*.md` -- role prompts

**There is no prompt bundled inside the package.** If a file exists at
neither the project layer nor the global layer, `resolve_prompt` returns
`None` and the cycle ABORTs. This is intentional -- a repository that
never chose a review prompt should be rejected, not silently reviewed
against someone else's.

The `generic` profile needs 5 files.

```
~/.reasona/prompts/generic/
  review.md        verify cycle
  recheck.md       bounded recheck (confirm + regression)
  bugbot.md        scan
  compliance.md    scan
  final_audit.md   conditional full-PR audit
```

Check which profiles are currently visible with:

```bash
reasona-dev prompts --workdir <repo>
# generic: bugbot, compliance, final_audit, recheck, review
```

**Do not change the item notation when editing a prompt.** A mismatch
between the shape the parser accepts and the shape the prompt instructs
drops findings, and that failure shows up as a silent false PASS (for a
real case, see `docs/ARCHITECTURE.md` §3.8.2).

### 2.3 `~/.reasona/bernstein-template.yaml` -- the Bernstein seed source

Copied from here whenever a target repository has no
`.bernstein/bernstein.yaml` of its own.

```yaml
goal: >
  ...
cli: claude
role_model_policy:
  backend:    {provider: claude}
  reviewer:   {provider: claude}
  bugbot:     {provider: claude}
  compliance: {provider: claude}
  final_audit: {provider: claude}
```

**All 5 roles in `role_model_policy` must be present.** This block also
doubles as the task server's **role whitelist** -- creating a task with a
role not on the list makes `POST /tasks` return 400 (confirmed live). If
`final_audit` is missing, the merge tail's audit dispatch fails the moment
it's attempted.

The `provider` values are auto-synced by `compile-plan` on every run to
match `model_config`'s resolved outcome, so hand-tuning them is
unnecessary.

## 3. Per-repository config -- optional

All of these live under `<repo>/.reasona/`, and override the global layer
**per file**. If a project only supplies `review.md`, every other role
still uses the global one.

| Path | Purpose |
|---|---|
| `<repo>/.reasona/reasona.yaml` | model / profile / profile mapping |
| `<repo>/.reasona/prompts/<profile>/*.md` | repo-specific prompts |
| `<repo>/.reasona/bernstein-template.yaml` | project-local seed template |

### 3.1 Mixed-language repositories -- per-unit profiles

When a single repository mixes language modules, one repo-wide profile
can't represent it. Reviewing a Python service against Rust-aware prompts
is worse than having no profile at all -- it produces confident findings
drawn from the wrong rulebook.

```yaml
# <repo>/.reasona/reasona.yaml
dev-profile: generic          # used when nothing in the map matches
dev-profile-map:
  "crates/**": rust
  "services/**/*.py": python
  "web/**": typescript
```

A PR unit is resolved from the `files:` its manifest declares; a unit can
override this by declaring `profile:` explicitly. Files that match nothing
in the map are ignored, so a Rust PR that also touches `README.md` is
still a Rust PR.

**If a unit's files match two profiles, compilation rejects it.** The
author must either declare `profile:` explicitly or split the unit.

## 4. Auto-generated -- do not touch

| Path | Generated by | Nature |
|---|---|---|
| `<repo>/.bernstein/bernstein.yaml` | `compile-plan` | copied from the template, then only `provider` is kept in sync |
| `<repo>/bernstein.yaml` | `compile-plan` | a symlink pointing at `.bernstein/` |
| the `bernstein.yaml` entry in `<repo>/.gitignore` | `compile-plan` | added automatically |
| `<repo>/.reasona/acceptance-<stage>.json` | `compile-plan` | the manifest's `acceptance:` |
| `<repo>/.reasona/model_config.json` | `compile-plan` | audit trail |
| `<repo>/.reasona/cycles.jsonl` | `run-plan` | instrumentation |
| `<repo>/.reasona/memory/*.md` | `run-plan` | generated from `cycles.jsonl` |
| `<repo>/.reasona/runs/<stage>/*.raw.txt` | `run-plan` | raw per-role output |

**The root `bernstein.yaml` must stay untracked.** Committing it makes git
materialize it into every agent worktree, and Bernstein's isolation check
then rejects it, so no agent can spawn at all. It's added to `.gitignore`
automatically, so leaving it alone is correct. Conversely, if the link is
missing entirely the orchestrator dies with `FATAL: no adapter
configured`, so `compile-plan` re-ensures the link on every run (it's
restored even right after a fresh clone).

`.reasona/memory/` is likewise not something to hand-write. It's a
generated artifact, rewritten on every `run-plan` -- automatic decay of
patterns that have stopped recurring is the whole point of that design.

## 5. Priority chain

Applies identically to every role's config.

```
flag  >  env var  >  <repo>/.reasona/reasona.yaml  >  ~/.reasona/reasona.yaml  >  default
```

| Env var | Target |
|---|---|
| `REASONA_DEV_DEV_MODEL` | dev |
| `REASONA_DEV_REVIEW_MODEL` | review |
| `REASONA_DEV_RECHECK_MODEL` | recheck |
| `REASONA_DEV_BUGBOT_MODEL` | bugbot |
| `REASONA_DEV_VERIFY_MODEL` | verify (compliance) |
| `REASONA_DEV_FINAL_AUDIT_MODEL` | final_audit |
| `REASONA_DEV_DEV_ESCALATION_MODEL` | dev_escalation |
| `REASONA_DEV_PROFILE` | prompt profile name |

The prompt files themselves are separate from this chain and use only
**two layers, project then global**.

## 6. Verifying the install

`--workdir` defaults to the current directory when omitted, and every
subcommand resolves it to an absolute path. Even so, **run these from the
repository root** -- `bernstein run` also takes cwd as the project root,
so running from a subdirectory makes it mistake that subdirectory for the
root.

```bash
# 1. Are the prompts visible? (the cycle ABORTs if not)
reasona-dev prompts --workdir <repo>

# 2. Bernstein adapters, keys, ports
bernstein doctor

# 3. Compile (the seed, the symlink, and the AC files are generated here)
reasona-dev compile-plan docs/plans/<plan>.md -o plan.yaml --workdir <repo>

# 4. Check the resolved model config
cat <repo>/.reasona/model_config.json
```

## 7. Running

```bash
# dev cycle-0 -- the plan's implementation stage runs under Bernstein's own scheduler
bernstein run plan.yaml --auto-approve --hard-budget 20usd

# review -> scan -> ship
reasona-dev run-plan docs/plans/<plan>.md --workdir <repo>

# + merge tail (through PR creation)
reasona-dev run-plan docs/plans/<plan>.md --workdir <repo> --ship

# + through squash-merge
reasona-dev run-plan docs/plans/<plan>.md --workdir <repo> --merge
```

`--merge` defaults to off. A squash-merge rewrites the real repository's
default branch, a hard-to-reverse action, so it must be requested
explicitly by the caller rather than discovered after the fact.

## 8. Measurement

```bash
reasona-dev cycles-report --workdir <repo>
```

Produces per-role first/duplicate/**unique** attribution, the budget's
exhaustion distribution and termination reasons, AC coverage, and the
gate-vs-acceptance four-way breakdown. Which review role to cut, and when
AC-undeclared should be promoted to a rejection, are decisions made only
after this table is populated.

`--effective` appends a separate approximate metric (file re-touch rate).
The original analysis measured this metric at 84% against a 77% baseline
rate for the control group, so it defaults to off and is never mixed with
the exact counts.
