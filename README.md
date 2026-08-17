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
$ .venv/bin/python -m pytest tests/ -v          # 89 passed
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
| Persistent config, no env var re-export | Added `reasona_dev/config_file.py` -- a two-layer cascade (`~/.reasona/config.yaml` global, `<workdir>/.reasona/config.yaml` project/local, mirroring Bernstein's own pattern) sitting between the env var and the hardcoded default in `model_config.py`'s priority chain. Anchored to the same `workdir` as everything else (never reasona-dev's own install location, since there won't be one once deployed). |

## Layout

```
docs/ARCHITECTURE.md      4-layer architecture, verified against installed Bernstein 3.15.1 source
bernstein.yaml             THIS repo's own committed project config (model_fallback correctly emptied,
                            cascade_router/bandit notes, role_model_policy) -- committed directly at repo
                            root, not under .reasona/ (see "bernstein.yaml for target repos" below for why)
.reasona/config.yaml        THIS repo's own committed model_config layer -- real dev-ralf-ported values
templates/review.yaml      static example only -- review_pipeline.py generates the real one
reasona_dev/
  plan_compile.py           plan document -> bernstein plan.yaml, auto-wires completion_signals, anchors to workdir
  model_config.py            per-role model/adapter/effort priority chain + CONDUCTOR-COLLAPSE audit trail
  config_file.py              reasona-dev's own 2-layer config cascade (~/.reasona -> <workdir>/.reasona)
  bernstein_config.py          bootstraps + syncs a target repo's bernstein.yaml (see "Bootstrapping" below)
  review_pipeline.py          model_config-driven review.yaml renderer (initial + bounded recheck)
  finding_adapter.py           || evidence-field text contract parser
  cycle_gate.py                  recheck routing, escalation, budget, fingerprints
  gate_check.py                   completion_signals entry point -- the actual merge gate
  squash.py                        squash message builder + guard
  plugin.py                         pluggy hookimpl (on_pre_task_create, on_agent_spawned)
  adapters/ocr.py                    OcrAdapter, registered via bernstein.adapters entry points
tests/                      pytest, 89 cases, all passing
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
reasona-dev compile-plan plan.md -o plan.yaml --workdir <target-repo> --dev opus
reasona-dev render-review -o review.yaml --workdir <target-repo> --bugbot deepseek-v4-pro
reasona-dev render-review -o review.yaml --workdir <target-repo> --bounded
```

Role flag names mirror dev-ralf's own one-to-one: `--dev`, `--review`,
`--recheck`, `--bugbot`, `--verify`, `--final-audit`.

`compile-plan` also bootstraps and keeps `<workdir>/bernstein.yaml` in
sync as a side effect (`reasona_dev/bernstein_config.py`) -- see the next
section for the mechanism and why THIS repo doesn't need it for itself.

## `bernstein.yaml` for target repos vs. this repo's own

Bernstein's own config loader (`cli/helpers.py:find_seed_file()`) only
ever reads a project-local `bernstein.yaml`/`.bernstein/bernstein.yaml` in
the invoking cwd. `bernstein run` alone accepts an explicit `--seed PATH`
override; bare `bernstein` ("run from bernstein.yaml or backlog") and
`bernstein doctor` have no such flag and always fall through to
`find_seed_file()` -- so a real, project-local `bernstein.yaml` has to
exist on disk for every ad-hoc `bernstein` invocation to work, `--seed` or
not (confirmed by reading the loader directly; see `docs/ARCHITECTURE.md`
§3.5.3 for the full trace). A truly global `bernstein.yaml` isn't
something Bernstein itself supports.

For every OTHER target repository, `reasona_dev.bernstein_config.
ensure_bernstein_yaml()` bootstraps that project-local file automatically
(never overwriting one that's already there), sourced from:

```
<workdir>/bernstein.yaml              what Bernstein reads
<workdir>/.reasona/bernstein.yaml     project-local template (checked first)
~/.reasona/bernstein.yaml             global template (checked second)
```

**This repo's own `bernstein.yaml` is just committed directly at the repo
root** -- the plain "already has one, so leave it alone" case above, not
the template cascade. That's deliberate: `bernstein.yaml`, unlike
`.reasona/config.yaml` (see below), has no `--seed`-independent fallback
for `bernstein doctor`/bare `bernstein`, so keeping it anywhere other than
the root would break those commands when run directly against this repo
(exactly what this session's own verification loop relies on throughout
`docs/ARCHITECTURE.md`).

`.reasona/config.yaml`, by contrast, IS committed under `.reasona/` here --
that file is `reasona_dev.config_file`'s own project-local model-config
layer (not something Bernstein itself ever reads), so it carries none of
`bernstein.yaml`'s constraint. It wins over whatever's in the machine's
`~/.reasona/config.yaml`, so running this repo's tests or tooling doesn't
depend on the operator's own global config.

## Next

The remaining work is an end-to-end run: one real PR unit through
`plan_compile.py` -> `bernstein run` -> the review pipeline -> `gate_check.py`
-> squash merge, on a real repository.
