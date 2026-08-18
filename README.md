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

## Status

Working V0. Most items flagged open after the first pass are resolved by
direct inspection of the installed Bernstein 3.15.1 source — see
`docs/ARCHITECTURE.md` §3. **One is not**: `docs/ARCHITECTURE.md` §3.6 --
Bernstein's retry-escalation path (`_choose_retry_escalation`, independent of
`cascade_router`/`bandit_router`/`model_fallback`) silently bumps the model
up a tier on a task's 2nd+ retry, for Claude-adapter roles, with no
declarative config surface (`bernstein.yaml` or `plan.yaml`) to prevent it.
Retries are still bounded (`max_retries=3`, then permanent failure) so this
is not the unbounded-retry form of CREDIT-BURN dev-ralf hit originally, but
the "never silently swap the model" half of that principle does not
currently hold. Install, tests, and real round-trips against the actual
`bernstein` CLI all pass as of this commit:

```
$ .venv/bin/python -m pytest tests/ -v          # 114 passed
$ .venv/bin/bernstein plan validate <compiled plan.yaml>          # "Plan is valid."
$ .venv/bin/bernstein review --pipeline <rendered review.yaml> --validate-only  # "Pipeline OK"
$ .venv/bin/bernstein doctor                    # "Plugin loading: no errors"
                                                 # "Adapter version: 1 tracked adapter(s), 0 below"
$ python3 -c "from bernstein.adapters.registry import get_adapter; \
  print(get_adapter('ocr').name())"             # "OCR (diff-scanning reviewer)"
```

## What was open, and how each resolved

