# Installation and configuration

Which files to touch when setting up reasona-dev, and which are
auto-generated (don't hand-edit those — they get overwritten).

## 0. Prerequisites

| Item | Requirement | Check |
|---|---|---|
| Python | >= 3.12 (Bernstein's own floor) | `python3 --version` |
| Bernstein | >= 3.15.1 | `bernstein --version` |
| Adapter auth | login for whichever CLI adapter you use | `bernstein doctor` |
| `gh` CLI | only for `--ship`/`--merge` | `gh auth status` |

```bash
uv tool install bernstein
uv pip install -e .        # or the published reasona-dev
```

## 1. Execution model

`run-plan` spins up its own Bernstein server per invocation and tears it
down when it finishes — nothing persists between runs.

```
run-plan  |- bernstein serve + worker (startup)
          |- per-unit review -> scan -> ship (-> merge tail)
          \- both terminate (shutdown)
```

## 2. Global config — `~/.reasona/`

```bash
mkdir -p ~/.reasona
cp -r <reasona-dev-repo>/.reasona/prompts                 ~/.reasona/prompts
cp    <reasona-dev-repo>/.reasona/bernstein-template.yaml ~/.reasona/
cp    <reasona-dev-repo>/.reasona/reasona.yaml            ~/.reasona/reasona.yaml
```

### 2.1 `reasona.yaml` — per-role models

```yaml
dev-models:
  dev: claude:sonnet:high
  review: claude:opus:high
  recheck: claude:sonnet:high
  bugbot: claude:opus:high
  compliance: claude:sonnet:high
  final_audit: claude:opus:high
  dev_escalation: claude:opus:high
```

Format is `tool:model:effort` (flag, env var, and this file all accept it
identically); a bare model name is also valid. Runs without this file, but
not recommended — defaults if absent:

| Role | Default | Note |
|---|---|---|
| `dev` | `claude:sonnet:high` | |
| `review` | `claude:opus:high` | |
| `recheck` | `claude:opus:high` | falls back to `review` — loses bounded-recheck savings |
| `compliance` | `claude:sonnet:high` | |
| `bugbot` | `kilo:deepseek-v4-pro:high` | fails without kilo auth |
| `final_audit` | `claude:opus:high` | |
| `dev_escalation` | `claude:opus:high` | |

**`effort` also sets the turn budget** (`scope: large` for every role):

| effort | turn budget |
|---|---|
| `max` | 200 |
| `high` | 100 |
| `medium` | 60 |
| `normal` | 50 |
| `low` | 30 |

The review prompt writes its report only as its last action, so a budget
that runs out mid-exploration loses the whole result (ERROR), not a
truncated one. **Keep `review` and `final_audit` at `high` or above.**

### 2.2 `prompts/<profile>/*.md` — role prompts

No prompt is bundled in the package — missing at both layers means
`resolve_prompt` returns `None` and the cycle aborts. The `rust-dev`
profile needs 5 files: `review.md`, `recheck.md`, `bugbot.md`,
`compliance.md`, `final_audit.md`.

```bash
reasona-dev prompts --workdir <repo>   # which profiles/roles resolve
```

Don't change a prompt's item notation without checking the parser — a
mismatch drops findings as a silent false PASS (`docs/ARCHITECTURE.md`
§3.8.2).

### 2.3 `bernstein-template.yaml` — the Bernstein seed source

Copied whenever a target repo has no `bernstein.yaml` of its own (§4 — for
a new repo, copy straight to `<repo>/bernstein.yaml`, no `.bernstein/` or
symlink needed).

```yaml
role_model_policy:
  backend: {provider: claude}
  reviewer: {provider: claude}
  bugbot: {provider: claude}
  compliance: {provider: claude}
  final_audit: {provider: claude}
```

**All 5 roles must be present** — this block doubles as the task server's
role whitelist (`POST /tasks` 400s for any role missing here). `provider`
is auto-synced by `compile-plan` on every run; no need to hand-tune it.

## 3. Per-repo config (optional) — `<repo>/.reasona/`

Overrides the global layer **per file**.

| Path | Purpose |
|---|---|
| `<repo>/.reasona/reasona.yaml` | model / profile / profile mapping |
| `<repo>/.reasona/prompts/<profile>/*.md` | repo-specific prompts |
| `<repo>/.reasona/bernstein-template.yaml` | project-local seed template |
| `<repo>/bernstein.yaml` | the Bernstein seed itself — see §4 |

### 3.1 Mixed-language repos — per-unit profiles

```yaml
# <repo>/.reasona/reasona.yaml
dev-profile: rust-dev
dev-profile-map:
  "crates/**": rust
  "services/**/*.py": python
```

Resolved per PR unit from its `files:`; explicit `profile:` on a unit
wins. Files matching nothing are ignored. **A unit whose files match two
profiles is refused at compile time** — split it or set `profile:`.

## 4. `<repo>/bernstein.yaml`

**Recommended: skip `.bernstein/` and the symlink, place a plain file at
the repo root:**

```bash
cp ~/.reasona/bernstein-template.yaml <repo>/bernstein.yaml
```

This satisfies every place Bernstein looks for it (root-first orchestrator
subprocess, `.bernstein/`-first CLI parsing — README `bernstein.yaml`
section). `compile-plan` finds it in place, leaves it alone, and keeps
`role_model_policy.*.provider` synced.

**Add it to `.gitignore`** — a tracked `bernstein.yaml` gets materialized
into every agent worktree and fails Bernstein's isolation check.

## 5. Auto-generated — do not touch

| Path | Generated by | Nature |
|---|---|---|
| `bernstein.yaml` entry in `.gitignore` | `compile-plan` | added if missing |
| `<repo>/.reasona/acceptance-<stage>.json` | `compile-plan` | the manifest's `acceptance:` |
| `<repo>/.reasona/model_config.json` | `compile-plan` | audit trail |
| `<repo>/.worktrees/<plan>/<stage>/` | `run-plan` | this PR unit's own git worktree (dev-0 through squash-merge/cleanup) |
| `<repo>/.reasona/log/<plan>/<stage>/plan.yaml` | `run-plan` (unless `--skip-dev`) | this unit's compiled single-stage cycle-0 plan, recompiled every dispatch |
| `<repo>/.reasona/log/<plan>/<stage>/ledger.json` | `run-plan` | this PR unit's dev-dispatched flag, review/scan progress, terminal outcome, and PR-url/issue-number hints (resume state) |
| `<repo>/.reasona/log/<plan>/<stage>/*.raw.txt` | `run-plan` | raw per-role output |
| `<repo>/.reasona/cycles.jsonl` | `run-plan` | instrumentation |
| `<repo>/.reasona/memory/*.md` | `run-plan` | generated from `cycles.jsonl` |

## 6. Priority chain

```
flag  >  env var  >  <repo>/.reasona/reasona.yaml  >  ~/.reasona/reasona.yaml  >  default
```

| Env var | Target |
|---|---|
| `REASONA_DEV_DEV_MODEL` | dev |
| `REASONA_DEV_REVIEW_MODEL` | review |
| `REASONA_DEV_RECHECK_MODEL` | recheck |
| `REASONA_DEV_BUGBOT_MODEL` | bugbot |
| `REASONA_DEV_COMPLIANCE_MODEL` | compliance |
| `REASONA_DEV_FINAL_AUDIT_MODEL` | final_audit |
| `REASONA_DEV_DEV_ESCALATION_MODEL` | dev_escalation |
| `REASONA_DEV_PROFILE` | prompt profile name |

Prompt files use only two layers (project, global), separate from this
chain.

## 7. Verifying the install

Run from the repo root (`bernstein run` uses cwd as the project root).

```bash
reasona-dev prompts --workdir <repo>     # 1. prompts visible? (aborts if not)
bernstein doctor                          # 2. adapters, keys, ports
reasona-dev compile-plan docs/plans/<plan>.md -o plan.yaml --workdir <repo>  # 3. compile
cat <repo>/.reasona/model_config.json     # 4. resolved model config
```

## 8. Running

`run-plan` drives every PR unit through its own dedicated git worktree
(`<repo>/.worktrees/<plan>/<stage>/`), in dependency order: cycle-0
(`bernstein run --auto-approve`, no reasona-dev gate runs on this step) ->
review -> scan -> ship_gate. It stops there by default.

```bash
reasona-dev run-plan docs/plans/<plan>.md --workdir <repo>
```

**PR creation and merge are opt-in**, not automatic:

```bash
reasona-dev run-plan docs/plans/<plan>.md --workdir <repo> --ship   # + opens a PR, watches CI/bots
reasona-dev run-plan docs/plans/<plan>.md --workdir <repo> --merge  # + squash-merges it
```

They default to off on purpose: opening a real PR and squash-merging it are
outward-facing, hard-to-undo actions on the target repo's real GitHub
state. `--ship` runs `gh_pr.py` (issue creation, structural title/body
validation and repair) then `gh_review.py` (CI/compliance/bugbot watching
with an auto-fix loop) -- ports of dev-ralf's `/gh-pr`/`/gh-review`, see
docs/ARCHITECTURE.md §3.12/§3.13. `--gh-review-max-wait SECONDS` (default
900, matching `/gh-review`'s own default) bounds how long the gh-review
watch loop waits on GitHub's own CI/workflow runs per unit.

**A plan's own `acceptance:` criteria are the only build/test gate
anywhere in this pipeline** (docs/ARCHITECTURE.md §3.7.3) -- unlike
dev-ralf's unconditional `make ci`/`cargo test` step, reasona-dev runs
nothing if the plan declares nothing. Every PR unit whose `files:` touch
source should declare an acceptance criterion running this repo's own
build/test command to get equivalent behavior.

**Resume an interrupted run** (network failure, killed process) by running
the exact same command again:

```bash
reasona-dev run-plan docs/plans/<plan>.md --workdir <repo>
```

A progress ledger under `.reasona/log/<plan>/<stage>/` (see
`reasona_dev/ledger.py`) records, per PR unit, whether its own cycle-0 has
already been dispatched (into that unit's own worktree, reused as-is on
resume rather than recreated), its terminal outcome, and its in-progress
review/scan checkpoint (cycle number, phase, `FixBudget`/
`RecurrenceTracker` state, pending findings). `run-plan` checks it
automatically: a unit's cycle-0 is skipped if its own ledger says it
already ran, a unit already marked shipped is reused as-is instead of
re-dispatched, and a unit interrupted mid-review/scan resumes from its
last checkpointed cycle rather than restarting from cycle 1. No flags are
needed for a plain retry.

`create_pr()` still asks `gh pr view` first on every resume (and `gh_pr.py`
applies the same pattern to the GitHub issue it creates); the ledger's
recorded PR url/issue number is consulted only as a fallback when that
live check finds nothing (e.g. the push succeeded but the process died
before the URL was read back). `sync_main()` uses no ledger at all -- git's
own merge is already idempotent, so re-running it is always safe on its
own.

`--from-pr <index>` / `--skip-dev` manually override this when the ledger
itself is unavailable or wrong -- they drop units / skip cycle-0 dispatch
(the worktree is still created either way) regardless of what's recorded,
trusting the caller instead of the ledger. `--restart` clears every unit's
ledger and reruns the whole plan from scratch; use it only when the plan
document itself changed, not for a plain retry.

To compile and inspect `plan.yaml` without dispatching anything,
`compile-plan` (§7) still works standalone.

## 9. Measurement

```bash
reasona-dev cycles-report --workdir <repo>
```

Per-role first/duplicate/unique attribution, budget exhaustion/termination
reasons, AC coverage, gate-vs-acceptance breakdown — the basis for which
review role to cut and whether AC-undeclared should become a rejection.
`--effective` adds an approximate file-re-touch metric, off by default
(measured ~base-rate noise).
