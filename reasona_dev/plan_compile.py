"""Compile a reasona-plan/dev-ralf-style plan document into Bernstein plan.yaml.

Ports `dev-ralf/tools/parse_plan.py`'s PR-unit extraction. Verified against
the real schema in the installed Bernstein 3.15.1 package
(`core/planning/plan_schema.py`):

  plan.yaml:
    name: str
    description: str
    cli: str                 # plan-WIDE adapter -- no per-step override exists (checked
                              # against plan_schema.py's _STEP_SCHEMA: no adapter/cli key there)
    stages:
      - name: str            # unique
        depends_on: [str]    # stage names
        steps:
          - title: str
            role: str
            files: [str]
            model: str
            effort: str
            completion_signals: [...]

`stages[].steps[]` run IN PARALLEL within a stage; there is no native
"sequential fix loop" construct at the plan-file level. This confirms the
architecture decision made in this design track: one PR unit compiles to
one stage with one "implement" step; the develop -> review -> fix ->
recheck loop is NOT pre-declared here -- it is driven at runtime by
`reasona_dev.cycle_gate.evaluate()` inside `reasona_dev.pr_cycle`, which
dispatches follow-up tasks as findings demand.

`dev`'s resolved adapter is written to the plan-level `cli:` key -- the
step schema has `model`/`effort` fields but no per-step adapter override,
so this is the only place in plan.yaml Bernstein lets an adapter be named.
Since this project only ever compiles one role (`dev`) into plan.yaml
steps, a single plan-wide `cli:` is not a loss of expressiveness here.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from reasona_dev.acceptance import AcceptanceCriterion, parse_criteria
from reasona_dev.model_config import ResolvedModel, resolve, write_resolved_config

_PR_HEADING_RE = re.compile(r"^## PR (?P<index>[\w.]+):\s*(?P<title>.+)$", re.MULTILINE)
_TYPE_RE = re.compile(r"^type:\s*(\w+)\s*$", re.MULTILINE)
_DEPENDS_RE = re.compile(r"^depends_on:\s*(.+)$", re.MULTILINE)
_FILES_TOKEN_RE = re.compile(
    r"(?:crates|src|docs|agent|config|tests|examples|scripts|\.github)/[\w./-]+"
    r"\.(?:rs|md|toml|yaml|yml|json|sh|py)"
)
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(?P<body>.*?)\n---\s*(?:\n|\Z)", re.DOTALL)

@dataclass
class PRUnit:
    index: str
    title: str
    depends_on: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    section: str = ""
    unit_type: str | None = None
    profile: str | None = None
    acceptance: list[AcceptanceCriterion] = field(default_factory=list)


class PlanError(ValueError):
    """A plan defect worth refusing to compile.

    Raised only for conditions a human must fix in the plan document
    (malformed acceptance criteria, a manifest/prose mismatch). Anything
    recoverable is a warning instead -- see `compile_to_bernstein_plan`'s
    `warnings` return path.
    """


def _parse_manifest(plan_text: str) -> tuple[dict | None, str]:
    """Split a leading YAML frontmatter manifest from the prose body.

    Returns `(manifest_or_None, body)`. A malformed frontmatter block is
    treated as absent rather than fatal, so a plan that merely opens with a
    `---` horizontal rule still parses through the prose fallback.
    """
    m = _FRONTMATTER_RE.match(plan_text)
    if not m:
        return None, plan_text
    try:
        data = yaml.safe_load(m.group("body"))
    except yaml.YAMLError:
        return None, plan_text
    if not isinstance(data, dict) or not isinstance(data.get("pr_units"), list):
        return None, plan_text
    return data, plan_text[m.end():]


def _sections_by_index(body: str) -> dict[str, str]:
    headings = list(_PR_HEADING_RE.finditer(body))
    out: dict[str, str] = {}
    for i, m in enumerate(headings):
        start = m.end()
        end = headings[i + 1].start() if i + 1 < len(headings) else len(body)
        out[m.group("index")] = body[start:end].strip()
    return out


def parse_manifest_units(plan_text: str) -> tuple[list[PRUnit], list[str]]:
    """Manifest-driven extraction -- the authoritative path when a plan
    carries YAML frontmatter.

    dev-ralf's plan format already declares the manifest "authoritative;
    the scheduler parses ONLY this for structure", but reasona-dev had
    implemented only the prose fallback. Adding the manifest path here is
    also what makes `acceptance:` expressible at all: it is structured data,
    and the prose fallback has nowhere to put it.

    Returns `(units, errors)`. Errors describe plan defects; the caller
    decides whether to refuse.
    """
    manifest, body = _parse_manifest(plan_text)
    if manifest is None:
        return [], []

    sections = _sections_by_index(body)
    units: list[PRUnit] = []
    errors: list[str] = []
    for raw in manifest["pr_units"]:
        if not isinstance(raw, dict):
            errors.append("pr_units entry is not a mapping")
            continue
        index = str(raw.get("index", "")).strip()
        if not index:
            errors.append("pr_units entry has no index")
            continue
        criteria, ac_errors = parse_criteria(raw.get("acceptance"))
        errors.extend(f"PR {index}: {e}" for e in ac_errors)
        depends_on = [str(d).strip() for d in (raw.get("depends_on") or []) if str(d).strip()]
        files = [str(f).strip() for f in (raw.get("files") or []) if str(f).strip()]
        if index not in sections:
            # The 1:1 manifest<->prose invariant the plan format states.
            # A manifest entry with no section means the PR was declared but
            # never specified, and the dev agent would receive an empty brief.
            errors.append(f"PR {index}: manifest entry has no matching '## PR {index}:' section")
        units.append(
            PRUnit(
                index=index,
                title=str(raw.get("title", "")).strip() or f"PR {index}",
                depends_on=depends_on,
                files=files,
                section=sections.get(index, ""),
                unit_type=str(raw["type"]).strip() if raw.get("type") else None,
                profile=str(raw["profile"]).strip() if raw.get("profile") else None,
                acceptance=criteria,
            )
        )
    return units, errors


def parse_plan_units(plan_text: str) -> list[PRUnit]:
    """Manifest first, prose fallback second.

    A plan carrying YAML frontmatter is parsed from it (authoritative, and
    the only path that can carry `acceptance:`); a plan without one falls
    back to `## PR <n>:` heading extraction, the same fallback spec
    dev-ralf documents. Errors from the manifest path are dropped here --
    `parse_manifest_units()` is the entry point for callers that want to
    act on them.
    """
    units, _ = parse_manifest_units(plan_text)
    if units:
        return units

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


def acceptance_path(workdir: str | Path, stage_name: str) -> Path:
    """`<workdir>/.reasona/acceptance-<stage>.json` -- the same `.reasona/`
    convention `review-<stage>.json` already uses, so the driver finds a
    unit's criteria by stage name without needing the plan document again
    at gate time.
    """
    return Path(workdir) / ".reasona" / f"acceptance-{stage_name}.json"


def _write_acceptance_file(workdir: Path, stage_name: str, criteria: list[AcceptanceCriterion]) -> None:
    """Write a unit's criteria, or REMOVE a stale file when it now has none.

    A unit with no declared criteria writes nothing at all -- an empty file
    and an absent one would mean the same thing to the gate, and creating
    `.reasona/` for a plan that declared nothing is a surprising side
    effect. The removal branch matters more than it looks: criteria deleted
    from a plan must stop being enforced, and a lingering file from a
    previous compile would keep gating on a contract the plan no longer
    states.
    """
    path = acceptance_path(workdir, stage_name)
    if not criteria:
        if path.is_file():
            path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "stage_name": stage_name,
                "criteria": [
                    {"id": c.id, "cmd": c.cmd, "expect": c.expect, "pattern": c.pattern,
                     "timeout_s": c.timeout_s}
                    for c in criteria
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


def compile_to_bernstein_plan(
    plan_text: str,
    *,
    plan_name: str,
    description: str,
    dev_role: str = "backend",
    dev_model: ResolvedModel | None = None,
    dev_flag: str | None = None,
    workdir: str | Path | None = None,
    write_audit_trail: bool = True,
    audit_trail_path: str | None = None,
    write_bernstein_yaml: bool = True,
    policy_flags: dict[str, str] | None = None,
    write_acceptance: bool = True,
    strict_plan: bool = True,
    validate_profiles: bool = True,
    only_index: str | None = None,
) -> dict:
    """Return a dict matching Bernstein's plan.yaml schema (validated shape).

    One stage per PR unit. `depends_on` wires stage names directly from the
    plan's declared PR dependencies -- the same DAG dev-ralf's scheduler
    used to compute a ready-set for; here Bernstein's own stage scheduler
    resolves it natively (no ready-set loop needed on our side).

    `only_index`, when given, compiles a plan.yaml carrying ONLY that one PR
    unit's stage (no `depends_on`, since there is nothing else in the
    output for it to depend on). `reasona_dev.orchestrate` uses this to
    dispatch each unit's cycle-0 into that unit's own git worktree,
    one unit at a time, in the dependency order it already enforces at the
    Python level -- rather than handing Bernstein a whole-plan multi-stage
    DAG to run unattended, which is what forced every unit's cycle-0 onto
    the SAME shared checkout before any unit-level worktree could exist
    (see docs/ARCHITECTURE.md §3.11 on why that was wrong). Every other
    side effect of this function (acceptance files, the audit trail, the
    `bernstein.yaml` bootstrap/sync) still runs for the filtered unit only,
    anchored at `workdir` as always -- which the caller points at that
    unit's own worktree, not the top-level repo.

    `dev_model` defaults to `reasona_dev.model_config.resolve("dev")` --
    the same flag > env var (`REASONA_DEV_DEV_MODEL`) > default chain
    dev-ralf used, so every generated step always carries a concrete,
    dev-ralf-faithful `model:` (never left for BanditRouter to guess at --
    docs/ARCHITECTURE.md §3.1/§3.5). Passing a `ResolvedModel` explicitly
    is for tests; production callers should resolve once via
    `model_config.resolve_all()` and pass the `"dev"` entry through, so the
    CONDUCTOR-COLLAPSE audit trail reflects the same values actually used
    everywhere else in the pipeline.

    `workdir` is the TARGET repository this plan runs against -- never
    reasona-dev's own install location. reasona-dev has no "home repo" once
    deployed (installed as a package, like `bernstein` itself, invoked
    against an arbitrary caller-supplied repo); the only stable filesystem
    anchor at compile time is whatever repo the operator points this at.
    Defaults to `Path.cwd()`, matching Bernstein's own convention (`bernstein
    run` with no `--workdir` uses the invoking CWD as the project root --
    `orch._workdir`).

    `audit_trail_path`, when not given explicitly, resolves to
    `<workdir>/.reasona/model_config.json` -- anchored to the SAME `workdir`
    that Bernstein's janitor uses to evaluate `completion_signals`
    (confirmed: `core/tasks/task_lifecycle.py`'s `_verify_via_janitor` runs
    every `test_passes` check with `cwd=orch._workdir`, a single fixed
    project root, not a per-task worktree). Anchoring both to the same
    `workdir` is what keeps every `.reasona/` artifact -- this audit trail,
    `acceptance-<stage>.json`, `cycles.jsonl` -- landing in the same place
    regardless of where the compile step happens to be invoked from.

    `write_bernstein_yaml` (default True) does two things, both in
    `bernstein_config.py`: (1) copies a local-or-global template into
    `<workdir>/.bernstein/bernstein.yaml` when the target repo has no seed
    file Bernstein would find on its own (checked at BOTH real locations
    it reads -- `.bernstein/bernstein.yaml` and the legacy repo-root
    `bernstein.yaml`); never overwrites either. (2) patches whichever file
    was found/created's `role_model_policy.<role>.provider` values in
    place (comments untouched) to match what `model_config.resolve_all()`
    resolves right now -- every `compile-plan` run, not just the first, so
    a role's adapter changing later (e.g. in `~/.reasona/reasona.yaml`)
    doesn't require a matching hand-edit here too.

    `policy_flags` is the flag layer for that same sync -- role -> value
    (dev-ralf's own `tool:model:effort` shape or a bare model name, same as
    every other flag in this project), e.g. `{"bugbot": "codex:o1:max"}`.
    Only affects the `role_model_policy` patch; `dev`'s own step-level
    `model:`/`effort:`/plan-level `cli:` are controlled by `dev_flag`/
    `dev_model` as before, independently of this.
    """
    workdir = Path(workdir) if workdir is not None else Path.cwd()

    if audit_trail_path is None:
        audit_trail_path = str(workdir / ".reasona" / "model_config.json")

    if dev_model is not None:
        resolved_dev = dev_model
    else:
        from reasona_dev import config_file

        resolved_dev = resolve(
            "dev",
            flag=dev_flag,
            project_cfg=config_file.load_project(workdir),
            global_cfg=config_file.load_global(),
        )
    if write_audit_trail:
        write_resolved_config({"dev": resolved_dev}, audit_trail_path)

    if write_bernstein_yaml:
        from reasona_dev.bernstein_config import ensure_bernstein_yaml, sync_role_model_policy
        from reasona_dev.model_config import resolve_all

        bernstein_yaml_path = ensure_bernstein_yaml(workdir)
        # `policy_flags` carries the flag > env var > project cfg > global
        # cfg chain's TOP layer through to review/recheck/bugbot/compliance/
        # final_audit -- without this, resolve_all() here would silently
        # skip the flag layer for every role except dev (a real gap found
        # by inspection: this call used to omit `flags=` entirely).
        # resolve_all()'s own "dev" entry is then overwritten by
        # `resolved_dev` -- a test/caller-supplied `dev_model` override (or
        # `dev_flag`) must win here too, same as it wins on the plan step
        # itself below.
        if bernstein_yaml_path is not None:
            policy_resolved = resolve_all(workdir=workdir, flags=policy_flags)
            policy_resolved["dev"] = resolved_dev
            sync_role_model_policy(bernstein_yaml_path, policy_resolved)

    manifest_units, manifest_errors = parse_manifest_units(plan_text)
    if manifest_errors and strict_plan:
        raise PlanError(
            "plan has "
            + str(len(manifest_errors))
            + " defect(s):\n  - "
            + "\n  - ".join(manifest_errors)
        )
    units = manifest_units or parse_plan_units(plan_text)

    if only_index is not None:
        units = [u for u in units if u.index == only_index]
        if not units:
            raise PlanError(f"only_index {only_index!r} does not match any PR unit in this plan")

    # Resolve every unit's profile now, at compile time, rather than at
    # dispatch. A unit whose files span two profiles is a plan defect, and a
    # plan defect should surface while the author still has the plan open --
    # not an hour into a run when `pr_cycle` reaches that stage.
    if validate_profiles:
        from reasona_dev import config_file
        from reasona_dev.prompt_profile import ProfileConflict, resolve_unit_profile

        project_cfg = config_file.load_project(workdir)
        global_cfg = config_file.load_global()
        conflicts: list[str] = []
        for u in units:
            try:
                resolve_unit_profile(
                    files=u.files, unit_profile=u.profile, unit_index=u.index,
                    project_cfg=project_cfg, global_cfg=global_cfg,
                )
            except ProfileConflict as exc:
                conflicts.append(str(exc))
        if conflicts:
            raise PlanError("\n\n".join(conflicts))

    # Acceptance criteria are consumed by the DRIVER before merge, not by
    # Bernstein's janitor (see reasona_dev/acceptance.py on why), so they are
    # written beside the other `.reasona/` runtime artifacts rather than
    # embedded in the plan step.
    if write_acceptance:
        for u in units:
            _write_acceptance_file(workdir, _stage_name(u.index), u.acceptance)

    stages = []
    for u in units:
        stage_name = _stage_name(u.index)
        # **No completion_signals on the dev step.** This used to carry
        # a `test_passes` signal reading a review verdict file, from a design
        # where that verdict was the merge gate. Two facts, both since
        # confirmed against the installed Bernstein source, make it unworkable:
        #
        #   1. The file does not exist yet. Review runs AFTER this step, in
        #      `pr_cycle`, so the janitor read a missing path, exited
        #      non-zero, and failed the dev task on every first attempt --
        #      which then hit Bernstein's retry path, respawning the agent
        #      and escalating its model on the second retry. A signal that
        #      cannot pass is worse than no signal: it converts every unit
        #      into the exact credit burn docs/ARCHITECTURE.md §3.6 is about.
        #   2. Even a signal about the dev step's OWN output could not work
        #      here. Signals are evaluated at `orch._workdir` -- one fixed
        #      project root -- and BEFORE the agent's branch is merged
        #      (`task_lifecycle.py`: janitor at :4055, merge at :3061 inside
        #      `_reap_and_cleanup_session`). The code being verified is not
        #      in the tree the command runs against.
        #
        # So gating lives entirely in `reasona_dev.ship_gate`, which runs
        # after the merge, in a checkout the driver controls. With no
        # signals Bernstein auto-completes the task on the agent's git
        # commits, which is the honest amount of verification available at
        # that point.
        step: dict = {
            "title": f"PR {u.index}: {u.title}",
            "description": u.section,
            "role": dev_role,
        }
        if u.files:
            step["files"] = u.files
        step["model"] = resolved_dev.model
        step["effort"] = resolved_dev.effort
        stage: dict = {
            "name": stage_name,
            "steps": [step],
        }
        # `only_index` compiles a single-stage plan.yaml -- any `depends_on`
        # would reference a stage name that does not exist in `stages`
        # (the dependency's own unit was filtered out). The dependency order
        # is already enforced one level up, by `orchestrate.py` running
        # units sequentially -- see this function's own docstring.
        if u.depends_on and only_index is None:
            stage["depends_on"] = [_stage_name(d) for d in u.depends_on]
        stages.append(stage)

    return {
        "name": plan_name,
        "description": description,
        "cli": resolved_dev.adapter,
        "stages": stages,
    }


def write_plan_yaml(plan_text: str, out_path: str, *, plan_name: str, description: str, **kwargs) -> None:
    compiled = compile_to_bernstein_plan(
        plan_text, plan_name=plan_name, description=description, **kwargs
    )
    with open(out_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(compiled, f, sort_keys=False, allow_unicode=True)
