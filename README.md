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
| Merge-gating hook | Confirmed no dedicated hookspec exists (`on_pre_task_create` and `on_pre_tool_use` are the only two that can block). Merge/completion gating is a `completion_signals: [{type: test_passes, ...}]` concern, not a hook -- `reasona_dev/gate_check.py` is that entry point, wired automatically by `plan_compile.py`. |
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
reasona_dev/
  plan_compile.py           plan document -> bernstein plan.yaml (dev's cycle-0 step), anchors to workdir
  pr_cycle.py                 dev-ralf-faithful develop -> verify -> bug+compliance scan driver (worker.md)
  prompt_profile.py            project/language-selectable review/bugbot/compliance prompts (.reasona/prompts/<profile>/)
  prompts/generic/               packaged default prompt profile (review/bugbot/compliance/final_audit.md)
  model_config.py            per-role model/adapter/effort priority chain + CONDUCTOR-COLLAPSE audit trail
  config_file.py              reasona-dev's own 2-layer config cascade (~/.reasona -> <workdir>/.reasona)
  bernstein_config.py          bootstraps + syncs a target repo's bernstein.yaml (see "Bootstrapping" below)
  finding_adapter.py           || text contract AND external-skill KV contract (`parse_kv_contract`) parsers
  cycle_gate.py                  recheck routing, escalation, budget, fingerprints
  gate_check.py                   completion_signals entry point -- the actual merge gate
  squash.py                        squash message builder + guard
  plugin.py                         pluggy hookimpl (on_pre_task_create, on_agent_spawned)
  adapters/ocr.py                    OcrAdapter, registered via bernstein.adapters entry points
tests/                      pytest, 114 cases, all passing
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

## Prompt profiles

review/bugbot/compliance/final_audit prompts are project- and
language-specific (dev-ralf's Rust-monorepo setup is Rust-aware and dispatches to
a target repo's own `ext-bugbot`/`ext-review` skills) -- they live as plain `.md`
files under a named **profile**, resolved through the same
flag > env var > project cfg > global cfg > default chain as everything
else (`reasona_dev/prompt_profile.py`):

```
<workdir>/.reasona/prompts/<profile>/<role>.md   project-local (e.g. a target repo's own Rust profile)
~/.reasona/prompts/<profile>/<role>.md           global (an operator's shared profile)
reasona_dev/prompts/<profile>/<role>.md          packaged with reasona-dev (only "generic" ships today)
```

Select a profile via `--profile NAME`, `REASONA_DEV_PROFILE`, or
`dev-profile:` in `reasona.yaml`. An unresolvable profile name returns no
prompt (never silently falls back to `generic`) -- `pr_cycle.py` aborts
rather than run with the wrong policy.

## Next

`reasona_dev/pr_cycle.py` (the dev-ralf-faithful develop -> verify ->
bug+compliance scan driver, see `docs/ARCHITECTURE.md` §3.5.4) is built and
unit-tested. Its `run_role()` boundary's underlying mechanism -- a real
`bernstein run <plan> --auto-approve` against a live server -- IS now
live-verified (2026-08-18, real paid run: haiku agent spawned, committed,
merged, task `done`, `result_summary` populated; see the `bernstein.yaml`
placement bug this same test caught, above). `pr_cycle.py`'s own
`run_role()` -- the file-handoff prompt convention specifically -- has not
yet been run end-to-end itself. The remaining work: one real PR unit
through `plan_compile.py`'s cycle-0 dev step -> `pr_cycle.run_pr_cycle()`
-> `gate_check.py` -> squash merge, on a real repository -- plus the
still-unbuilt tail (`sync-main -> /gh-pr -> /gh-review -> up-to-date gate
-> final_audit`, worker.md's last third) and bounded (vs. always-full)
recheck routing.
