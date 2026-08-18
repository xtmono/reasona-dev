"""Deterministic cycle control: recheck routing, escalation, budget, fingerprints.

Zero-LLM logic. `reasona_dev.pr_cycle` calls `evaluate()` before deciding
whether to dispatch a fix or recheck. It ran inside a `on_pre_task_create`
pluggy hook in an earlier design, where fix cycles were separate `bernstein
run` invocations the driver could not see coming; the driver now owns the
loop, so the decision happens where the budget and trackers already live
(see `reasona_dev.plugin` on why the hook half was removed rather than kept
alongside). Ports dev-ralf's `cycle_gate.py` + the renewal contract
(dev-ralf-renewal-claude.md §3.5, §3.8, §3.9) onto Bernstein's task model.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field

from reasona_dev.finding_adapter import Finding, ReviewResult

# dev-ralf-renewal-claude.md §3.9 -- stage caps + one binding total.
MAX_REVIEW_CYCLES = 8
MAX_SCAN_CYCLES = 8
MAX_FINAL_CYCLES = 2
MAX_TOTAL_FIX_CYCLES = 16

# New rule agreed in this design track: a MUST_FIX key surviving one
# completed fix earns exactly one bounded escalation of the dev role to a
# stronger model before the PR is declared FAIL. This is NOT the same
# mechanism as Bernstein's `model_fallback` (exhaustion/429/503/529) or
# `cascade_router` (confidence-based auto-escalation) -- those stay
# constrained per CREDIT-BURN; this is our own hook logic, gated on
# non-convergence, bounded to one attempt, and always logged.
ESCALATION_ATTEMPTS_ALLOWED = 1

# How many times a role may come back INCONCLUSIVE before the PR is given up
# on. An INCONCLUSIVE role is one whose VERIFICATION did not run (dev-ralf-
# renewal §6) -- re-running it is the right first response, but only a bounded
# number of times: the condition is usually environmental (a tool missing, a
# service down) and does not improve by asking again.
MAX_INCONCLUSIVE_ATTEMPTS = 3


# NOTE: review-reuse fingerprinting (`three_dot_diff_hash` / `base_scope_hash`
# / a `Fingerprint` comparing plan-unit + patch + base-scope hashes) lived
# here and was removed. It was never called and never tested: the design it
# belonged to -- skip a re-review when the diff and its base are provably
# unchanged -- was carried over from dev-ralf §3.6 but no caller in this
# project ever reached it. Uncalled, untested code is a claim the codebase
# cannot back; the design is recorded in docs/ARCHITECTURE.md and can be
# rebuilt from there when there is a caller for it.


@dataclass
class FixBudget:
    review_cycles: int = 0
    scan_cycles: int = 0
    final_cycles: int = 0
    total_used: int = 0

    def can_spend(self, stage: str) -> bool:
        cap = {
            "review": MAX_REVIEW_CYCLES,
            "scan": MAX_SCAN_CYCLES,
            "final": MAX_FINAL_CYCLES,
        }[stage]
        used = {"review": self.review_cycles, "scan": self.scan_cycles, "final": self.final_cycles}[stage]
        return used < cap and self.total_used < MAX_TOTAL_FIX_CYCLES

    def spend(self, stage: str) -> None:
        if stage == "review":
            self.review_cycles += 1
        elif stage == "scan":
            self.scan_cycles += 1
        elif stage == "final":
            self.final_cycles += 1
        self.total_used += 1


@dataclass
class RecurrenceTracker:
    """Tracks how many completed fixes a MUST_FIX key has survived."""

    survived: dict[str, int] = field(default_factory=dict)
    escalated: set[str] = field(default_factory=set)

    def record_post_fix(self, still_present: list[Finding]) -> None:
        for f in still_present:
            self.survived[f.key()] = self.survived.get(f.key(), 0) + 1

    def clear(self, key: str) -> None:
        self.survived.pop(key, None)
        self.escalated.discard(key)

    def decide(self, key: str) -> str:
        """PROCEED | ESCALATE_ONCE | FAIL -- dev-ralf-renewal-claude.md §3.5 + new escalation rule."""
        n = self.survived.get(key, 0)
        if n == 0:
            return "PROCEED"
        if n == 1 and key not in self.escalated:
            self.escalated.add(key)
            return "ESCALATE_ONCE"
        return "FAIL"  # survived escalation too -- stop-the-world, not another retry


@dataclass
class ConvergenceTracker:
    """Tracks whether MUST_FIX COUNT is actually falling across cycles.

    **The hole this closes.** `RecurrenceTracker` only fires when the SAME
    finding key survives a fix. A PR whose every cycle produces a fresh set
    of MUST_FIX keys therefore gets `PROCEED` forever and burns the entire
    stage cap (8 cycles) before failing -- the most expensive failure mode
    in the budget, and the one `recheck_route()` cannot help with, since
    that only lowers the cost of each cycle, never the number of them.

    dev-ralf's escalation trigger was `cross_reviewer_convergence` (two
    reviewers naming the same location). This is its missing temporal dual:
    agreement across CYCLES rather than across reviewers. Both exist for
    the same reason -- a single observation is weak evidence, so the gate
    waits for a second one before acting.

    Deliberately counts findings rather than judging them. "Is this PR
    getting better" is a question a model would answer with a narrative;
    `len(must_fix)` over a window answers it arithmetically, which is the
    only kind of answer this pipeline acts on.
    """

    counts: list[int] = field(default_factory=list)

    def record(self, must_fix_count: int) -> None:
        self.counts.append(must_fix_count)

    def diverging(self, window: int = 3) -> bool:
        """True once `window` cycles have passed with no net reduction.

        Compares the window's last count to its first: strictly fewer
        findings than `window` cycles ago is progress, anything else is
        not. Oscillation (3 -> 5 -> 3) reads as no progress, which is the
        intended reading -- it is a PR trading one defect for another, not
        one converging.

        Returns False until `window` cycles exist, so a PR is never failed
        before it has had a real chance to improve.
        """
        if len(self.counts) < window:
            return False
        recent = self.counts[-window:]
        return recent[-1] >= recent[0]


def recheck_route(repo: str, pre_fix_head: str, finding_files: set[str]) -> str:
    """BOUNDED | FULL -- dev-ralf-renewal-claude.md §3.8 / §8.4.

    fix_files subset of finding_files -> bounded (confirm + regression only).
    Any path outside finding_files -> full (the fix touched new ground, so
    the initial reviewer set re-runs a fresh omission hunt on it). This is a
    pure function of `git diff --name-only`, unrelated to any model's
    self-reported severity -- deliberately not routed by severity
    self-classification (dev-ralf-renewal-claude.md §3.3).
    """
    out = subprocess.run(
        ["git", "-C", repo, "diff", "--name-only", f"{pre_fix_head}..HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout
    fix_files = {line.strip() for line in out.splitlines() if line.strip()}
    return "BOUNDED" if fix_files <= finding_files else "FULL"


@dataclass
class GateDecision:
    action: str  # "spawn_fix" | "spawn_fix_escalated" | "pass" | "fail" | "inconclusive_retry" | "abort"
    reason: str
    escalated_model: str | None = None


def evaluate(
    result: ReviewResult,
    budget: FixBudget,
    stage: str,
    recurrence: RecurrenceTracker,
    inconclusive_attempts: int,
    escalation_model: str = "opus",
    convergence: ConvergenceTracker | None = None,
    convergence_window: int = 3,
) -> GateDecision:
    """The single entry point `pr_cycle` calls before spawning a fix task.

    `convergence` is optional so existing callers keep their exact
    behaviour; when supplied, it adds the non-convergence exit described in
    `ConvergenceTracker` -- a PR that stops improving fails at
    `convergence_window` cycles instead of at the stage cap.
    """
    gate = result.gate()

    if gate == "INCONCLUSIVE":
        # The caller MUST carry this count across cycles. It used to be passed
        # as a literal 0 by `pr_cycle`, which made this branch return
        # `inconclusive_retry` forever -- and because it returns before the
        # budget check below, no budget was ever spent and no cap was ever
        # reached. A role stuck on INCONCLUSIVE re-dispatched a real agent on
        # an unbounded loop.
        if inconclusive_attempts >= MAX_INCONCLUSIVE_ATTEMPTS:
            return GateDecision(
                "abort",
                f"inconclusive retry budget exhausted ({MAX_INCONCLUSIVE_ATTEMPTS} attempts) "
                "-- verification never ran, which is an environment problem, not a code one",
            )
        return GateDecision(
            "inconclusive_retry",
            f"attempt {inconclusive_attempts + 1}/{MAX_INCONCLUSIVE_ATTEMPTS}",
        )

    if gate == "ERROR":
        return GateDecision("abort", "role/model unavailable -- hard blocker, never swap")

    if gate in ("PASS", "PASS_WITH_NOTES"):
        return GateDecision("pass", gate)

    # FIX_REQUIRED
    if not budget.can_spend(stage):
        return GateDecision("fail", f"{stage} budget exhausted ({budget.total_used}/{MAX_TOTAL_FIX_CYCLES})")

    # Recorded here so the window always reflects THIS cycle's count, but
    # CHECKED below, after recurrence -- the two rules are independent
    # exits that overlap on the same-key case, and recurrence's reason
    # ("this exact finding survived an escalated fix") is the more specific
    # of the two, so it should be the one reported when both apply.
    if convergence is not None:
        convergence.record(len(result.must_fix))

    decisions = {recurrence.decide(f.key()) for f in result.must_fix}
    if "FAIL" in decisions:
        return GateDecision("fail", "MUST_FIX key survived escalated fix -- stop-the-world")

    # The exit recurrence structurally cannot reach: findings that keep
    # changing identity never accumulate a survival count, so without this
    # the stage would run to its full cap.
    if convergence is not None and convergence.diverging(convergence_window):
        return GateDecision(
            "fail",
            f"no net reduction in MUST_FIX over {convergence_window} cycles "
            f"(counts: {convergence.counts[-convergence_window:]}) -- not converging",
        )

    if "ESCALATE_ONCE" in decisions:
        budget.spend(stage)
        return GateDecision(
            "spawn_fix_escalated",
            "recurring finding -- one bounded escalation before FAIL",
            escalated_model=escalation_model,
        )

    budget.spend(stage)
    return GateDecision("spawn_fix", f"{len(result.must_fix)} MUST_FIX finding(s)")
