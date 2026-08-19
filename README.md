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

**Run a plan** (compile → cycle-0 → review → scan → ship_gate verdict --
stops there, no PR):
```bash
reasona-dev run-plan docs/plans/<plan>.md --workdir .
```

**PR creation and merge are opt-in, not automatic.** `final_phase.create_pr()`
is an incomplete stand-in for dev-ralf's `/gh-pr` (no issue, no PR body
validation, no repair loop), and there is no `/gh-review` equivalent at
all -- see `docs/ARCHITECTURE.md` §3.9. Ask for them explicitly, and check
the PR yourself:
```bash
reasona-dev run-plan docs/plans/<plan>.md --workdir . --ship    # + opens a PR
reasona-dev run-plan docs/plans/<plan>.md --workdir . --merge   # + squash-merges it
```

**Interrupted partway (network failure, killed process)? Just run the exact
same command again:**
```bash
reasona-dev run-plan docs/plans/<plan>.md --workdir .
```
A progress ledger (`.reasona/log/<plan>/`) skips cycle-0 if it already ran,
resumes any PR unit's review/scan cycle from where it stopped (not from
cycle 1), and skips any PR unit that already shipped -- all automatically,
no flags needed. `--from-pr <index>`/`--skip-dev` are manual overrides for
when the ledger itself is unavailable; `--restart` clears it and reruns
everything from scratch (use only when the plan itself changed).

Details, global vs. per-repo config, and everything else: `docs/INSTALL.md`.
Design rationale for every decision below: `docs/ARCHITECTURE.md`.

## Status

Working V0, live end-to-end verified (compile-plan → cycle-0 → review → scan
→ ship → merge tail, real agents and cost). One open item: Bernstein's own
retry-escalation path can bump a task's model up a tier on its 2nd+ retry
with no config surface to prevent it — bounded (`max_retries=3`), but not
yet fully blockable. Full trace: `docs/ARCHITECTURE.md` §3.6.

```bash
uv run --with pytest python3 -m pytest tests/    # 293 passed
```

## Layout

```
docs/ARCHITECTURE.md       design rationale and the full Bernstein source trace
docs/INSTALL.md             setup, configuration, running
bernstein.yaml              this repo's own seed config (see "bernstein.yaml" below)
.reasona/reasona.yaml        this repo's own model_config layer, under `dev-models:`
.reasona/prompts/generic/     this repo's own prompt profile
reasona_dev/
  plan_compile.py           plan document -> bernstein plan.yaml (dev's cycle-0 step)
  orchestrate.py              runs a whole plan: units in dependency order, per-unit profile
  pr_cycle.py                 develop -> review -> bug+compliance scan driver
  bernstein_dispatch.py        one-step plan.yaml + `bernstein run` -- one role dispatch
  acceptance.py                 executable acceptance criteria
  ship_gate.py                    pre-merge verdict: review AND acceptance, called from final_phase, own bounded dev-fix loop
  final_phase.py                   gh check -> final phase (sync<->conflict-fix -> final_audit -> ship_gate<->acceptance-fix, re-verified as a round) -> PR -> squash-merge
  cycles_log.py / cycles_query.py    per-cycle measurement log + queries
  memory.py                        repo-scoped priors generated from cycles.jsonl
  prompt_profile.py            per-unit profile resolution, two-layer prompt lookup
  model_config.py / config_file.py   per-role model priority chain + 2-layer config cascade
  bernstein_config.py          bootstraps/syncs a target repo's bernstein.yaml
  finding_adapter.py           text + KV contract parsers
  cycle_gate.py                  recheck routing, escalation, budget, convergence
  squash.py                        squash message builder + guard
  plugin.py                         pluggy hookimpl
tests/                      pytest, 293 cases
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
`.reasona/prompts/generic/` as the checked-in example to copy from.

**Mixed-language repos** resolve the profile per PR unit, from the `files:`
it declares:

```yaml
# <repo>/.reasona/reasona.yaml
dev-profile: generic
dev-profile-map:
  "crates/**": rust
  "services/**/*.py": python
```

Resolution order: unit's own `profile:` > `dev-profile-map:` glob match >
`dev-profile:` > `"generic"`. A unit whose files match two profiles is
refused at compile time — split it or set `profile:` explicitly.

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
