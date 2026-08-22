# reasona-dev

Deterministic plan-to-PR pipeline. Orchestration runs on
[Bernstein](https://github.com/sipyourdrink-ltd/bernstein) (Apache-2.0) —
that dependency is not hidden; see `docs/ARCHITECTURE.md`. The differentiated
layer built here is the finding contract: disposition/severity separation,
an INCONCLUSIVE channel distinct from code findings, evidence-carrying
MUST_FIX findings, deterministic recheck routing, and one bounded
model-escalation attempt before a PR fails on a recurring defect.

`reasona-dev` is the successor to `dev-ralf` (Claude Code skill, LLM
scheduler). `reasona-plan` (separate repo, `plan-ralf` successor) is the
upstream PRD/plan-authoring layer this project consumes.

## Quick start (use reasona-dev on a new target repo)

**Setup, once per machine:**
```bash
uv tool install bernstein
uv pip install -e .                                # this repo
mkdir -p ~/.reasona
cp -r .reasona/prompts ~/.reasona/prompts
cp    .reasona/bernstein-template.yaml .reasona/reasona.yaml ~/.reasona/
```

**Setup, once per target repo:**
```bash
cd <target-repo>
cp ~/.reasona/bernstein-template.yaml bernstein.yaml   # plain file at repo root, no .bernstein/, no symlink
printf '%s\n' bernstein.yaml .reasona/ .sdd/ .worktrees/ >> .gitignore
```

**Run a plan** (per unit: its own worktree → cycle-0 → review → scan →
ship_gate verdict -- stops there, no PR):
```bash
reasona-dev run-plan docs/plans/<plan>.md --workdir .
```

**PR creation and merge are opt-in, not automatic.** `--ship` opens a real
PR (issue creation, structural title/body validation+repair, then CI/bot
watching with an auto-fix loop -- ports of dev-ralf's `/gh-pr`/`/gh-review`,
see `docs/ARCHITECTURE.md` §3.12/§3.13) and `--merge` squash-merges it once
that settles -- both outward-facing, hard-to-undo actions on the target
repo's real GitHub state, so ask for them explicitly:
```bash
reasona-dev run-plan docs/plans/<plan>.md --workdir . --ship    # + opens a PR, watches CI/bots
reasona-dev run-plan docs/plans/<plan>.md --workdir . --merge   # + squash-merges it
```

**Interrupted partway (network failure, killed process)? Just run the exact
same command again:**
```bash
reasona-dev run-plan docs/plans/<plan>.md --workdir .
```
A progress ledger (`.reasona/dev/<plan>/`) skips cycle-0 per unit if it
already ran (reusing that unit's own worktree), resumes any PR unit's
review/scan cycle from where it stopped (not from cycle 1), and skips any
PR unit that already shipped -- all automatically, no flags needed.
`--from-pr <index>`/`--skip-dev` are manual overrides for when the ledger
itself is unavailable; `--restart` clears it and reruns everything from
scratch (use only when the plan itself changed).

Details, global vs. per-repo config, and everything else: `docs/INSTALL.md`.
Design rationale for every decision below: `docs/ARCHITECTURE.md`.

## Status

Working V0, live end-to-end verified (per-unit worktree → cycle-0 → review
→ scan → ship → gh-pr → gh-review → squash-merge, real agents and cost).
One open item: Bernstein's own retry-escalation path can bump a task's
model up a tier on its 2nd+ retry with no config surface to prevent it —
bounded (`max_retries=3`), but not yet fully blockable. Full trace:
`docs/ARCHITECTURE.md` §3.6.

```bash
uv run --with pytest python3 -m pytest tests/
```

## Layout

```
docs/ARCHITECTURE.md       design rationale and the full Bernstein source trace
docs/INSTALL.md             setup, configuration, running
bernstein.yaml              this repo's own seed config (see "bernstein.yaml" below)
.reasona/reasona.yaml        this repo's own model_config layer, under `dev-models:`
.reasona/prompts/rust-dev/     this repo's own prompt profile
reasona_dev/
  plan_compile.py           plan document -> bernstein plan.yaml (dev's cycle-0 step, `only_index` for one unit)
  orchestrate.py              runs a whole plan: per-unit worktree + cycle-0 dispatch, dependency order, per-unit profile
  worktree.py                  one git worktree per PR unit, dev-0 through squash-merge/cleanup
  pr_cycle.py                 develop -> review -> bug+compliance scan driver
  bernstein_dispatch.py        one-step plan.yaml + `bernstein run` -- one role dispatch
  acceptance.py                 executable acceptance criteria
  ship_gate.py                    pre-merge verdict: review AND acceptance, called from final_phase, own bounded dev-fix loop
  final_phase.py                   gh check -> final phase (sync<->conflict-fix -> final_audit -> ship_gate<->acceptance-fix, re-verified as a round) -> gh-pr -> gh-review -> squash-merge
  gh_pr.py                          /gh-pr port -- issue, branch rename, PR create, structural validate/repair
  gh_review_watch.py                /gh-review's watcher, ported near-verbatim -- CI/compliance/bugbot GraphQL snapshot + classify
  gh_review.py                      /gh-review's auto-fix loop -- dispatch dev, one push per cycle, budget-bounded
  cycles_log.py / cycles_query.py    per-cycle measurement log + queries
  plan_report.py               plan-level teardown: promised-but-absent names + undeclared file scope (reports, never blocks)
  memory.py                        repo-scoped priors generated from cycles.jsonl
  prompt_profile.py            per-unit profile resolution, two-layer prompt lookup
  model_config.py / config_file.py   per-role model priority chain + 2-layer config cascade
  bernstein_config.py          regenerates a target repo's bernstein.yaml from its template every run + syncs role_model_policy
  finding_adapter.py           text + KV contract parsers
  cycle_gate.py                  recheck routing, escalation, budget, convergence
  ci_gate.py                       local CI gate -- ci.fast after every dev fix (revert on failure), ci.full before /gh-pr
  open_decisions.py                the Open Decisions Gate -- refuses an undecided plan entry before any dispatch
  squash.py                        squash message builder + guard
  ledger.py                         per-plan, per-unit resume state
  plugin.py                         pluggy hookimpl
tests/                      pytest, 571 cases
```

## CLI

```bash
reasona-dev compile-plan plan.md -o plan.yaml --workdir <target-repo> --dev opus --bugbot codex:o1:max
```

Role flags mirror dev-ralf's: `--dev`, `--review`, `--recheck`, `--bugbot`,
`--compliance`, `--final-audit`. `compile-plan` is the only subcommand — review
and scan run through `pr_cycle`'s runtime driver, not a second subcommand —
and it also keeps `role_model_policy` in `bernstein.yaml` synced to
`model_config`'s resolved models. Chain: `flag > env var > project config >
global config > default`.

`--review` is repeatable (`--review claude:opus:high --review codex:o1:max`)
to run multiple independent reviewers, merged via `finding_adapter.merge()`;
appending `,ocr` to any one of them (e.g. `--review claude:opus:high,ocr`)
also dispatches the OCR co-reviewer once alongside them. `run-plan --job K`
(default 1) runs up to `K` independent PR units concurrently, each on its
own port — see `docs/ARCHITECTURE.md` §3.14.

## `bernstein.yaml`

**For a new target repo, place a plain file directly at
`<repo>/bernstein.yaml`** — see Quick start above. This satisfies both of
Bernstein's lookup paths (root-first orchestrator, `.bernstein/`-first CLI
parsing), so no `.bernstein/` directory or symlink is needed.
`ensure_bernstein_yaml()` leaves a root file that already exists alone.

This repo's own `bernstein.yaml` stays at `.bernstein/` (not root) —
`.bernstein/` is what `find_seed_file()` checks first, and this project
never runs `bernstein run` against itself for real execution, so it never
needs the root-only fallback. Full
trace of why Bernstein needs a real file at all (and the `.bernstein/` +
symlink fallback `compile-plan` still bootstraps for a repo with neither):
`docs/ARCHITECTURE.md` §3.5.3. Setup steps: `docs/INSTALL.md`.

## Prompt profiles

review/recheck/bugbot/compliance/final_audit prompts are project- and
language-specific `.md` files under a named **profile**, resolved through
exactly two layers:

```
<workdir>/.reasona/prompts/<profile>/<role>.md   project-local
~/.reasona/prompts/<profile>/<role>.md           global
```

Precedence is per file, not per profile directory. A repo with neither
layer for a role gets `None` and the cycle aborts rather than silently
falling back to a packaged default. This repo commits its own
`.reasona/prompts/rust-dev/` as the checked-in example to copy from.

**Mixed-language repos** resolve the profile per PR unit, from the `files:`
it declares:

```yaml
# <repo>/.reasona/reasona.yaml
dev-profile: rust-dev
dev-profile-map:
  "crates/**": rust
  "services/**/*.py": python
```

Resolution order: unit's own `profile:` > `dev-profile-map:` glob match >
`dev-profile:` > `"rust-dev"`. A unit whose files match two profiles is
refused at compile time — split it or set `profile:` explicitly.

## Local CI gate (opt-in)

```yaml
# <repo>/.reasona/reasona.yaml
ci:
  fast: "cargo check --workspace --all-targets"   # after every dev fix -- revert on failure
  full: "make ci"                                  # once, right before /gh-pr creates anything
```

Unconfigured (no `ci:` key, the default) is a no-op on both — nothing changes for a repo that
doesn't set this. `fast` catches a broken fix locally, in seconds, instead of only via GitHub's
own CI after the PR is already public; `full` refuses to open a PR at all when it fails.

## Open decisions

A plan's `## Open decisions (human)` section (plan-ralf's own output format) must have every entry
marked `decided: <choice>` before `run-plan` dispatches a single agent — even choosing the printed
default is a decision that must be recorded. `run-plan` refuses to start, listing every undecided
entry, otherwise.

## Running a plan

```bash
reasona-dev run-plan docs/plans/<plan>.md --workdir .
```

`run-plan` compiles the plan and dispatches cycle-0 (`bernstein run
--auto-approve`) itself before `orchestrate.py` takes over: resolves each
unit's profile, orders by `depends_on`, skips a unit whose dependency
didn't ship (rather than attempting it), gates approval on the first unit
only, and shares one Bernstein server for the whole plan. Progress is
recorded in a ledger (`reasona_dev/ledger.py`) as each unit ships, so a
re-run after an interruption resumes at the first not-yet-shipped unit
automatically -- no flags needed for the common case. `--ship`/`--merge`
opt IN to PR creation and squash-merge (both off by default -- see the PR
caveat above); `--from-pr`/`--skip-dev` manually override the ledger, and `--restart`
clears it. Design rationale for each of these choices, plus how a role
is dispatched (batch `bernstein run`, not HTTP), the quality-budget shape,
and the generated-memory design: all in `docs/ARCHITECTURE.md` (§3.5.3,
§3.7, §3.11, "Memory").
