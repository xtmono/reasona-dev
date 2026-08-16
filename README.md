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

Working V0. Install, tests, and a real Bernstein plan-validation round-trip
all pass as of this commit:

```
$ .venv/bin/python -m pytest tests/ -v      # 22 passed
$ .venv/bin/bernstein plan validate <compiled plan.yaml>   # "Plan is valid."
$ .venv/bin/bernstein doctor                # "Plugin loading: no errors"
```

## What's real vs. what's open

See `docs/ARCHITECTURE.md` §3 for the full list. The two that block a
production run:

- `cascade_router.py`'s confidence-based auto-escalation has no confirmed
  off switch yet (separate from `model_fallback`, which does).
- The exact hookspec that gates task **completion/merge** (as opposed to
  `on_pre_task_create`, which gates **creation** and is confirmed) was not
  found in this pass.

## Layout

```
docs/ARCHITECTURE.md      4-layer architecture, verified against installed Bernstein 3.15.1 source
bernstein.yaml             project config (model_fallback deliberately emptied)
templates/review.yaml      multi-lens verification pipeline (strategy: all)
reasona_dev/
  plan_compile.py           plan document -> bernstein plan.yaml
  finding_adapter.py         || evidence-field text contract parser
  cycle_gate.py               recheck routing, escalation, budget, fingerprints
  squash.py                    squash message builder + guard
  plugin.py                     pluggy hookimpl (on_pre_task_create)
tests/                      pytest, 22 cases, all passing
```

## Setup

```bash
uv venv .venv
uv pip install --python .venv/bin/python -e ".[test]"
.venv/bin/python -m pytest tests/
```
