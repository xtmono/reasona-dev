"""The single pre-merge verdict: the review outcome AND the plan's own
acceptance criteria, composed deterministically.

**Why a composition module exists at all.** Both checks were built
independently and each has its own entry point, which left the pipeline in
the state the analysis this work came from criticized: the checks were
*available*, and running them was an operator's discipline. "A reviewer
asserts completeness" and "an operator remembers to run the completeness
check" are the same failure with a different actor. A gate that has to be
remembered is not a gate.

So this module is the one place that decides whether a PR unit may merge,
and it decides by conjunction:

    review/scan verdict == PASS     the cycle converged (reasona_dev.pr_cycle)
    acceptance          == PASS     the plan's own claims executed (acceptance.py)

**Conjunction, with no weighting and no override.** A composed gate invites
exactly one bad idea -- letting a strong result on one axis excuse a weak
one ("the review was thorough, the missing test can follow"). The two axes
measure different things and neither substitutes for the other: a review
cannot execute a test, and a test cannot judge whether a contract holds.
Either failing is a different kind of not-ready.

**Both report independently, even after one fails.** Running the second
after the first fails costs nothing here and it is the difference between an
author fixing one thing per round and fixing everything in one. Same
reasoning as `acceptance.run_all()` not stopping at the first failing
criterion.

**Every verdict is recorded.** `cycles_log.record_ship()` writes which
sub-gate decided the outcome, so which gate actually stops units in practice
is measurable rather than assumed.

**What this does NOT do: it does not merge.** It returns a verdict;
`reasona_dev.final_phase` acts on it. Keeping the decision separate from the
action is what lets this run as a CI step, a pre-merge hook, or a driver
call without change.

**A structural gate used to be a third axis and was removed.** It checked
file size, single-PR growth, cross-file duplication, dependency direction
and public-API growth -- judgments a diff-reading reviewer genuinely cannot
make. It was removed because its checks are not equally suited to being a
hard gate: a refactor that splits a file improves the size check and trips
the growth check, and the waiver mechanism was repo-scoped and permanent
while a refactor's exemption is unit-scoped and temporary. Re-adding it
would need per-unit, plan-recorded waivers and an understanding of
`type: refactor`, neither of which existed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from reasona_dev import acceptance, cycles_log
from reasona_dev.plan_compile import acceptance_path


@dataclass
class GateOutcome:
    name: str
    passed: bool
    detail: str
    # Non-fatal notes -- currently the one case where a gate passes while
    # reporting something a reader should still see (a unit with no declared
    # acceptance criteria).
    warning: str | None = None


@dataclass
class ShipDecision:
    stage_name: str
    passed: bool
    outcomes: list[GateOutcome] = field(default_factory=list)

    @property
    def failures(self) -> list[GateOutcome]:
        return [o for o in self.outcomes if not o.passed]

    @property
    def reason(self) -> str:
        if self.passed:
            return "review + acceptance clean"
        return "; ".join(f"{o.name}: {o.detail}" for o in self.failures)

    def render(self) -> str:
        lines = [f"reasona-dev ship gate [{self.stage_name}]: {'PASS' if self.passed else 'FAIL'}"]
        for o in self.outcomes:
            mark = "PASS" if o.passed else "FAIL"
            lines.append(f"  [{mark}] {o.name}: {o.detail}")
            if o.warning:
                lines.append(f"         warning: {o.warning}")
        return "\n".join(lines)


def _review_outcome(cycle_verdict: str | None) -> GateOutcome:
    """The review/scan cycle's own verdict, passed in by the caller.

    Taken as a parameter rather than re-derived here because the cycle is a
    long-running process this module does not own; re-reading its result
    from disk would add a second source of truth for something the caller
    already holds. `None` means the caller is running the gate outside a
    cycle (a CI invocation, say) and is not asserting anything about it --
    reported explicitly as skipped rather than silently treated as a pass.
    """
    if cycle_verdict is None:
        return GateOutcome("review", True, "skipped (no cycle verdict supplied)")
    ok = cycle_verdict in ("PASS", "PASS_WITH_NOTES")
    return GateOutcome("review", ok, cycle_verdict)


def _acceptance_outcome(workdir: Path, stage_name: str, record: bool, log_workdir: Path) -> GateOutcome:
    criteria, declared = acceptance.load_criteria_file(acceptance_path(workdir, stage_name))
    if not declared or not criteria:
        if record:
            cycles_log.record_acceptance(
                workdir=workdir, log_workdir=log_workdir, stage_name=stage_name, results=[], declared=False
            )
        # Passing-with-warning, not failing: promoting this to a refusal is
        # the intended end state, but flipping it before any plan declares
        # criteria would block every unit. The recorded rows are what make
        # the coverage rate measurable, and therefore the promotion
        # decidable.
        return GateOutcome(
            "acceptance", True, "no criteria declared",
            warning="this unit's plan declares no executable acceptance criteria",
        )

    report = acceptance.run_all(criteria, workdir, stage_name=stage_name)
    if record:
        cycles_log.record_acceptance(
            workdir=workdir, log_workdir=log_workdir, stage_name=stage_name,
            results=[
                {"id": r.id, "expect": r.expect, "passed": r.passed,
                 "exit_code": r.exit_code, "error": r.error}
                for r in report.results
            ],
            declared=True,
        )
    if report.passed:
        return GateOutcome("acceptance", True, f"{len(report.results)}/{len(report.results)} criteria met")
    failed = ", ".join(r.id for r in report.failures)
    return GateOutcome(
        "acceptance", False,
        f"{len(report.failures)}/{len(report.results)} criteria failed: {failed}",
    )


def evaluate(
    workdir: str | Path,
    stage_name: str,
    *,
    cycle_verdict: str | None = None,
    record: bool = True,
    log_workdir: str | Path | None = None,
) -> ShipDecision:
    """Run all three gates and compose their verdicts.

    `record=False` is for callers that want the verdict without appending to
    `cycles.jsonl` -- a dry run, or a second evaluation of the same unit
    that would otherwise double-count in every later query.

    `log_workdir` (defaults to `workdir`): see `cycles_log.record_dispatch()`'s
    own docstring -- `workdir` here is a PR unit's own worktree (acceptance
    criteria have to run against its actual code), but `cycles.jsonl` needs
    the TOP-LEVEL repo or the record is lost the moment `orchestrate.py`
    deletes this worktree on a successful merge -- which is exactly the
    call this function's own `record_ship()` makes right before that
    happens.
    """
    workdir = Path(workdir)
    log_workdir = Path(log_workdir) if log_workdir is not None else workdir
    outcomes = [
        _review_outcome(cycle_verdict),
        _acceptance_outcome(workdir, stage_name, record, log_workdir),
    ]
    decision = ShipDecision(
        stage_name=stage_name,
        passed=all(o.passed for o in outcomes),
        outcomes=outcomes,
    )
    if record:
        cycles_log.record_ship(
            workdir=workdir, log_workdir=log_workdir, stage_name=stage_name, passed=decision.passed,
            gates={o.name: o.passed for o in outcomes}, reason=decision.reason,
        )
    return decision


def main(argv: list[str]) -> int:
    """CLI, same exit convention as the gates it composes.

        python3 -m reasona_dev.ship_gate <stage_name> [workdir]
    """
    import sys

    if not argv:
        print("usage: python3 -m reasona_dev.ship_gate <stage_name> [workdir] [base] [head]", file=sys.stderr)
        return 2
    decision = evaluate(argv[1] if len(argv) > 1 else ".", argv[0])
    print(decision.render(), file=sys.stderr)
    return 0 if decision.passed else 1


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