| Item | Resolution |
|---|---|
| `cascade_router.py` off switch | Confirmed dead code -- no call sites anywhere in the installed package outside its own file. Live initial-model-selection path is `bandit_router.py`, a different (lower-risk) concern. |
| `bandit_router.py` scope | Default routing mode is `static` (`BERNSTEIN_ROUTING` env var, unset here) -- the bandit object isn't even instantiated unless explicitly turned on. Even when on, its tier floor (`haiku < sonnet < opus`) is Claude-only (`router_applicable()`) and is skipped entirely for other adapters. Not a concern for this project's default config. |
| `model_fallback.strike_limit: 0` | Was a bug in the first draft -- `should_fallback = consecutive_errors >= strike_limit`, and errors start at 0, so `0` fires immediately. Fixed: `fallback_chain: []` alone disables it; `strike_limit` left unset. |
| Merge-gating hook | Confirmed no dedicated hookspec exists (`on_pre_task_create` and `on_pre_tool_use` are the only two that can block), and `completion_signals` cannot substitute: they run at the project root BEFORE the agent's branch merges, so they cannot see the code they would gate (§3.8). Gating is `reasona_dev/ship_gate.py`, run by the driver after the merge. |
| `agy`/`ocr` adapters | `agy` turned out to already be a native Bernstein adapter ("Antigravity CLI") -- same binary and flags dev-ralf's `dispatch.md` already documents. Only `ocr` needed writing; `reasona_dev/adapters/ocr.py` is registered via `bernstein.adapters` entry points and resolves through Bernstein's own `get_adapter()`. |
| Per-role model config | Ported dev-ralf's `flag > env var > fallback > default` priority chain (`DEV_RALF_*` -> `REASONA_DEV_*`) as `reasona_dev/model_config.py`, since bandit routing being off means nothing else picks a model. Caught and fixed a real asymmetry bug in the first draft: `bugbot`/`final_audit` must fall back only to the `VERIFY_MODEL` **env var/config slot**, never to `verify`'s fully-resolved value -- only `recheck` inherits `review`'s resolved outcome. |
| **CREDIT-BURN on retry -- NOT resolved** | `_choose_retry_escalation` (`core/tasks/task_lifecycle.py`) is a fourth, independent model-selection path: bumps the model up `haiku -> sonnet -> opus` on a task's 2nd+ retry. `role_model_policy` only guards non-Claude-adapter roles (confirmed via docstring); the `terminal_reason == "model_error"` exemption is unreachable (nothing in the package ever sets that value); `max_retries` has no config surface in either `plan_schema.py` or `seed_config.py`. See `docs/ARCHITECTURE.md` §3.6 for the full trace and candidate next steps. |
| Persistent config, no env var re-export | Added `reasona_dev/config_file.py` -- a two-layer cascade (`~/.reasona/reasona.yaml` global, `<workdir>/.reasona/reasona.yaml` project/local, mirroring Bernstein's own pattern) sitting between the env var and the hardcoded default in `model_config.py`'s priority chain. Anchored to the same `workdir` as everything else (never reasona-dev's own install location, since there won't be one once deployed). |

## Layout

```
docs/ARCHITECTURE.md       4-layer architecture, verified against installed Bernstein 3.15.1 source
.bernstein/bernstein.yaml   THIS repo's own committed project config (model_fallback correctly emptied,
                             cascade_router/bandit notes, role_model_policy) -- see "bernstein.yaml for
                             target repos vs. this repo's own" below for why it lives under .bernstein/
.reasona/reasona.yaml        THIS repo's own committed model_config layer, under the `dev-models:` key
                              (a future reasona-plan gets its own `plan-models:` key, same file)
.reasona/bernstein-template.yaml   committed copy of .bernstein/bernstein.yaml, kept purely as a real
                                     example of bernstein_config's project-local template shape
.reasona/prompts/generic/     THIS repo's own prompt profile -- no packaged layer exists (see "Prompt profiles")
reasona_dev/
  plan_compile.py           plan document -> bernstein plan.yaml (dev's cycle-0 step), anchors to workdir
  orchestrate.py              runs a whole plan: units in dependency order, each under its own profile
  pr_cycle.py                 dev-ralf-faithful develop -> verify -> bug+compliance scan driver (worker.md)
  bernstein_dispatch.py        one-step plan.yaml + `bernstein run` -- one role dispatch, synchronous
  acceptance.py                 executable acceptance criteria -- RUNS the plan's own claims
  ship_gate.py                    THE pre-merge verdict: review AND acceptance, composed
  merge_tail.py                    sync-main -> final audit -> squash guard -> PR -> squash-merge
  cycles_log.py                   append-only per-cycle finding log (.reasona/cycles.jsonl) -- the measurement substrate
  cycles_query.py                  attribution / budget / coverage queries -- what makes the log a decision
  memory.py                        repo-scoped priors GENERATED from cycles.jsonl, file-scoped retrieval
  prompt_profile.py            per-unit profile resolution + two-layer prompt lookup (.reasona/prompts/<profile>/)
  model_config.py            per-role model/adapter/effort priority chain + CONDUCTOR-COLLAPSE audit trail
  config_file.py              reasona-dev's own 2-layer config cascade (~/.reasona -> <workdir>/.reasona)
  bernstein_config.py          bootstraps + syncs a target repo's bernstein.yaml (see "Bootstrapping" below)
  finding_adapter.py           || text contract AND external-skill KV contract (`parse_kv_contract`) parsers
  cycle_gate.py                  recheck routing, escalation, budget, convergence, fingerprints
  squash.py                        squash message builder + guard
  plugin.py                         pluggy hookimpl (on_pre_task_create, on_agent_spawned)
  adapters/ocr.py                    OcrAdapter, registered via bernstein.adapters entry points
tests/                      pytest, 254 cases, all passing
```

## Setup

```bash
uv venv .venv
uv pip install --python .venv/bin/python -e ".[test]"
.venv/bin/python -m pytest tests/
```

## CLI

`resolve()`/`resolve_all()`'s `flag`/`flags` parameters had no real caller
until this: `reasona-dev` is now an actual installed command
(`[project.scripts]`), the top of the `flag > env var > project config >
global config > default` chain typed at a real shell.

```bash
reasona-dev compile-plan plan.md -o plan.yaml --workdir <target-repo> --dev opus --bugbot codex:o1:max
```

Role flag names mirror dev-ralf's own one-to-one: `--dev`, `--review`,
`--recheck`, `--bugbot`, `--verify`, `--final-audit`. `compile-plan` is now
the ONLY subcommand -- review/bugbot/compliance dispatch went from a
static `render-review`-generated YAML to `reasona_dev.pr_cycle`'s runtime
driver (see below), so every role's flag still reaches `role_model_policy`
sync through `compile-plan`, just not through a second subcommand anymore.

`compile-plan` also bootstraps and keeps `<workdir>/.bernstein/
bernstein.yaml` in sync as a side effect (`reasona_dev/bernstein_config.py`)
-- see the next section for the mechanism.

## `bernstein.yaml` for target repos vs. this repo's own

Bernstein's own config loader (`cli/helpers.py:find_seed_file()`) checks,
in order, `.bernstein/bernstein.yaml` THEN the repo-root `bernstein.yaml`
in the invoking cwd -- both cwd-relative, no home-directory fallback.
`bernstein run` alone accepts an explicit `--seed PATH` override; bare
`bernstein` ("run from bernstein.yaml or backlog") and `bernstein doctor`
have no such flag and always fall through to `find_seed_file()` -- so a
real, project-local file has to exist at one of those two locations for
every ad-hoc `bernstein` invocation to work, `--seed` or not (confirmed by
reading the loader directly; see `docs/ARCHITECTURE.md` §3.5.3 for the
full trace). A truly global `bernstein.yaml` isn't something Bernstein
itself supports.

`reasona_dev.bernstein_config.ensure_bernstein_yaml()` bootstraps
`<workdir>/.bernstein/bernstein.yaml` automatically for any target repo
that has neither real location already (checked at both; whichever one
already exists is left completely untouched, never duplicated), sourced
from:

```
<workdir>/.bernstein/bernstein.yaml   Bernstein reads this FIRST if present -- left alone, never overwritten; the bootstrap TARGET for fresh repos
<workdir>/bernstein.yaml              a fresh bootstrap creates this as a relative SYMLINK to .bernstein/bernstein.yaml (see below); a repo that already has a real file here is left alone
<workdir>/.reasona/bernstein-template.yaml     project-local template (checked first when bootstrapping)
~/.reasona/bernstein-template.yaml             global template (checked second when bootstrapping)
```

**Why a symlink, not a plain choice of one location:** Bernstein disagrees
with itself about where the seed file lives. `find_seed_file()` (used by
bare `bernstein`/`doctor`/top-level `run` CLI parsing) checks
`.bernstein/bernstein.yaml` FIRST. But `bernstein run`'s background
orchestrator subprocess (`core/server/server_launch.py::_start_spawner`)
does NOT call `find_seed_file()` at all -- when the seed path isn't
explicitly propagated to it (confirmed: happens with the exact invocation
`reasona_dev.pr_cycle` uses, `bernstein run <plan> --auto-approve`), it
independently re-derives `workdir / "bernstein.yaml"`, root only, with no
`.bernstein/` check whatsoever. A real, paid `bernstein run` against a
scratch repo (2026-08-18) confirmed this: a repo with ONLY
`.bernstein/bernstein.yaml` spawns ZERO agents, looping "FATAL: no adapter
configured" until the watchdog gives up. Rather than pick one location and
break the other caller, `ensure_bernstein_yaml()` now writes the real file
at `.bernstein/bernstein.yaml` (what `find_seed_file()` prefers) and
creates `<workdir>/bernstein.yaml` as a relative symlink to it -- a symlink
is transparent to `Path.exists()`/`.read_text()`, so the orchestrator
subprocess's hardcoded root lookup resolves it exactly like a real file
would (confirmed for free via a direct, non-spawning invocation of
Bernstein's own orchestrator module: `resolved seed_path=.../.bernstein/
bernstein.yaml (from --seed-path=None, exists=True)`, then correctly
proceeding past the earlier FATAL point). One source file now satisfies
both of Bernstein's lookup paths.

**This repo's own `bernstein.yaml` stays at `.bernstein/` regardless** --
this project never runs `bernstein run` against itself for real execution
(only `doctor`/`plan validate`, neither of which spawns the buggy
subprocess), so it never hits this bug and isn't worth churning back.
`.reasona/bernstein-template.yaml` is ALSO committed here, as an identical
copy -- not consumed by this repo, just a real, checked-in example of the
shape a project-local template takes for every other repo.

`.reasona/reasona.yaml` is a different, unrelated file: it's
`reasona_dev.config_file`'s own project-local model-config layer (never
read by Bernstein itself), committed here under its `dev-models:` key so
running this repo's tests or tooling doesn't depend on whatever's in the
operator's own `~/.reasona/reasona.yaml`.

See `docs/INSTALL.md` for installation and configuration.

## Prompt profiles

review/recheck/bugbot/compliance/final_audit prompts are project- and
language-specific -- they live as plain `.md` files under a named **profile**,
resolved through exactly **two layers, project then global**:

```
<workdir>/.reasona/prompts/<profile>/<role>.md   project-local
~/.reasona/prompts/<profile>/<role>.md           global (an operator's shared set)
```

There is deliberately no packaged third layer. Every other setting in this
project resolves the same two ways (`reasona.yaml` via `config_file.py`, the
Bernstein seed template via `bernstein_config.py`), and a copy living inside
site-packages is the one layer an operator cannot see or edit -- so a repo
that thought it had customized its review prompt could silently be running a
shipped one for any role file it forgot to add. A repo with neither layer
gets `None` and the cycle aborts, which is the same refusal an unknown
profile name already gets.

Precedence is **per file, not per profile directory**: a project overriding
`review.md` still inherits the global `bugbot.md`.

This repo commits its own `.reasona/prompts/generic/` -- it is what this repo
runs on, and it doubles as the checked-in example to copy into
`~/.reasona/prompts/generic/` when setting up the global layer.

### Per-unit profiles, for repos with more than one language

A single repo-wide profile cannot describe a monorepo whose Rust crates and
Python services need different review policies -- and reviewing a Python
service against Rust-aware prompts is worse than having no profile, because
it produces confident findings from the wrong rulebook. So the profile is
resolved **per PR unit**, from the `files:` that unit already declares:

```yaml
# <repo>/.reasona/reasona.yaml
dev-profile: generic              # when nothing in the map matches
dev-profile-map:
  "crates/**": rust
  "services/**/*.py": python
  "web/**": typescript
```

```yaml
# plan.md manifest
pr_units:
  - index: 3
    files: [crates/flow/src/x.rs]     # -> rust, via the map
  - index: 4
    files: [crates/gen/build.rs]
    profile: rust-buildscript          # explicit, wins outright
```

Resolution order: the unit's own `profile:` > `dev-profile-map:` glob match >
`dev-profile:` > `"generic"`. Files matching nothing are ignored rather than
counted as the default, so a Rust PR that also edits `README.md` is still a
Rust PR.

**A unit whose files map to two profiles is refused at compile time:**

```
$ reasona-dev compile-plan plan.md -o out.yaml
reasona-dev: PR 3 spans 2 profiles:
  crates/flow/src/x.rs -> rust
  services/api/ingest.py -> python
A single review policy cannot cover both. Either set `profile:` on PR 3
explicitly, or split it into separate PR units.
```

Picking the most-specific glob or the majority language would be
deterministic and would also mean half the change goes unreviewed by any
rulebook that applies to it, silently. The check runs at compile time so the
defect surfaces while the author still has the plan open, not an hour into a
run.

## Running a plan

`reasona_dev/orchestrate.py` is what joins the pieces: it parses the plan
manifest, resolves each unit's profile from its own `files:`, orders units by
`depends_on`, and drives each through `pr_cycle` and then `ship_gate`.

```
reasona-dev run-plan docs/plans/flow-compat.md --workdir .
```

```
plan run: 2 shipped, 1 failed, 0 skipped
  [shipped] pr-1 (rust): review + acceptance + structure all clean
  [shipped] pr-2 (rust): review + acceptance + structure all clean
  [ failed] pr-3 (python): acceptance: 1/2 criteria failed: AC-3-2
```

Four decisions live here because this is the only layer that sees a whole
plan at once:

- **Profiles are resolved up front**, so a two-language unit is refused
  before the first agent spawns rather than after four units merged.
- **A unit whose dependency did not ship is SKIPPED, not attempted.** Its
  premise is a contract that never merged, so reviewing it produces findings
  the author must re-derive after the upstream fix. Skipped is a distinct
  outcome from failed -- reporting five failures when one broke and four were
  never run misstates what happened.
- **Approval gates the first unit only.** `pr_cycle` sees one unit at a time
  and cannot know which is first; this layer can.
- **One Bernstein server for the whole plan**, passed into every
  `run_pr_cycle` call. Same argument that moved role dispatch off per-role
  subprocesses.

The dev (cycle-0) step is deliberately NOT here: `plan_compile` emits a
Bernstein plan.yaml whose stages carry it, and Bernstein's own scheduler runs
that DAG. Owning it here would mean re-implementing a scheduler that already
runs.

## How a role is dispatched

Each role dispatch is a one-step `plan.yaml` executed by `bernstein run` --
the same CLI surface an operator types by hand. Synchronous: the run spawns,
executes, merges and exits, so there is no server whose lifetime this project
has to own.

It was briefly an HTTP path instead (`bernstein serve` + `bernstein worker`,
`POST /tasks`), chosen to avoid paying Bernstein's bootstrap per dispatch.
The premise was never measured, and when it was, it did not hold: **~1.0s of
bootstrap against ~90s for the agent it starts.** What the premise cost was
three of the defects found in live verification, all from running Bernstein
in a shape it does not support -- `bernstein start` is a seed bootstrap not a
bare server, the raw orchestrator self-stops on quiescence by design, and
completion had to be inferred from the artifact because Bernstein's
orphan-completion path parks finished work at `claimed` forever. Batch mode
has none of them, and Bernstein's own watchdog, retry and worktree salvage
supervise each dispatch.

**Turn budget is the only resource control, declared as `complexity`.**
`Task.max_turns` is reachable only over HTTP -- the plan-step schema has no
such field -- but Bernstein derives the budget from a step's `complexity`
(low=20 / medium=40 / high=80 / critical=120) and the claude adapter forwards
it to `--max-turns`. Live, a reviewer died at 23 turns having completed its
analysis and written nothing: the review prompt writes its report as its LAST
action, so a budget that runs out during exploration loses the whole result
rather than truncating it.

**Cost capping is deliberately not attempted.** `--hard-budget` exists but
cannot fire on this path -- the agent reports its own spend in its runner log
(`[RESULT] ... cost=$0.1736`) while `runtime/costs/*.json` records
`spent_usd: 0.0`, and Bernstein's own retrospective logs the gap. A cap that
cannot observe spend is not a cap.

## Quality budget, and why it is shaped this way

A zero-base analysis of dev-ralf's 3.5-month production record (329,721 lines
of Rust, 292 planned PR units) found the architecture sound and the budget
misallocated: 30% of PR units were follow-up corrections and 27% of written
lines were later deleted, *despite* a 16-fix-cycle budget spread over five
review roles. The marginal return of another reviewer was already near zero.
Three of the mechanisms here follow from that, and none of them add a
reviewer:

**Claims the plan makes get executed, not read.** The plan format already
required "Tests (positive + negative)", as prose; a reviewer confirms such an
item exists, never that it runs. `acceptance.py` moves it into the manifest
as `acceptance: [{id, cmd, expect}]` and runs it as a pre-merge gate.
`expect: exit_nonzero` is what makes the negative half statable at all.

**The budget's three costs are separated.** The 8/8/16 cycle caps are a
ceiling, not a spend. What costs money is how expensive each cycle is
(`cycle_gate.recheck_route()` -- a fix confined to the files its findings
named earns a BOUNDED confirm+regression pass on the cheaper `recheck` model
instead of a full re-review) and how many cycles a doomed PR burns
(`cycle_gate.ConvergenceTracker` -- `RecurrenceTracker` only fires when the
SAME finding survives, so a PR emitting fresh findings every cycle used to
run to the full cap; non-convergence now exits at 3).

**One gate, not three available ones.** `ship_gate.py` is the single
pre-merge verdict: `review AND acceptance AND structure`, by conjunction, with
no weighting and no override. Three separately-runnable checks would have left
the pipeline in the state this whole analysis criticized -- "a reviewer asserts
completeness" and "an operator remembers to run the completeness check" are the
same failure with a different actor. All three still report even after one
fails, so an author fixes everything in one round.

    reasona-dev ship-gate pr-3 --cycle-verdict PASS

**Measurement comes before the next cut.** `cycles_log.py` records every
dispatch, gate decision, acceptance outcome, and ship verdict to
`.reasona/cycles.jsonl`, keyed by `Finding.key()`. `cycles_query.py` is the
other half -- a log with no query cannot discharge a single deferred decision,
so the deferral would be permanent by construction rather than by evidence:

    reasona-dev cycles-report

    role attribution (exact)
      role          first  dup  uniq  total
      reviewer         4    1     3      4
      bugbot           1    0     1      1
      compliance       0    1     0      1        <- unique=0: the drop candidate

    acceptance coverage: 2/3 units declare criteria (67%), 1 passed, 1 failed
    gate vs acceptance (units with declared criteria only)
      gate_only=1  acceptance_only=1  both=0  neither=0

`unique` matters more than `first_catch` -- first-catch is resolved by dispatch
order, so a role that runs second in the same cycle is structurally
disadvantaged by it. A role with high `duplicate` and near-zero `unique` is the
one to drop, and the table supports that conclusion directly rather than
requiring interpretation. The one heuristic (`--effective`, "a later commit
touched the same file") is off by default and reported separately: the original
analysis measured that proxy at 84% against a 77% control base rate, i.e.
almost entirely base rate.

**Three mechanisms were built from this analysis and then removed.** They are
recorded here because the reasoning that removed them is as much a result as
the reasoning that built them:

- *A structural gate* (file size, single-PR growth, cross-file duplication,
  dependency direction, public-API growth). The judgment is real -- a
  diff-reading reviewer cannot see an 11,288-line file grow 200 lines at a
  time. But its checks are not equally suited to being a hard gate: a
  refactor that splits a file improves the size check while tripping the
  growth check, and waivers were repo-scoped and permanent where a refactor's
  exemption is unit-scoped and temporary. Enforcing it as built would have
  produced reflexive waivers, which is the failure it existed to prevent.
- *A 5-unit plan cap.* Drawn from a correlation with N=2 (the two largest
  plans produced second-order corrections), and the mechanism it claimed --
  learning from PR 1 cannot reach PR 12's specification -- is not fixed by
  splitting: plan B written before plan A runs has the same problem, and
  nothing enforced sequential authoring. It fragmented the dependency DAG
  across documents in exchange for a proxy. The real fix is mid-plan
  revision, which Bernstein's up-front stage DAG blocks.
- *A first-unit human approval gate.* It fired only when the first unit
  needed a dev fix, so a clean first PR was never gated at all -- backwards
  from the intent of approving the contract shape. Nothing surfaced the wait
  to the operator (the notification callback had no production caller), and
  it gated a fix task's effect rather than the merge. `--merge` defaulting to
  off is the actual human gate, and that is a default, not an approval.

## Memory: generated, never written

`.reasona/memory/*.md` holds priors about where defects have recurred in a
repository -- derived by `memory.py` from `cycles.jsonl`, never hand-authored.
That constraint is the design. A memory directory is the same kind of surface
a skill document is, and skill documents bloat because entries are easy to add
and nobody owns deleting one (dev-ralf's SKILL.md reached 472 lines, much of
it explaining why superseded revisions were wrong, all of it loaded into every
agent's context on every run).

Generation gives three properties nobody has to maintain: a memory cannot
drift from what happened, because it is computed from what happened; a pattern
that stops recurring stops being written, because generation only reads the
last N PR units; and retrieval is scoped by the `files:` a PR unit already
declares, so an unrelated PR gets an unchanged prompt rather than a growing
preamble.

Clustering is exact -- same (path, symbol), or same normalized contract text,
across distinct PR units. Paraphrases are deliberately not clustered: a memory
shapes what the next reviewer looks for, so a wrong grouping actively
misdirects attention, and missing a pattern costs less than inventing one.

Anything a program can enforce does not belong here. That is `structure_gate`
or an acceptance criterion, and writing it as a memory would be choosing to
remind a model of something the pipeline could guarantee.

## Next

**Live end-to-end verified (2026-08-18).** A real repository ran the whole
pipeline -- `compile-plan` -> `bernstein run` (dev cycle-0) -> `run-plan`
(review -> scan -> ship) -- with real agents and real cost:

```
plan run: 1 shipped, 0 failed, 0 skipped
  [shipped] pr-1 (generic): review + acceptance + structure all clean

  review c1  reviewer    PASS
  scan   c1  bugbot      PASS
             compliance  FIX_REQUIRED  mf=1
  decision   spawn_fix   -> dev fix dispatched
  scan   c2  bugbot      PASS
             compliance  PASS          <- the fix held
  acceptance declared=True passed=True ['AC-1-1']
  ship       passed=True {review: T, acceptance: T, structure: T}
```

Getting there took **11 defects, 10 of them ours, and none of them findable by
unit test** -- contract mismatches, process lifetime, path resolution. They
only surface when a real agent writes real output in a real worktree. The full
account is in `docs/ARCHITECTURE.md` §3.8; the three that changed the
architecture:

- **Bernstein has three execution modes and only one is a daemon.**
  `bernstein run` is batch; the orchestrator module is the batch engine's
  claim loop and self-stops on quiescence *by design*; `bernstein serve` +
  `bernstein worker` is the long-lived pair ("Blocks until SIGINT/SIGTERM").
  Using the first two as a daemon produced, in order, a spawner that never
  started and a spawner that quit the moment the review stage drained. The
  worker mode is also what makes remote execution possible later -- it takes
  `--server URL --token`, so the executor need not live where tasks are
  posted.
- **Wire shape is a property of the prompt, not the role.** `pr_cycle` picked
  its parser from the role name, which is only true for a profile delegating
  bugbot to an external skill. The packaged `generic` prompts ask all roles
  for the text contract, so well-formed output was read as a malformed KV
  block and aborted the whole scan stage. `parse_role_output()` now detects
  by literal marker.
- **The root `bernstein.yaml` symlink must exist and must not be tracked.**
  Absent, the orchestrator FATALs with no adapter; committed, git materializes
  it into every agent worktree where Bernstein's isolation check refuses it.
  Both are zero agents. `ensure_bernstein_yaml()` creates the link on every
  call and adds it to the target repo's `.gitignore`.

One defect is upstream and stays defended rather than fixed: Bernstein's
orphan-completion path raises `TypeError: Object of type AgentLogSummary is
not JSON serializable`, leaving a finished task at `claimed` forever. The
defence is to treat the output FILE as the completion indicator -- which was
always the real contract here, since `result_summary` never carried the
agent's report.

**The merge tail is built** (`reasona_dev/merge_tail.py`): sync-main ->
conditional final audit -> squash-message guard -> PR creation -> up-to-date
gate -> squash-merge. It consumes `ship_gate`'s verdict rather than
re-deciding, and every step fails by name -- `gh` missing, `gh`
unauthenticated, a sync conflict, a rejected squash title, a PR behind its
base -- instead of degrading into a quiet skip.

```
reasona-dev run-plan plan.md --ship          # stop at an open PR
reasona-dev run-plan plan.md --merge         # ...and squash-merge it
```

Merging is off by default. A squash-merge rewrites a real default branch, so
it is something the caller asks for, not something they discover afterwards.

`final_audit` now has a dispatch site, and it is conditional: a unit that
passed review and both scan roles on its first cycle has been read by three
independent roles with nothing found, and a fresh whole-PR audit there mostly
re-derives that. The audit earns its cost where fixes accumulated -- each fix
is a change no reviewer saw in its final combined form. It runs under the
`"final"` stage of the SAME `FixBudget` the review and scan stages spent, so
a PR cannot quietly buy 8+8+2 fix cycles while every stage reports itself
within cap.

**One thing remains: two decisions deferred to measurement, not judgment.**
Both are blocked on data that only accumulated runs produce, and both have
the exact query that decides them (`reasona-dev cycles-report`):

- *Which review role to drop* -- the `unique` column. A role with high
  `duplicate` and near-zero `unique` is the candidate.
- *Whether a unit with no acceptance criteria should be refused rather than
  warned* -- the `acceptance coverage` line.

Two items from the source analysis were examined and deliberately NOT built,
with reasons in `docs/ARCHITECTURE.md` §3.7.4: mid-plan revision (Bernstein
declares its stage DAG up front) and the runtime feedback loop
(product-specific; its general form is post-merge acceptance).
