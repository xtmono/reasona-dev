"""Drives one PR unit through dev-ralf's actual cycle -- worker.md's
"Pipeline you run": ``develop -> review (max 8 cycles) -> bug+compliance
scan (parallel, max 8 cycles)``. `reasona_dev.ship_gate` and
`reasona_dev.final_phase` pick up after this returns.

**Why this exists instead of a bigger `plan.yaml`.** The number of review
cycles, whether bugbot+compliance even run, and whether a MUST_FIX
recurrence earns a bounded model escalation are all facts only known AFTER a
role's actual output is parsed -- this cannot be predeclared as a fixed
`plan.yaml` DAG (docs/ARCHITECTURE.md §1). Bernstein can create follow-up
tasks from inside a run (`POST /tasks/self-create`), but that puts the
decision in the AGENT, which is the one thing this project exists to avoid:
disposition comes from section membership, and budget/recurrence/convergence
from `cycle_gate`, with no model in the loop.

So the driver runs OUTSIDE Bernstein, one level up. Each role dispatch is a
one-step `plan.yaml` executed by `bernstein run` -- the same CLI surface an
operator types by hand -- and the agent's structured output comes back
through a file the step's description tells it to write. See
`reasona_dev.bernstein_dispatch` for why this is a batch call rather than
the HTTP dispatch it briefly was.

**Two budget mechanisms, two different failure modes.** The 8/8/16 cycle
caps inherited from dev-ralf are a ceiling, not a cost; what actually spends
is how expensive each cycle is and how many run before a doomed PR gives up:

- *Cost per cycle* -- `cycle_gate.recheck_route()`. When a dev fix touched
  only files already named in the findings it was fixing, the next pass is a
  BOUNDED recheck (confirm + regression, `recheck.md`, the cheaper `recheck`
  model) instead of a full omission hunt. Scan roles get the same treatment
  through a scope suffix rather than a separate prompt, since their prompts
  come from an external skill contract.
- *Number of cycles* -- `cycle_gate.ConvergenceTracker`. `RecurrenceTracker`
  only fires when the SAME finding survives, so a PR emitting fresh findings
  every cycle used to burn the whole stage cap. Non-convergence now exits at
  3 cycles, and a stuck INCONCLUSIVE role at 3 attempts.

Which rule ended a unit is recorded by `cycles_log`, so the caps can
eventually be re-derived from measurement instead of inherited on faith.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from reasona_dev import ci_gate, config_file, cycles_log, ledger, memory
from reasona_dev.bernstein_dispatch import (
    DEFAULT_ROLE_SCOPE,
    SINGLE_STEP_TASK_ID,
    run_plan_file,
    write_role_plan,
)
from reasona_dev.cycle_gate import (
    ConvergenceTracker,
    FixBudget,
    RecurrenceTracker,
    evaluate,
    recheck_route,
)
from reasona_dev.finding_adapter import (
    Disposition,
    Finding,
    ReviewResult,
    RoleStatus,
    convergent_keys,
    merge,
    parse_role_output,
)
from reasona_dev.model_config import ResolvedModel
from reasona_dev.prompt_profile import available_profiles, resolve_prompt

# The wire shape is a property of the PROMPT, not the role -- see
# `finding_adapter.parse_role_output()`. This module used to keep a
# role -> parser table (`bugbot`/`compliance` -> KV), which was correct only
# for a profile that delegates those roles to dev-ralf's external skills.
# This project's own `rust-dev` profile asks all of them for the `||` text
# contract, and the table turned that valid output into a hard scan abort.


@dataclass
class RoleRunResult:
    role: str
    cycle: int
    review_result: ReviewResult
    raw_output_path: Path
    error_detail: str | None = None


@dataclass
class CycleResult:
    verdict: str  # "PASS" | "PASS_WITH_NOTES" | "FAIL" | "ABORT" | "INCONCLUSIVE"
    stage: str  # "review" | "scan" -- which stage produced the terminal verdict
    reason: str
    review_cycles: int = 0
    scan_cycles: int = 0
    role_results: list[RoleRunResult] = field(default_factory=list)
    # Carried out so the merge tail can continue the SAME budget and
    # recurrence state rather than starting fresh. A final audit that got its
    # own budget would let a PR spend 8+8+2 fix cycles while every stage
    # reports itself within cap, and a fresh RecurrenceTracker would forget
    # that a finding the audit raises had already survived a fix earlier.
    budget: FixBudget | None = None
    recurrence: RecurrenceTracker | None = None


def _build_role_description(prompt: str, raw_output_path: Path) -> str:
    """Appends the file-handoff instruction to a role's prompt -- unchanged
    from the old plan-step description, just no longer embedded in a YAML
    file (see module docstring) -- plus a commit-message stamp instruction
    that closes a gap `owned_files` alone cannot (see `run_role()`'s
    `files` parameter docstring and `docs/ARCHITECTURE.md` §3.21 for the
    full incident).

    **Why a commit-trailer stamp, on top of `owned_files`.** Bernstein's
    janitor attributes a completed task's work FIRST by `git log
    --grep=<task_id>` (any file, no scope restriction) and only falls back
    to a `git diff` scoped to `owned_files` (restricted to those declared
    paths) when no commit matches. `owned_files` only helps when the
    agent's actual diff stays inside the unit's declared manifest files --
    an agent that (incorrectly) edits a file outside that scope defeats the
    fallback the exact same way the original incident happened, no matter
    how accurately `owned_files` was populated. Stamping the task id
    directly into the commit message makes attribution succeed via the
    PRIMARY path instead, independent of which files were actually
    touched -- the only fix that closes the gap regardless of whether the
    agent stayed in scope.
    """
    return (
        f"{prompt}\n\n"
        "---\n"
        "When you are completely done, write your ENTIRE output above "
        "(the markdown report AND the RESULT block, verbatim, nothing "
        f"added or removed) to the file `{raw_output_path}` as your "
        "final action, then stop.\n\n"
        "If this task involves making a git commit, add this exact line as "
        "its own line in the commit message (a trailer, after the subject "
        f"and body): `Bernstein-Task: {SINGLE_STEP_TASK_ID}` -- required so "
        "this dispatch's work is correctly attributed and merged."
    )


def run_role(
    *,
    workdir: Path,
    role: str,
    title: str,
    prompt: str,
    model: ResolvedModel,
    rundir: Path,
    cycle: int,
    port: int = 8052,
    scope: str = DEFAULT_ROLE_SCOPE,
    label: str | None = None,
    files: list[str] | None = None,
) -> RoleRunResult:
    """Dispatch one role once via `bernstein run` on a one-step plan.

    Synchronous: the run drives the agent to completion and exits, so there
    is no polling and no server lifetime to own (see
    `reasona_dev.bernstein_dispatch` on why this is a batch call rather than
    an HTTP dispatch).

    Returns `role_status=ERROR` -- never a silent PASS -- when the agent's
    output file does not appear, matching worker.md -> *RESULT parsing*:
    "Missing block ... -> cycle FAIL", not "treat as clean". The run's exit
    code and stderr tail are carried in `error_detail` so a turn-budget death
    and a misconfigured adapter are distinguishable; `cycle_gate` collapses
    both into the same abort, which is the right gate decision and a useless
    diagnostic on its own.

    `label`, when given, distinguishes this dispatch's raw-output/plan
    filenames and its `RoleRunResult.role` (so `cycles_log` records each
    reviewer separately) from another dispatch made under the SAME `role`
    at the SAME `cycle` -- needed when the review stage runs multiple
    independent reviewers (`role="reviewer"` for all of them) in one cycle.
    `role` itself is unchanged and still goes out on the wire -- it is what
    Bernstein's task server checks against its role whitelist
    (`bernstein_config.py`'s `role_model_policy`), and every reviewer, no
    matter how many, is legitimately the `"reviewer"` role.
    """
    # ABSOLUTE, always. The agent runs inside a per-task git worktree, so a
    # relative path in its instructions resolves against THAT tree, not the
    # project root the driver reads from. Live symptom when this was
    # relative: the agent wrote into its own worktree, spent its remaining
    # turns hunting for the file the driver was asking about, and died on
    # `error_max_turns` while the driver reported ERROR.
    rundir = rundir.resolve()
    file_key = label or role
    raw_output_path = rundir / f"{file_key}-c{cycle}.raw.txt"
    plan_path = rundir / f"{file_key}-c{cycle}.plan.yaml"
    rundir.mkdir(parents=True, exist_ok=True)
    if raw_output_path.exists():
        raw_output_path.unlink()

    write_role_plan(
        path=plan_path, role=role, title=title,
        description=_build_role_description(prompt, raw_output_path),
        model=model.model, effort=model.effort, cli=model.adapter,
        scope=scope, files=files,
    )
    dispatch = run_plan_file(plan_path, workdir, port=port)

    if not raw_output_path.is_file():
        return RoleRunResult(
            role=file_key, cycle=cycle,
            review_result=ReviewResult(role_status=RoleStatus.ERROR),
            raw_output_path=raw_output_path,
            error_detail=(
                f"no output at {raw_output_path.name}; "
                f"bernstein run exit={dispatch.returncode} "
                f"stderr={dispatch.stderr_tail[:200]!r}"
            ),
        )

    text = raw_output_path.read_text(encoding="utf-8")
    # `dispatch.ok` is diagnostic-only here, deliberately not gating on it
    # the way a missing output file does: `bernstein run`'s own "spawn,
    # execute, merge" sequence (`bernstein_dispatch.run_plan_file()`'s
    # docstring) can write the role's real output before a LATER step in
    # that same run (the agent-worktree-to-unit-branch merge) fails, so a
    # non-zero exit here does not necessarily mean the output text itself
    # is untrustworthy -- but it is exactly the kind of signal a real
    # incident showed gets silently discarded otherwise (TAS plan 49 PR2,
    # 2026-08-22: the dev-fix agent's commit never reached the unit branch,
    # `cycle_gate.recheck_route()`'s own fix closes the routing half of
    # that incident; this half makes the underlying dispatch failure
    # visible in `cycles_log` instead of a clean-looking dispatch that
    # quietly produced no code change). Recorded, never raised or
    # re-classified as ERROR -- the parsed `review_result` is still
    # returned as-is.
    error_detail = None if dispatch.ok else (
        f"bernstein run exit={dispatch.returncode} despite producing output; "
        f"stderr={dispatch.stderr_tail[:200]!r}"
    )
    return RoleRunResult(
        role=file_key, cycle=cycle,
        review_result=parse_role_output(text), raw_output_path=raw_output_path,
        error_detail=error_detail,
    )


def _render_findings(findings) -> list[str]:
    """One block per finding with every evidence field verbatim -- shared by
    the dev fix prompt and the bounded recheck prompt so both name findings
    the same way, and neither summarizes.
    """
    lines: list[str] = []
    for f in findings:
        loc = f.path + (f":{f.line}" if f.line else "") + (f" {f.symbol}" if f.symbol else "")
        lines.append(f"- [{f.severity.value if f.severity else '?'}] {loc}")
        if f.contract:
            lines.append(f"  || contract: {f.contract}")
        if f.scenario:
            lines.append(f"  || scenario: {f.scenario}")
        if f.fix:
            lines.append(f"  || fix: {f.fix}")
        if f.note:
            lines.append(f"  || note: {f.note}")
        lines.append("")
    return lines


def _build_fix_prompt(pr_title: str, findings) -> str:
    """worker.md -> *Loop control*: "dispatch dev (fix-cycle $DEV_PROMPT +
    `must_fix` list, `contract`/`scenario`/`fix` fields included
    verbatim)". Every field is passed through unedited -- this driver
    never rewrites or summarizes a finding on the way to dev.
    """
    return "\n".join(
        [
            f"Fix the following MUST_FIX findings from review on {pr_title}. "
            "Do not address anything not listed here.",
            "",
            *_render_findings(findings),
        ]
    )


def _pr_unit_context_block(
    *, pr_index: str | None, pr_title: str, workdir: Path, section: str | None,
    files: list[str] | None = None,
) -> str:
    """The plan's own `## PR <N>:` section, plus the unit/worktree identity
    every packaged prompt's closing `[Current PR unit]:`/`[Worktree]:` line
    already asks for -- appended to a role's prompt the same way
    `memory_block` is (a fixed-shape block the role reads, not a
    placeholder substituted into the template text, so it works
    identically against a customized profile that never had dev-ralf's
    `<N>`/`<worktree_path>` markers to begin with).

    `section` is `None` when a unit has no manifest entry to point at
    (defensive -- `plan_compile.py` always sets `PRUnit.section` for a
    validated plan) or when this dispatch is a bounded recheck confirming
    findings rather than a full omission hunt; in either case the block
    still names the unit and worktree, just without the prose to
    cross-check against.

    `files` is the manifest's OWN `files:` list (`plan_compile.PRUnit.files`)
    -- printed explicitly and separately from `section`'s prose, because the
    manifest entry lives in the plan's YAML frontmatter, not necessarily
    repeated inside the `## PR <N>:` prose body a role reads. A role that has
    to re-derive "what is this unit even allowed to touch" from prose alone
    can miss it; a real incident did (a diff edited a file declared by a
    DIFFERENT unit's manifest entry, and internal review/scan never flagged
    it -- `compliance.md`'s poc-scope check item consumes this list).
    """
    lines = [
        "\n\n---",
        f"[Current PR unit]: PR {pr_index or '?'} -- {pr_title}",
        f"[Worktree]: {workdir}",
    ]
    if files:
        lines.append(
            "\n[Manifest files for this PR unit] (ONLY these are in scope -- a diff file "
            "outside this list belongs to a DIFFERENT unit's manifest entry, not this one):"
        )
        lines.extend(f"- {f}" for f in files)
    if section and section.strip():
        lines.append(
            "\nThe plan's own `## PR " + (pr_index or "<N>") + ":` section for THIS unit "
            "(authoritative -- cross-check every checklist item and every named file/symbol "
            "against THIS worktree's actual diff):\n"
        )
        lines.append(section.strip())
    return "\n".join(lines)


def _build_recheck_prompt(recheck_profile_prompt: str, findings) -> str:
    """`recheck.md` + the exact findings to confirm.

    The prompt file states the BOUNDED contract (confirm + regression, no
    fresh omission hunt); this appends which findings are being confirmed,
    with the same verbatim evidence the fix dispatch received, so the
    recheck is judged against the same stated contract the fix was.
    """
    return "\n".join(
        [
            recheck_profile_prompt,
            "",
            "---",
            "Findings to CONFIRM (these are what the fix claimed to address):",
            "",
            *_render_findings(findings),
        ]
    )


def _build_evidence_correction_prompt(finding: Finding) -> str:
    """worker.md -> *Incomplete evidence*: a MUST_FIX missing contract/
    scenario/fix is never silently downgraded to ADVISORY -- instead the
    reviewer gets ONE chance to restate it complete, or say explicitly it
    cannot supply a concrete failure scenario. Restating does not change
    the finding's disposition either way; `_correct_incomplete_evidence()`
    below enforces that regardless of what this reply says.
    """
    loc = finding.path + (f":{finding.line}" if finding.line else "") + (f" {finding.symbol}" if finding.symbol else "")
    severity = finding.severity.value if finding.severity else "HIGH"
    return "\n".join(
        [
            "Your prior review reported this MUST_FIX finding without a complete "
            "contract/scenario/fix:",
            "",
            f"- [{severity}] {loc}",
            f"  || contract: {finding.contract or '(missing)'}",
            f"  || scenario: {finding.scenario or '(missing)'}",
            f"  || fix: {finding.fix or '(missing)'}",
            "",
            "Restate this ONE finding with a complete contract/scenario/fix if you "
            "can supply one, or state explicitly that you cannot supply a concrete "
            "failure scenario. This does not change the finding's disposition -- it "
            "stays MUST_FIX either way.",
            "",
            "Reply with EXACTLY one MUST_FIX bullet in this shape, nothing else:",
            "",
            "MUST_FIX:",
            f"- [{severity}] {loc}",
            "  || contract: <the requirement or invariant this violates>",
            "  || scenario: <a concrete input/state that reproduces the failure>",
            "  || fix: <the minimal correct change>",
        ]
    )


def _correct_incomplete_evidence(
    *,
    workdir: Path,
    pr_title: str,
    role: str,
    findings: list[Finding],
    model: ResolvedModel,
    rundir: Path,
    cycle: int,
    port: int,
    run_role_fn,
) -> list[RoleRunResult]:
    """worker.md -> *Incomplete evidence*: once per MUST_FIX finding missing
    contract/scenario/fix, re-query before this cycle's findings ever reach
    a dev-fix or recheck prompt. Mutates `findings` in place; NEVER changes
    a disposition -- a MUST_FIX stays MUST_FIX even when the reply says it
    cannot supply evidence, only `contract`/`scenario`/`fix`/
    `contract_incomplete` are updated. Not a re-review: does not consume a
    review/scan cycle or an INCONCLUSIVE retry -- the caller never routes
    this through `evaluate()`.

    dev-ralf re-queries the SAME reviewer SESSION (a narrow follow-up on
    the same conversation). This project's dispatch layer has no
    session-continuation concept at all (`bernstein_dispatch.py` -- every
    `run_role()` call is an independent one-shot `bernstein run`), so this
    is a FRESH dispatch on the same model instead -- the closest available
    approximation, not a literal session resume.
    """
    dispatches: list[RoleRunResult] = []
    idx = 0
    for finding in findings:
        if finding.disposition is not Disposition.MUST_FIX or not finding.contract_incomplete:
            continue
        idx += 1
        result = run_role_fn(
            workdir=workdir, role=role, label=f"{role}-evidence-correction-{idx}",
            title=f"{pr_title} -- evidence correction c{cycle} [{idx}]",
            prompt=_build_evidence_correction_prompt(finding),
            model=model, rundir=rundir, cycle=cycle, port=port,
        )
        dispatches.append(result)
        corrected = next(
            (f for f in result.review_result.findings if f.disposition is Disposition.MUST_FIX),
            None,
        )
        if corrected is not None and corrected.contract and corrected.scenario and corrected.fix:
            finding.contract = corrected.contract
            finding.scenario = corrected.scenario
            finding.fix = corrected.fix
            finding.contract_incomplete = False
        # else: leave the original finding exactly as it was -- still
        # MUST_FIX, still contract_incomplete -- dispatched to dev as-is.
    return dispatches


def _bounded_scope_suffix(fix_files: set[str]) -> str:
    """Scope restriction for scan roles on a BOUNDED cycle.

    bugbot/compliance prompts come from an external skill contract, so
    they get a suffix rather than a separate bounded prompt file the way
    review does. The saving is the same: the role re-examines only what the
    fix touched instead of re-scanning the entire diff.
    """
    listed = "\n".join(f"- {p}" for p in sorted(fix_files))
    return (
        "\n\n---\n"
        "SCOPE RESTRICTION for this cycle: the previous fix touched only the "
        "files listed below, and every other file is unchanged since your "
        "last scan of it. Examine ONLY these files; do not re-report findings "
        "elsewhere.\n\n"
        f"{listed}\n"
    )


def _head_sha(workdir: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(workdir), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None
    return out.stdout.strip() or None


def _changed_files(workdir: Path, pre_fix_head: str) -> set[str]:
    try:
        out = subprocess.run(
            ["git", "-C", str(workdir), "diff", "--name-only", f"{pre_fix_head}..HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return set()
    return {line.strip() for line in out.splitlines() if line.strip()}


def _safe_recheck_route(workdir: Path, pre_fix_head: str | None, finding_files: set[str]) -> str:
    """`recheck_route()` with a conservative failure mode.

    Any condition that makes the routing question unanswerable -- no git
    repo, an unknown pre-fix HEAD, a failed diff -- returns FULL. Bounding a
    recheck is an optimization; doing it on an unverified premise would
    narrow the review's scope without evidence that narrowing is safe, which
    is the one direction this pipeline must never guess in.
    """
    if pre_fix_head is None or not finding_files:
        return "FULL"
    try:
        return recheck_route(str(workdir), pre_fix_head, finding_files)
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return "FULL"


def _finding_files(findings) -> set[str]:
    return {f.path for f in findings if f.path}


# worker.md's Bug + compliance scan: "no source path in pr_files
# (docs/config-only: .md/.toml/.yaml/.json)" -- the exact set it names,
# not a broader "anything non-source" guess.
_DOCS_ONLY_EXTENSIONS = frozenset({".md", ".toml", ".yaml", ".json"})


def _is_docs_only(files: list[str] | None) -> bool:
    """True only when EVERY one of the unit's own declared `files:` has a
    docs/config extension. No declared files at all is NOT docs-only --
    that just means the plan omitted the metadata, not that the unit is
    known to touch nothing but docs, so bugbot still runs (the safe
    default: dispatching an unnecessary scan costs a cycle, skipping a
    needed one costs a missed defect).
    """
    if not files:
        return False
    return all(Path(f).suffix.lower() in _DOCS_ONLY_EXTENSIONS for f in files)


def _run_dev_fix(
    *,
    workdir: Path,
    pr_title: str,
    findings,
    dev_model: ResolvedModel,
    escalated_model: str | None,
    rundir: Path,
    cycle: int,
    run_role_fn,
    port: int = 8052,
    pre_fix_head: str | None = None,
    ci_fast_command: str | None = None,
    files: list[str] | None = None,
) -> RoleRunResult:
    """Dispatch one dev fix-cycle. `escalated_model` (from
    `GateDecision.escalated_model`) overrides `dev_model.model` for exactly
    this dispatch when set -- the bounded, logged, one-time escalation
    `cycle_gate.evaluate()` already decided on, never a silent swap.

    **B-5**: when `ci_fast_command` is configured (`reasona_dev.ci_gate`),
    run it right after the fix commits and revert to `pre_fix_head` on
    failure -- a fix that does not even compile must not survive into the
    next cycle's diff. `pre_fix_head` MUST be captured by the caller
    BEFORE this dispatch (the pattern every call site already follows for
    `_safe_recheck_route()`'s own diffing) -- this function never computes
    it itself, so a caller that omits it simply gets no revert (the same
    "unconfigured, no-op" default `ci_fast_command=None` gets). A CI
    failure is recorded on `RoleRunResult.error_detail` (surfaced via the
    same `cycles_log.record_dispatch()` path any other dispatch error
    already reaches) rather than changing this function's return shape --
    the caller's own `_safe_recheck_route()` diff against the (now
    reverted) HEAD already reports "nothing changed" on its own, which is
    the correct downstream signal.
    """
    model = dev_model if escalated_model is None else ResolvedModel(
        role="dev", model=escalated_model, adapter=dev_model.adapter,
        effort=dev_model.effort, source="cycle_gate:escalated",
    )
    result = run_role_fn(
        workdir=workdir, role="backend", title=f"{pr_title} -- fix c{cycle}",
        prompt=_build_fix_prompt(pr_title, findings), model=model, rundir=rundir, cycle=cycle,
        port=port, files=files,
    )
    if ci_fast_command:
        ok, tail = ci_gate.run_fast(workdir, ci_fast_command, pre_fix_head=pre_fix_head)
        if not ok:
            note = f"ci-fast failed, reverted to pre-fix HEAD: {tail[-500:]}"
            result.error_detail = f"{result.error_detail}; {note}" if result.error_detail else note
    return result


def _missing_prompt_reason(role: str, profile: str, workdir: Path) -> str:
    """An abort message that says what to do about it.

    Prompts resolve through exactly two layers with nothing packaged
    underneath, so "not configured anywhere" is now a real and reachable
    state rather than an impossible one -- and the bare form of this message
    ("no review prompt for profile 'rust-dev'") cannot be told apart from a
    typo'd profile name. Naming both searched paths and what IS present
    separates those two cases at the point of failure.
    """
    found = available_profiles(workdir)
    if found:
        have = "; ".join(f"{name} ({', '.join(roles)})" for name, roles in found.items())
        detail = f"available: {have}"
    else:
        detail = "no prompt profile found in either layer"
    return (
        f"no {role} prompt for profile {profile!r} -- searched "
        f"{workdir}/.reasona/prompts/{profile}/{role}.md then "
        f"~/.reasona/prompts/{profile}/{role}.md; {detail}"
    )


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "pr"


def run_pr_cycle(
    *,
    workdir: str | Path,
    repo_workdir: str | Path | None = None,
    pr_title: str,
    resolved: dict[str, ResolvedModel],
    rundir: str | Path,
    profile: str,
    port: int = 8052,
    stage_name: str | None = None,
    files: list[str] | None = None,
    plan_name: str | None = None,
    resume: bool = False,
    pr_index: str | None = None,
    pr_section: str | None = None,
    run_role_fn=run_role,
) -> CycleResult:
    """develop -> review -> bug+compliance scan, worker.md-faithful.

    Assumes `dev`'s cycle-0 implementation already happened (this driver
    picks up at *Review cycles* -- `orchestrate.py`'s `_process_unit()`
    covers cycle-0 development and its own `$CI_FAST` gate before this
    driver is ever called, not the `completion_signals` mechanism worker.md
    used -- that mechanism was never ported, and was removed for good from
    `plan_compile.py` in an earlier parity pass; see `ci_gate.run_fast()`
    called from `orchestrate.py` for the real gate this docstring used to
    describe inaccurately).

    Each role dispatch is its own `bernstein run`, so there is no server
    lifetime to manage here and no cleanup path to get wrong.

    **`workdir` vs `repo_workdir`.** `workdir` is this unit's own git
    worktree -- everything that has to run AGAINST this unit's actual code
    (role dispatch, `resolve_prompt()`, `config_file.load_project()`) uses
    it. `repo_workdir` is the TOP-LEVEL target repo, used ONLY for
    `cycles_log`/`memory` -- those are meant to accumulate across the
    WHOLE repo's history, across every PR unit that ever ran, not reset
    per unit, and `orchestrate.py` deletes a unit's worktree outright on a
    successful merge (`worktree.remove_unit_worktree()`). Defaulting
    `repo_workdir` to `workdir` when not given keeps every caller that does
    not care about this distinction working exactly as before; production
    callers (`orchestrate.py`) must pass the real top-level repo or the
    exact loss this parameter exists to prevent happens again (see
    `cycles_log.record_dispatch()`'s own docstring for the incident).

    **Resume (`resume=True`, `plan_name` given).** `FixBudget`/
    `RecurrenceTracker`/`ConvergenceTracker` are checkpointed to
    `reasona_dev.ledger` after every review/scan cycle -- a `git`/`gh`
    query can't re-derive them the way `final_phase.py` re-derives its own
    state, since they're pure in-memory bookkeeping with no external
    system of record. A resumed run restores them instead of starting a
    fresh `FixBudget()` at cycle 0, so a unit interrupted mid-review picks
    back up at its next cycle rather than repeating cycles it already
    spent budget on.

    `run_role_fn` is injectable purely for testing -- production callers
    never pass it.

    **`pr_index`/`pr_section`** -- the plan's `## PR <N>: <title>` prose for
    THIS unit (`plan_compile.PRUnit.section`), appended to every dispatched
    reviewer/scanner prompt via `_pr_unit_context_block()`. Every packaged
    prompt (`review.md` item 4, COMPLETENESS) instructs the role to
    "enumerate EVERY checklist item ... named in the plan's `## PR <N>:`
    section" -- without this, a reviewer running inside the unit's own
    worktree has no way to see that section at all (dev-ralf's worker reads
    it via `sed -n '<section_lines>p' "$plan_rel"` against a plan file path
    it was spawned with; this driver has no equivalent path to give an
    agent dispatched through `bernstein run`, so the section text is
    embedded directly instead -- the same choice `plan_compile.py` already
    makes for the dev role's own cycle-0 step, `"description": u.section`).
    Omitting these two leaves the mandate that INCOMPLETE-MERGE (dev-ralf's
    own failure catalog, rationale.md) exists to prevent structurally
    unenforceable: the review prompt asks a question no dispatched agent
    can answer.
    """
    workdir = Path(workdir)
    log_workdir = Path(repo_workdir) if repo_workdir is not None else workdir
    rundir = Path(rundir)
    stage_name = stage_name or _slug(pr_title)

    # B-5: resolved ONCE per cycle, not per fix dispatch -- a plain read of
    # two small YAML files, cheap enough that re-resolving it per dispatch
    # would only be waste, not a correctness concern either way.
    ci_fast_command = config_file.resolve_ci_command(
        "fast", config_file.load_project(workdir), config_file.load_global(),
    )

    progress = ledger.load_progress(log_workdir, plan_name, stage_name) if (resume and plan_name) else None

    recurrence = RecurrenceTracker.from_dict(progress["recurrence"]) if progress else RecurrenceTracker()
    # ONE shared pool across review, scan, final, sync and ship -- worker.md
    # -> *Fix budget*: "review, scan, /gh-pr retries, final-audit, sync, ship
    # fixes are ALL drawn from the same pool." `FixBudget` already tracks all
    # five stages' per-stage caps plus one shared `total_used`; the review
    # and scan loops below both spend against the SAME instance, not two
    # separate `FixBudget()`s, so `MAX_TOTAL_FIX_CYCLES` actually bounds the
    # whole PR the way it is documented to (previously: a `review_budget`
    # that was never merged into `scan_budget` meant the real ceiling was
    # review's 8 cycles PLUS whatever scan/final/sync/ship's own 16-cycle
    # pool spent, up to 24 -- and `final_phase.should_run_final_audit()`
    # read `scan_budget` alone, so a PR with review-only fixes and a clean
    # scan looked exactly like a PR with zero fixes anywhere).
    budget = FixBudget.from_dict(progress["budget"]) if progress else FixBudget()
    review_convergence = (
        ConvergenceTracker.from_dict(progress["review_convergence"]) if progress else ConvergenceTracker()
    )
    scan_convergence = (
        ConvergenceTracker.from_dict(progress["scan_convergence"]) if progress else ConvergenceTracker()
    )
    # Carried across cycles, reset the moment a stage produces a conclusive
    # result. Passing a literal 0 (as this did) makes `evaluate`'s
    # INCONCLUSIVE branch unable to ever reach its own cap.
    review_inconclusive = progress["review_inconclusive"] if progress else 0
    scan_inconclusive = progress["scan_inconclusive"] if progress else 0
    role_results: list[RoleRunResult] = []

    def _checkpoint(**kw) -> None:
        if resume and plan_name:
            ledger.save_progress(log_workdir, plan_name, stage_name, kw)

    def _log(stage: str, cycle: int, result: RoleRunResult, model: ResolvedModel) -> None:
        cycles_log.record_dispatch(
            workdir=workdir, log_workdir=log_workdir, stage_name=stage_name, stage=stage, cycle=cycle,
            role=result.role, model=model.model, adapter=model.adapter,
            result=result.review_result, error_detail=result.error_detail,
        )

    def _log_decision(stage: str, cycle: int, decision) -> None:
        cycles_log.record_decision(
            workdir=workdir, log_workdir=log_workdir, stage_name=stage_name, stage=stage, cycle=cycle,
            action=decision.action, reason=decision.reason,
            escalated_model=decision.escalated_model,
            escalation_trigger=decision.escalation_trigger,
        )

    # Priors derived from THIS repo's own recorded review history, scoped to
    # the files this unit declares (`memory.select`). Empty when the unit
    # declares no files, when nothing has recurred yet, or when nothing
    # intersects -- so a fresh repo and an unrelated PR both get an unchanged
    # prompt rather than a growing preamble.
    memory_block = memory.render_for_prompt(memory.select(log_workdir, files or []))
    pr_context_block = _pr_unit_context_block(
        pr_index=pr_index, pr_title=pr_title, workdir=workdir, section=pr_section, files=files,
    )

    review_profile_prompt = resolve_prompt("review", profile=profile, workdir=workdir)
    if review_profile_prompt is None:
        return CycleResult(
            verdict="ABORT", stage="review",
            reason=_missing_prompt_reason("review", profile, workdir),
        )
    review_profile_prompt += memory_block + pr_context_block
    # Absent `recheck.md` is not fatal -- it only means every cycle stays
    # FULL, which is the pre-existing behaviour. A profile opts into the
    # cheaper path by shipping the file, and never silently gets a bounded
    # review it did not define the contract for.
    recheck_profile_prompt = resolve_prompt("recheck", profile=profile, workdir=workdir)
    if recheck_profile_prompt is not None:
        recheck_profile_prompt += pr_context_block

    resuming_into_scan = bool(progress) and progress.get("phase") == "scan"

    def _snapshot(*, phase: str, review_cycle: int, route: str, pending_confirm: list[Finding],
                  scan_cycle: int, scope_suffix: str) -> None:
        _checkpoint(
            phase=phase, review_cycle=review_cycle, route=route,
            pending_confirm=[f.to_dict() for f in pending_confirm],
            scan_cycle=scan_cycle, scope_suffix=scope_suffix,
            budget=budget.to_dict(),
            review_convergence=review_convergence.to_dict(), scan_convergence=scan_convergence.to_dict(),
            review_inconclusive=review_inconclusive, scan_inconclusive=scan_inconclusive,
            recurrence=recurrence.to_dict(),
        )

    try:
        # --- Review cycles, max 8 -- worker.md -> *Develop & review* ---
        # `while not resuming_into_scan` -- when resuming into the scan
        # phase, review already reached a "pass" decision in the
        # interrupted run (that's the only way `phase` ever advances past
        # "review", see the checkpoint right after this loop), so the body
        # never executes even once and `cycle` stays at its restored value.
        # Re-running review would just re-spend cycles it already spent.
        # `role_results` stays empty for this phase on that path: the
        # review phase's own `RoleRunResult`s live in the interrupted run's
        # (lost) return value, not reconstructed here -- `CycleResult.
        # role_results` is informational (nothing downstream gates on it),
        # so an incomplete list on a resumed run is a real but
        # non-correctness-affecting gap, not silently pretended otherwise.
        cycle = progress["review_cycle"] if (progress and not resuming_into_scan) else 0
        route = progress["route"] if (progress and not resuming_into_scan) else "FULL"
        pending_confirm: list[Finding] = (
            [Finding.from_dict(f) for f in progress["pending_confirm"]]
            if (progress and not resuming_into_scan) else []
        )
        while not resuming_into_scan:
            cycle += 1
            bounded = route == "BOUNDED" and recheck_profile_prompt is not None
            # BOUNDED never fans out to a second reviewer, so there is no
            # cross-reviewer signal to observe on that route.
            convergent: set[str] = set()
            scope_exceeded = False
            if bounded:
                # The cheap bounded re-check never fans out to multiple
                # reviewers or the OCR co-reviewer -- it exists specifically
                # to re-confirm a small, already-identified set of findings
                # cheaply, not to re-open full independent review.
                model = resolved["recheck"]
                result = run_role_fn(
                    workdir=workdir, role="reviewer",
                    title=f"{pr_title} -- recheck c{cycle}",
                    prompt=_build_recheck_prompt(recheck_profile_prompt, pending_confirm),
                    model=model, rundir=rundir, cycle=cycle, port=port,
                )
                role_results.append(result)
                _log("review", cycle, result, model)
                role_results.extend(_correct_incomplete_evidence(
                    workdir=workdir, pr_title=pr_title, role="reviewer",
                    findings=result.review_result.must_fix, model=model,
                    rundir=rundir, cycle=cycle, port=port, run_role_fn=run_role_fn,
                ))
            else:
                # FULL route: every reviewer in `resolved["review_all"]`
                # (>=1, `--review` is dev-ralf's one repeatable role flag)
                # dispatches against the SAME prompt, sequentially -- the
                # same "several roles, one merged verdict" shape the scan
                # cycle already uses for bugbot+compliance, just applied to
                # independent reviewers instead of distinct roles. The OCR
                # co-reviewer (`,ocr` marker on any dispatched reviewer,
                # see model_config.ResolvedModel.ocr) joins the same fan-out
                # as one more dispatch, per adapters/ocr.py's own design
                # ("run OCR as an ADDITIONAL reviewer beside the primary
                # one, merging both verdicts through finding_adapter.merge").
                reviewers = resolved.get("review_all") or [resolved["review"]]
                dispatched: list[tuple[RoleRunResult, ResolvedModel]] = []
                for idx, reviewer_model in enumerate(reviewers):
                    label = "reviewer" if idx == 0 else f"reviewer_{idx + 1}"
                    r = run_role_fn(
                        workdir=workdir, role="reviewer", label=label,
                        title=f"{pr_title} -- review c{cycle}" + (f" [{label}]" if idx else ""),
                        prompt=review_profile_prompt, model=reviewer_model, rundir=rundir, cycle=cycle,
                        port=port,
                    )
                    dispatched.append((r, reviewer_model))
                    role_results.extend(_correct_incomplete_evidence(
                        workdir=workdir, pr_title=pr_title, role="reviewer",
                        findings=r.review_result.must_fix, model=reviewer_model,
                        rundir=rundir, cycle=cycle, port=port, run_role_fn=run_role_fn,
                    ))
                if resolved.get("review_ocr_requested"):
                    ocr_model = ResolvedModel(
                        role="ocr_reviewer", model="default", adapter="ocr",
                        effort="high", source="review:,ocr",
                    )
                    r = run_role_fn(
                        workdir=workdir, role="ocr_reviewer", label="ocr_reviewer",
                        title=f"{pr_title} -- review c{cycle} [ocr_reviewer]",
                        prompt=review_profile_prompt, model=ocr_model, rundir=rundir, cycle=cycle,
                        port=port,
                    )
                    dispatched.append((r, ocr_model))
                for r, m in dispatched:
                    role_results.append(r)
                    _log("review", cycle, r, m)
                merged_result = merge(*(r.review_result for r, _ in dispatched))
                result = RoleRunResult(
                    role="review", cycle=cycle, review_result=merged_result,
                    raw_output_path=dispatched[0][0].raw_output_path,
                )
                # worker.md's other two escalation triggers (the third,
                # `observed_recurrence`, is `recurrence`'s own `survived`
                # count below): `cross_reviewer_convergence` (>=2
                # independently dispatched reviewers named the SAME key
                # THIS cycle -- empty with a single reviewer, correctly)
                # and `scope_exceeded` (worker.md: "`recheck_route == full`
                # -- the fix diff spilled outside the files the findings
                # named"). Tested on `route` DIRECTLY, never on "we are in
                # the FULL dispatch branch": `bounded` is also False when a
                # profile ships no `recheck.md`, in which case a genuinely
                # BOUNDED route still lands here -- reading the branch
                # instead of `route` reported scope_exceeded for every such
                # profile from cycle 2 on, spending the one-per-key
                # escalation allowance on a signal that never fired (and so
                # turning the NEXT, genuine recurrence into a FAIL).
                # `cycle > 1` because `route` starts at its "FULL" default
                # before any fix has happened; worker.md likewise computes
                # the route only "before EVERY review cycle after cycle 1".
                convergent = convergent_keys(*(r.review_result for r, _ in dispatched))
                scope_exceeded = cycle > 1 and route == "FULL"
            # EVERY cycle, before evaluate(): a key only counts as having
            # survived a fix when the PREVIOUS cycle raised it too, so the
            # tracker needs this cycle's key set regardless of whether an
            # intersection can exist yet (see `RecurrenceTracker.record_cycle`).
            recurrence.record_cycle(result.review_result.must_fix)
            decision = evaluate(
                result.review_result, budget, "review", recurrence,
                inconclusive_attempts=review_inconclusive,
                escalation_model=resolved["dev_escalation"].model,
                convergence=review_convergence,
                convergent_keys=convergent,
                route_full=scope_exceeded,
                dev_model=resolved["dev"].model,
            )
            _log_decision("review", cycle, decision)
            if decision.action in ("pass",):
                break
            if decision.action in ("fail", "abort"):
                # "fail" -- review actually evaluated the code and it does
                # not meet the bar (findings survived, or the review budget
                # ran out chasing real findings): a genuine defect. "abort"
                # -- ERROR (role/model unavailable) or an exhausted
                # INCONCLUSIVE retry budget (verification never ran): both
                # are `cycle_gate.evaluate()`'s own "environment problem,
                # not a code one" cases, so ABORT stays a distinct verdict
                # rather than collapsing into FAIL -- `orchestrate.py` maps
                # it to a `blocked` unit outcome, not `failed` (§3.7.11.1).
                verdict = "ABORT" if decision.action == "abort" else "FAIL"
                return CycleResult(
                    verdict=verdict, stage="review", reason=decision.reason,
                    review_cycles=cycle, role_results=role_results,
                )
            if decision.action == "inconclusive_retry":
                review_inconclusive += 1
                _snapshot(phase="review", review_cycle=cycle, route=route, pending_confirm=pending_confirm,
                          scan_cycle=0, scope_suffix="")
                continue  # re-run the SAME reviewer, no dev dispatch, no budget spend
            review_inconclusive = 0  # conclusive result -- the streak is over
            # spawn_fix / spawn_fix_escalated
            pending_confirm = list(result.review_result.must_fix)
            finding_files = _finding_files(pending_confirm)
            pre_fix_head = _head_sha(workdir)
            fix_result = _run_dev_fix(
                workdir=workdir, pr_title=pr_title, findings=pending_confirm,
                dev_model=resolved["dev"], escalated_model=decision.escalated_model,
                rundir=rundir, cycle=cycle, run_role_fn=run_role_fn, port=port,
                pre_fix_head=pre_fix_head, ci_fast_command=ci_fast_command, files=files,
            )
            role_results.append(fix_result)
            _log("review", cycle, fix_result, resolved["dev"])
            route = _safe_recheck_route(workdir, pre_fix_head, finding_files)
            _snapshot(phase="review", review_cycle=cycle, route=route, pending_confirm=pending_confirm,
                      scan_cycle=0, scope_suffix="")

        review_cycles_used = progress["review_cycle"] if resuming_into_scan else cycle
        if not resuming_into_scan:
            # Review just reached "pass" -- checkpoint the phase boundary so an
            # interruption between here and the scan loop's first dispatch
            # still resumes into scan (fresh, cycle 0) instead of re-running
            # review from scratch.
            _snapshot(phase="scan", review_cycle=review_cycles_used, route=route,
                      pending_confirm=[], scan_cycle=0, scope_suffix="")

        # --- Bug + compliance scan, parallel, max 8 -- worker.md -> *Pipeline* ---
        # "tas-bugbot only when the PR changes code -- no source path in
        # pr_files (docs/config-only) -> skip it, bug_verdict = SKIPPED."
        # compliance ALWAYS runs regardless.
        docs_only = _is_docs_only(files)
        bugbot_prompt = None if docs_only else resolve_prompt("bugbot", profile=profile, workdir=workdir)
        compliance_prompt = resolve_prompt("compliance", profile=profile, workdir=workdir)
        if compliance_prompt is None or (not docs_only and bugbot_prompt is None):
            missing = "compliance" if compliance_prompt is None else "bugbot"
            return CycleResult(
                verdict="ABORT", stage="scan",
                reason=_missing_prompt_reason(missing, profile, workdir),
                review_cycles=review_cycles_used, role_results=role_results,
            )
        if bugbot_prompt is not None:
            bugbot_prompt += memory_block + pr_context_block
        compliance_prompt += memory_block + pr_context_block

        cycle = progress["scan_cycle"] if resuming_into_scan else 0
        scope_suffix = progress["scope_suffix"] if resuming_into_scan else ""
        # `scope_exceeded` for the scan stage -- the same signal the review
        # loop derives from its own `route`, tracked here explicitly because
        # the scan stage expresses its route as a prompt scope suffix rather
        # than a stored route string. False on cycle 1: no fix has happened
        # yet for a route to describe.
        scan_route_full = False
        while True:
            cycle += 1
            if docs_only:
                # SKIPPED, not dispatched -- a clean pass with zero findings
                # is the correct input to `merge()` below (never blocks,
                # never counts as INCONCLUSIVE).
                bugbot_result = RoleRunResult(
                    role="bugbot", cycle=cycle,
                    review_result=ReviewResult(role_status=RoleStatus.COMPLETE, findings=[]),
                    raw_output_path=Path("/dev/null"),
                )
            else:
                bugbot_result = run_role_fn(
                    workdir=workdir, role="bugbot", title=f"{pr_title} -- bugbot c{cycle}",
                    prompt=bugbot_prompt + scope_suffix, model=resolved["bugbot"], rundir=rundir, cycle=cycle,
                    port=port,
                )
            compliance_result = run_role_fn(
                workdir=workdir, role="compliance", title=f"{pr_title} -- compliance c{cycle}",
                prompt=compliance_prompt + scope_suffix, model=resolved["compliance"], rundir=rundir, cycle=cycle,
                port=port,
            )
            role_results.extend((bugbot_result, compliance_result))
            if not docs_only:
                _log("scan", cycle, bugbot_result, resolved["bugbot"])
            _log("scan", cycle, compliance_result, resolved["compliance"])
            # worker.md scopes the *Incomplete evidence* re-query to review
            # only -- its own bugbot/compliance are external skills with a
            # KV wire shape that never carries contract/scenario/fix at all
            # ("not a bug to work around here"). This project's own
            # `rust-dev` profile asks bugbot/compliance for the SAME `||`
            # text contract as review (parse_role_output()'s own docstring:
            # "the wire shape is a property of the PROMPT, not the role"),
            # so unlike dev-ralf's scan stage, this one CAN produce an
            # incomplete MUST_FIX -- applying the same correction round
            # here is the faithful adaptation, not an extension beyond
            # worker.md's intent.
            if not docs_only:
                role_results.extend(_correct_incomplete_evidence(
                    workdir=workdir, pr_title=pr_title, role="bugbot",
                    findings=bugbot_result.review_result.must_fix, model=resolved["bugbot"],
                    rundir=rundir, cycle=cycle, port=port, run_role_fn=run_role_fn,
                ))
            role_results.extend(_correct_incomplete_evidence(
                workdir=workdir, pr_title=pr_title, role="compliance",
                findings=compliance_result.review_result.must_fix, model=resolved["compliance"],
                rundir=rundir, cycle=cycle, port=port, run_role_fn=run_role_fn,
            ))
            merged = merge(bugbot_result.review_result, compliance_result.review_result)
            # worker.md's *Merge scans & fix loop*: "the SAME
            # `finding_merge.py merge` call as reviewers, with
            # `bugbot`/`compliance` as the reviewer ids ... recompute
            # recheck route + escalation decision (same tools, same rules
            # as review)" -- so the two scanners agreeing on one key IS
            # `cross_reviewer_convergence` here, exactly as two reviewers
            # agreeing is in the review stage. Skipped when bugbot did not
            # run (docs-only, `_is_docs_only`): a single scanner cannot
            # converge with anything.
            scan_convergent = (
                set() if docs_only
                else convergent_keys(bugbot_result.review_result, compliance_result.review_result)
            )
            recurrence.record_cycle(merged.must_fix)
            decision = evaluate(
                merged, budget, "scan", recurrence,
                inconclusive_attempts=scan_inconclusive,
                escalation_model=resolved["dev_escalation"].model,
                convergence=scan_convergence,
                convergent_keys=scan_convergent,
                route_full=scan_route_full,
                dev_model=resolved["dev"].model,
            )
            _log_decision("scan", cycle, decision)
            if decision.action == "pass":
                break
            if decision.action in ("fail", "abort"):
                # See the review loop's identical branch above: ABORT stays
                # distinct from FAIL so `orchestrate.py` can report a
                # `blocked` unit outcome rather than `failed`.
                verdict = "ABORT" if decision.action == "abort" else "FAIL"
                return CycleResult(
                    verdict=verdict, stage="scan", reason=decision.reason,
                    review_cycles=review_cycles_used, scan_cycles=cycle, role_results=role_results,
                )
            if decision.action == "inconclusive_retry":
                scan_inconclusive += 1
                _snapshot(phase="scan", review_cycle=review_cycles_used, route="FULL",
                          pending_confirm=[], scan_cycle=cycle, scope_suffix=scope_suffix)
                continue
            scan_inconclusive = 0  # conclusive result -- the streak is over
            # spawn_fix / spawn_fix_escalated
            finding_files = _finding_files(merged.must_fix)
            pre_fix_head = _head_sha(workdir)
            fix_result = _run_dev_fix(
                workdir=workdir, pr_title=pr_title, findings=merged.must_fix,
                dev_model=resolved["dev"], escalated_model=decision.escalated_model,
                rundir=rundir, cycle=cycle, run_role_fn=run_role_fn, port=port,
                pre_fix_head=pre_fix_head, ci_fast_command=ci_fast_command, files=files,
            )
            role_results.append(fix_result)
            _log("scan", cycle, fix_result, resolved["dev"])
            if _safe_recheck_route(workdir, pre_fix_head, finding_files) == "BOUNDED":
                scope_suffix = _bounded_scope_suffix(_changed_files(workdir, pre_fix_head))
                scan_route_full = False
            else:
                scope_suffix = ""
                scan_route_full = True
            _snapshot(phase="scan", review_cycle=review_cycles_used, route="FULL",
                      pending_confirm=[], scan_cycle=cycle, scope_suffix=scope_suffix)

        return CycleResult(
            verdict="PASS", stage="scan", reason="review + bug/compliance scan clean",
            review_cycles=review_cycles_used, scan_cycles=cycle, role_results=role_results,
            budget=budget, recurrence=recurrence,
        )
    finally:
        # Regenerated from the records this cycle just appended, so the NEXT
        # unit's priors already include whatever recurred in this one. In the
        # `finally` because a failed cycle is exactly the one whose findings
        # are worth carrying forward.
        try:
            memory.regenerate(log_workdir)
        except Exception:  # noqa: BLE001 -- derived data, never worth failing a cycle over
            pass
