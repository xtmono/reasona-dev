"""DEPRECATED -- do not build on this module. Kept only until its CLI
surface (`reasona-dev render-review`) and tests are formally removed.

Render Bernstein's `review --pipeline` YAML from resolved model config.

**Why deprecated:** `bernstein review --pipeline`'s actual runner
(`core/quality/review_pipeline/runner.py`) turned out to ignore `adapter`
and `prompt_template` entirely -- every agent in a stage gets a single
fixed, role-blind prompt built only from the diff + task title/description
(`_build_agent_prompt` -> `cross_model_verifier._build_prompt`), with no
tool access (no Bash/Read -- a bare LLM completion call, not an agentic
session). That makes it structurally unable to run dev-ralf's actual
bugbot/compliance dispatch (`ext-bugbot --dir ...`, which needs a real
agentic session) or give reviewer/bugbot/compliance genuinely different
instructions (see docs/ARCHITECTURE.md §3.5.4 for the full trace). The
replacement is `reasona_dev.pr_cycle` (dev-ralf-faithful develop -> verify
-> bug+compliance scan loop, driven by real `bernstein run` dispatches)
plus `reasona_dev.prompt_profile` (project-selectable prompt files instead
of this module's hardcoded `prompt_template:` path, which the runner never
even read).

Everything below this docstring still works and is still tested -- it just
isn't wired into anything real anymore. Model/adapter/effort DO still flow
through `reasona_dev.model_config.resolve_all()`'s priority chain here, for
whatever it's worth to a caller that still wants a `review --pipeline` YAML
for some other purpose.

Schema verified against installed Bernstein 3.15.1
(`core/quality/review_pipeline/schema.py`): `version`, `name`,
`pass_threshold`, `block_on_fail`, `stages[].parallelism`,
`stages[].aggregator.strategy` (any|all|majority|weighted -- `all` is what
expresses dev-ralf's "merged PASS iff ALL reviewers PASS" rule, see
docs/ARCHITECTURE.md §2), `stages[].agents[].{role,model,adapter,
prompt_template,effort}`.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from reasona_dev.model_config import ResolvedModel, resolve_all


def render(
    resolved: dict[str, ResolvedModel] | None = None,
    *,
    workdir: str | Path | None = None,
    flags: dict[str, str] | None = None,
) -> dict:
    """Build the pipeline dict. Resolves config itself if not supplied.

    `workdir` is forwarded to `resolve_all()` so `<workdir>/.reasona/reasona.yaml`
    is consulted -- the same TARGET-repo anchor `plan_compile.
    compile_to_bernstein_plan()` uses (docs/ARCHITECTURE.md §0.1), never
    reasona-dev's own install location. Defaults to `Path.cwd()` like every
    other entry point in this project.

    `flags` is the CLI-flag layer (`--review`, `--bugbot`, etc. --
    `reasona_dev.cli`) -- the highest-priority input in
    `model_config.resolve()`'s chain.
    """
    resolved = resolved if resolved is not None else resolve_all(workdir=workdir, flags=flags)

    return {
        "version": 1,
        "name": "reasona-dev-initial-review",
        "pass_threshold": 1.0,
        "block_on_fail": True,
        "stages": [
            {
                "name": "initial_review",
                "parallelism": 2,
                "aggregator": {"strategy": "all"},
                "agents": [
                    {
                        "role": "reviewer",
                        "model": resolved["review"].model,
                        "adapter": resolved["review"].adapter,
                        "prompt_template": "prompts/review.md",
                        "effort": resolved["review"].effort,
                    },
                    {
                        # ocr is stateless/tool-native; no LLM model slot.
                        # `effort` is NOT nullable in Bernstein's AgentSpec
                        # (real schema: `effort: EffortLevel = "low"`, no
                        # None allowed -- confirmed via `bernstein review
                        # --pipeline ... --validate-only` rejecting
                        # `effort: null`) so it is omitted here rather than
                        # set to None, letting the schema default apply.
                        # (reasona_dev.adapters.ocr.OcrAdapter -- see
                        # docs/ARCHITECTURE.md §3.4).
                        "role": "ocr_reviewer",
                        "model": None,
                        "adapter": "ocr",
                        "prompt_template": None,
                    },
                ],
            },
            {
                "name": "bug_and_compliance_scan",
                "parallelism": 2,
                "aggregator": {"strategy": "all"},
                "agents": [
                    {
                        "role": "bugbot",
                        "model": resolved["bugbot"].model,
                        "adapter": resolved["bugbot"].adapter,
                        "prompt_template": "prompts/bugbot.md",
                        "effort": resolved["bugbot"].effort,
                    },
                    {
                        "role": "compliance",
                        "model": resolved["verify"].model,
                        "adapter": resolved["verify"].adapter,
                        "prompt_template": "prompts/compliance.md",
                        "effort": resolved["verify"].effort,
                    },
                ],
            },
        ],
    }


def render_bounded_recheck(
    resolved: dict[str, ResolvedModel] | None = None,
    *,
    workdir: str | Path | None = None,
    flags: dict[str, str] | None = None,
) -> dict:
    """Bounded recheck pipeline -- Sonnet(-tier)+OCR, confirm/regression only.

    Used when reasona_dev.cycle_gate.recheck_route() returns "BOUNDED"
    (fix_files subset of finding_files). The reviewer's own text-contract
    prompt (finding_adapter.py) is what actually narrows the task to
    confirmation + regression -- this pipeline just points the reviewer
    role at the resolved `recheck` model instead of `review`.
    """
    resolved = resolved if resolved is not None else resolve_all(workdir=workdir, flags=flags)
    pipeline = render(resolved)
    pipeline["name"] = "reasona-dev-bounded-recheck"
    pipeline["stages"][0]["agents"][0]["model"] = resolved["recheck"].model
    pipeline["stages"][0]["agents"][0]["adapter"] = resolved["recheck"].adapter
    pipeline["stages"][0]["agents"][0]["effort"] = resolved["recheck"].effort
    pipeline["stages"][0]["agents"][0]["prompt_template"] = "prompts/recheck.md"
    # bounded recheck never re-runs the scan stage -- only verify results matter here.
    pipeline["stages"] = pipeline["stages"][:1]
    return pipeline


def write_review_yaml(
    out_path: str | Path,
    *,
    bounded: bool = False,
    resolved: dict | None = None,
    workdir: str | Path | None = None,
    flags: dict[str, str] | None = None,
) -> None:
    pipeline = (
        render_bounded_recheck(resolved, workdir=workdir, flags=flags)
        if bounded
        else render(resolved, workdir=workdir, flags=flags)
    )
    Path(out_path).write_text(yaml.safe_dump(pipeline, sort_keys=False, allow_unicode=True))
