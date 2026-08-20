"""The last third of worker.md: pre-ship sync -> `gh-pr` -> `gh-review` ->
final phase (`sync -> final_audit -> ship_gate`, one self-verifying loop) ->
squash-merge -- this module's own `run_final_stage()` composes them in that
order, matching worker.md's *Ship via /gh-pr* / *Final phase* sections. (An
earlier revision of this module ran the WHOLE round loop BEFORE gh-pr/
gh-review, ordering itself around a `run_final_phase()` design that
predated gh-pr/gh-review's own port -- found and fixed once that inversion
was checked against worker.md directly: a gh-review fix commit was never
re-verified by anything before squash-merge, see docs/ARCHITECTURE.md
§3.14.5.)

**Why ship_gate moved behind sync and final_audit.** `ship_gate.evaluate()`
runs `acceptance.run_all()` against whatever is on disk at call time. Both
`sync_main()` (merging `origin/main` in) and `run_final_audit()`'s own
dev-fix loop change what is on disk. Calling ship_gate BEFORE either of them
-- the original order -- stamps a PASS on code that is not what actually
ends up in the PR: a sync-time conflict fix or an audit fix made after that
PASS is never re-verified before the squash-merge that ships it. Moving
ship_gate to run only after sync and the audit have both settled closes
that gap; see docs/ARCHITECTURE.md §3.11.2 for the fuller argument.

**Why sync got its own bounded fix loop instead of a one-shot block.** A
merge conflict is a defect exactly like a review finding -- dev can resolve
it -- not an external failure like a missing `gh` binary. Treating it as an
immediate terminal block contradicted this project's own completion
contract: reasona-dev is supposed to run to a shipped PR unless something
truly outside its control (network, `gh`, `git` itself) stops it.
`run_sync_cycle()` now dispatches dev to resolve conflict markers and
conclude the merge, bounded by the `"sync"` stage of the same `FixBudget`
(`MAX_SYNC_CYCLES`), the same shape as every other stage in this pipeline.

**Why the tail is a round-bounded outer loop, not three linear steps.**
Either sync or final_audit can still change code in a given pass. If either
one did, that pass's ship_gate verdict already covers something the OTHER
step never saw operating on the pre-change tree, so the whole
sync -> final_audit -> ship_gate sequence runs again. `run_final_phase()`
tracks whether anything changed in a round and only accepts the round's
ship_gate verdict once a round changes nothing -- bounded by
`MAX_FINAL_PHASE_ROUNDS`, on the same reasoning as every other cap in this
pipeline: base moving faster than this pipeline can settle is not something
retrying forever would fix.

**Merging is opt-in.** `merge=False` is the default and stops after the PR
exists. A squash-merge is outward-facing and hard to undo -- it rewrites the
default branch of a real repository -- so the caller has to ask for it
explicitly rather than discover it happened. Everything up to that point
(sync, audit, message construction, PR creation) is safe to run repeatedly.

**What still fails loudly rather than being retried.** `gh` missing, `gh`
unauthenticated, a non-conflict sync failure (fetch failure, unreadable
remote), a malformed squash title, a PR behind its base at the final
pre-merge check: these are genuinely outside dev's ability to fix by
editing files, so each still returns a `blocked` status immediately rather
than entering a fix loop that could never converge.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from reasona_dev import _shell, cycles_log, squash
from reasona_dev import gh_review as gh_review_mod
from reasona_dev.cycle_gate import (
    MAX_FINAL_PHASE_ROUNDS,
    FixBudget,
    RecurrenceTracker,
    evaluate,
)
from reasona_dev.model_config import ResolvedModel
from reasona_dev.pr_cycle import RoleRunResult, _run_dev_fix, run_role
from reasona_dev.prompt_profile import resolve_prompt
from reasona_dev.ship_gate import ShipDecision
from reasona_dev.squash import SquashMessage

MERGED = "merged"
PR_OPEN = "pr_open"
BLOCKED = "blocked"
# sync resolved a SUBSTANTIVE merge conflict (`parse_conflict_kind()`) --
# gh-pr/gh-review/squash-merge never ran this call. `orchestrate.py`'s
# `_process_unit()` is what re-invokes `pr_cycle.run_pr_cycle()` on seeing
# this, then retries the final stage -- see `docs/ARCHITECTURE.md` §3.14.4.
NEEDS_REVIEW = "needs_review"


@dataclass
class TailResult:
    stage_name: str
    status: str  # MERGED | PR_OPEN | BLOCKED | NEEDS_REVIEW
    reason: str
    pr_url: str | None = None
    squash_message: SquashMessage | None = None
    final_audit: RoleRunResult | None = None
    role_results: list[RoleRunResult] = field(default_factory=list)
    ship_decision: ShipDecision | None = None

    @property
    def blocked(self) -> bool:
        return self.status == BLOCKED

    def render(self) -> str:
        line = f"  [{self.status:>8}] {self.stage_name}: {self.reason}"
        return f"{line}\n             {self.pr_url}" if self.pr_url else line


# --- shell helpers ----------------------------------------------------------

_run = _shell.run  # see reasona_dev/_shell.py -- shared across every git/gh caller


def gh_available(workdir: Path) -> str | None:
    """None when `gh` is usable, else the reason it is not.

    Checked before anything is attempted rather than discovered halfway
    through: a tail that syncs and audits and only then finds it cannot
    create a PR has spent an audit for nothing.
    """
    if shutil.which("gh") is None:
        return "gh CLI is not on PATH"
    code, _, err = _run(["gh", "auth", "status"], workdir, timeout=30)
    if code != 0:
        return f"gh is not authenticated ({err.strip().splitlines()[0] if err.strip() else 'auth status failed'})"
    return None


# --- steps ------------------------------------------------------------------

def _conflicted_files(workdir: Path) -> list[str]:
    code, out, _ = _run(["git", "diff", "--name-only", "--diff-filter=U"], workdir)
    return [p for p in out.splitlines() if p.strip()] if code == 0 else []


def sync_main(workdir: Path, *, base: str = "origin/main") -> tuple[bool, str]:
    """Fetch and merge `base` into the current branch.

    Merge, not rebase: the branch may already be pushed, and rebasing
    published history forces every later push to be a force-push, which the
    final pre-merge check then cannot distinguish from someone else's work
    being overwritten.

    **A conflict is left in place, not auto-aborted.** `run_sync_cycle()`
    resolves it by dispatching dev against the in-progress merge (conflict
    markers still on disk, `MERGE_HEAD` still set) -- aborting here would
    erase exactly the state dev needs to see. Any OTHER merge failure (fetch
    failed, no conflict markers to point to) is not something dev editing
    files can fix, so that case still aborts and returns a plain failure.
    """
    remote = base.split("/", 1)[0] if "/" in base else "origin"
    code, _, err = _run(["git", "fetch", remote], workdir)
    if code != 0:
        return False, f"git fetch {remote} failed: {err.strip()[:200]}"

    code, out, err = _run(["git", "merge", "--no-edit", base], workdir)
    if code == 0:
        return True, "up to date with base"

    paths = _conflicted_files(workdir)
    if paths:
        return False, f"merge conflict with {base} in: {', '.join(paths[:5])}"
    _run(["git", "merge", "--abort"], workdir)
    return False, f"merge with {base} failed: {(err or out).strip()[:200]}"


_CONFLICT_KIND_RE = re.compile(r"^CONFLICT_KIND:\s*(mechanical|substantive)\s*$", re.IGNORECASE | re.MULTILINE)


def parse_conflict_kind(text: str) -> str:
    """The dev role's own self-report of whether ITS conflict resolution was
    `"mechanical"` (import order, formatting, a line moved -- no semantic
    change) or `"substantive"` (overlapping logic, the same function edited
    on both sides -- a real code change review/scan never saw). Defaults to
    `"substantive"` when the marker is missing or unparseable -- the same
    "an unanswerable routing question never narrows scope" rule
    `_safe_recheck_route()` already follows: guessing MECHANICAL on missing
    evidence could let a real conflicting change skip re-review entirely,
    which is the one direction this decision must never guess in.
    """
    match = _CONFLICT_KIND_RE.search(text)
    return match.group(1).lower() if match else "substantive"


def _build_conflict_fix_prompt(base: str, conflicted_files: list[str]) -> str:
    listed = "\n".join(f"- {p}" for p in conflicted_files)
    return "\n".join(
        [
            f"Merging {base} into this branch produced conflicts in the files "
            "below. Resolve every conflict marker in each of them, keeping "
            f"both this branch's intent and {base}'s changes where they do "
            "not contradict, then stage the resolved files and run "
            "`git commit --no-edit` to conclude the merge. Do not touch any "
            "file not listed here.",
            "",
            listed,
            "",
            "When you are completely done, on its own final line write "
            "exactly one of:",
            "CONFLICT_KIND: mechanical",
            "CONFLICT_KIND: substantive",
            "",
            "Write `mechanical` ONLY if your resolution involved no semantic "
            "change at all (e.g. import order, formatting, a line moved "
            "without altering behavior). Write `substantive` if resolving "
            "the conflict required combining or reconciling actual logic "
            "changes from both sides -- when in doubt, write `substantive`.",
        ]
    )


def _run_conflict_fix(
    *, workdir: Path, pr_title: str, base: str, conflicted_files: list[str],
    dev_model: ResolvedModel, rundir: Path, cycle: int, port: int, run_role_fn,
) -> RoleRunResult:
    return run_role_fn(
        workdir=workdir, role="backend",
        title=f"{pr_title} -- resolve merge conflict c{cycle}",
        prompt=_build_conflict_fix_prompt(base, conflicted_files),
        model=dev_model, rundir=rundir, cycle=cycle, port=port,
    )


def run_sync_cycle(
    *,
    workdir: Path,
    pr_title: str,
    resolved: dict[str, ResolvedModel],
    rundir: Path,
    budget: FixBudget,
    base: str = "origin/main",
    port: int = 8052,
    run_role_fn=run_role,
) -> tuple[str, str, list[RoleRunResult], bool, bool]:
    """Sync with `base`, resolving a conflict via dev instead of blocking on it.

    Returns `(status, reason, dispatches, changed, substantive)`. `status`
    is `"ok"` (in sync, whether or not a conflict needed resolving) or
    `"blocked"` (a non-conflict sync failure, or the `"sync"` stage budget
    ran out while still conflicted). `changed` is True iff a
    conflict-resolution commit was made this call -- the caller uses it to
    decide whether `final_audit` and `ship_gate` need to run again on top
    of it. `substantive` is True iff ANY conflict resolution this call made
    was self-reported (`parse_conflict_kind()`) as substantive rather than
    mechanical -- `run_final_phase()` uses it to force a full
    `pr_cycle.run_pr_cycle()` re-review before the unit is allowed past
    this tail (worker.md's mechanical/substantive distinction,
    `docs/ARCHITECTURE.md` §3.14.4).
    """
    dispatches: list[RoleRunResult] = []
    changed = False
    substantive = False
    cycle = 0
    while True:
        # Idempotent cleanup: aborts a merge left over from a prior
        # iteration where dev edited the conflict markers but did not
        # conclude the merge with a commit. A no-op when nothing is
        # in progress.
        _run(["git", "merge", "--abort"], workdir)
        ok, reason = sync_main(workdir, base=base)
        if ok:
            return "ok", reason, dispatches, changed, substantive

        conflicted = _conflicted_files(workdir)
        if not conflicted:
            return "blocked", reason, dispatches, changed, substantive
        if not budget.can_spend("sync"):
            _run(["git", "merge", "--abort"], workdir)
            return "blocked", f"sync budget exhausted: {reason}", dispatches, changed, substantive

        budget.spend("sync")
        cycle += 1
        fix_result = _run_conflict_fix(
            workdir=workdir, pr_title=pr_title, base=base, conflicted_files=conflicted,
            dev_model=resolved["dev"], rundir=rundir, cycle=cycle, port=port, run_role_fn=run_role_fn,
        )
        dispatches.append(fix_result)
        changed = True
        if fix_result.raw_output_path.is_file():
            kind = parse_conflict_kind(fix_result.raw_output_path.read_text(encoding="utf-8"))
        else:
            kind = "substantive"  # no output at all -- same fail-safe default
        substantive = substantive or (kind == "substantive")


def is_up_to_date(workdir: Path, *, base: str = "origin/main") -> tuple[bool, str]:
    """True when HEAD already contains the tip of `base`.

    Re-checked immediately before merging, not only after `sync_main`: base
    can advance between the two, and merging a PR that no longer contains
    its base is how a green PR lands red.
    """
    code, out, err = _run(["git", "merge-base", "--is-ancestor", base, "HEAD"], workdir)
    if code == 0:
        return True, "HEAD contains base"
    if code == 1:
        return False, f"branch is behind {base} -- re-run sync"
    return False, f"could not compare against {base}: {err.strip()[:160]}"


def build_squash_message(
    *, unit_type: str | None, title: str, body_lines: list[str] | None = None
) -> tuple[SquashMessage | None, str]:
    """Construct and independently re-check the squash message.

    `squash.build` is the only constructor and `squash.guard` re-derives
    validity without consulting it, so a violation means the two disagree --
    never "go fix the message by hand". A `T#` (title) violation blocks the
    merge outright; a `B#` (body-only) violation merges with the title alone,
    which is `squash.classify`'s TITLE_ONLY verdict.
    """
    msg = squash.build(unit_type or "feat", title, body_lines or [])
    verdict = squash.classify(squash.guard(msg))
    if verdict == "FAIL":
        return None, f"squash message rejected by its own guard: {'; '.join(squash.guard(msg))}"
    if verdict == "TITLE_ONLY":
        return SquashMessage(title=msg.title, body=""), "body dropped (guard: body-only violation)"
    return msg, "ok"


def existing_pr_url(workdir: Path) -> str | None:
    code, out, _ = _run(
        ["gh", "pr", "view", "--json", "url", "--jq", ".url"], workdir, timeout=60
    )
    return out.strip() if code == 0 and out.strip() else None


def create_pr(
    workdir: Path, *, title: str, body: str, head: str | None = None,
    base: str | None = None, known_pr_url: str | None = None,
) -> tuple[str | None, str]:
    """Create the PR, or return the one that already exists.

    Idempotent on purpose: the tail is safe to re-run after a blocked step,
    and a second `gh pr create` for the same branch fails rather than
    duplicating -- surfacing that failure as an error would make retry look
    broken when it is the normal path.

    `known_pr_url`, when given (`reasona_dev.ledger`'s record of a PR this
    same unit already created in an earlier, interrupted run), is checked
    only as a FALLBACK -- `gh pr view` below is still the live source of
    truth and wins whenever it has an answer. The ledger only matters when
    `gh` itself comes up empty (e.g. this call runs before the working tree
    is on the PR's branch): using the remembered URL there avoids `gh pr
    create` producing a duplicate that a live-but-incomplete `gh` query
    missed, without ever trusting a stale ledger over a live one.

    `head`/`base`, when given, are passed to `gh pr create` explicitly
    (`gh_pr.py` always supplies both -- `/gh-pr` SKILL.md §8: "never rely on
    `gh` detecting the current branch from CWD", which breaks whenever the
    caller's working directory differs from the branch's own worktree,
    exactly the shape this project's per-unit worktrees have).
    """
    existing = existing_pr_url(workdir)
    if existing:
        return existing, "reused existing PR"
    if known_pr_url:
        return known_pr_url, "reused PR recorded in the ledger (gh pr view did not see it)"

    code, _, err = _run(["git", "push", "-u", "origin", "HEAD"], workdir, timeout=180)
    if code != 0:
        return None, f"git push failed: {err.strip()[:200]}"

    cmd = ["gh", "pr", "create", "--title", title, "--body", body or title]
    if head:
        cmd += ["--head", head]
    if base:
        cmd += ["--base", base]
    code, out, err = _run(cmd, workdir, timeout=120)
    if code != 0:
        return None, f"gh pr create failed: {err.strip()[:200]}"
    url = out.strip().splitlines()[-1] if out.strip() else existing_pr_url(workdir)
    return url, "PR created"


def squash_merge(workdir: Path, msg: SquashMessage) -> tuple[bool, str]:
    """`gh pr merge --squash` with the built title and body.

    GitHub appends ` (#<pr>)` to a squash title itself, which is why
    `squash.build` never adds one.
    """
    cmd = ["gh", "pr", "merge", "--squash", "--subject", msg.title]
    if msg.body:
        cmd += ["--body", msg.body]
    code, out, err = _run(cmd, workdir, timeout=180)
    if code != 0:
        return False, f"gh pr merge failed: {(err or out).strip()[:200]}"
    return True, "squash-merged"


# --- final audit ------------------------------------------------------------

def should_run_final_audit(budget: FixBudget) -> bool:
    """True when the unit needed at least one fix. See module docstring."""
    return budget.total_used > 0


def run_final_audit(
    *,
    workdir: Path,
    stage_name: str,
    pr_title: str,
    profile: str,
    resolved: dict[str, ResolvedModel],
    rundir: Path,
    budget: FixBudget,
    recurrence: RecurrenceTracker,
    port: int = 8052,
    run_role_fn=run_role,
) -> tuple[bool, str, list[RoleRunResult]]:
    """A fresh whole-PR audit, with its own bounded fix loop.

    Dispatched as the `compliance` role rather than `final_audit`: the audit's
    prompt is what makes it an audit, and Bernstein's role whitelist plus its
    per-role worktree conventions are shared. Using a role the seed already
    declares keeps the tail from depending on a `role_model_policy` entry a
    target repo may not have. The MODEL still comes from
    `resolved["final_audit"]`, so the audit runs on the model configured for
    it.

    Returns `(passed, reason, dispatches)`. A MUST_FIX here spends the
    `"final"` stage budget (cap `MAX_FINAL_CYCLES`), which no other caller
    produces -- the audit is the only thing that runs after the scan stage has
    already passed, so a finding at this point is by definition something
    every earlier role missed.
    """
    prompt = resolve_prompt("final_audit", profile=profile, workdir=workdir)
    if prompt is None:
        # Not fatal: a profile that ships no final_audit.md has declared it
        # does not want one. Silently skipping a REQUESTED audit would be
        # wrong; skipping an undefined one is the profile's own decision.
        return True, "profile defines no final_audit prompt -- skipped", []

    dispatches: list[RoleRunResult] = []
    cycle = 0
    while True:
        cycle += 1
        result = run_role_fn(
            workdir=workdir, role="compliance",
            title=f"{pr_title} -- final audit c{cycle}",
            prompt=prompt, model=resolved["final_audit"], rundir=rundir, cycle=cycle,
            port=port,
        )
        dispatches.append(result)
        cycles_log.record_dispatch(
            workdir=workdir, stage_name=stage_name, stage="final", cycle=cycle,
            role="final_audit", model=resolved["final_audit"].model,
            adapter=resolved["final_audit"].adapter, result=result.review_result,
        )
        if cycle > 1:
            recurrence.record_post_fix(result.review_result.must_fix)

        decision = evaluate(
            result.review_result, budget, "final", recurrence,
            inconclusive_attempts=0, escalation_model=resolved["dev_escalation"].model,
        )
        cycles_log.record_decision(
            workdir=workdir, stage_name=stage_name, stage="final", cycle=cycle,
            action=decision.action, reason=decision.reason,
            escalated_model=decision.escalated_model,
        )
        if decision.action == "pass":
            return True, f"final audit clean after {cycle} cycle(s)", dispatches
        if decision.action in ("fail", "abort"):
            return False, f"final audit: {decision.reason}", dispatches
        if decision.action == "inconclusive_retry":
            # The audit is the last gate; an INCONCLUSIVE one cannot be
            # retried indefinitely here without the counter the review/scan
            # loops carry, and re-auditing is cheaper to simply refuse.
            return False, "final audit inconclusive -- verification did not run", dispatches

        fix = _run_dev_fix(
            workdir=workdir, pr_title=pr_title,
            findings=result.review_result.must_fix, dev_model=resolved["dev"],
            escalated_model=decision.escalated_model, rundir=rundir,
            cycle=cycle, run_role_fn=run_role_fn, port=port,
        )
        dispatches.append(fix)


# --- ship_gate: bounded dev-fix loop for a failing acceptance criterion ----

def _build_ship_fix_prompt(decision: ShipDecision) -> str:
    listed = "\n".join(f"- {o.name}: {o.detail}" for o in decision.failures)
    return "\n".join(
        [
            "The ship gate found the following unmet criteria. Fix them so "
            "the plan's own executable acceptance criteria pass. Do not "
            "address anything not listed here.",
            "",
            listed,
        ]
    )


def _run_ship_fix(
    *, workdir: Path, pr_title: str, decision: ShipDecision,
    dev_model: ResolvedModel, rundir: Path, cycle: int, port: int, run_role_fn,
) -> RoleRunResult:
    return run_role_fn(
        workdir=workdir, role="backend",
        title=f"{pr_title} -- ship gate fix c{cycle}",
        prompt=_build_ship_fix_prompt(decision), model=dev_model, rundir=rundir, cycle=cycle,
        port=port,
    )


def run_ship_cycle(
    *,
    workdir: Path,
    stage_name: str,
    pr_title: str,
    resolved: dict[str, ResolvedModel],
    rundir: Path,
    budget: FixBudget,
    cycle_verdict: str,
    ship_gate_fn,
    port: int = 8052,
    run_role_fn=run_role,
) -> tuple[ShipDecision, bool, list[RoleRunResult]]:
    """Runs `ship_gate_fn` and, on a failing acceptance criterion, dispatches
    dev against it and retries -- bounded by the `"ship"` stage of `budget`
    (`MAX_SHIP_CYCLES`). The review axis is already guaranteed to pass by
    the time this runs (`orchestrate.py` only calls the final phase when
    the review/scan cycle itself passed), so a failure here is always the
    acceptance axis.

    Returns `(decision, changed, dispatches)`. `changed` is True iff a ship
    fix was dispatched this call -- `run_final_phase()` uses it exactly like
    `sync_changed`/`audit_changed`: a fix here means the whole round needs
    to re-verify from sync, not just this check. `decision.passed` tells
    the caller whether this returned because it settled or because the
    `"ship"` budget ran out while still failing.
    """
    dispatches: list[RoleRunResult] = []
    changed = False
    cycle = 0
    while True:
        decision = ship_gate_fn(workdir, stage_name, cycle_verdict=cycle_verdict)
        if decision.passed or not budget.can_spend("ship"):
            return decision, changed, dispatches
        budget.spend("ship")
        cycle += 1
        dispatches.append(_run_ship_fix(
            workdir=workdir, pr_title=pr_title, decision=decision,
            dev_model=resolved["dev"], rundir=rundir, cycle=cycle, port=port, run_role_fn=run_role_fn,
        ))
        changed = True


# --- final phase: sync -> final_audit -> ship_gate, re-verified as a whole -

def run_final_phase(
    *,
    workdir: Path,
    stage_name: str,
    pr_title: str,
    profile: str,
    resolved: dict[str, ResolvedModel],
    rundir: Path,
    budget: FixBudget,
    recurrence: RecurrenceTracker,
    cycle_verdict: str,
    ship_gate_fn,
    base: str = "origin/main",
    port: int = 8052,
    run_role_fn=run_role,
) -> tuple[ShipDecision | None, str, list[RoleRunResult], str]:
    """`sync -> (conditional) final_audit -> ship_gate (with its own bounded
    dev-fix loop, `run_ship_cycle()`)`, re-run whole as long as a round
    actually changed something. See the module docstring's "why the tail is
    a round-bounded outer loop" for the reasoning.

    Returns `(ship_decision, status, dispatches, reason)`. `status` is
    `"passed"` (a round changed nothing and its `ship_decision.passed` is
    True), `"needs_review"` (`sync` resolved a SUBSTANTIVE merge conflict --
    see `parse_conflict_kind()` -- so nothing here, and nothing gh-pr/
    gh-review/squash-merge would do next, may proceed until
    `pr_cycle.run_pr_cycle()` re-reviews the result; `run_final_stage()`
    stops immediately on this status, without dispatching `final_audit` or
    `ship_gate` for that round -- `orchestrate.py`'s `_process_unit()` is
    what actually re-invokes review/scan, see `docs/ARCHITECTURE.md`
    §3.14.4), or `"blocked"` (a sync/audit/ship step's own bounded dev-fix
    budget ran out while still failing, or `MAX_FINAL_PHASE_ROUNDS` was
    reached without settling). By the time this runs, review/scan already
    passed, so nothing failing this deep is treated as an ordinary
    review-found defect (`failed`) -- see `cycle_gate.MAX_SHIP_CYCLES`'s own
    docstring. `ship_decision` is None only when the block happened before
    ship_gate ever ran.
    """
    dispatches: list[RoleRunResult] = []
    for round_ in range(1, MAX_FINAL_PHASE_ROUNDS + 1):
        sync_status, sync_reason, sync_dispatches, sync_changed, sync_substantive = run_sync_cycle(
            workdir=workdir, pr_title=pr_title, resolved=resolved, rundir=rundir,
            budget=budget, base=base, port=port, run_role_fn=run_role_fn,
        )
        dispatches.extend(sync_dispatches)
        if sync_status != "ok":
            return None, "blocked", dispatches, sync_reason
        if sync_substantive:
            return None, "needs_review", dispatches, (
                "sync resolved a substantive merge conflict -- review/scan must re-run "
                "before this unit may proceed (worker.md's mechanical/substantive rule)"
            )

        audit_changed = False
        if should_run_final_audit(budget):
            passed, audit_reason, audit_dispatches = run_final_audit(
                workdir=workdir, stage_name=stage_name, pr_title=pr_title,
                profile=profile, resolved=resolved, rundir=rundir, budget=budget,
                recurrence=recurrence, port=port, run_role_fn=run_role_fn,
            )
            dispatches.extend(audit_dispatches)
            if not passed:
                return None, "blocked", dispatches, audit_reason
            audit_changed = len(audit_dispatches) > 1  # more than the clean first read means a fix ran

        decision, ship_changed, ship_dispatches = run_ship_cycle(
            workdir=workdir, stage_name=stage_name, pr_title=pr_title, resolved=resolved,
            rundir=rundir, budget=budget, cycle_verdict=cycle_verdict, ship_gate_fn=ship_gate_fn,
            port=port, run_role_fn=run_role_fn,
        )
        dispatches.extend(ship_dispatches)
        if not decision.passed:
            return decision, "blocked", dispatches, f"ship gate did not pass: {decision.reason}"

        if not sync_changed and not audit_changed and not ship_changed:
            return decision, "passed", dispatches, decision.reason
        # Something changed this round -- the ship_gate verdict just above
        # covers code the OTHER step in this same round never saw. Loop.

    return None, "blocked", dispatches, f"final phase did not converge after {MAX_FINAL_PHASE_ROUNDS} round(s)"


# --- composition ------------------------------------------------------------

def run_final_stage(
    *,
    workdir: str | Path,
    stage_name: str,
    pr_title: str,
    unit_type: str | None,
    unit,
    profile: str,
    resolved: dict[str, ResolvedModel],
    rundir: str | Path,
    cycle_verdict: str,
    budget: FixBudget,
    recurrence: RecurrenceTracker,
    ship_gate_fn,
    base: str = "origin/main",
    merge: bool = False,
    plan_name: str | None = None,
    gh_review_max_wait_seconds: int = gh_review_mod.DEFAULT_MAX_WAIT_SECONDS,
    port: int = 8052,
    run_role_fn=run_role,
) -> TailResult:
    """gh -> pre-ship sync -> gh-pr -> gh-review -> final phase (sync ->
    final_audit -> ship_gate) -> squash-merge.

    Ordering matches worker.md's actual pipeline (`~/repository/
    tas-dev-plugins/plugins/dev/skills/dev-ralf/reference/worker.md`
    -> *Ship via /gh-pr* / *Final phase*), not the order this function used
    to run in. worker.md syncs main TWICE: once here, before gh-pr/gh-review
    even start ("sync to main FIRST -- before any CI runs"), and once more
    INSIDE the final phase's round loop, positioned deliberately AFTER
    gh-review -- specifically to catch "main moving DURING this PR's
    gh-pr+gh-review CI window", and specifically so `final_audit`/
    `ship_gate` re-verify whatever fix commits `gh_review.run_gh_review()`
    just made. This function used to run the WHOLE round loop (including
    final_audit/ship_gate) before gh-pr/gh-review ever ran -- an ordering
    bug found and fixed after gh-pr/gh-review were ported onto an already-
    settled final-phase design without re-checking worker.md's real
    position for them: a gh-review fix commit was never re-verified by
    anything before squash-merge. See docs/ARCHITECTURE.md §3.9/§3.12/
    §3.13/§3.14.5.

    Named separately from `run_final_phase()` because it is a superset: `gh`
    availability, the pre-ship sync, gh-pr/gh-review, and squash-merge are
    not part of the round-verified sync/audit/ship_gate loop, so they stay
    outside `run_final_phase()` and are composed here instead.

    `cycle_verdict` and `ship_gate_fn` replace the old pre-computed
    `ship_decision` parameter: the verdict can no longer be computed by the
    caller before this runs, since it has to be evaluated on whatever the
    final phase's own sync/audit fixes leave on disk -- see
    `run_final_phase()` and the module docstring.

    `unit` (a `plan_compile.PRUnit`) is what `reasona_dev.gh_pr.run_gh_pr()`
    needs to build the issue/PR content (`unit.section`, `unit.index`) --
    imported lazily inside this function, not at module level, because
    `gh_pr.py` itself imports `final_phase` (to reuse `create_pr()`'s
    idempotency logic) and a top-level import here would be circular.

    `plan_name`, when given, is `orchestrate`'s resume flag passed through:
    `reasona_dev.ledger`'s recorded PR url/issue number for this unit (if
    any) is offered to `gh_pr.run_gh_pr()`/`create_pr()` as a fallback --
    see their own docstrings for why that's a fallback and not a
    replacement for the live `gh` check. A newly created PR/issue is
    recorded back to the same ledger entry so the NEXT call (e.g. the
    `--merge` run after a `--ship` run already opened the PR) has it
    available too.
    """
    workdir = Path(workdir)
    rundir = Path(rundir)

    def _blocked(reason: str, **kw) -> TailResult:
        result = TailResult(stage_name=stage_name, status=BLOCKED, reason=reason, **kw)
        cycles_log.record_ship(
            workdir=workdir, stage_name=stage_name, passed=False,
            gates={"final_stage": False}, reason=reason,
        )
        return result

    def _needs_review(
        reason: str, dispatches: list[RoleRunResult],
        decision: ShipDecision | None = None, pr_url: str | None = None,
    ) -> TailResult:
        # A SUBSTANTIVE conflict resolution -- from either sync point below
        # -- means gh-pr/gh-review/squash-merge must not run (or must not be
        # trusted, if they already ran) against code review/scan never saw.
        # Distinct from `_blocked()`: this is not a stall, it is a signal
        # for the CALLER (`orchestrate.py`'s `_process_unit()`) to re-run
        # `pr_cycle.run_pr_cycle()` and retry this WHOLE stage from the top
        # -- which naturally redoes gh-pr (idempotent, reuses the existing
        # PR) and gh-review too, matching worker.md's own substantive branch
        # ("re-enter review+scan on the resolved diff first ... commit ->
        # push -> re-run /gh-review -> loop back to retry the merge").
        result = TailResult(
            stage_name=stage_name, status=NEEDS_REVIEW, reason=reason,
            role_results=dispatches, ship_decision=decision, pr_url=pr_url,
        )
        cycles_log.record_ship(
            workdir=workdir, stage_name=stage_name, passed=False,
            gates={"final_stage": False}, reason=reason,
        )
        return result

    unavailable = gh_available(workdir)
    if unavailable:
        return _blocked(unavailable)

    # --- Pre-ship sync: "sync to main FIRST -- before any CI runs" -------
    pre_status, pre_reason, dispatches, pre_changed, pre_substantive = run_sync_cycle(
        workdir=workdir, pr_title=pr_title, resolved=resolved, rundir=rundir,
        budget=budget, base=base, port=port, run_role_fn=run_role_fn,
    )
    if pre_status != "ok":
        return _blocked(pre_reason, role_results=dispatches)
    if pre_substantive:
        return _needs_review(
            "pre-ship sync resolved a substantive merge conflict -- review/scan must re-run "
            "before this unit may proceed (worker.md's mechanical/substantive rule)",
            dispatches,
        )

    # Imported lazily -- gh_pr.py imports this module (to reuse create_pr()'s
    # idempotency logic), so a module-level import here would be circular.
    from reasona_dev import gh_pr

    gh_pr_result = gh_pr.run_gh_pr(
        workdir=workdir, stage_name=stage_name, unit=unit, plan_name=plan_name, base=base,
    )
    if not gh_pr_result.passed:
        return _blocked(gh_pr_result.reason, pr_url=gh_pr_result.pr_url, role_results=dispatches)
    url = gh_pr_result.pr_url

    review_result = gh_review_mod.run_gh_review(
        workdir=workdir, pr_url=url, pr_num=gh_pr_result.pr_num, pr_title=pr_title,
        resolved=resolved, rundir=rundir, budget=budget, max_wait_seconds=gh_review_max_wait_seconds,
        port=port,
    )
    dispatches = dispatches + review_result.dispatches
    if not review_result.passed:
        return _blocked(review_result.reason, pr_url=url, role_results=dispatches)

    # --- Final phase: sync (catches main moving DURING the gh-pr+gh-review
    # CI window) -> conditional final_audit -> ship_gate, round-bounded.
    # Runs HERE -- after gh-pr/gh-review, not before -- so it re-verifies
    # whatever fix commits gh-review just made (`should_run_final_audit()`
    # already triggers on `budget.total_used > 0`, and gh_review.py spends
    # the SAME shared `budget` on its own fix dispatches).
    decision, status, phase_dispatches, reason = run_final_phase(
        workdir=workdir, stage_name=stage_name, pr_title=pr_title, profile=profile,
        resolved=resolved, rundir=rundir, budget=budget, recurrence=recurrence,
        cycle_verdict=cycle_verdict, ship_gate_fn=ship_gate_fn, base=base,
        port=port, run_role_fn=run_role_fn,
    )
    dispatches = dispatches + phase_dispatches
    audit = next((d for d in phase_dispatches if d.role == "compliance"), None)
    if status == "needs_review":
        return _needs_review(reason, dispatches, decision, pr_url=url)
    if status != "passed":
        return _blocked(reason, pr_url=url, final_audit=audit, role_results=dispatches, ship_decision=decision)

    msg, msg_reason = build_squash_message(unit_type=unit_type, title=pr_title)
    if msg is None:
        return _blocked(msg_reason, pr_url=url, final_audit=audit,
                        role_results=dispatches, ship_decision=decision)

    if not merge:
        return TailResult(
            stage_name=stage_name, status=PR_OPEN,
            reason=f"{gh_pr_result.reason}; merge not requested (pass merge=True to squash-merge)",
            pr_url=url, squash_message=msg, final_audit=audit, role_results=dispatches,
            ship_decision=decision,
        )

    # Re-checked here, not only inside the final phase: gh-pr/gh-review's own
    # round trips take more time for base to move in. Not looped back into
    # the final phase on failure -- see the module docstring's "what still
    # fails loudly rather than being retried".
    fresh, gate_reason = is_up_to_date(workdir, base=base)
    if not fresh:
        return _blocked(gate_reason, pr_url=url, squash_message=msg,
                        final_audit=audit, role_results=dispatches, ship_decision=decision)

    merged, merge_reason = squash_merge(workdir, msg)
    if not merged:
        return _blocked(merge_reason, pr_url=url, squash_message=msg,
                        final_audit=audit, role_results=dispatches, ship_decision=decision)

    cycles_log.record_ship(
        workdir=workdir, stage_name=stage_name, passed=True,
        gates={"final_stage": True}, reason=merge_reason,
    )
    return TailResult(
        stage_name=stage_name, status=MERGED, reason=merge_reason, pr_url=url,
        squash_message=msg, final_audit=audit, role_results=dispatches,
        ship_decision=decision,
    )
