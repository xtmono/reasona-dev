# reasona-dev Architecture

Written: 2026-08-16
Status: **Initial implementation underway** — the mapping below was verified by directly
cross-checking the actually installed Bernstein 3.15.1 source (`site-packages/bernstein`).
It is measured, not guessed.

## 0. What this document is

`reasona-dev` is the successor to the existing `dev-ralf` (a Claude Code skill, LLM-scheduler
based). The orchestration layer uses [Bernstein](https://github.com/sipyourdrink-ltd/bernstein)
(Apache-2.0) as-is, as an opaque execution engine, and layers our own code on top of it for the
assets `dev-ralf` proved out through measured operation — plan-authoring discipline
(`reasona-plan`, successor to `plan-ralf`), multi-lens adversarial verification, and a deterministic
finding contract.

The use of Bernstein is not hidden. It is stated explicitly in the architecture document and README.

### 0.1 After deployment there is no "reasona-dev repository" as such

Right now, during development, there is a source checkout at `~/repository/reasona-dev`, but
**once deployed this directory will not exist.** `reasona-dev` becomes an installed package just
like `bernstein` (`uv tool install`, or a library dependency) — the same way the `bernstein`
binary itself is installed at `~/.local/share/uv/tools/bernstein/` while always operating against
**a target repository the caller specifies.**

**So every state file must be anchored to the target repository (`workdir`), not to reasona-dev's
own install location.** Missing this produces a real bug — the draft `compile_to_bernstein_plan()`
wrote `.reasona/model_config.json` as a path relative to **the current working directory (CWD) of
the process running the compile script.** Running the compile from the target repo's root happened
to work by accident, but running it from anywhere else put the file in the wrong place.

**Convention after the fix:**

| File | Anchor | Timing |
|---|---|---|
| `.reasona/model_config.json` | `workdir` (explicit argument, default `Path.cwd()`) | at `compile_to_bernstein_plan()` call time (compile time) |
| `.reasona/review-<stage>.json` | `orch._workdir` (Bernstein orchestrator's fixed project root) | when the janitor evaluates `completion_signals` (runtime) |

**The key point is that these two must point at the same directory.** Like
`compile_to_bernstein_plan(..., workdir=<target repo>)`, **the caller must guarantee that the
`workdir` at compile time and the directory from which `bernstein run` is later executed always
point to the same path** — the same convention as Bernstein itself, which uses the invocation-time
CWD as the project root when run without `--workdir`. It was confirmed from source that
`orch._workdir` is **a single fixed path per orchestrator**, not a per-task worktree
(`core/tasks/task_lifecycle.py`'s `_verify_via_janitor` runs every task's `test_passes` with
`cwd=orch._workdir`).

`write_audit_trail=False` can turn off audit-trail recording entirely (for tests).

## 1. Four-layer structure

```
[0] PRD/plan authoring — reasona-plan (Claude Code skill, separate repo / successor to plan-ralf)
        │  Human + LLM collaboration; the Open Decisions Gate settles undecided items
        ▼
[1] Plan compiler — reasona_dev/plan_compile.py (this repo)
        │  PR units + depends_on + files → bernstein plan.yaml (stages/steps)
        ▼
[2] Bernstein core — opaque execution engine (installed via pip, source unmodified)
        │  worktree isolation · K-way parallelism (max_agents) · CLI adapters (claude/codex/cursor/kilo, etc.)
        │  review --pipeline (multiple lenses expressed via strategy: all)
        │  model_fallback (deliberately constrained/empty — CREDIT-BURN compliance)
        ▼
[3] reasona-dev contract layer — reasona_dev/*.py (this repo, the core new code)
        │  completion_signals(test_passes → gate_check.py) — merge/no-merge decision
        │  pluggy hookimpl(on_pre_task_create) — decides only whether to create the next fix task
        │  finding adapter + cycle_gate + squash
        │  disposition/severity split · INCONCLUSIVE · evidence contract · dev escalation
        ▼
merged to origin/main
```

## 2. Mapping verified by measurement

| Needed capability | dev-ralf's equivalent | Bernstein's actual capability (verified) |
|---|---|---|
| "must merge only if everything PASSes" | `worker.md` merge rule | `review_pipeline/schema.py`'s `AggregatorStrategy: any\|all\|majority\|weighted` — **uses `all`** |
| plan → PR DAG | `parse_plan.py` | `bernstein run plan.yaml` — stages/steps, LLM decomposition can be skipped |
| worktree isolation | *Provision worktree* | native, configurable via `worktree_setup` (symlink/copy/setup_command) |
| merge/no-merge decision | `cycle_gate.py` | **`completion_signals`'s `test_passes`** — confirmed there is no dedicated pre-merge hook (§3.3). `gate_check.py` fills that role |
| whether to create the next fix task | *Loop control* | `plugins/hookspecs.py`'s `on_pre_task_create` — "hooks may block by raising" (confirmed, a separate responsibility from the merge decision) |
| forbid silent model switching on exhaustion (CREDIT-BURN) | a hard blocker in *RESULT parsing* | confirmed disabled via `model_fallback.fallback_chain: []` (§3.2). `strike_limit` is left unset (the value is moot) |
| escalation during initial model selection | (not applicable — no such concept in dev-ralf) | `cascade_router.py` is **dead code** (§3.1); the real path is `bandit_router.py` — can be bypassed by setting `model:` explicitly on a step, no action needed |
| CLI adapters | `run.py` (471 lines) + `dispatch.md` | `claude`/`codex`/`cursor`/`kilo`/**`agy` (native, §3.4)** all ship by default. Only `ocr` needed a new custom adapter (§3.4, `reasona_dev/adapters/ocr.py`) |

## 3. Investigation complete — confirmed conclusions

All four items left unresolved at the previous starting point (§3.1–§3.4) have now been resolved
by direct source cross-checking. The only remaining unstarted work is an actual end-to-end run of
one real PR through this pipeline, start to finish.

### 3.1 `cascade_router.py` is dead code (confirmed)

Grepping the entire installed package for uses of `cascade_router.` / `CascadeRouter` finds
**no call site outside the module's own file.** In other words this module remains in the
codebase but is not wired into any live execution path.

**The initial model-selection path that is actually live is `core/routing/bandit_router.py`
(`BanditRouter`)**, called from `core/orchestration/orchestrator.py`,
`core/tasks/task_lifecycle.py`, and `core/agents/spawner_warm_pool.py`. This is a different kind
of thing from what CREDIT-BURN was worried about ("silently switching on exhaustion") — it picks
the model **at task start** based on past reward data (a static heuristic for cold start, a LinUCB
bandit thereafter), not switching in response to an in-flight failure.

**Two additional facts were re-confirmed from source (follow-up investigation).**

1. **`BERNSTEIN_ROUTING` defaults to `"static"` — the bandit is off by default.**
   `core/orchestration/orchestrator.py`:
   ```python
   _routing_mode = os.environ.get("BERNSTEIN_ROUTING", "static").lower()
   self._bandit_router = (
       BanditRouter(policy_dir=...) if _routing_mode in {"bandit", "bandit-shadow"} else None
   )
   ```
   Unless this env var is explicitly set to `bandit`/`bandit-shadow`, the `BanditRouter` object
   itself is never constructed. **`reasona-dev` does not set this env var, so under the current
   configuration the bandit never gets involved.**
2. **The tier system (`_MODEL_TIER = {"haiku": 0, "sonnet": 1, "opus": 2}`) is Anthropic-only and
   never fires for other provider adapters.** `router_applicable()` docstring: "The bandit
   router's arms (haiku/sonnet/opus) are Claude-specific model names. For non-Claude adapters
   (qwen, gemini, codex, etc.) the router cannot produce a meaningful model selection **and should
   be skipped**." This guard is not dead code — it is confirmed to be consumed at real call sites
   (`task_lifecycle.py:1026`, `spawner_warm_pool.py:62,94`) — unlike `model_fallback`'s
   `strike_limit`, this one really is wired in.

**Conclusion: unless `BERNSTEIN_ROUTING=bandit` is turned on (and this repository does not turn it
on), every step's model is whatever value was set on the step (or the static default, if unset).**
The same guarantee holds as in the dev-ralf era: "it runs on exactly the model you specified."
§3.5 covers where that setting is actually made (including the current gap).

### 3.2 `model_fallback.strike_limit: 0` was a bug (fixed)

Measured from `core/routing/model_fallback.py`:

```python
should_fallback = (not state.is_fallback) and (state.consecutive_529_errors >= self._strike_limit)
```

`consecutive_529_errors` starts at 0. With `strike_limit: 0`, even with **zero** actual errors,
`0 >= 0` is true and a fallback fires immediately — the setting put in at the previous starting
point was the exact opposite of what was intended. **What actually prevents fallback is not
`strike_limit` but an empty `fallback_chain`** — `activate_fallback()` is gated by
`if state.fallback_chain and ...`, so with an empty chain there is nowhere to switch to in the
first place. `bernstein.yaml` was fixed to leave `strike_limit` unset entirely (moot with an empty
chain) and keep only `fallback_chain: []`.

### 3.3 There is no dedicated hook that blocks a merge — `completion_signals` fills that role (confirmed)

Across all of `plugins/hookspecs.py`, the only hooks documented as able to block by raising are
**`on_pre_task_create` and `on_pre_tool_use`.** There is no dedicated hook such as
`on_pre_merge` or `on_pre_task_complete`. `on_task_hook_rejection` is a hook that is notified
**after** a block has already happened, not one that performs the block itself.

In other words, the judgment of "is this task's result good enough to move toward a PR/merge" is,
by Bernstein's actual design, expressed not through a pluggy hook but through
`completion_signals`'s **`test_passes`** (a shell command's exit code) — exactly the same slot
dev-ralf currently fills with `make ci-fast`/`make lint-md` as gates. `reasona_dev/gate_check.py`
was written as this entry point, and it is automatically attached to every step
`plan_compile.py` generates:

```yaml
completion_signals:
  - type: test_passes
    command: "python3 -m reasona_dev.gate_check .reasona/review-<stage>.json"
```

`on_pre_task_create` (the hook the plugin actually uses) plays a different role — it decides
**whether to create the next fix task** (budget exhaustion, recurrence escalation), not whether
the result of an already-created task is mergeable. The two mechanisms' responsibilities are kept
separate.

### 3.4 `agy`/`ocr` adapters — investigation complete

The judgment made at the previous starting point ("not in Bernstein's default list, both need to
be custom") was based only on skimming the README's prose list and was inaccurate. It was
re-confirmed by opening `adapters/registry.py` directly.

**`agy` is already a native adapter.** `AgyAdapter` ("Antigravity CLI") — the binary name `agy` and
the `-p`/`--dangerously-skip-permissions`/`--conversation <id>` flags match dev-ralf's
`dispatch.md` invocation contract for agy exactly. Judged to be the same tool and used
**with no changes.** However, the adapter's own docstring states that "native resume is not wired
yet... orchestrator falls back to fresh sessions with scratchpad reinjection" — instead of reusing
the same session across cycles the way dev-ralf currently does (`--resume` reuse, continuing with a
short prompt), it is a fresh session plus prior-context reinjection on every cycle. This does not
affect the decision logic, but the per-cycle cost of the agy role may come out differently from
dev-ralf's measured figures.

**`ocr`'s absence was re-confirmed as real** (the string `ocr` does not appear anywhere in the
adapter tree or registry.py) — `OcrAdapter` was newly written in `reasona_dev/adapters/ocr.py`. It
implements the `CLIAdapter` abstract interface (`spawn()`/`name()`), with command assembly split
out into `build_ocr_command()` so it is testable as a pure function with no process spawn
(`tests/test_ocr_adapter.py`, 6 cases). It was registered via
`[project.entry-points."bernstein.adapters"]` and verified in practice.

```
$ python3 -c "from bernstein.adapters.registry import get_adapter; \
  print(get_adapter('ocr').name())"
OCR (diff-scanning reviewer)

$ bernstein doctor   # "Adapter version: 1 tracked adapter(s), 0 below"
```

Key invariants preserved (verbatim from dispatch.md's original contract): stateless (a fresh
`origin/main..HEAD` diff every cycle, no `$prompt`/session), `--timeout` given in seconds is
**converted to minutes** when passed along (this keeps ocr's own per-file timeout aligned with the
outer wrapper's timeout — without it, a large file can hit ocr's internal 10-minute default and be
silently skipped as `"classification":"timeout"` even while still within the outer budget), and
`--model` is omitted when it is the `default` sentinel.

### 3.5 Per-role model configuration — `reasona_dev/model_config.py`

Since `BERNSTEIN_ROUTING` is off by default (§3.1), the bandit does not pick anything for us. In
other words, **something has to explicitly set the model somewhere for this to run on exactly the
same model, the way dev-ralf did.** This module is that place — it ports the priority chain from
dev-ralf-renewal-claude.md §3.7 verbatim, renamed to `REASONA_DEV_*` environment variables.

Each layer's value accepts not just a bare model name but dev-ralf's own
`tool:model:effort[,extra]` composite form (e.g. `claude:sonnet:high`) — see §3.5.0.

```
dev:            --dev            → REASONA_DEV_DEV_MODEL            → project cfg → global cfg → claude:sonnet:high
review(first pass): --review     → REASONA_DEV_REVIEW_MODEL         → project cfg → global cfg → claude:opus:high
recheck:        --recheck        → REASONA_DEV_RECHECK_MODEL        → project cfg → global cfg → review's final resolved value
bugbot:         --bugbot         → REASONA_DEV_BUGBOT_MODEL         → project cfg → global cfg → [compliance's slot, same 4 steps] → kilo:deepseek-v4-pro:high
compliance:     --compliance     → REASONA_DEV_COMPLIANCE_MODEL     → project cfg → global cfg → claude:sonnet:high
final audit:    --final-audit    → REASONA_DEV_FINAL_AUDIT_MODEL    → project cfg → global cfg → [compliance's slot, same 4 steps] → claude:opus:high
dev_escalation: (no CLI flag)     → REASONA_DEV_DEV_ESCALATION_MODEL → project cfg → global cfg → claude:opus:high
```

`dev_escalation` is referenced only at runtime, while `plugin.py`'s `on_pre_task_create` hook is
alive, not at `plan.yaml`/`review.yaml` generation time, so it has no natural slot in either the
`reasona-dev compile-plan` or `render-review` subcommand — its env-var/config-file layers behave
the same as any other role's, but the flag layer is not yet wired into the CLI.

**A real bug was caught while implementing this.** The draft had `bugbot`/`final_audit` fall back
onto the `compliance` role's **fully resolved outcome** (its value), but the original spec says
they should fall back only onto the `DEV_RALF_COMPLIANCE_MODEL` **environment variable (and config
slot) itself.** The two are different — if only a `--compliance` flag is given with no
`COMPLIANCE_MODEL` env var/config, the draft let that flag value leak through to bugbot, but the
original spec does not. Only `recheck` is the exception that inherits review's fully resolved value
("first-pass reviewers") — this asymmetry is intentional and must be implemented precisely
(`tests/test_model_config.py`'s `test_bugbot_does_not_inherit_compliances_own_default` and
`tests/test_config_file.py`'s
`test_bugbot_falls_back_to_compliance_config_slot_not_compliances_resolved_value` pin this
regression).

**Guarding against CONDUCTOR-COLLAPSE**: every value `resolve_all()` returns carries not just
`value` but also `source` (`flag`/`env:<VAR>`/`config:project:<role>`/`config:global:<role>`/
`fallback:<role>`/`default`). `write_resolved_config()` records this into
`.reasona/model_config.json`, so if the wrong model ran, it is possible after the fact to trace
which layer diverged.

Both `plan_compile.py` (the dev role) and `review_pipeline.py` (review/recheck/bugbot/compliance)
determine their model only through this module — measured: with no environment variables set, both
`bernstein plan validate` and `bernstein review --pipeline ... --validate-only` were confirmed to
pass with output that correctly reflects `model: sonnet` (dev) and `bugbot(deepseek-v4-pro)`.

#### 3.5.0 Adapter and effort now also follow the same chain (fixed — previously hardcoded)

This module originally followed the priority chain **for the model value only** — the adapter
(`claude`/`kilo`) and `effort` were hardcoded literals in `review_pipeline.py`
(`"adapter": "claude"`, `"effort": "high"`), and `plan_compile.py`'s dev step did not fill in
`effort` at all. This gap surfaced while porting dev-ralf's actual env var
(`DEV_RALF_DEV_MODEL=claude:sonnet:high`) verbatim — dev-ralf treated the **tool:model:effort**
triple as one value from the start, while reasona-dev only ran the model through the chain and
hardcoded the other two.

**Fix**: `_split_composite()` parses the same `tool:model:effort[,extra]` form dev-ralf uses,
identically regardless of which layer it comes from (flag/env var/config file).
`ResolvedModel` carries `model`/`adapter`/`effort`/`source` together (the field that used to be
`.value` was renamed to `.model` — keeping all three values under one field name only invites
confusion), and `review_pipeline.py`/`plan_compile.py` no longer contain any literals, reading only
`resolved[role].{model,adapter,effort}`. However, the `plan.yaml` step schema has `model`/`effort`
but **no adapter field** (confirmed against `core/planning/plan_schema.py`'s `_STEP_SCHEMA` —
only a plan-wide `cli:` key exists) — since this project compiles only a single dev role into
plan.yaml, it is enough to put `resolved_dev.adapter` into the plan-level `cli:`.

`dev_escalation` (the `escalation_model` argument to `cycle_gate.evaluate()`, previously a
hardcoded `"opus"` default in the function signature) was added as the same kind of role, and
`plugin.py` explicitly passes `resolve_all()["dev_escalation"].model`.

**Actual rollout**: the user's real 7 dev-ralf environment variables (`DEV_RALF_*_MODEL`) were
carried over verbatim into `~/.reasona/reasona.yaml` (the global config). In the process it was
confirmed that `bugbot`'s measured dev-ralf value is `claude:opus:high` (not reasona-dev's existing
hardcoded default of the kilo adapter/`deepseek-v4-pro`), and `bernstein.yaml`'s
`role_model_policy.bugbot.provider` was corrected from `kilo` to `claude` to match.
`role_model_policy` is still a static file maintained by hand, so this kind of drift can recur —
`tests/test_bernstein_yaml_consistency.py` cross-checks `bernstein.yaml`'s `provider` values
against `resolve_all()`'s actual resolved results and catches drift automatically.

#### 3.5.1 `reasona_dev/config_file.py` — a two-layer cfg mirroring Bernstein's own pattern

Separate from Bernstein's own six-layer cascade (§0.1), this is a **two-layer** cfg for
reasona-dev's own configuration (exactly two layers as requested — not a full copy of Bernstein's
six):

```
~/.reasona/reasona.yaml           global (per-user default)
<workdir>/.reasona/reasona.yaml   project/local (overrides global)
```

Format (the file is named `reasona.yaml`, not `config.yaml` — `.reasona/` is a namespace shared by
this whole product family, and this one file is shared as well. The future `reasona-plan`
(successor to `plan-ralf`) will add its own settings to the same file under a separate top-level
key, `plan-models:` — `reasona_dev` reads only the `dev-models:` key and never touches
`plan-models:`):

```yaml
dev-models:
  dev: claude:sonnet:high
  bugbot: claude:opus:high
  # It is fine to fill in only some of these -- any role that is missing falls
  # through to the next layer (env var has already been passed, so next is
  # the hardcoded default, or recheck/bugbot/final_audit's own fallback).
  # A bare model name ("sonnet") is still valid -- adapter/effort keep that
  # role's default (§3.5.0).
plan-models:
  ...   # future reasona-plan's own key, same file -- reasona_dev never reads this key
```

The user's 7 measured dev-ralf settings were actually placed under the `dev-models:` key in
`~/.reasona/reasona.yaml` (see the bottom of §3.5.0) — this example is an abbreviated version of
that file.

`<workdir>` is exactly the same anchor as in §0.1 — not reasona-dev's own install location, but
**the target repository.** `resolve_all(workdir=...)` reads it fresh on every call with a default
of `Path.cwd()` (no caching — the file is small and call frequency is low, so there is no
performance concern). `load_config_files=False` can turn off filesystem access entirely (for
tests — every unit test in this repository is isolated this way).

Its priority sits **below env var, above the hardcoded default (or sibling fallback)** — following
the ordinary CLI-tool convention that a one-shot override (env var) beats a persistent setting
(config file), and a project cfg beats a global cfg. `bugbot`/`final_audit` keep the same §3.7
asymmetry at the config layer too — they reference `compliance`'s **cfg slot**, not `compliance`'s
fully resolved value.

Measured: in a temporary repository under `/tmp` unrelated to the reasona-dev source tree,
`bugbot: kilo-custom-model` was set via `.reasona/reasona.yaml`, and running
`bernstein review --pipeline ... --validate-only` was confirmed to produce `Pipeline OK` with
`bugbot(kilo-custom-model)` actually reflected.

#### 3.5.2 Relationship with Bernstein's own `--model`/`BERNSTEIN_MODEL` — investigation complete

Bernstein has its own native model override too: `bernstein run --model TEXT` (help text: "Force
specific model... overrides config file"), falling back to the `BERNSTEIN_MODEL` environment
variable (`orchestrator.py`: "Falls back to BERNSTEIN_MODEL env var"). `--cli`/`BERNSTEIN_ADAPTER`
exist as the same kind of pair.

**However this is a single run-wide value, not per-role, and its applicability condition is
narrow.** Cross-checked against its actual consumer (`spawner_core.py`):

```python
_model_unpinned = not pinned_model_flag and (not tasks[0].model or tasks[0].model in _CLAUDE_TIER_MODELS)
```

`run_model` (i.e. `--model` or `BERNSTEIN_MODEL`) only steps in when either ① a task has no
`model:` at all, or ② `model:` is a bare Claude tier name (`opus`/`sonnet`/`haiku`) being run on a
non-Claude adapter (in which case it is force-converted to a name that adapter understands). Its
own comment: "Claude-compatible adapters and non-tier models pass through byte-identical."

**None of the steps `reasona-dev` generates hit either condition** — `plan_compile.py`/
`review_pipeline.py` always fill in an explicit `model:` on every step (ruling out condition ①),
`dev`/`review`/`compliance`/`recheck` use the `adapter: claude` + tier-name combination and pass
through byte-identical (ruling out condition ②, since it's the Claude adapter), and `bugbot` uses
the non-tier name `deepseek-v4-pro`, which was never a match target to begin with (ruling out
condition ② too). **No action needed — though should any future step end up with a bare tier name
like `model: opus` on `adapter: codex` or `adapter: gemini`, this path becomes relevant again and
should be revisited then.**

#### 3.5.3 `reasona_dev/bernstein_config.py` — automatic placement and sync of `bernstein.yaml`

There was a deployment gap where `bernstein.yaml` itself had to be hand-prepared for every target
repository (first flagged in §4's deployment checklist). Investigation confirmed Bernstein really
has no global-config concept at all:

- The function `bernstein run`/`bernstein doctor`/bare `bernstein` actually use to find their
  config is `cli/helpers.py`'s `find_seed_file()`, and its search list is just two entries, **both
  cwd-relative**: `<cwd>/.bernstein/bernstein.yaml` → `<cwd>/bernstein.yaml` (including `.yml`).
  There is no `Path.home()` call anywhere in this function.
- Only `bernstein run` can point at a different file via `--seed PATH`; `bernstein doctor`/bare
  `bernstein` have no such option at all — meaning that however you run it, a real file has to
  exist at the repo root (or `.bernstein/`).
- There is separate code that does look at `~/.bernstein/bernstein.yaml` (`core/agents/
  warm_pool.py`, `core/routes/embedding.py`, `core/protocols/mcp/mcp_composition.py`, a few TUI
  settings) — but every one of them reads **only its own narrow section** (e.g. the `warm_pool:`
  key), which has nothing to do with the whole configuration `bernstein run` actually needs, such
  as `role_model_policy`/`model_fallback`/`approval`/`worktree_setup`.

**Conclusion: there is no "real global bernstein.yaml" on Bernstein's side.** So reasona-dev built
its own "reasona-dev-only global" — in exactly the same shape as
`reasona_dev.config_file`'s `reasona.yaml` two-layer cascade:

```
<workdir>/.bernstein/bernstein.yaml   the spot Bernstein checks first -- left alone if it already exists; the real target of a fresh bootstrap
<workdir>/bernstein.yaml              on a fresh bootstrap, created as a relative symlink pointing at .bernstein/bernstein.yaml (see below); left alone if a real file already exists
<workdir>/.reasona/bernstein-template.yaml     project-local template (checked first at bootstrap time)
~/.reasona/bernstein-template.yaml             global template (GLOBAL_BERNSTEIN_YAML, checked next at bootstrap time)
```

**Why a symlink was used instead of making both spots real files (correction, confirmed by an
actual paid run plus a free re-verification on 2026-08-18) —** Bernstein itself is split between
two paths on where the seed file lives. `find_seed_file()` (used by `bernstein doctor`/bare
`bernstein`/`run`'s top-level CLI parsing) checks `.bernstein/bernstein.yaml` first. But the
background orchestrator subprocess that `bernstein run` launches
(`core/server/server_launch.py::_start_spawner`) **does not use `find_seed_file()` at all** —
when no seed_path is passed explicitly (exactly the case for the call shape `pr_cycle.py` actually
uses, `bernstein run <plan> --auto-approve`, no `--from-plan`), it independently re-derives only
`workdir/bernstein.yaml` (root only, no `.bernstein/` check).

Building an actual target repository with only `.bernstein/bernstein.yaml` present and running it
for real produced **not a single agent spawned** — the cause was confirmed in
`.sdd/runtime/spawner.log`:

```
resolved seed_path=<workdir>/bernstein.yaml (from --seed-path=None, exists=False)
FATAL: no adapter configured...
```

`_start_spawner()`'s own docstring had already warned of exactly this bug ("passing seed_path as
None causes role_model_policy to silently vanish and spawns with the default model with no
error"). The watchdog attempted 5 restarts and gave up, leaving the task stuck `open` forever.

Moving the same file to the root made it work immediately — a real haiku agent was spawned, created
and committed a file, merged into main, and `GET /tasks/{id}`'s `result_summary` was confirmed to
correctly reflect `"Auto-completed: agent ... made git commits"`. But fixing the bootstrap target
to the root alone means simply giving up on `find_seed_file()`'s preference for `.bernstein/` —
there was no reason to pick one path and break the other.

Making `root_target.symlink_to(".bernstein/bernstein.yaml")` a relative symlink means
`Path.exists()`/`.read_text()` transparently follow the symlink, so the orchestrator subprocess's
hardcoded root lookup resolves the link to the same real file — confirmed with a zero-cost
re-verification (running Bernstein's own orchestrator module directly, with no agent spawn:
`python3 -m bernstein.core.orchestration.orchestrator --seed-path None`):

```
resolved seed_path=<workdir>/.bernstein/bernstein.yaml (from --seed-path=None, exists=True)
constructing AgentSpawner with ... adapter=<ClaudeCodeAdapter>
```

Confirmed to proceed normally through adapter construction with no more `FATAL`. So
`ensure_bernstein_yaml()`'s bootstrap target was set back to `.bernstein/` (the real file), with
only a symlink at the root pointing to it — both `find_seed_file()` and the orchestrator subprocess
now read the same real file identically.

**`sync_role_model_policy()`** solves a separate, ongoing problem — the `role_model_policy` value
from §3.5 is a static file that has to be kept in sync by hand, and it really did drift once
(bugbot changed to `claude:opus:high` in `~/.reasona/reasona.yaml` while `bernstein.yaml` stayed
at `kilo`). This function patches only the `provider:` value, textually, every time `compile-plan`
runs (a regex substitution rather than a YAML re-serialization, so the extensive comments — e.g.
the CREDIT-BURN explanation — are all preserved intact) — extending what
`tests/test_bernstein_yaml_consistency.py` used to only "detect" into now "auto-fix."

This sync also has to follow the exact `flag > env var > project cfg > global cfg` chain, and the
draft had a bug calling `resolve_all(workdir=workdir)` with no `flags=` at all — `dev` was handled
through a separate path (`resolved_dev`) so its flag was reflected, but `review`/`recheck`/
`bugbot`/`compliance`/`final_audit` had their whole flag layer silently ignored. Fixed by adding a
`policy_flags` parameter to `compile_to_bernstein_plan()` and extending the `compile-plan` CLI to
accept and pass through the full set of role flags, not just `--dev`. Re-measured layer by layer
using `bugbot` alone, stacking all four levels (global → local config → env var → flag) via the
actual CLI, confirming at each step that the lower layer's value correctly overrode the one above.

**The reasona-dev repository itself also keeps using the `.bernstein/bernstein.yaml` placement** —
`.bernstein/bernstein.yaml` is committed, which is exactly the "already exists, so leave it alone"
case in the cascade above (no template copy actually ran). `bernstein doctor`/bare `bernstein` have
no `--seed` alternative, so as long as those commands are run directly against this repository
(the mode used throughout this session's verification), a real file has to exist at either
`.bernstein/` or the root, and `.bernstein/` — the one `find_seed_file()` checks first — was
chosen. `.reasona/reasona.yaml`, on the other hand, is not read by Bernstein at all but by
`reasona_dev.config_file`'s pure reasona-dev-own configuration (the `dev-models:` key), so it has
no such constraint, and it is committed as this repository's own project-local layer (so this
repository's tests/tooling always behave the same regardless of what's in the operator's personal
machine's `~/.reasona/reasona.yaml`).

#### 3.5.4 `bernstein review --pipeline` cannot be used for review/bugbot/compliance (confirmed, `review_pipeline.py` retired)

Opening the runner's execution code directly confirmed that the original design decision to
implement review/bugbot/compliance via `review_pipeline.py` was itself wrong. The actual
`_run_one_agent()` in `core/quality/review_pipeline/runner.py`:

```python
async def _run_one_agent(agent: AgentSpec, ...):
    model = agent.model or select_reviewer_model("any", override=None)
    prompt = _build_agent_prompt(diff_src, prior_stages)   # does not take agent
    raw = await llm_caller(prompt=prompt, model=model, ...)  # a pure LLM API call
```

`_build_agent_prompt` → `cross_model_verifier._build_prompt` is a fixed template that fills in
only `{title}`/`{description}`/`{diff}` and does not take `role` at all. `_run_one_agent` never
reads `agent.adapter`/`agent.prompt_template` anywhere either. In other words:

- **There is no way to give different roles different prompts** — `reviewer`/`bugbot`/`compliance`
  all receive the byte-identical prompt, differing only in `model:`.
- **There is no tool access whatsoever** — it is a single one-shot LLM API call that throws a
  chunk of diff text and gets an answer back, with no CLI process spawned. There is no way at all
  to run an agentic skill like `ext-bugbot --dir ...` that needs bash / file reading.
- The `--pr` mode also just fetches a diff via `gh pr diff` and goes through the exact same path
  (`run_pipeline` → `_run_stage` → `_run_one_agent`) — there is no separate agentic path.

The same kind of trap as `cascade_router`/`strike_limit` — this one went unnoticed for a while
because it was judged "done" from `--validate-only` (YAML schema validation) alone, without reading
the runner body — `--validate-only` never looks at whether fields are actually used.

**Replacement design — reproduce dev-ralf's `worker.md` verbatim.** `review_pipeline.py` and the
`render-review` CLI subcommand, `samples/review.yaml`, and related tests it used were all deleted
and replaced with two modules:

- **`reasona_dev/prompt_profile.py`** — makes review/recheck/bugbot/compliance/final_audit prompts
  swappable per language/project. The profile name is decided through the chain
  `flag > REASONA_DEV_PROFILE env var > the project reasona.yaml's dev-profile: > global >
  "generic"`, and the actual `.md` files are looked up across exactly **two layers**:
  `<workdir>/.reasona/prompts/<profile>/` → `~/.reasona/prompts/<profile>/` (the package layer was
  removed in §3.7.10). A profile that does not exist does not silently fall through to another
  profile — it returns `None` (reapplying the CONDUCTOR-COLLAPSE principle).
  This repository commits `.reasona/prompts/generic/{review,recheck,bugbot,compliance,
  final_audit}.md` — review/final_audit port `worker.md`'s actual prompts verbatim (they were
  already language-neutral to begin with), while bugbot/compliance were written as self-contained
  defaults with no dependency on any external skill. The target repository itself can put an
  `ext-bugbot --dir`-style delegating prompt at `.reasona/prompts/<profile>/bugbot.md`
  (out of scope here — authored directly in the target repository).
  Per-unit profile resolution for mixed-language repositories is covered in §3.7.10.

- **`reasona_dev/pr_cycle.py`** — a deterministic driver that reproduces `worker.md`'s "Pipeline
  you run: develop → review (up to 8 cycles) → bug+compliance scan in parallel (up to 8 cycles)"
  verbatim. It was also confirmed that Bernstein itself has no hook that can express this loop
  (already surfaced during the §3.6 CREDIT-BURN investigation: `fire_task_completed` is called
  **synchronously** in the same process as the task server — a re-entrant HTTP call back into that
  same server from inside it carries event-loop deadlock risk that cannot be trusted without
  verification). So this driver runs **outside** Bernstein, one layer up — it initially built a
  1-step `plan.yaml` per role and ran `bernstein run <that plan> --auto-approve` as a subprocess
  (the same CLI surface a human would type by hand), but since a single call to `run_pr_cycle()`
  can dispatch review/bugbot/compliance/dev-fix repeatedly, up to 8 cycles each, spinning up a
  fresh server/orchestrator/worktree on every single role call was pure overhead. Noting that
  `bernstein start`/`run` actually "detaches the task server into a background process and returns
  immediately" (confirmed both from `bernstein start --help`'s own description and from
  `server_launch.py`'s actual behavior), `reasona_dev/bernstein_server.py` was newly written to
  launch a server **only once** per `run_pr_cycle()` call, after which every role dispatch is sent
  to that server via `POST /tasks` and polled via `GET /tasks/{id}` — the `plan.yaml` file itself
  is no longer needed at all (`model`/`effort`/`cli`/`completion_signals` were confirmed, directly
  from the installed Bernstein package, to be first-class fields in the `TaskCreate` request body).
  The result-handoff approach of instructing the agent by prompt to "write your full output to
  file X on completion" and then reading that file is kept as-is — `POST /tasks`'s
  `result_summary` is just Bernstein's own one-line auto-completion note (measured:
  `"Auto-completed: agent backend-<id> made git commits on branch (no signals to verify)"`), and
  does not carry the actual free-form report (markdown + a `RESULT:` line) `finding_adapter.py`
  needs to parse. `review`/`final_audit` are parsed with `finding_adapter.parse_text_contract`, and
  `bugbot`/`compliance` with the newly written `parse_kv_contract`, and the next action (keep
  reviewing / dispatch a dev fix / proceed to bugbot+compliance / FAIL / ABORT) is decided by
  `cycle_gate.evaluate()`/`FixBudget`/`RecurrenceTracker` (already existing). When dispatching a
  dev fix, the `must_fix` list's `contract`/`scenario`/`fix` fields are passed through **verbatim**
  (identical to `worker.md`'s *Loop control*).

  **A real bug was caught during implementation**: the call to
  `RecurrenceTracker.record_post_fix()` was initially missed, so even when a MUST_FIX finding
  survived a fix, recurrence judgment (`ESCALATE_ONCE`/`FAIL`) never fired and it kept returning
  `PROCEED` every time — caught by writing and running a regression test
  (`test_review_budget_exhausted_fails`) first.

  **The individual HTTP primitives have been verified live, but this particular combination has
  not.** `POST /tasks`/`GET /tasks/{id}`/`GET /health` were each confirmed with an actual paid run
  (2026-08-18 — a hand-written task was confirmed to spawn a real haiku agent, get committed and
  merged, and have completion correctly reflected in `GET /tasks/{id}`'s `result_summary`).
  However, this driver's own particular combination — one call to `run_pr_cycle()` launching a
  single server and sequentially dispatching multiple roles on top of it — has never been run all
  the way through against a real server, since doing so spends real agent budget and was
  deliberately deferred (README "Next"). Everything above `run_role()` (the loop decisions driven
  by `FixBudget`/`RecurrenceTracker`/`evaluate()`) is pure Python and covered by tests.

  **What is explicitly not yet done**: deterministic recheck routing (`cycle_gate.
  recheck_route()`'s BOUNDED/FULL distinction — needs pre-fix-head/finding-file tracking, which
  this driver does not yet do, so it always does a FULL re-review after every fix), `final_audit`
  (requires gh-pr/gh-review integration first), and the remaining tail up through squash-merge are
  all out of scope for this implementation.

### 3.6 CREDIT-BURN unresolved — the model auto-escalates on retry (confirmed, cannot be blocked by configuration)

There is a fourth path, **completely separate** from `core/routing/*` (§3.1's initial selection,
§3.2's switch-on-exhaustion). And this path is not one but **two independent** ones — implemented
as two different functions in the same file, each with its own escalation logic.

| Path | Entry function | When called | Escalation logic |
|---|---|---|---|
| reap path | `retry_or_fail_task()` (`task_lifecycle.py:834`) | the instant a specific failure is detected, called synchronously | inline in the function body |
| tick-loop path | `maybe_retry_task()` (`task_lifecycle.py:483`) | every orchestrator tick, sweeping all `status=="failed"` tasks | calls `_choose_retry_escalation()` (`task_lifecycle.py:374`) |

`_choose_retry_escalation` is used **only in the tick-loop path** — the reap path,
`retry_or_fail_task`, never calls it at all. Each path's escalation rule:

```python
# reap path (retry_or_fail_task, inline)
# scope==LARGE or role in (architect, security): opus / max
# retry_count >= 1 (2nd or later retry):          opus / high
# otherwise (first retry):                         keep current model / keep current effort

# tick-loop path (_choose_retry_escalation)
_MODEL_LADDER = ["haiku", "sonnet", "opus"]
# terminal_reason == "error_max_turns"/"error_max_budget_usd"/"model_error"/
#   "blocking_limit": 4 special-case branches -- all confirmed unreachable below
# scope==LARGE or role in (architect, security), or deadline exceeded: opus / max
# next_retry == 1: keep the model, raise only effort
# next_retry >= 2: escalate the model up the ladder (sonnet→opus), reset effort to high
```

**Both paths converge on the same rule in practice: from the 2nd retry (= the 3rd attempt)
onward, the model escalates to opus.** The 4 `terminal_reason`-based special-case branches are
dead code — nowhere in the entire package does any code **assign**
`"error_max_turns"`/`"error_max_budget_usd"`/`"model_error"`/`"blocking_limit"` to
`task.terminal_reason` (the string appears only in this `match` statement itself and in comments).
Bernstein's own `core/quality/retrospective.py` comment concedes this too — "`Task.terminal_reason`
is only ever set by a small number of agent self-reported outcomes, and is never populated by
orchestrator-forced terminations such as watchdog/timeout/janitor."

Every actual trigger for a retry, exhaustively, via the reap path (9+2 sites) — regardless of
cause, the instant the same task hits its 2nd retry, it escalates to opus with no exceptions:

| Trigger | Location |
|---|---|
| context compaction retries exhausted | `agent_lifecycle.py` |
| an exception in the compaction pipeline itself | `agent_lifecycle.py` |
| HTTP 413 (payload too large) | `agent_lifecycle.py` |
| gate rejection by guardrails | `agent_lifecycle.py` |
| a fast-fail log pattern detected | `agent_lifecycle.py` |
| suspicious result-free normal exit (exit 0, no artifacts) | `agent_lifecycle.py` |
| agent died with no artifacts | `agent_lifecycle.py` |
| agent died and janitor verification failed | `agent_lifecycle.py` |
| heartbeat timeout | `agent_lifecycle.py` |
| `max_cost_per_agent` exceeded | `orchestrator.py:4041` |
| a retry delegated from manager-queue review | `orchestrator.py:4476` (`_retry_or_fail_task`) |

`_dynamic_retry_limit` adjusts the retry **ceiling** (0/3) based on whether the reason string
contains a `rate limit/timeout/503/429/...` marker, but whether the model **escalates** is decided
purely by `retry_count` and has nothing to do with that marker.

**`role_model_policy` tracing results (correction made).** It was initially judged that, of the
reap path's 9 sites, all 9 in `agent_lifecycle.py` fail to pass `role_model_policy`/
`default_adapter_name` through — re-checking found this was an error. All 9 actually spread a
helper called `_retry_escalation_context(orch)` (`agent_lifecycle.py:50`) via `**`, which passes
along the spawner's live `role_model_policy`/`default_adapter_name` exactly as-is. The 2 sites in
`orchestrator.py` do the same. In other words **the entire reap path (all 9+2 sites) genuinely
does have `role_model_policy` alive.**

However this protection is conditional — if the relevant role has no `role_model_policy` entry,
`adapter_for_role` falls back from `role_policy_entry.get("provider")` (missing) to the spawner's
`default_adapter_name` (= `bernstein.yaml`'s `cli:`, which is `"claude"` for this project). In
other words, **switching some role's adapter to non-Claude without declaring `role_model_policy`
means the reap path has no way to know this and still mis-stamps a Claude tier name onto it** —
exactly the pattern Bernstein's own comments flag as a "run-9 attempt-8" defect.

**Fix applied: `role_model_policy` is now explicitly declared in `bernstein.yaml`**
(`provider` only — `model` is deliberately left blank. Statically pinning a model value here would
create a second source of truth alongside the value `model_config.py` computes on every run,
self-inflicting a CONDUCTOR-COLLAPSE. `provider` alone is enough for the Claude-compatibility
check, and leaving `model` blank means a non-Claude role's retry falls through from
`pinned_model or task.model` to `task.model` — i.e. whatever value model_config actually injected
at that point is preserved):

```yaml
role_model_policy:
  backend: {provider: claude}      # dev
  reviewer: {provider: claude}     # review / recheck
  bugbot: {provider: kilo}
  compliance: {provider: claude}
```

Side effect: this declaration also makes Bernstein's task-creation route
(`core/routes/task_crud.py`) behave as a `role` whitelist — creating a task with a role not on the
list returns HTTP 400. Currently reasona-dev only ever creates these 4 roles
(`plan_compile.py` → `backend`, `pr_cycle.py` → `reviewer`/`bugbot`/`compliance`), and since
auto-spawn features like evolution/watchdog are not turned on either, no other role can appear.
Turning any of those on in the future means the corresponding auto-spawn role must be added here
too, or task creation will be blocked.

**The tick-loop path (`maybe_retry_task`) is still unconditionally exposed** — this function's
signature has no `role_model_policy` parameter at all. Nothing declared in `bernstein.yaml` affects
this path.

**Every defensible line was checked.**

| Defense attempted | Result |
|---|---|
| `role_model_policy` (bernstein.yaml) | genuinely effective on the reap path (9+2 sites) — **declared (see above)**. Still ineffective on the tick-loop path (`maybe_retry_task`), which has no such parameter at all |
| `terminal_reason == "model_error"` special case (keeps the model) | no code anywhere **in the entire package** actually assigns this string to `task.terminal_reason` — an effectively unreachable branch. So even genuine quota/auth exhaustion never hits this special case and falls through to the ordinary escalation path |
| lowering `max_retries` to 1 to avoid the escalation branch (2nd retry+) entirely | **there is no configuration surface for this.** Neither `plan_schema.py` (the step schema) nor `seed_config.py` (the bernstein.yaml schema) has a `max_retries` field at all — `Task.max_retries=3` is a pure code default and cannot be overridden declaratively |
| `metadata["pinned_model"]` (pinning directly via task metadata) | this really is the kill switch, but the `plan.yaml` step schema has no `metadata:` field at all, so it cannot be expressed through plan.yaml |

**Conclusion: the reap path is now blocked via `role_model_policy`, but the tick-loop path still
has no declarative way to block it.** This is different from the cascade_router dead code,
model_fallback's unconsumed setting, and the bandit's default-off state — those turned out, on
inspection, to "never have been involved in the first place," while this one turned out to be
"genuinely involved, and only half-blocked" — a **partially resolved** state.

**Fix applied: `on_agent_spawned` post-hoc monitoring.** `on_pre_task_create` was originally the
candidate, but investigation confirmed this hook cannot do it — its signature
`(task_id, role, title, description)` does not carry `model` at all. At the point where
`task_crud.py` calls `pm.fire_pre_task_create()`, `effective_body.model` (the value the retry path
has already computed) exists as a local variable but is not passed as a hook argument. Instead,
`on_agent_spawned(session_id, role, model)` (`hookspecs.py:174`, confirmed actually called from
`spawner_core.py:4648`) is the only hook that receives the actual `model` at spawn time, so it was
implemented there — `reasona_dev/plugin.py`'s `ReasonaGatePlugin.on_agent_spawned` compares the
spawned `model` against `model_config.resolve_all()`'s expected value, and if they differ, records
it to `.reasona/model_divergence.jsonl` and logs a `logging.warning`. This is non-blocking (the
agent has already spawned by this point, so it cannot be prevented), but it preserves the "does not
pass silently" half of CREDIT-BURN.

**Candidate next steps (not yet started):**

1. Propose to Bernstein upstream that the `plan.yaml` step schema expose `metadata.pinned_model`
   (or expose a step-level `max_retries`, or a patch that also passes `role_model_policy` into
   `maybe_retry_task`) — the same pattern as the earlier `provider_availability` case: contribute a
   minimal change rather than forking.
2. Measure (needs production data) how often a 2nd-or-later retry actually happens in practice, and
   how many of those go through the tick-loop path rather than the reap path — if the tick-loop
   path is rarely hit, `on_agent_spawned` monitoring alone may be sufficient; if it is frequent,
   item 1 becomes more urgent.

## 3.7 Reshaping the quality budget — reflecting dev-ralf's measured analysis

A zero-base analysis of dev-ralf's 3.5 months of production operation (329,721 lines of Rust,
292 planned PR units) concluded that the architecture is sound but the quality budget's allocation
is off. The evidence is a 30% follow-up-correction plan rate and a 27% cumulative deletion rate,
both observed under a per-PR budget of 8 review cycles + 8 scan cycles with 5 role types attached —
meaning the marginal return on adding yet another reviewer was already near zero.

Since reasona-dev inherited this same budget shape (`MAX_REVIEW_CYCLES=8`/`MAX_SCAN_CYCLES=8`/
`MAX_TOTAL_FIX_CYCLES=16`) from dev-ralf, the diagnosis carries over too. But with no production
history of its own yet, the numbers themselves do not carry over — what carries over is the
structural cause behind them.

### 3.7.1 Decomposing budget inefficiency into three axes

The ceiling numbers themselves are not the cost. The actual spend happens along three axes.

| Axis | Owning module | Status |
|---|---|---|
| cost per cycle | `cycle_gate.recheck_route()` | already implemented but not yet wired in — now wired into `pr_cycle` |
| number of cycles | `cycle_gate.ConvergenceTracker` | new |
| which rule terminated it | `cycles_log` | new |

**The number-of-cycles axis was the unresolved point.** `RecurrenceTracker`'s termination
condition only fires when the same finding key survives a fix. A PR where a different set of
MUST_FIX findings shows up every cycle keeps getting `PROCEED` from `recurrence.decide()`, only
FAILing after burning through the entire 8-cycle stage ceiling. `recheck_route()` only lowers
cost per cycle and does not touch this axis at all.

`ConvergenceTracker` judges not the sameness of findings but the **downward trend in their count**.
Where dev-ralf's escalation trigger was `cross_reviewer_convergence` (agreement across reviewers),
this is its time-axis dual — it looks at agreement across cycles rather than agreement across
reviewers. Where the two rules overlap on the same key, the check order was arranged so that the
more specific recurrence-side reason is the one recorded.

### 3.7.2 Three mechanisms adopted, then withdrawn — the structure gate, the plan-size cap, and the approval gate

These were derived from the original analysis, implemented, and then removed. The reasons for
removal are recorded here so that anyone re-reading the same analysis and reaching the same
conclusion does not have to retrace the same steps back out.

**The structure gate.** It deterministically judged file size, single-PR growth, cross-file
duplication, dependency direction, and public-API growth. **The judgment itself is real** — a
reviewer reading a diff cannot, in principle, see an 11,288-line file growing by 200 lines at a
time. The reason for removal lies elsewhere.

The checks differ in how well they fit as hard gates. A refactor that splits a file up improves
`max_file_lines` while violating `max_added_lines_per_file`. But a waiver goes into `reasona.yaml`
per `(check, path)` and is **permanent and repo-scoped**, whereas the exemption a refactor needs is
**temporary and PR-scoped**. Editing the repo's settings and then reverting them for a single
refactor is the wrong tool, and in practice waivers pile up reflexively — exactly the failure this
gate was meant to prevent.

Reviving it would need a unit-scoped, plan-recorded waiver and `type: refactor` awareness, and
`structure_gate` had no concept of a PR unit at all (`ship_gate` calls it against the whole
repository).

**The plan-size cap (5 units).** Its basis was a correlation where the two largest plans on record
caused a second round of correction — and it was **N=2**. More decisively, the claimed mechanism
is not fixed by splitting — if the problem is "PR 1's learnings can't reach PR 12's spec," writing
plan B before executing plan A has exactly the same issue, since nothing enforced sequential
authoring. In exchange, dependency relationships get scattered across multiple documents and the
DAG fragments. It was a trade that worsened the real structure in exchange for a proxy metric. The
actual fix — in-plan revision — is blocked by Bernstein's up-front declarative stage DAG (§3.7.4).

**The first-unit approval gate.** Its intent was "a human approves the contract shape the first PR
sets," but all three parts of the implementation were off.

- `approval_required` was passed only into the `_run_dev_fix` path, so it only ever fired **when
  review found a MUST_FIX.** If the first PR passed cleanly, approval never happened at all — a
  structure that claims to approve the contract but only calls in a human when it failed.
- There was no production call site for the `on_awaiting_approval` callback. Once a task enters
  `pending_approval`, the driver silently polls for up to 24 hours and the human has no idea they
  were even supposed to be called.
- Bernstein's `PENDING_APPROVAL` means "complete, but not yet in effect," so what is being approved
  is the effect of a dev-fix task, not a PR merge.

The one thing that actually acted as a human gate was simply the fact that `--merge` defaults to
off — and that is a default, not an approval.

**What remains.** `poll_task`'s handling of `pending_approval` is kept. Since we never set
`approval_required`, our own tasks never enter that state, but this is a defense against a state
machine we do not own, and it is covered by tests — it is a guard against external state, not a
defunct feature of our own.

### 3.7.3 Executable acceptance criteria — `acceptance.py`

plan-format already requires "Tests (positive + negative)" on every PR, but only as **prose**. A
reviewer confirms the item exists, not that it runs. The observed consequence is an
INCOMPLETE-MERGE case (a merge landing with the named test symbol never actually written) — added
review does not catch this, because every reviewer reads the same diff, and an absence does not
show up in a diff.

**Run this from the driver, not from Bernstein's `completion_signals`.** Two facts were confirmed
from the installed 3.15.1 source, and together they rule out that placement.

1. Signals are evaluated against `orch._workdir` (a single fixed project root) —
   `task_lifecycle.py:3916`'s `executor.submit(verify_task_completion, task, orch._workdir)`. Not
   the per-task worktree the agent actually worked in.
2. **That evaluation happens before the agent's branch is merged.** The janitor future is resolved
   in `task_lifecycle.py:4055`'s `_resolve_janitor_result()`, and the merge happens afterward,
   inside `_reap_and_cleanup_session()` at :3061's `orch._spawner.reap_completed_agent()`.
   Moreover, task completion after the merge is itself conditioned on the janitor having passed
   (:3076's `if janitor_passed and not skip_merge and merge_ok`).

So at the moment a `test_passes` command runs, the PR's code is not, with certainty, present in the
tree that command targets — it still exists only on the `agent/<id>` branch. Placing AC there is
not merely risky; it either always fails, or vacuously passes by checking pre-existing code. This
is the same structure `gate_check.py` uses to get around the same constraint, by reading a file the
driver wrote at the root.

Side note confirmed: `skip_merge` is decided by `_evaluate_approval_gate()`, so approval mode
affects whether the merge happens but does not change this ordering itself.

`expect` is restricted to exactly three values: `exit0`/`exit_nonzero`/`stdout_matches`. Without
`exit_nonzero` there is no way to express "this input must be rejected," and a negative test
silently degenerates into just another positive test. A timeout is treated as a failure, not as
undetermined — leaving it undetermined would read a hung command as "cannot judge, proceed," which
is precisely the silent pass this gate exists to stop.

Partial passes are not defined. "7 of 9 passed" reintroduces exactly the judgment call this gate
was built to remove.

**Explicitly out of scope**: whether a criterion is *correct* is not verified. A badly defined AC
deterministically approves a bad state. That layer belongs to plan authoring and its multi-reviewer
convergence. Blurring that boundary turns AC back into prose.

**Unlike dev-ralf's `make ci`/language-specific build+test gate, `acceptance:` is opt-in per plan
unit, not unconditional.** dev-ralf's `/gh-pr` §4 runs `make ci` (or `cargo test`, or the
repo-appropriate equivalent) automatically for any source-touching change, with no way for a plan
author to omit it. reasona-dev has no such automatic step anywhere in its pipeline — not in
cycle-0, not in review/scan, not in `gh_pr.py` (deliberately not ported, §3.12: re-running a
build/test gate there would duplicate what `acceptance:` already covers, IF the plan declared it),
not in `gh_review.py` (which only watches the target repo's own external CI, §3.13). `ship_gate`'s
own acceptance axis passes WITH A WARNING, not a failure, when a unit declares no criteria at all
(`ship_gate._acceptance_outcome()`) — so a plan that never writes an `acceptance:` block gets zero
build/test verification anywhere in this pipeline, silently.

**Consequence: plan authors must declare a build/test acceptance criterion for every source-touching
PR unit to get behavior equivalent to dev-ralf's unconditional gate.** This is a requirement on the
plan document itself, written by whoever authors it — reasona-dev has no plan-generation step of
its own to enforce this at (plans are consumed as already-written input, from a human or from the
separate `reasona-plan`/`plan-ralf`-successor repo; see `README.md`'s own note on that boundary).
Concretely, every PR unit whose `files:` touch source should include something in the shape:

    acceptance:
      - id: AC-<index>-1
        cmd: "cargo test -p mycrate"        # or `make ci`, `pytest`, etc. -- whatever this repo's own build/test entry point is
        expect: exit0

Omitting it is not an error reasona-dev currently surfaces loudly (a warning, not a block) --
readers of a plan should treat a source-touching unit with no `acceptance:` block as under-specified
relative to what dev-ralf would have guaranteed for the same change.

### 3.7.4 The manifest parser and the plan-size cap

`parse_plan_units()` had only its prose-fallback path implemented, even though plan-format
specifies the manifest as authoritative. `acceptance:` is structured data with no home in the
prose fallback, so introducing a manifest parser was a precondition for AC — this was original
design rather than a migration, which made it cheaper than it might otherwise have been.

The plan-size cap was introduced and then removed (§3.7.2).

**In-plan revision (the latter half of proposal 2) was not adopted.** Bernstein's plan.yaml
declares its entire stage DAG up front, with no surface for adding or editing a stage mid-run.
dev-ralf, being its own scheduler, could recompute the ready-set every wave, but handing that
scheduling over to Bernstein was this project's design decision (§3.5.3). This is the price of
moving to that substrate, and the alternative (compiling and running each PR unit separately, in
sequence) gives up some of the parallel-DAG benefit. This will be judged once §3.7.6's measurements
are in.

**The runtime feedback loop (the source analysis's other deferred proposal) was also examined and
not built.** It is product-specific — what "the runtime told us this was wrong" means depends on
the target product's own observability, which this project cannot assume. Its general form already
exists here in a substrate-agnostic shape: post-merge acceptance (§3.7.3's `acceptance.py`, run as
a pre-merge gate today, extendable to a scheduled post-merge check) is the same idea — verify a
claim by running it — without requiring a product-specific feedback channel.

### 3.7.5 `poll_task`'s handling of the approval wait

The approval gate itself was removed (§3.7.2), but the polling fix that surfaced alongside it while
implementing it is kept. `_TERMINAL_STATUSES` did not include `pending_approval` and there was no
separate handling either, so a task entering that state was polled all the way to the ordinary
timeout and then failed **as if the agent had hung.** A human's response time and an agent's work
time are different timescales, so they are split into a separate deadline with a one-time
notification. This is a defense against a state machine we do not own, and it is pinned by tests.

### 3.7.6 Measurement — `cycles_log.py`

The original analysis ranked attribution measurement 5th in priority; for reasona-dev it is 1st.
dev-ralf has to retrofit measurement after the fact, but reasona-dev has zero execution history, so
this is the one and only moment measurement can be built into the design from the start. The
original analysis itself noted in its final reservation that "running proposal 5 first could
replace this analysis's own conclusions with actual measurement."

One row per role dispatch and one row per gate decision are appended to `.reasona/cycles.jsonl`.
The join key is `Finding.key()`, which excludes the line number, so a fix that shifts line numbers
does not cause the same finding to be mistaken for a new one. Since instrumentation must never be
able to fail a PR cycle, it swallows every exception — a missed measurement is a cost, but a cycle
aborted by its own logger is a defect.

Once this record is populated, the following table becomes computable, and only then does a real
basis exist for deciding which role to cut.

| Category | Judgment |
|---|---|
| caught by both gate and AC | that role's marginal return is low |
| caught by gate only | review's unique value — keep it |
| caught by AC only | a review blind spot — expand AC |
| caught by neither | a post-merge defect — a gap in AC's design itself |

### 3.7.7 Memory — a generated artifact, not authored content

`.reasona/memory/*.md` is **generated** from `cycles.jsonl`. The constraint that it must never be
hand-written is itself the design. The memory directory is the same kind of surface as skill
documentation, and skill documentation bloats not because of its format but because adding an entry
is easy while nobody is ever responsible for removing one — dev-ralf's own `SKILL.md` reached 472
lines, much of it explaining why superseded revisions were wrong, all of it loaded into every
agent's context on every run. Moving that habit into `memory/` would just reproduce it.

Generation gives three properties for free, with no discipline required — drift is impossible
because it is computed from what actually happened, patterns that have stopped recurring
automatically disappear because only the most recent `window_units` units are read, and a
recurrence threshold plus an injection cap bound its size.

Clustering uses exact matching only (the same `(path, symbol)`, or the same normalized contract
text, across different PR units). Paraphrases are deliberately not clustered — memory shapes what
the next reviewer sees, so a wrong cluster actively misdirects attention. The cost of missing a
pattern is smaller than the cost of manufacturing a false one.

Retrieval is scoped to the intersection of a PR unit's already-declared `files:` and a memory
entry's `scope_files:`. Both search keys already exist on either side, so there is no added cost.
An unrelated PR sees no change to its prompt.

Anything the program can enforce is not put into memory. That belongs to `structure_gate` or an
acceptance criterion; what goes into memory is a deliberate choice to remind the model of something
the pipeline itself can guarantee.

### 3.7.8 The composite gate — `ship_gate.py`

Having each gate built separately with its own CLI attached is exactly the state the analysis this
work started from was pointing at. The checks are merely *available*; running them still depends
on operator discipline. "The reviewer asserts completeness" and "the operator remembers to run the
completeness checks" are the same defect wearing a different actor. A gate that must be remembered
is not a gate.

`ship_gate.evaluate()` is the single point that decides a PR unit's merge/no-merge outcome, and the
judgment is a logical AND.

| Axis | Source | What it measures |
|---|---|---|
| review | `pr_cycle`'s `CycleResult.verdict` | whether the cycle converged |
| acceptance | `acceptance.py` | execution results of the claims the plan declared |
| structure | `structure_gate.py` | structural violations |

**No weighting and no override path.** A composite gate invites the idea that an excellent result
on one axis excuses a shortfall on another ("the review was thorough, so a missing test can be a
follow-up"). The three axes measure different things and none substitutes for another — review
cannot execute a test, a test cannot see a 10,000-line file, and a line count cannot judge whether
a contract holds.

All three checks run even if one fails. All three are cheap relative to a review cycle, and this is
what separates an author fixing things one round at a time from fixing everything in one pass — the
same reasoning as `acceptance.run_all()` not stopping at the first failure.

A call that does not pass a `cycle_verdict` (e.g. from CI) **explicitly reports the review axis as
skipped.** Silently treating "made no claim" as "passed" is exactly how a gate loses its meaning.

**It does not perform the merge.** It only returns a verdict. The merge tail
(`sync-main → /gh-pr → /gh-review → up-to-date gate → final_audit → squash-merge`) is not yet built
— and because verdict and action are kept separate, this can be used unchanged from a CI step, a
pre-merge hook, or a driver call.

### 3.7.9 Querying — `cycles_query.py`

`cycles_log` only records. Without querying, the record is inert, and every question it was built
to answer stays answerable only as opinion. This project's deferred decisions explicitly condition
themselves on "once measurement results are in" — which of review/bugbot/compliance to cut, when
an undeclared AC should be promoted to a rejection, how far the 8/8/16 ceilings sit from the
observed distribution — and a log with no query resolves none of them. In other words the deferral
becomes permanent **by structure**, not by evidence. This module removes that structural
permanence.

Every query is a count, a group-by, or a set operation, and none of it estimates. The output is a
table for a human to judge, not a recommendation.

```
role attribution (exact)
  role          first  dup  uniq  total
  reviewer         4    1     3      4
  bugbot           1    0     1      1
  compliance       0    1     0      1
```

**`unique` matters more than `first_catch`.** first-catch is decided by append order — i.e. the
driver's actual dispatch order — so a role that runs later within the same cycle is structurally
disadvantaged (in scan, bugbot dispatches before compliance). A role with high `duplicate` and
`unique` near zero is a drop candidate, and the table supports that conclusion directly, with no
interpretation needed.

```
acceptance coverage: 2/3 units declare criteria (67%), 1 passed, 1 failed
gate vs acceptance (units with declared criteria only)
  gate_only=1  acceptance_only=1  both=0  neither=0
```

`gate_vs_acceptance()` produces the four-way split above. It counts only units that **declare** an AC —
a unit that does not declare one cannot testify to "what would AC have caught," and counting it as
"AC caught nothing" would misread plan coverage as AC's value.

**The one approximation is isolated and labeled.** `effective_findings()` uses "did a later commit
touch the same file" as a proxy for effectiveness. The original analysis measured this same proxy
at 84% at this exact point, found the control-group base rate to be 77%, and wrote "excluded from
the judgment basis." It is disabled by default (`--effective`), reported in a separate section, and
never mixed with the exact counts.

### 3.7.10 Aligning the prompt layer and per-unit profiles

Prompts were the only thing in this project still on three layers (project → global → package).
`config_file.py`'s `reasona.yaml`, and `bernstein_config.py`'s template, are both global+project —
two layers, nothing beneath. The package layer was removed to bring prompts into line.

Consistency alone is not the reason for removal. The copy inside site-packages is a layer the
**operator can neither edit, nor see in the repository, nor know is even the one answering.** A
repository that believes it has customized its review prompts may, for a role file it forgot to
add, be silently reviewed against the package's copy instead. If neither layer exists, it returns
`None` and the cycle ABORTs — a consistent extension of the same rejection already applied to a
profile name that does not exist.

Priority is resolved **per file.** If a project overrides only `review.md`, `bugbot.md` still uses
the global one. It is not a wholesale swap of the entire profile directory.

This repository commits its own `.reasona/prompts/generic/`. It is the file this repository
actually uses, while also serving as a checked-in example of what an operator would copy to
`~/.reasona/prompts/generic/` to build the global layer — the same structure as
`.reasona/bernstein-template.yaml`.

**Per-unit profiles.** Once a single repository mixes language modules, a single repo-wide
`dev-profile:` cannot express it. A Rust crate and a Python service need different review policies,
and reviewing a Python service against a Rust-aware prompt is worse than having no profile at all —
it produces confident findings drawn from the wrong rulebook.

So a profile is resolved per PR unit, keyed on the `files:` that unit already declares in the
manifest (the same key `memory.select()` uses, so there is no added cost).

| Priority | Source |
|---|---|
| 1 | the pr_unit's own `profile:` — the author's explicit statement |
| 2 | a `dev-profile-map:` glob match — declared once per repo |
| 3 | `dev-profile:` — the repo's default |
| 4 | `"generic"` |

A file that matches nothing in the mapping is **ignored**, not counted toward the default profile.
A PR that touches both `crates/x/lib.rs` and `README.md` is a Rust PR; counting the README toward
generic would cause a conflict on every documentation change.

**If a unit's files map to two profiles, it is rejected rather than resolved.** Picking the most
specific glob, or a majority vote, would also be deterministic, but it would **silently** paper
over the fact that a unit spanning two languages gets reviewed against only one language's policy
while the other half is checked against no rulebook at all. This is the same class of fallback
failure this module already rejects for a nonexistent profile name. The author either states
`profile:` explicitly or splits the unit — and splitting is a direction the 5-unit cap already
nudges toward anyway.

Validation happens at compile time, inside `compile_to_bernstein_plan()`. A plan defect should
surface while the author still has the plan open, not an hour into execution when `pr_cycle`
happens to reach that stage.

### 3.7.11 Plan orchestration — `orchestrate.py`

`plan_compile` knows units, `files:`, `depends_on:`, and profiles; `pr_cycle` reviews one unit
under one profile; `ship_gate` decides one unit's merge/no-merge. Nothing connected these three, so
per-unit profile computation had no caller, and executing a plan meant an operator invoking all
three modules by hand per unit, hand-deriving the arguments each time — one layer up from the
"available but must be remembered" state `ship_gate` removed.

**The dev stage IS here now, per unit — this reverses an earlier design decision.** An earlier
version of this section said the opposite ("the dev stage is not here... owning it here would mean
reimplementing a DAG scheduler"), when `cli.py` compiled every unit's cycle-0 into one multi-stage
Bernstein plan.yaml and dispatched it as a single `bernstein run`, entirely before this module ran.
That turned out to be the wrong call: Bernstein's own merge-back landed every unit's commits on the
SAME shared `workdir` checkout before any unit could have its own isolated branch, which is exactly
backwards for opening a per-unit PR (§3.11 has the full account). Fixed by moving cycle-0 dispatch
in here, per unit, into that unit's own fresh worktree, immediately before that unit's own
review/scan — no DAG scheduler is being reimplemented, since this module's own sequential loop
(already enforcing `depends_on` via `_blocking_dependency`) is sufficient once cycle-0 no longer
needs Bernstein's stage-DAG to sequence units at all.

**On a dependency failure, downstream units are skipped, not attempted.** Since the precondition is
an unmerged contract, a review of that unit would target a shape that does not exist, and any
finding produced would just be noise the author has to re-triage after the upstream is fixed.
`skipped` is a distinct outcome from `failed` — reporting "1 unit broken, 4 not run" as "5 failed"
misstates what happened.

A dependency index not present in this plan is **ignored.** Depending on a unit already merged from
a previous plan is legitimate, and rejecting it would make the plan-splitting the 5-unit cap
encourages impossible in the first place. A cycle is fatal — there is no satisfying order, and
falling back to declared order would silently review at least one unit against an unbuilt
dependency.

**Approval applies only to the first unit.** `pr_cycle` left this decision to its caller because it
only ever sees one unit at a time (§3.7.5), and this layer, which sees the whole plan, is that
caller.

**One server per plan.** Spinning up a fresh server/orchestrator for every unit is the same kind of
arbitrary cost as doing it per role (§3.5.4). A `server=` argument was added to `run_pr_cycle` to
reuse an external handle, but **a server this layer did not start is not stopped by it either.**

**Profile conflicts surface before execution.** Because every unit's profile is resolved up front,
a plan with a unit spanning two languages is rejected before the first agent is even spawned — not
after four units have already merged. Conflicting units are reported **all at once**, not just the
first one found.

### 3.7.11.1 `blocked` is a distinct outcome from `failed`

`UnitOutcome.status` used to be one of `shipped` / `failed` / `skipped` only. Every non-passing
final-phase outcome (`gh` unavailable, a sync conflict whose fix budget ran out, `final_audit`
failing, a ship-gate fix budget exhausted, the final phase not settling within
`MAX_FINAL_PHASE_ROUNDS`) collapsed into `failed` — the same label as "review/scan actually
evaluated this code and it does not meet the bar." That conflated two operationally different
situations: a `failed` unit's code was judged and found wanting, so the right response is editing
the plan or the code; a unit stopped by, say, `gh` not being authenticated was never judged at all,
so the right response is fixing the environment and re-running the exact same command.

**Fix: `status` gained a fourth value, `blocked`.** `pr_cycle.py`'s review/scan loop already
produces two different terminal verdicts from `cycle_gate.evaluate()`'s own `action` field — `FAIL`
(the `"fail"` action: budget exhausted against real MUST_FIX findings, `RecurrenceTracker`'s
stop-the-world, or non-convergence) and `ABORT` (the `"abort"` action: `ERROR` — role/model
unavailable — or an INCONCLUSIVE role's retry budget exhausted, which `cycle_gate.evaluate()`'s own
comment already called "an environment problem, not a code one"). `orchestrate.py` used to map both
to `verdict="FAIL"` and both to `status="failed"`; it now preserves `ABORT` as its own
`CycleResult.verdict` and maps it to `status="blocked"`. Everything final_phase.py itself reports as
`BLOCKED` (§3.9) is mapped to `status="blocked"` the same way, replacing the old blanket `"failed"`.

Nothing about `_blocking_dependency`'s own logic needed to change — it already treats any
`status != "shipped"` as blocking a dependent, so `blocked` and `failed` behave identically there,
and the ledger stores whichever string is passed without validating it, so a `blocked` unit is
retried on the next run exactly like a `failed` one.

## 3.8 Live end-to-end verification (2026-08-18) — 11 defects

The full path `compile-plan → bernstein run (dev cycle-0) → run-plan
(review → scan → ship)` was run against a real repository, at real cost. The final result:

```
plan run: 1 shipped, 0 failed, 0 skipped
  [shipped] pr-1 (generic): review + acceptance + structure all clean

  dispatch review c1 reviewer    gate=PASS
  dispatch scan   c1 bugbot      gate=PASS
  dispatch scan   c1 compliance  gate=FIX_REQUIRED  mf=1
  decision   spawn_fix           1 MUST_FIX finding(s)
  dispatch scan   c2 bugbot      gate=PASS
  dispatch scan   c2 compliance  gate=PASS
  decision   pass
  acceptance declared=True passed=True ['AC-1-1']
  ship       passed=True  gates={'review': True, 'acceptance': True, 'structure': True}
```

The whole designed path was confirmed live — compliance catching a MUST_FIX → `cycle_gate`
deciding `spawn_fix` → dev fixing it → the finding disappearing on rescan → AC execution → the
triple-gate composite verdict.

**11 defects were fixed along the way. 10 were this project's own, and all of them were the kind
no unit test could ever have caught** — contract mismatches, process lifetimes, path resolution.
They only surface when a real agent, in a real worktree, produces real output.

### 3.8.1 Bernstein's three execution modes, and a mode-selection mistake

The single largest error produced three separate defects from one root cause. Bernstein offers
three execution modes, and only one of them is a resident daemon.

| Mode | Invocation | Lifetime |
|---|---|---|
| batch | `bernstein run <plan>` | spawn → execute → merge → **exit** |
| batch engine's claim loop | `python -m ...orchestration.orchestrator` | **self-stops** when the queue empties (by design) |
| cluster-resident | `bernstein serve` + `bernstein worker` | **blocks until SIGINT/SIGTERM** |

`bernstein_server.start_server()` originally used `bernstein start`, which is a **bootstrap** that
decomposes and executes the seed's `goal:` — not a server that accepts external POSTs. The observed
symptoms matched exactly — the server came up and `POST /tasks` returned 201, but `/health`
reported `spawner: {pid: null}`, and every dispatch sat queued until the poll timeout.

Switching to launching the orchestrator module directly got tasks running, but exposed the next
layer of the same problem — `Quiescence confirmed after 2.0s settle window - self-stopping`. The
moment the review stage's queue emptied, it self-terminated, leaving nothing to claim the scan-stage
tasks queued afterward. This is not a Bernstein limitation but the result of **misusing the batch
engine as a daemon.**

`bernstein worker` is the mode actually designed for this shape (`worker_cmd.py:636` — "Main worker
loop. Blocks until SIGINT/SIGTERM"). After switching to it, the pipeline ran to completion.

**A side effect: this also opens up a remote-execution path.** `worker` accepts `--server URL
--token`, so the executor no longer has to live on the same machine that submits tasks. A central
node binds every interface with `BERNSTEIN_BIND_HOST=0.0.0.0` + `BERNSTEIN_CLUSTER_ENABLED=1`, and
`serve` is documented as a container's PID-1 resident node. Had the batch mode been kept, this path
would have been foreclosed entirely.

### 3.8.2 Contract mismatches — the prompt and the parser assumed different shapes

Both cases had the parser fail to handle a shape the prompt itself instructs. Neither raised an
exception, so neither was visible statically.

**ADVISORY's `-- description` tail.** `review.md` instructs
`- [MEDIUM|LOW] path[:line] [symbol] -- <description>`, but `_ITEM_RE` had no slot for that tail,
so every advisory written exactly as instructed was dropped entirely. Since advisories do not
affect the gate (PASS vs PASS_WITH_NOTES), nothing failed loudly — it simply never reached
`cycles.jsonl` or memory, which is a worse outcome for a measurement-based system.

**bugbot/compliance's wire shape.** `pr_cycle` selected its parser by role name
(`_KV_ROLES = {bugbot, compliance}` → KV parser). This assumed dev-ralf's Rust-monorepo profile,
which delegates those roles to an external skill, but this project's packaged `generic` profile
requires all three roles to use the same `||` text contract. As a result, perfectly well-formed
text output was judged "no BLOCKING_JSON → ERROR," and **the entire scan stage aborted.**

Correction: **the wire shape is a property of the prompt, not the role.**
`finding_adapter.parse_role_output()` now judges by the presence of a literal marker
(`BLOCKING_JSON=` / `=== <skill> RESULT ===`). If the marker is present, it is KV; if not, text.
Since this is an exact match rather than an inference, a body with the marker present but broken KV
still correctly fails as KV parsing (ERROR) — preserving worker.md's "missing block → cycle FAIL."

### 3.8.3 Path resolution — anything not absolute gets written into the worktree

Because `--workdir .` was relative, `raw_output_path` also ended up relative, and the agent
resolved it **against its own worktree** and wrote the file there. The agent's own log shows this
exactly.

```
[Write] .sdd/worktrees/reviewer-<id>/.reasona/runs/pr-1/reviewer-c1.raw.txt
[RESULT] subtype=error_max_turns cost=$0.2035 turns=23
```

The driver looks in the project root, so it finds nothing. The agent burned its remaining turns
searching for the file and died with `error_max_turns`. `run_role()` now absolutizes `rundir` via
`.resolve()`.

### 3.8.4 Seed placement — the symlink is required, but must never be tracked

The symlink design in §3.5.3 was only half right. The link itself is required and works (re-
confirmed that without a root file, the orchestrator dies with `FATAL: no adapter configured`).
But **if it is committed**, git materializes it into every agent worktree, and Bernstein's
isolation check rejects it.

```
Worktree isolation violation: Symlink 'bernstein.yaml' points into
parent repo mutable state
Cannot create workspace for agent backend-<id>
```

Zero agents again. Bernstein's own worktree exclusion list already listing `/bernstein.yaml`
alongside `/.sdd/`·`/.env`·`/CLAUDE.md` confirms the intent — the root file is not meant to enter a
worktree at all.

So `ensure_bernstein_yaml()` now creates the link and adds `bernstein.yaml` to the target
repository's `.gitignore`. Being untracked means it does not exist in a fresh clone —
**the link is guaranteed on every call**, and it no longer early-returns just because
`.bernstein/` already exists. An early return there means a freshly cloned repository is
guaranteed to hit a FATAL on its first run.

### 3.8.5 Removing the dev step's completion_signal

The generated dev step was carrying `gate_check .reasona/review-<stage>.json` as its signal.
Review runs **after** it in `pr_cycle`, so the file does not exist yet, and the janitor exits
non-zero, so **every first attempt failed.** That failure entered Bernstein's retry path, respawned
the agent, and escalated its model on the 2nd attempt — a configuration that deterministically
triggered the credit burn §3.6 covers, on every single PR unit.

Moreover a signal cannot even hold for the dev step's own output here. The signal is evaluated
against `orch._workdir` (a fixed project root) and **before** the merge (§3.7.3). The code under
test is not in the tree the command runs against.

The conclusion is that gating belongs entirely to `ship_gate`, and a signal-free dev step is
auto-completed by Bernstein via the agent's git commits — that is the honest amount of verification
available at that point.

### 3.8.6 An upstream defect — `AgentLogSummary` serialization

The one item that cannot be fixed on our side. When an agent finishes and exits,
`handle_orphaned_task` builds the completion payload from `collect_completion_data()`'s result, and
the `AgentLogSummary` inside it is not JSON-serializable, so `POST /tasks/{id}/complete` dies with
an exception.

```
TypeError: Object of type AgentLogSummary is not JSON serializable
```

The task gets permanently stuck at `claimed`. Confirmed live — a review agent wrote its full report
normally and exited, while the dispatch side waited out the entire 30-minute timeout.

The defense is to **treat the output artifact, not task status, as the completion signal.** This is
not a workaround bolted on afterward but the original contract — file handoff was already adopted
precisely because `result_summary` does not carry the agent's report (§3.5.4), so the file's
appearance was already the definition of "the role is done." Status is a secondary signal, and
when the two disagree, whichever side has an actual artifact wins. Two consecutive polls with an
unchanged file size are treated as recording-complete.

### 3.8.7 The remaining three

- **`final_audit` missing from the role whitelist.** The `role_model_policy` block also acts as
  the task server's role whitelist (measured: a role not on the list gets a 400). This was a
  latent defect that would have failed the instant the merge tail dispatched `final_audit`, so it
  was declared in the template.
- **`gate_check`'s traceback.** When the file is missing, it now returns a diagnostic containing
  the path plus exit code 1, instead of a traceback. "Nobody ever wrote a verdict" and "the verdict
  was FAIL" need to be distinguishable.
- **An invalid `approval:` key.** The seed parser had been silently ignoring it
  (`Ignoring unknown top-level key 'approval'`). Bernstein's `approvals:` is a tool-call approval
  gate, an unrelated concept. The actual mechanism for merge approval is
  `TaskCreate.approval_required`, which `orchestrate` sets on a plan's first unit.

## 3.9 The final phase — `final_phase.py`

Renamed from `merge_tail.py`: that name described only the last step (a squash-merge), but the
module now also owns the sync-conflict fix loop, the conditional final audit, and ship_gate's
verdict itself (§3.9.4) — a squash-merge is just where it ends, not what it is.

Implements the final third of `worker.md`, restructured: `sync → final_audit → ship_gate` now runs
as one self-verifying loop (§3.9.4), followed by `gh-pr → squash-merge`. `ship_gate`'s verdict used
to be computed by `orchestrate.py` before the tail ran at all; it moved inside the tail, and behind
sync and the audit, for the reason in §3.9.4.

**Merging is opt-in.** `merge=False` is the default, stopping at PR creation. A squash-merge is a
hard-to-reverse external action that rewrites the real repository's default branch, so the caller
must ask for it explicitly, and it must never be something discovered only after the fact. The
earlier steps (sync, audit, message construction, PR creation) are safe to run repeatedly.

**What still fails with a name rather than being retried.** `gh` missing, `gh` not authenticated, a
non-conflict sync failure (fetch failed, nothing to point dev at), a rejected squash title, a PR
that has fallen behind base at the final pre-merge check: each returns a `blocked` naming its exact
condition, immediately, because none of these are something dev editing files can fix. A conflict
is the one sync failure that IS handled by a fix loop instead (§3.9.2) — everything else here still
degrades to "blocked," never "merged anyway" or "silently skipped," because a merge tail that
occasionally does nothing is worse than one that refuses outright.

### 3.9.1 Why `final_audit` is conditional

A unit that passed both review and scan on its first cycle is one that **three independent roles
already read and found nothing in.** A full-PR audit at that point would largely just re-derive
the same result.

Where an audit earns its cost is when **fixes have accumulated.** Each fix is a change no reviewer
has ever seen in its final combined form, and interaction between fixes is exactly what per-cycle
review structurally cannot see. So the trigger is `budget.total_used > 0`.

The audit runs in the `"final"` phase of the **same `FixBudget`** review and scan already consumed
(`MAX_FINAL_CYCLES` = 3). Giving it a separate budget would let a PR spend 8+8+3+3 cycles (review +
scan + final + sync, §3.9.2) while every stage still reports itself within its own ceiling, and
giving it a fresh `RecurrenceTracker` would let it forget that a finding the audit raises already
survived one fix earlier. So `pr_cycle` exports budget and recurrence via `CycleResult`.

The audit dispatches under the `compliance` role. What makes an audit an audit is the prompt, and
Bernstein's role whitelist and per-role worktree conventions are shared with it — this keeps the
tail from depending on a target repository's `role_model_policy` necessarily having a
`final_audit` entry. **The model** comes from `resolved["final_audit"]`, so the audit runs under
whatever model that resolves to.

### 3.9.2 sync is a merge, not a rebase — and a conflict is now a fix loop, not a block

The branch may already have been pushed, and rebasing published history turns every subsequent push
into a force-push, at which point the final pre-merge check can no longer tell it apart from someone
overwriting another person's work.

**A merge conflict used to be an immediate terminal block**, on the same footing as `gh` being
missing. That was wrong: unlike a missing `gh` binary, a conflict is a defect dev can resolve by
editing files, exactly like a review MUST_FIX — and reasona-dev's own completion contract is that a
run reaches a shipped PR unless something genuinely outside its control stops it (network, `gh`,
`git` itself). `sync_main()` now leaves a real conflict in place (conflict markers on disk,
`MERGE_HEAD` still set) instead of auto-aborting it, and `run_sync_cycle()` dispatches dev with the
conflicting paths, instructing it to resolve the markers and run `git commit --no-edit` to conclude
the merge, then retries. Bounded by the `"sync"` stage of the same `FixBudget` (`MAX_SYNC_CYCLES` =
3) — the same shape as every other stage in this pipeline. Every retry starts by aborting any merge
left over from a previous attempt where dev edited files but never committed, so a cycle that fails
never leaves the tree stuck mid-merge; a non-conflict sync failure (fetch failed, no conflicting
paths to name) still blocks immediately, since there is nothing there for dev to fix.

**Up-to-date is still re-checked right before the actual merge call**, separately from the final
phase loop below — `create_pr()`'s own push/`gh pr create` round trip is more time for base to move
in. Unlike a conflict found during sync, a failure here is not looped back into the final phase; it
still blocks immediately (§3.9's "what still fails with a name").

### 3.9.3 The squash message

`squash.build` is the single generator, and `squash.guard` independently re-derives validity rather
than referring back to it. A mismatch between the two means they disagree, not "go fix the message
by hand." A `T#` (title) violation blocks the merge; a `B#` (body-only) violation still allows a
merge with just the title — `squash.classify`'s TITLE_ONLY judgment. GitHub appends
` (#<pr>)` to the squash title itself, so `build` never appends it.

### 3.9.4 `run_final_phase()` — why ship_gate moved behind sync and the audit, and why the tail is a round-bounded loop

`ship_gate.evaluate()` runs `acceptance.run_all()` against whatever is on disk when it is called.
The original pipeline called it BEFORE the merge tail (`orchestrate.py`, computed once, passed into
`run_final_stage()` as an already-decided `ship_decision`), then ran sync and the audit afterward.
Both of those change what is on disk — a sync conflict resolved by dev (§3.9.2), a fix from the
audit (§3.9.1). So the original order stamped a PASS on code that was not what actually ended up in
the PR: neither a sync-conflict fix nor an audit fix was ever re-verified by acceptance before the
squash-merge that shipped it.

**Fix: `ship_gate` now runs inside `final_phase.run_final_phase()`, after sync and the audit have
both already run in the same pass.** This closes the gap for a single pass, but reopens a smaller
version of the same problem: if EITHER sync or the audit changed something in this pass, that
pass's own ship_gate verdict already ran on a tree the OTHER step never saw operating on the
pre-change version of. `run_final_phase()` tracks whether a pass changed anything (a sync conflict
resolved this pass, or `run_final_audit()` dispatching more than one role call meaning a fix
happened) and only accepts a pass's `ship_gate` verdict once a pass changes nothing:

```
for round in 1..MAX_FINAL_PHASE_ROUNDS:
    sync_status, sync_changed    = run_sync_cycle(...)        # may block, may resolve a conflict
    audit_changed                = run_final_audit(...) if should_run_final_audit(budget) else False
    decision, ship_changed       = run_ship_cycle(...)         # may block, may fix a failing criterion (§3.9.5)
    if not decision.passed: return blocked
    if not sync_changed and not audit_changed and not ship_changed: return decision  # settled
    # else: something changed this round -- loop, re-verify from sync
```

Bounded by `MAX_FINAL_PHASE_ROUNDS` (3), on the same reasoning as every other cap in this pipeline:
`origin/main` moving faster than this pipeline's own final-phase processing can settle is not
something retrying forever would fix, and in practice a pass settles in one or two rounds since the
window in which base can move again is just this pass's own sync+audit+ship_gate wall-clock time.

**`orchestrate.py`'s non-`--ship` path is unaffected.** Without `--ship`, none of sync,
`final_audit`, or the authoritative `ship_gate` run at all — the unit is provisionally reported
`shipped`/`failed` on the review/scan verdict alone, using a direct `ship_gate_fn(...)` call as a
preview only (no side effects either way, since nothing merges without `--ship`). This is the same
"default stops at the review/scan verdict" behaviour documented in `README.md`/`docs/INSTALL.md`;
only the `--ship` path's INTERNAL ordering changed.

**Bonus fix found while moving this call.** The original `orchestrate.py` call site was
`ship_gate_fn(workdir, stage_name, cycle_verdict=cycle.verdict, base=base, head=head)`, but
`ship_gate.evaluate()` has never accepted `base`/`head` — every test exercising this path injected a
`ship_gate_fn` stub accepting `**kwargs`, which silently absorbed the mismatch, so the bug reached
this refactor undetected. The real default `ship_gate_fn=ship_gate.evaluate` would have raised
`TypeError` on the very first unit whose review/scan cycle passed in any run that did not override
it. Fixed as part of relocating the call; a regression test now runs `orchestrate.run_plan()`
without overriding `ship_gate_fn`.

### 3.9.5 `run_ship_cycle()` — a failing acceptance criterion earns a fix, not an immediate stop

`ship_gate`'s acceptance axis used to have no fix loop at all: the moment `decision.passed` was
False, the unit was done. Every OTHER check in this pipeline dispatches a bounded dev-fix before
giving up — review, scan, `final_audit`, sync's own conflict handling (§3.9.2) — so a ship-gate
failure was the one place a genuinely fixable problem (a failing executable acceptance criterion)
got zero attempts at a fix. This directly contradicted this project's own completion contract:
reasona-dev runs to a shipped PR unless something outside its control stops it, and an acceptance
criterion is not outside dev's control the way `gh` being unauthenticated is.

**Fix: `run_ship_cycle()` re-checks `ship_gate_fn` in a loop, dispatching dev against the failing
criteria (`ShipDecision.failures`, each `GateOutcome`'s `name`/`detail`) between checks, bounded by
the `"ship"` stage of the same `FixBudget` (`MAX_SHIP_CYCLES`).** The review axis is already
guaranteed to pass by the time this runs — `orchestrate.py` only enters the final phase when the
review/scan cycle itself passed — so a ship-gate failure reaching this loop is always the
acceptance axis. A fix here is exactly the kind of code change `run_final_phase()`'s round loop
already exists to catch: it sets the same `changed` signal `sync_changed`/`audit_changed` use, so a
ship-gate fix forces the whole round to re-verify from sync rather than being accepted on its own.

**Exhausting `MAX_SHIP_CYCLES` is still a `blocked` outcome, not `failed`** (§3.7.11.1) — by the
point ship_gate runs, review, scan, and (conditionally) `final_audit` have all already vetted this
code; a stall specifically here, after three-to-four independent checks already passed it, is
treated as an anomaly needing investigation rather than an ordinary review-found defect. This
mirrors `MAX_INCONCLUSIVE_ATTEMPTS`'s own framing in `cycle_gate.py` — budget exhaustion this deep
in the pipeline is closer to "verification could not complete" than "the code is bad."

## 3.10 Reverting to batch — withdrawing the HTTP approach

`run_role` was reverted from `bernstein serve` + `bernstein worker` + `POST /tasks` back to
`bernstein run <1-step plan>`. Belatedly measuring the "bootstrap cost" that had originally
justified the switch overturned that justification.

| Item | Measured |
|---|---|
| `bernstein run` bootstrap | **1.0–1.1 s** |
| `serve` + `worker` startup | 1.6 s + registration |
| **one agent run** | **86–119 s** |

Bootstrap is about **1%** of an agent's runtime. Across 24 dispatches that is 25 seconds, against a
30-minute execution budget — meaningless. **This was optimized without measuring, and the price
paid was three self-inflicted defects** — every one of them from using Bernstein in an unsupported
shape (§3.8.1).

What reverting to batch removes:

| Item | Result |
|---|---|
| server lifetime management | unnecessary — a run finishes itself |
| completion-verdict defenses (defect 8) | unnecessary — a run's exit is completion |
| spawner liveness (defects 6·9) | unnecessary — Bernstein's watchdog supervises |
| worktree salvage/retry | recovered by Bernstein |

### 3.10.1 Turn budget moves to `complexity`

`Task.max_turns` is reachable only through the HTTP `TaskCreate`, and the plan-step schema has no
such field. But Bernstein does derive a turn budget from a step's `complexity`
(`core/agents/claude_max_turns.py` — low=20 / medium=40 / **high=80** / critical=120, with model-
tier adjustment). The claude adapter receives that result as `explicit_max_turns` and passes it
through as the CLI's `--max-turns`.

In the live run where a reviewer died at turn 23, the analysis was finished but the report never
got written — since the review prompt places writing the report as the **last** action, exhausting
the budget mid-exploration does not truncate the result, it erases it entirely. `high` (80 turns)
was kept as the default to preserve that control.

**Side note confirmed**: the live log's `max_turns resolution ... source=skipped
(adapter does not enforce the openai_agents turn-cap resolver)` did not mean the value was ignored
— it meant the openai_agents-specific resolver was being skipped. The claude adapter does actually
use `Task.max_turns`.

### 3.10.2 No attempt at a cost ceiling

One of the justifications for reverting to batch was `--hard-budget`, but **measurement showed it
cannot fire on this path at all.**

| Source | Value |
|---|---|
| the agent's own runner log | `[RESULT] subtype=success **cost=$0.1736**` |
| `runtime/costs/*.json`'s `spent_usd` | **0.0** |
| `metrics/tasks.jsonl`'s `cost_usd` | **0.0** |
| summary / retrospective | **$0.0000** |

Bernstein logs this itself — `cost_aggregation: agent_metrics total was
$0.0000, falling back to source=task`. The aggregation wiring is broken on the claude adapter path.
With `spent_usd` always 0, no threshold can ever be reached, so `--hard-budget` blocks nothing.

Parsing the agent log's `[RESULT] cost=` line is the only reliable source available, but a control
that depends on an adapter's log format was deliberately not adopted. **Resource control is limited
to turn budget alone.**

### 3.10.3 What remains coupled

`bernstein_dispatch.py` is the entirety of the coupling to Bernstein. The judgment layer
(`finding_adapter`·`cycle_gate`·`acceptance`·`cycles_log`·`memory`·`ship_gate`) never references
Bernstein at all, which leaves the backend swappable. Bernstein's role is **an agent execution
substrate, not an orchestrator** (spawning, worktrees, merge-back, supervision, observability), and
trying to use it as the latter is what produced the missing hooks, the static DAG, and the signal-
visibility problems.

## 3.11 `run-plan` drives every unit through its own worktree, cycle-0 included

The CLI used to require two separate manual commands to run a plan: `compile-plan` (write
`plan.yaml`), then a raw `bernstein run plan.yaml --auto-approve` typed by hand, then `run-plan`
(review → scan → ship). An earlier fix collapsed that into one command by having `cli.py` compile
**the whole plan** into one multi-stage `plan.yaml` (one stage per PR unit, wired with `depends_on`)
and dispatch it as a single `bernstein run`, entirely before `orchestrate.run_plan()` ever started.

**That fix was itself wrong, discovered while porting gh-pr/gh-review (§3.12/§3.13).** Bernstein's
own merge-back lands each stage's work on `workdir`'s single checked-out branch, sequentially, in
dependency order. By the time unit 2's review started, unit 1's (and unit 3's) commits were already
mixed into that one branch's history — there was no way to open a unit-scoped PR, or even reason
about "this branch belongs to this unit," without commit surgery. The batching that made one
`bernstein run` call convenient is exactly what made per-unit isolation structurally impossible.

**Fix: cycle-0 moved into `orchestrate.py`'s own per-unit loop, and every unit gets its own git
worktree before cycle-0 ever runs** (`reasona_dev/worktree.py`, §3.11.1). `cli._cmd_run_plan()` no
longer compiles or dispatches anything itself — it only threads flags through to
`orchestrate.run_plan()`. For each unit, in dependency order, `run_plan()` now:

1. `worktree.ensure_unit_worktree()` — create (or, on resume, reuse) this unit's own worktree,
   branched from `base`.
2. `dispatch_unit_cycle0()` — compile a **single-stage** plan.yaml for just this unit
   (`plan_compile.compile_to_bernstein_plan(..., only_index=...)`, §3.11.2) and dispatch it into
   that worktree. Skipped if the unit's own ledger already says cycle-0 ran (or `--skip-dev`).
3. `pr_cycle.run_pr_cycle()` — review/scan, now against the worktree, not the shared `workdir`.
4. (`--ship` only) `final_phase.run_final_stage()` — sync/final_audit/ship_gate, then gh-pr
   (§3.12), gh-review (§3.13), squash-merge — same worktree throughout.
5. On an actual `MERGED` outcome, `worktree.remove_unit_worktree()` cleans it up. A failed/blocked
   unit's worktree is left in place deliberately, as evidence for the operator to inspect.

Dependency ordering no longer needs to be expressed as a Bernstein DAG at all — `orchestrate.py`'s
own sequential loop (already enforcing `depends_on` via `_blocking_dependency`) is sufficient once
cycle-0 is dispatched per unit instead of all at once; `plan_compile.compile_to_bernstein_plan()`'s
`only_index` filter drops `depends_on` from the compiled stage for exactly this reason (referencing
a dependency's stage would be unresolvable — that stage is not in a single-unit plan.yaml at all).

**The default still stops at the review/scan `ship_gate` verdict -- no PR, no merge.** `--ship`
opts IN to the final stage (sync/final_audit/ship_gate/gh-pr/gh-review, stops at an open PR);
`--merge` opts IN further to squash-merging it. Both default to off, unlike cycle-0 (which now runs
unconditionally per unit unless `--skip-dev`/that unit's own ledger says otherwise): opening a real
PR and squash-merging it are outward-facing, hard-to-undo actions on the target repo's real GitHub
state, not something to run unattended by default. `compile-plan` remains a standalone subcommand
for inspecting a compiled `plan.yaml` without dispatching anything.

### 3.11.1 Per-unit worktrees -- `reasona_dev/worktree.py`

`ensure_unit_worktree(workdir, plan_name, stage_name, base=...)` runs `git worktree add -b <branch>
<path> <base>` at `<workdir>/.worktrees/<plan_name>/<stage_name>/`, and returns that path unchanged
on a second call if the directory already exists (a resumed run reuses whatever cycle-0/review/fix
work already landed there, rather than recreating and losing it). `remove_unit_worktree()` is
best-effort cleanup (`git worktree remove --force` + delete whatever branch the worktree is
CURRENTLY on — not necessarily its original name, see below — never raising on a worktree that was
never created).

**The branch is named by the PR unit, not by an eventual GitHub issue.** dev-ralf's own `/gh-pr`
creates `issue/<N>-<slug>` because it can be invoked standalone, against whatever a human already
has checked out — it has no choice but to mint its own branch from scratch at that point.
reasona-dev's gh-pr port is never invoked standalone: by the time it runs, this unit's worktree has
already existed since before cycle-0, and there is no issue number yet at worktree-creation time (the
issue is a gh-pr-stage artifact, §3.12). So the worktree/branch starts out named
`reasona/<plan_name>/<stage_name>`, and `gh_pr.rename_branch_for_pr()` renames it in place
(`git branch -m`) once the issue exists — always the "on a feature/temp branch" path `/gh-pr` itself
documents, never `checkout -b` (the worktree's branch can never literally be `base`).

**`.reasona/log/<plan_name>/<stage_name>/` (the ledger/raw-output layout, §3.11.3) is unaffected by
any of this** — only the actual git checkout moved to a per-unit worktree; logs and the ledger stay
anchored to the top-level `workdir` exactly as before, so they remain readable/browsable after a
unit's worktree is cleaned up.

### 3.11.2 Compiling one unit's cycle-0 -- `plan_compile.compile_to_bernstein_plan(only_index=...)`

`only_index`, when given, filters the parsed plan down to exactly one PR unit's stage before
building `stages: [...]`, and — since a single-stage plan.yaml has nothing else in it for a
`depends_on` to reference — drops that unit's `depends_on` entirely, on the documented assumption
that the caller (`orchestrate.py`) already enforces the order. Every other side effect of this
function (the acceptance file, the audit trail, the `bernstein.yaml` bootstrap/sync via
`bernstein_config.ensure_bernstein_yaml()`) still runs exactly as before, just anchored at
`workdir=<this unit's worktree>` instead of the top-level repo — which is also how a fresh
worktree, which never inherits a target repo's gitignored `bernstein.yaml` through a plain `git
worktree add` checkout, ends up with one anyway: `dispatch_unit_cycle0()` calling this function
with the worktree as `workdir` is what (re-)bootstraps it there.

### 3.11.3 Resuming after an interruption -- `reasona_dev/ledger.py`

A single command that runs a whole plan through squash-merge is also a single command that can be
killed partway through by exactly the same class of failure the split-command design was trying to
avoid in the first place -- a network drop, a killed process. Re-running the same `run-plan`
command needs to pick up where it left off, not redo units that already shipped, re-create a
worktree that already exists, or re-dispatch cycle-0 against code that already exists.

**Layout: `<workdir>/.reasona/log/<plan_name>/<stage_name>/`, namespaced by plan first, then PR
unit** -- not a flat `<workdir>/.reasona/`. Two plans that both exist under the same workdir (or two
different plans that both happen to name a unit `pr-1`, the common case since
`plan_compile._stage_name()` is just `f"pr-{index}"`) must not share a ledger file or a compiled
`plan.yaml`; the flat layout silently collided on both before this. `reasona_dev/ledger.py` is the
single module owning this layout:

    <workdir>/.reasona/log/<plan_name>/<stage_name>/plan.yaml     this unit's compiled cycle-0 plan
    <workdir>/.reasona/log/<plan_name>/<stage_name>/ledger.json   dev-dispatched flag + progress + terminal outcome + PR-url/issue-number hints
    <workdir>/.reasona/log/<plan_name>/<stage_name>/<role>-c<cycle>.raw.txt  raw per-role output, same as before

**Cycle-0 dispatch is tracked per unit, not once for the whole plan.** `dev_already_dispatched()`/
`mark_dev_dispatched()` used to be keyed by `plan_name` alone (one flag covering every unit, from
when cycle-0 was one batched dispatch); now that cycle-0 is dispatched per unit (§3.11), both take
`(plan_name, stage_name)` and live in that unit's own `ledger.json`, alongside its terminal outcome
-- there is no more plan-wide `ledger-plan.json`.

**Per-unit progress is checkpointed inside the review/scan loop itself, not only at the unit's
terminal outcome.** `pr_cycle.run_pr_cycle()` takes `plan_name` and `resume`, and after every cycle
(including an `inconclusive_retry`) it snapshots `FixBudget`, `RecurrenceTracker`,
`ConvergenceTracker`, the pending `must_fix` findings, and the current phase/cycle/route via
`ledger.save_progress()` -- all three trackers gained `to_dict()`/`from_dict()` for exactly this. A
resumed run reloads that snapshot and continues from the same cycle with the same budget already
spent, instead of re-entering at zero. `ledger.mark_unit_terminal()` clears the progress snapshot
once a unit reaches `shipped`/`failed`/`blocked` -- nothing is left to resume past that point.

**`create_pr()` and `sync_main()` still ask gh/git first -- the ledger is a fallback, never a
replacement.** `create_pr()` calls `existing_pr_url()` (`gh pr view`) exactly as before; only when
that live check finds nothing does it fall back to `ledger.known_pr_url()`, a PR URL this same unit
recorded on an earlier, interrupted run (`git push` can succeed and the process still die before the
URL is read back, which is exactly the case the live check alone cannot recover from). A successful
`create_pr()` records the URL via `ledger.mark_pr_created()` for the next run's fallback; `gh_pr.py`
applies the identical pattern to the GitHub issue it creates (`known_issue_number()`/
`mark_issue_created()` -- never a second throwaway issue for the same unit on resume).
`sync_main()` gained no ledger integration at all -- git's own merge is already fully idempotent
(re-running `git merge origin/main` on an already-merged tree is a no-op), so a ledger check there
would duplicate what `git status` already answers for free.

**Manual overrides exist for when the ledger itself is wrong or unavailable.** `from_pr` drops every
unit ordered before the named one from the run regardless of ledger state (`run_plan(resume=...)`'s
own check is skipped for those units by construction -- they are never in the unit list to begin
with). `--skip-dev` force-skips cycle-0 dispatch for every unit regardless of the ledger (the
worktree is still created either way -- `--skip-dev` exists for "cycle-0 already ran/was set up by
hand," not "skip the worktree too"). `--restart` (`ledger.clear()`) wipes every unit's ledger and
reruns everything fresh -- the right tool when the plan document itself changed since the last run,
not for a plain retry.

## 3.12 Porting `/gh-pr` -- `reasona_dev/gh_pr.py`

Read in full from `~/repository/tas-dev-plugins/plugins/dev/skills/gh-pr/SKILL.md` before porting.
Runs inside `final_phase.run_final_stage()`, right after `run_final_phase()`'s sync/audit/ship_gate
round loop settles (§3.9.4) — in place of what used to be a direct `create_pr()` call.

**Not ported: §4's `make ci`/`make lint-md` re-validation gate — and this is a real gap, not a
clean substitution.** The original skill runs this UNCONDITIONALLY for any source-touching change.
reasona-dev's nearest equivalent, `ship_gate`'s acceptance axis, only runs commands the PLAN
ITSELF declared via `acceptance:` (§3.7.3) — a plan that declares nothing gets a passing-with-warning
verdict, not a failure, and no build/test command runs anywhere in this pipeline for that unit. So
this module does not duplicate a check reasona-dev is GUARANTEED to have already made; it relies on
the plan having declared one. See §3.7.3's own note on this gap and the requirement it places on
plan authors.

**Branch handling is the one deliberate divergence, and it follows from §3.11.1.** `/gh-pr` §6
creates its own branch because it can be invoked standalone against whatever a human already has
checked out (`checkout -b` on base, or `branch -m` on a feature branch). This module is never
invoked standalone — the unit already has its own dedicated worktree, on a unit-named branch, since
before cycle-0 even started — so it always takes the "rename in place" path
(`gh_pr.rename_branch_for_pr()`); the `checkout -b`-on-base case never applies here.

**Title/body are built deterministically, then independently re-checked — the same `build()`/
`guard()` split `reasona_dev.squash` already uses for the squash commit message (§3.9.3), applied
here to a different artifact.** `build_pr_title()` sanitizes the plan's own freeform `## PR
<index>: <title>` heading text (strips a stray `#N` prefix, a trailing period, coerces an
unrecognized type to `feat`) the same way `squash.build()` sanitizes commit body lines.
`build_pr_body()` fills the three required sections (`## Changes`, `## Why we need this`,
`## Test`) from what this pipeline actually knows — the unit's own plan section as the change
description, and "review/scan/ship_gate's acceptance axis already passed" as the only test evidence
that genuinely exists at this point — rather than fabricating detail an LLM would normally supply.
`validate_pr_meta()` re-derives `/gh-pr` §8's P1-P7 checks independently of the builder (never
consults it), so a violation on a fresh build should be vanishingly rare; `repair_pr()` still exists
as insurance, pushing a corrected version via `gh pr edit` (never `gh pr create` again) up to
`MAX_PR_REPAIR_ATTEMPTS` (3) times. Exhausting that budget reports the unit `blocked`, not
`failed` — a PR-metadata violation is not a judgment about the code's quality.

**Idempotency reuses `final_phase.create_pr()`/`existing_pr_url()` rather than duplicating it.**
`create_pr()` gained `head`/`base`/keyword-only `title`/`body` parameters so `gh_pr.py` could pass
`--head <branch> --base <base_branch>` explicitly to `gh pr create` — `/gh-pr` §8's own rule
("never rely on `gh` detecting the current branch from CWD"), which matters here specifically
because every dispatch already runs against a per-unit worktree whose CWD is never the caller's own.
The live-`gh`-first, ledger-fallback pattern (§3.11.3) is otherwise unchanged. The equivalent
pattern is applied to issue creation too: `ledger.known_issue_number()`/`mark_issue_created()`
avoid opening a second throwaway issue for the same unit on a resumed run.

## 3.13 Porting `/gh-review` -- `reasona_dev/gh_review_watch.py` + `reasona_dev/gh_review.py`

Read in full from `~/repository/tas-dev-plugins/plugins/dev/skills/gh-review/SKILL.md` and its
`tools/watch.py` before porting. Runs immediately after `gh_pr.py` succeeds, inside the same
`run_final_stage()` call, before the final `is_up_to_date()`/`squash_merge()` pair.

**This is not a second review pass, and not a duplicate of `pr_cycle`'s local scan cycle.**
`pr_cycle.py`'s bugbot/compliance dispatch is a LOCAL Bernstein run, before a PR exists. The three
signals this module watches — `statusCheckRollup`, a `claude[bot]`-family PR comment, a
`github-actions[bot]`-family PR review — are produced by the TARGET REPO'S OWN GitHub Actions,
against the pushed commit, on GitHub's own infrastructure. Confirmed directly with the user: these
are genuinely separate, independent checks, not a re-run of the local one.

**`gh_review_watch.py` is a near-verbatim port of `watch.py`.** The original is already pure Python
+ `gh api graphql` subprocess calls with zero LLM involvement — a deterministic classifier over
GraphQL JSON — so it needed no redesign to fit this project's "no model in the judgment loop" rule.
Only the subprocess helper (swapped for `reasona_dev._shell.run()`) and the CLI entry point
(`main()`'s `argparse` + its own polling `while True` loop) changed; `gh_review.py` calls
`take_snapshot()`/`classify()` as a library and owns the polling loop itself. (An earlier revision
of this section claimed a transcription bug in the original's `parse_bugbot_analysis()` --
`submittedAt` stored but `submitted_at` read back. Re-verified directly against
`~/repository/tas-dev-plugins/plugins/dev/skills/gh-review/tools/watch.py`: the original already
reads `latest.get("submittedAt")` correctly. That claim came from a research fork's summary that
mis-transcribed the line, trusted without re-checking the primary source before writing it into
this document -- corrected here.)

`classify()`'s decision tree is unchanged: `ci.failing → actionable`; `ci != passing → continue`
(CI gates the bot signals — a stale artefact from a prior head SHA could otherwise mis-classify
while CI is still in flight); once `ci == passing`: `compliance.fail OR bugbot.found → actionable`;
either `missing → continue` (replication lag); else `terminal`.

**`gh_review.py` owns the auto-fix loop, using this pipeline's own dispatch/budget primitives
instead of the original skill's "runs in the dispatching agent" model.** Each actionable cycle
gathers every actionable signal (CI failing / compliance fail / bugbot found) into ONE dev-fix
prompt, dispatches `pr_cycle.run_role`'s `backend` role, then pushes deterministically itself
(`git push`, not trusted to an instruction inside the prompt) — the same split
`final_phase.py`'s sync-conflict/ship-gate fix loops already use (§3.9.2, §3.9.5). One push per
cycle re-triggers every workflow at once, per `/gh-review` §3.3's own rule. Reply bullets to the
compliance/bugbot comment threads are NOT fabricated from a summary this deterministic layer does
not actually have — the posted reply names only the fixing commit.

**Two budgets, tracked separately, mirroring dev-ralf's own pooling rule.** Waiting for CI/bot
workflows to finish is wall-clock time (`max_wait_seconds`, `time.monotonic()`), independent of
`FixBudget`. Actually dispatching a fix spends `budget`'s `"gh_review"` stage
(`cycle_gate.MAX_GH_REVIEW_CYCLES` = 3, `/gh-review`'s own default `--max-cycle`), pooled into the
same `MAX_TOTAL_FIX_CYCLES` every other stage shares:
`min(MAX_GH_REVIEW_CYCLES, MAX_TOTAL_FIX_CYCLES - budget.total_used)`. Exhausting either is
`blocked`, not `failed` -- by this point review, scan, and ship_gate have already passed, so a
CI/compliance/bugbot failure this deep is either a defect those local checks could not see (not a
re-derivable local judgment) or an external stall, matching the same reasoning
`cycle_gate.MAX_SHIP_CYCLES` already documents for ship_gate's own bounded fix loop.

`--gh-review-max-wait` exposes only the wall-clock budget as a `run-plan` flag (default 900s,
matching `/gh-review`'s own default) — the cycle-count budget stays a fixed pipeline constant, like
every other stage's cap, not something exposed per invocation.

## 4. Directory structure

```
reasona-dev/
├── docs/ARCHITECTURE.md      this document
├── .bernstein/bernstein.yaml project config (model_fallback deliberately constrained, role_model_policy) — committed here because
│                             it's the spot find_seed_file() checks first (§3.5.3)
├── .reasona/reasona.yaml     reasona-dev's own model_config layer, the `dev-models:` key (the future reasona-plan will use `plan-models:`)
├── reasona_dev/
│   ├── cli.py                the actual `reasona-dev` executable entry point ([project.scripts]) — the only place flags are actually entered
│   ├── plan_compile.py       plan document → bernstein plan.yaml compiler (dev's cycle-0 step, `only_index` for a single unit, workdir anchoring) (§3.11.2)
│   ├── orchestrate.py        plan-unit execution — worktree creation, per-unit cycle-0 dispatch, dependency order, per-unit profiles (§3.11, §3.11.1)
│   ├── worktree.py           one git worktree per PR unit, from before cycle-0 through squash-merge/cleanup (§3.11.1)
│   ├── pr_cycle.py           reproduces worker.md — deterministic develop → review → bug+compliance scan driver (§3.5.4)
│   ├── bernstein_dispatch.py 1-step plan.yaml + `bernstein run` — one synchronous role dispatch (§3.10)
│   ├── acceptance.py         executable acceptance criteria — runs the manifest's acceptance: deterministically right before merge (§3.7.3)
│   ├── structure_gate.py     structural judgment — file size, duplication, dependency direction, public-API growth (§3.7.2)
│   ├── ship_gate.py          the single pre-merge verdict — review AND acceptance, logical AND; called FROM final_phase, with its own bounded dev-fix loop (§3.7.8, §3.9.4, §3.9.5)
│   ├── final_phase.py        gh check → final phase (sync ↔ conflict-fix → conditional final_audit → ship_gate ↔ acceptance-fix, re-verified as a round) → gh-pr → gh-review → squash-merge (§3.9, §3.12, §3.13)
│   ├── gh_pr.py               `/gh-pr` port — issue → branch rename → push+create PR → structural validate/repair (§3.12)
│   ├── gh_review_watch.py     `/gh-review`'s watcher ported near-verbatim — CI/compliance/bugbot GraphQL snapshot + classify (§3.13)
│   ├── gh_review.py           `/gh-review`'s auto-fix loop — dispatch dev against actionable signals, one push per cycle, budget-bounded (§3.13)
│   ├── cycles_log.py         per-cycle finding measurement (.reasona/cycles.jsonl) — the basis for attribution measurement (§3.7.6)
│   ├── cycles_query.py       attribution/budget/AC-coverage queries — turns the log into judgment (§3.7.9)
│   ├── memory.py             per-repo prior-observation notes generated from cycles.jsonl, file-scoped retrieval (§3.7.7)
│   ├── prompt_profile.py     per-unit profile resolution + two-layer prompt lookup (§3.5.4, §3.7.10)
│   ├── model_config.py       the per-role model priority chain (flag > env > project cfg > global cfg > fallback > default), CONDUCTOR-COLLAPSE audit trail
│   ├── config_file.py        reasona-dev's own two-layer cfg (~/.reasona → <workdir>/.reasona, reasona.yaml)
│   ├── bernstein_config.py   automatic placement of the target repo's .bernstein/bernstein.yaml + role_model_policy sync (§3.5.3)
│   ├── finding_adapter.py    parser for the `||` text contract + the external-skill KV contract (`parse_kv_contract`)
│   ├── cycle_gate.py         result-invariant verification (inherited from dev-ralf's cycle_gate.py)
│   ├── gate_check.py         the completion_signals(test_passes) entry point — merge/no-merge decision
│   ├── squash.py             squash-message builder + guard
│   ├── ledger.py             per-plan, per-unit resume state (§3.11.3)
│   ├── _shell.py             the one subprocess-run helper every git/gh-calling module shares
│   ├── plugin.py             pluggy hookimpl (on_pre_task_create, on_agent_spawned)
│   └── adapters/
│       └── ocr.py            OcrAdapter — registered via the bernstein.adapters entry point
├── pyproject.toml
└── tests/
```
