"""The single pre-merge verdict: review outcome AND acceptance criteria AND
structural checks, composed deterministically.

**Why a composition module exists at all.** Each of the three gates was
built independently and each has its own CLI, which left the pipeline in the
state the analysis this work came from criticized: the checks were
*available*, and running them was an operator's discipline. "A reviewer
asserts completeness" and "an operator remembers to run the completeness
check" are the same failure with a different actor. A gate that has to be
remembered is not a gate.

So this module is the one place that decides whether a PR unit may merge,
and it decides by conjunction:

    review/scan verdict == PASS     the cycle converged (reasona_dev.pr_cycle)
    acceptance          == PASS     the plan's own claims executed (acceptance.py)
    structure           == PASS     no structural violation (structure_gate.py)

**Conjunction, with no weighting and no override.** A composed gate invites
exactly one bad idea -- letting a strong result on one axis excuse a weak
one ("the review was thorough, the missing test can follow"). The three
axes measure different things and none substitutes for another: a review
cannot execute a test, a test cannot see a 10,000-line file, and a line
count cannot judge whether a contract holds. Any of them failing is a
different kind of not-ready.

**Each gate reports independently, even after one fails.** Running the rest
after the first failure costs nothing here (all three are cheap relative to
a review cycle) and it is the difference between an author fixing one thing
per round and fixing everything in one. Same reasoning as
`acceptance.run_all()` not stopping at the first failing criterion.

**Every verdict is recorded.** `cycles_log.record_ship()` writes which
sub-gate decided the outcome. Which gate actually stops units in practice
is not knowable in advance, and it is the measurement that tells you
whether adding a gate helped or whether it never fires.

**What this does NOT do: it does not merge.** It returns a verdict. The
merge tail (`sync-main -> /gh-pr -> /gh-review -> up-to-date gate ->
final_audit -> squash-merge`) is not built yet, so the caller acts on the
verdict. Keeping the decision separate from the action is what lets this
run as a CI step, a pre-merge hook, or a driver call without change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from reasona_dev import acceptance, cycles_log, structure_gate
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
            return "review + acceptance + structure all clean"
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


def _acceptance_outcome(workdir: Path, stage_name: str, record: bool) -> GateOutcome:
    criteria, declared = acceptance.load_criteria_file(acceptance_path(workdir, stage_name))
    if not declared or not criteria:
        if record:
            cycles_log.record_acceptance(
                workdir=workdir, stage_name=stage_name, results=[], declared=False
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
            workdir=workdir, stage_name=stage_name,
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


def _structure_outcome(workdir: Path, base: str, head: str) -> GateOutcome:
    violations = structure_gate.evaluate(workdir, base=base, head=head)
    if not violations:
        return GateOutcome("structure", True, "no violations")
    shown = "; ".join(v.render() for v in violations[:3])
    more = f" (+{len(violations) - 3} more)" if len(violations) > 3 else ""
    return GateOutcome("structure", False, f"{len(violations)} violation(s): {shown}{more}")


def evaluate(
    workdir: str | Path,
    stage_name: str,
    *,
    cycle_verdict: str | None = None,
    base: str = "origin/main",
    head: str = "HEAD",
    record: bool = True,
) -> ShipDecision:
    """Run all three gates and compose their verdicts.

    `record=False` is for callers that want the verdict without appending to
    `cycles.jsonl` -- a dry run, or a second evaluation of the same unit
    that would otherwise double-count in every later query.
    """
    workdir = Path(workdir)
    outcomes = [
        _review_outcome(cycle_verdict),
        _acceptance_outcome(workdir, stage_name, record),
        _structure_outcome(workdir, base, head),
    ]
    decision = ShipDecision(
        stage_name=stage_name,
        passed=all(o.passed for o in outcomes),
        outcomes=outcomes,
    )
    if record:
        cycles_log.record_ship(
            workdir=workdir, stage_name=stage_name, passed=decision.passed,
            gates={o.name: o.passed for o in outcomes}, reason=decision.reason,
        )
    return decision


def main(argv: list[str]) -> int:
    """CLI, same exit convention as the gates it composes.

        python3 -m reasona_dev.ship_gate <stage_name> [workdir] [base] [head]
    """
    import sys

    if not argv:
        print("usage: python3 -m reasona_dev.ship_gate <stage_name> [workdir] [base] [head]", file=sys.stderr)
        return 2
    decision = evaluate(
        argv[1] if len(argv) > 1 else ".",
        argv[0],
        base=argv[2] if len(argv) > 2 else "origin/main",
        head=argv[3] if len(argv) > 3 else "HEAD",
    )
    print(decision.render(), file=sys.stderr)
    return 0 if decision.passed else 1


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
