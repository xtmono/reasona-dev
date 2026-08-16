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

Working V0. All items flagged open after the first pass are now resolved by
direct inspection of the installed Bernstein 3.15.1 source — see
`docs/ARCHITECTURE.md` §3. Install, tests, and real round-trips against the
actual `bernstein` CLI all pass as of this commit:

```
$ .venv/bin/python -m pytest tests/ -v          # 47 passed
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
| Per-role model config | Ported dev-ralf's `flag > env var > fallback > default` priority chain (`DEV_RALF_*` -> `REASONA_DEV_*`) as `reasona_dev/model_config.py`, since bandit routing being off means nothing else picks a model. Caught and fixed a real asymmetry bug in the first draft: `bugbot`/`final_audit` must fall back only to the `VERIFY_MODEL` **env var**, never to `verify`'s fully-resolved value -- only `recheck` inherits `review`'s resolved outcome. |

## Layout

```
docs/ARCHITECTURE.md      4-layer architecture, verified against installed Bernstein 3.15.1 source
bernstein.yaml             project config (model_fallback correctly emptied, cascade_router/bandit notes)
templates/review.yaml      static example only -- review_pipeline.py generates the real one
reasona_dev/
  plan_compile.py           plan document -> bernstein plan.yaml, auto-wires completion_signals
  model_config.py            per-role model priority chain + CONDUCTOR-COLLAPSE audit trail
  review_pipeline.py          model_config-driven review.yaml renderer (initial + bounded recheck)
  finding_adapter.py           || evidence-field text contract parser
  cycle_gate.py                  recheck routing, escalation, budget, fingerprints
  gate_check.py                   completion_signals entry point -- the actual merge gate
  squash.py                        squash message builder + guard
  plugin.py                         pluggy hookimpl (on_pre_task_create) -- next-fix-task gating only
  adapters/ocr.py                    OcrAdapter, registered via bernstein.adapters entry points
tests/                      pytest, 47 cases, all passing
```

## Setup

```bash
uv venv .venv
uv pip install --python .venv/bin/python -e ".[test]"
.venv/bin/python -m pytest tests/
```

## Next

The remaining work is an end-to-end run: one real PR unit through
`plan_compile.py` -> `bernstein run` -> the review pipeline -> `gate_check.py`
-> squash merge, on a real repository.
