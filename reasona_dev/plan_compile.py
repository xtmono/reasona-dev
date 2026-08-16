"""Compile a reasona-plan/dev-ralf-style plan document into Bernstein plan.yaml.

Ports `dev-ralf/tools/parse_plan.py`'s PR-unit extraction. Verified against
the real schema in the installed Bernstein 3.15.1 package
(`core/planning/plan_schema.py`):

  plan.yaml:
    name: str
    description: str
    stages:
      - name: str            # unique
        depends_on: [str]    # stage names
        steps:
          - title: str
            role: str
            files: [str]
            completion_signals: [...]

`stages[].steps[]` run IN PARALLEL within a stage; there is no native
"sequential fix loop" construct at the plan-file level. This confirms the
architecture decision made in this design track: one PR unit compiles to
one stage with one "implement" step; the develop -> verify -> fix ->
recheck loop is NOT pre-declared here -- it is driven at runtime by
`reasona_dev.cycle_gate.evaluate()` inside the `on_pre_task_create` hook,
which spawns follow-up tasks as findings demand.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import yaml

_PR_HEADING_RE = re.compile(r"^## PR (?P<index>[\w.]+):\s*(?P<title>.+)$", re.MULTILINE)
_TYPE_RE = re.compile(r"^type:\s*(\w+)\s*$", re.MULTILINE)
_DEPENDS_RE = re.compile(r"^depends_on:\s*(.+)$", re.MULTILINE)
_FILES_TOKEN_RE = re.compile(
    r"(?:crates|src|docs|agent|config|tests|examples|scripts|\.github)/[\w./-]+"
    r"\.(?:rs|md|toml|yaml|yml|json|sh|py)"
)


@dataclass
class PRUnit:
    index: str
    title: str
    depends_on: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    section: str = ""


def parse_plan_units(plan_text: str) -> list[PRUnit]:
    """Fallback extraction (no YAML frontmatter manifest) -- dev-ralf's
    `parse_plan.py` fast path (manifest-driven) is preferred when the plan
    carries one; this covers the same fallback spec dev-ralf documents for
    plans without a manifest.
    """
    headings = list(_PR_HEADING_RE.finditer(plan_text))
    units: list[PRUnit] = []
    for i, m in enumerate(headings):
        start = m.end()
        end = headings[i + 1].start() if i + 1 < len(headings) else len(plan_text)
        section = plan_text[start:end]

        depends_match = _DEPENDS_RE.search(section)
        depends_on = []
        if depends_match:
            raw = depends_match.group(1).strip()
            if raw.lower() != "none":
                depends_on = [d.strip() for d in raw.split(",") if d.strip()]

        files = sorted(set(_FILES_TOKEN_RE.findall(section)))

        units.append(
            PRUnit(
                index=m.group("index"),
                title=m.group("title").strip(),
                depends_on=depends_on,
                files=files,
                section=section.strip(),
            )
        )
    if not units:
        # No PR markers -- single PR, matches dev-ralf's documented fallback.
        units.append(PRUnit(index="1", title="single unit", section=plan_text.strip()))
    return units


def _stage_name(pr_index: str) -> str:
    return f"pr-{pr_index}"


def compile_to_bernstein_plan(
    plan_text: str,
    *,
    plan_name: str,
    description: str,
    dev_role: str = "backend",
    dev_model: str | None = None,
) -> dict:
    """Return a dict matching Bernstein's plan.yaml schema (validated shape).

    One stage per PR unit. `depends_on` wires stage names directly from the
    plan's declared PR dependencies -- the same DAG dev-ralf's scheduler
    used to compute a ready-set for; here Bernstein's own stage scheduler
    resolves it natively (no ready-set loop needed on our side).
    """
    units = parse_plan_units(plan_text)

    stages = []
    for u in units:
        stage_name = _stage_name(u.index)
        # Convention: the review-pipeline run for this stage writes its
        # merged, canonical ReviewResult here before the janitor evaluates
        # this signal. See reasona_dev/gate_check.py -- confirmed against
        # installed Bernstein source that `test_passes` (a completion
        # signal, NOT a pluggy hook) is the only mechanism that gates
        # whether a task's result proceeds toward PR/merge
        # (docs/ARCHITECTURE.md §3).
        review_result_path = f".reasona/review-{stage_name}.json"
        step: dict = {
            "title": f"PR {u.index}: {u.title}",
            "description": u.section,
            "role": dev_role,
            "completion_signals": [
                {
                    "type": "test_passes",
                    "command": f"python3 -m reasona_dev.gate_check {review_result_path}",
                }
            ],
        }
        if u.files:
            step["files"] = u.files
        if dev_model:
            step["model"] = dev_model
        stage: dict = {
            "name": stage_name,
            "steps": [step],
        }
        if u.depends_on:
            stage["depends_on"] = [_stage_name(d) for d in u.depends_on]
        stages.append(stage)

    return {
        "name": plan_name,
        "description": description,
        "stages": stages,
    }


def write_plan_yaml(plan_text: str, out_path: str, *, plan_name: str, description: str, **kwargs) -> None:
    compiled = compile_to_bernstein_plan(
        plan_text, plan_name=plan_name, description=description, **kwargs
    )
    with open(out_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(compiled, f, sort_keys=False, allow_unicode=True)
