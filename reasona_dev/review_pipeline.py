"""Render Bernstein's `review --pipeline` YAML from resolved model config.

Replaces the previously-hardcoded `templates/review.yaml` (literal model
names, no override mechanism) with a generator driven by
`reasona_dev.model_config.resolve_all()` -- the same priority chain
(flag > env var > fallback > default) that governs every other role.

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


def render(resolved: dict[str, ResolvedModel] | None = None, *, workdir: str | Path | None = None) -> dict:
    """Build the pipeline dict. Resolves config itself if not supplied.

    `workdir` is forwarded to `resolve_all()` so `<workdir>/.reasona/config.yaml`
    is consulted -- the same TARGET-repo anchor `plan_compile.
    compile_to_bernstein_plan()` uses (docs/ARCHITECTURE.md §0.1), never
    reasona-dev's own install location. Defaults to `Path.cwd()` like every
    other entry point in this project.
    """
    resolved = resolved if resolved is not None else resolve_all(workdir=workdir)

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
                        "model": resolved["review"].value,
                        "adapter": "claude",
                        "prompt_template": "prompts/review.md",
                        "effort": "high",
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
                        "model": resolved["bugbot"].value,
                        "adapter": "kilo",
                        "prompt_template": "prompts/bugbot.md",
                        "effort": "high",
                    },
                    {
                        "role": "compliance",
                        "model": resolved["verify"].value,
                        "adapter": "claude",
                        "prompt_template": "prompts/compliance.md",
                        "effort": "high",
                    },
                ],
            },
        ],
    }


def render_bounded_recheck(
    resolved: dict[str, ResolvedModel] | None = None, *, workdir: str | Path | None = None
) -> dict:
    """Bounded recheck pipeline -- Sonnet(-tier)+OCR, confirm/regression only.

    Used when reasona_dev.cycle_gate.recheck_route() returns "BOUNDED"
    (fix_files subset of finding_files). The reviewer's own text-contract
    prompt (finding_adapter.py) is what actually narrows the task to
    confirmation + regression -- this pipeline just points the reviewer
    role at the resolved `recheck` model instead of `review`.
    """
    resolved = resolved if resolved is not None else resolve_all(workdir=workdir)
    pipeline = render(resolved)
    pipeline["name"] = "reasona-dev-bounded-recheck"
    pipeline["stages"][0]["agents"][0]["model"] = resolved["recheck"].value
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
) -> None:
    pipeline = (
        render_bounded_recheck(resolved, workdir=workdir)
        if bounded
        else render(resolved, workdir=workdir)
    )
    Path(out_path).write_text(yaml.safe_dump(pipeline, sort_keys=False, allow_unicode=True))
