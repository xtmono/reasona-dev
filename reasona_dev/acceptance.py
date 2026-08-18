"""Executable acceptance criteria -- turning "a reviewer asserts the PR is
complete" into "the process executes completeness".

**The failure this addresses.** dev-ralf's plan format already requires
"Tests (positive + negative)" in every PR section, but as PROSE. A reviewer
confirms such an item EXISTS; nothing confirms it RUNS, or that what it
asserts is the thing the plan promised. The observed consequence is a merged
PR whose named test symbol was never written -- a defect no amount of
additional review catches, because every reviewer reads the same diff and
the diff is not where the absence is visible. The plan says a symbol should
exist; only executing something can find out whether it does.

So the criterion moves from the prose body into the machine-readable
manifest, and from "checked by reading" into "checked by running":

    pr_units:
      - index: 3
        title: "Add parse_config_compat"
        acceptance:
          - id: AC-3-1
            cmd: "cargo test -p mycrate parse_config_compat"
            expect: exit0
          - id: AC-3-2
            cmd: "cargo test -p mycrate parse_config_incompat_rejected"
            expect: exit_nonzero

**Why `exit_nonzero` exists.** A negative test is the half most often
dropped, and an `expect: pass`-only vocabulary cannot express it at all --
"this input must be REJECTED" becomes unstatable, so it silently degrades
into another positive test. Three expectations cover what a deterministic
gate can honestly assert:

    exit0           the command succeeds
    exit_nonzero    the command fails -- the negative case actually rejects
    stdout_matches  stdout matches `pattern` -- the shape of a value

**Why this runs in reasona-dev's driver, not as a Bernstein
`completion_signals` entry.** Two facts about Bernstein's completion path,
both traced in the installed 3.15.1 source, rule that placement out:

1. Signals are evaluated against `orch._workdir` -- one fixed project root
   (`task_lifecycle.py:3916`, `executor.submit(verify_task_completion,
   task, orch._workdir)`), never the per-task worktree the agent worked in.
2. That evaluation happens BEFORE the agent's branch is merged. The janitor
   future is resolved at `task_lifecycle.py:4055`
   (`_resolve_janitor_result`); the merge happens afterwards, inside
   `_reap_and_cleanup_session` at :3061 (`orch._spawner.
   reap_completed_agent(...)`), and is in fact CONDITIONAL on the janitor
   having already passed (:3076).

So at the moment a `test_passes` command runs, the PR's code is
definitively not in the tree the command runs against -- it is still only
on `agent/<id>`. An acceptance criterion placed there does not merely risk
being wrong; it either fails always, or passes vacuously by testing the
pre-existing code. The driver, by contrast, controls when and against which
tree it runs, so the criterion executes where the PR's code demonstrably
is. This mirrors how `gate_check.py` already sidesteps the same constraint
by reading a file the driver itself wrote to that root.

**What this deliberately does NOT do.** It does not validate that the
criterion is the RIGHT one. A wrong AC deterministically approves a wrong
state; that layer belongs to plan authoring and its multi-reviewer
convergence, not here. Keeping the boundary sharp is what stops an AC from
drifting back into prose: this module answers "was the stated thing done",
never "was the right thing stated".

**Partial credit does not exist.** One failing criterion fails the unit.
A gate that reports "7 of 9 passed" invites the same judgment call it was
built to remove.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

EXPECT_EXIT0 = "exit0"
EXPECT_EXIT_NONZERO = "exit_nonzero"
EXPECT_STDOUT_MATCHES = "stdout_matches"
_VALID_EXPECTATIONS = frozenset({EXPECT_EXIT0, EXPECT_EXIT_NONZERO, EXPECT_STDOUT_MATCHES})

DEFAULT_TIMEOUT_S = 600


@dataclass(frozen=True)
class AcceptanceCriterion:
    id: str
    cmd: str
    expect: str = EXPECT_EXIT0
    pattern: str | None = None
    timeout_s: int = DEFAULT_TIMEOUT_S

    def validation_error(self) -> str | None:
        """Structural problems, checked at parse time rather than run time.

        A malformed criterion is a plan defect, and a plan defect should
        surface when the plan is compiled -- not thirty minutes into a run
        when the gate finally tries to execute it.
        """
        if not self.id.strip():
            return "acceptance criterion has no id"
        if not self.cmd.strip():
            return f"{self.id}: no cmd"
        if self.expect not in _VALID_EXPECTATIONS:
            return f"{self.id}: expect must be one of {sorted(_VALID_EXPECTATIONS)}, got {self.expect!r}"
        if self.expect == EXPECT_STDOUT_MATCHES:
            if not self.pattern:
                return f"{self.id}: expect=stdout_matches requires a pattern"
            try:
                re.compile(self.pattern)
            except re.error as exc:
                return f"{self.id}: pattern is not a valid regex ({exc})"
        return None


@dataclass
class ACResult:
    id: str
    cmd: str
    expect: str
    passed: bool
    exit_code: int | None
    stdout_tail: str
    duration_s: float
    error: str | None = None


@dataclass
class AcceptanceReport:
    stage_name: str
    results: list[ACResult] = field(default_factory=list)
    declared: bool = True

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results)

    @property
    def failures(self) -> list[ACResult]:
        return [r for r in self.results if not r.passed]


def parse_criteria(raw) -> tuple[list[AcceptanceCriterion], list[str]]:
    """Build criteria from a manifest's `acceptance:` list.

    Returns `(criteria, errors)`. Errors are collected rather than raised so
    a plan with several malformed criteria reports all of them at once --
    fixing them one compile at a time is the kind of friction that makes an
    author drop the field entirely.
    """
    if raw is None:
        return [], []
    if not isinstance(raw, list):
        return [], ["acceptance must be a list"]

    criteria: list[AcceptanceCriterion] = []
    errors: list[str] = []
    seen: set[str] = set()
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            errors.append(f"acceptance[{i}] is not a mapping")
            continue
        timeout = item.get("timeout_s", DEFAULT_TIMEOUT_S)
        # `or ""` rather than a `.get` default: YAML turns a bare `id:` into
        # an explicit None, which `.get("id", "")` happily returns and
        # `str()` then renders as the literal "None" -- a non-empty string
        # that passes every validation and becomes a real, silently wrong
        # join key.
        c = AcceptanceCriterion(
            id=str(item.get("id") or "").strip(),
            cmd=str(item.get("cmd") or ""),
            expect=str(item.get("expect") or EXPECT_EXIT0),
            pattern=item.get("pattern"),
            timeout_s=timeout if isinstance(timeout, int) and timeout > 0 else DEFAULT_TIMEOUT_S,
        )
        err = c.validation_error()
        if err:
            errors.append(err)
            continue
        # The id is the join key into cycles.jsonl, so a duplicate would
        # silently merge two different criteria in every later query.
        if c.id in seen:
            errors.append(f"duplicate acceptance id: {c.id}")
            continue
        seen.add(c.id)
        criteria.append(c)
    return criteria, errors


def _judge(criterion: AcceptanceCriterion, exit_code: int, stdout: str) -> bool:
    if criterion.expect == EXPECT_EXIT0:
        return exit_code == 0
    if criterion.expect == EXPECT_EXIT_NONZERO:
        return exit_code != 0
    return bool(re.search(criterion.pattern or "", stdout))


def run_criterion(criterion: AcceptanceCriterion, workdir: Path) -> ACResult:
    """Execute one criterion. A timeout is a FAILURE, never an unknown.

    Leaving a criterion unjudged would let a hanging command read as
    "inconclusive, proceed", which is exactly the silent pass this gate
    exists to prevent.
    """
    started = time.monotonic()
    try:
        proc = subprocess.run(
            criterion.cmd, shell=True, cwd=str(workdir),
            capture_output=True, text=True, timeout=criterion.timeout_s,
        )
    except subprocess.TimeoutExpired:
        return ACResult(
            id=criterion.id, cmd=criterion.cmd, expect=criterion.expect, passed=False,
            exit_code=None, stdout_tail="", duration_s=time.monotonic() - started,
            error=f"timed out after {criterion.timeout_s}s",
        )
    except OSError as exc:
        return ACResult(
            id=criterion.id, cmd=criterion.cmd, expect=criterion.expect, passed=False,
            exit_code=None, stdout_tail="", duration_s=time.monotonic() - started,
            error=f"could not execute: {exc}",
        )

    stdout = proc.stdout or ""
    return ACResult(
        id=criterion.id, cmd=criterion.cmd, expect=criterion.expect,
        passed=_judge(criterion, proc.returncode, stdout),
        exit_code=proc.returncode,
        # Tail, not head: a test runner's verdict is at the end of its output.
        stdout_tail=stdout[-2000:],
        duration_s=time.monotonic() - started,
    )


def run_all(
    criteria: list[AcceptanceCriterion],
    workdir: str | Path,
    *,
    stage_name: str = "",
    stop_on_first_failure: bool = False,
) -> AcceptanceReport:
    """Run every criterion in declaration order.

    Runs them ALL by default rather than stopping at the first failure: the
    point of a pre-merge gate is to tell the author everything that is
    wrong in one pass, not to make them rediscover the next failure after
    each fix.
    """
    workdir = Path(workdir)
    report = AcceptanceReport(stage_name=stage_name, declared=bool(criteria))
    for c in criteria:
        result = run_criterion(c, workdir)
        report.results.append(result)
        if stop_on_first_failure and not result.passed:
            break
    return report


def render(report: AcceptanceReport) -> str:
    if not report.declared:
        return "acceptance: no criteria declared for this unit"
    lines = []
    for r in report.results:
        mark = "PASS" if r.passed else "FAIL"
        detail = r.error or f"exit={r.exit_code}"
        lines.append(f"  [{mark}] {r.id} ({r.expect}, {detail}) -- {r.cmd}")
    verdict = "PASS" if report.passed else "FAIL"
    return f"acceptance {verdict} ({len(report.results)} criteria)\n" + "\n".join(lines)


def load_criteria_file(path: str | Path) -> tuple[list[AcceptanceCriterion], bool]:
    """Read the `.reasona/acceptance-<stage>.json` `plan_compile` wrote.

    Returns `(criteria, declared)`. `declared=False` means the file is
    absent -- a unit whose plan named no criteria, which is currently a
    warning rather than a refusal (see `main`).
    """
    path = Path(path)
    if not path.is_file():
        return [], False
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return [], False
    criteria, _ = parse_criteria(raw.get("criteria"))
    return criteria, True


def main(argv: list[str]) -> int:
    """Pre-merge acceptance gate, same exit convention as `gate_check.py`.

        python3 -m reasona_dev.acceptance <acceptance.json> [workdir]

    Exit 0 -> every criterion met (or none declared, see below).
    Exit 1 -> at least one criterion failed.

    **A unit with no declared criteria currently PASSES with a warning.**
    Refusing outright is the eventual target, but flipping it on today
    would block every plan written before the field existed. The warning is
    counted in `.reasona/cycles.jsonl` by the driver, so the decision to
    promote it to a refusal can be made from the measured coverage rate
    rather than by guess.
    """
    import sys

    if not argv:
        print("usage: python3 -m reasona_dev.acceptance <acceptance.json> [workdir]", file=sys.stderr)
        return 2
    workdir = argv[1] if len(argv) > 1 else "."
    criteria, declared = load_criteria_file(argv[0])
    if not declared or not criteria:
        print("reasona-dev acceptance: WARN -- no criteria declared for this unit", file=sys.stderr)
        return 0
    report = run_all(criteria, workdir, stage_name=Path(argv[0]).stem)
    print(render(report), file=sys.stderr)
    return 0 if report.passed else 1


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
