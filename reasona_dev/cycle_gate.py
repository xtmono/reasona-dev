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
MAX_FINAL_CYCLES = 3
MAX_SYNC_CYCLES = 3
MAX_TOTAL_FIX_CYCLES = 16

# `ship_gate`'s acceptance axis used to have no fix loop at all -- the first
# failure terminated the unit immediately, unlike every other check in this
# pipeline (review/scan/final_audit/sync all dispatch a bounded dev-fix
# before giving up). That made a genuinely fixable acceptance failure
# terminate the unit with zero attempts at a real fix. `MAX_SHIP_CYCLES`
# bounds dev's chance to fix a failing acceptance criterion the same way
# every other stage is bounded. This budget being exhausted is still a
# `blocked` outcome, not a `failed` one -- see final_phase.py's module
# docstring: everything past review/scan (sync, final_audit, ship_gate,
# final-phase non-convergence) reports `blocked` when its own bounded
# dev-fix attempts run out, since by that point three independent roles
# have already vetted the code and a stall this late is an anomaly to
# investigate, not an ordinary review-found defect.
MAX_SHIP_CYCLES = 3

# How many times the sync -> final_audit -> ship_gate tail re-verifies
# itself. A conflict resolved by `sync_cycle` or a fix made by
# `final_audit` both change the code AFTER the step before it already
# looked -- so either one, in the same round, means that round's verdict
# is stale and the whole tail runs again from sync. Bounded rather than
# unconditional: base moving again during our own tail processing is rare,
# and a plan whose target keeps moving faster than this pipeline can settle
# is not something retrying indefinitely would fix.
MAX_FINAL_PHASE_ROUNDS = 3

# `/gh-review`'s own default `--max-cycle` (`~/repository/tas-dev-plugins/
# plugins/dev/skills/gh-review/SKILL.md` §1). Bounds `reasona_dev.gh_review`'s
# CI/compliance/bugbot auto-fix loop, pooled into the same
# `MAX_TOTAL_FIX_CYCLES` every other stage shares
# (`min(MAX_GH_REVIEW_CYCLES, MAX_TOTAL_FIX_CYCLES - budget.total_used)`,
# mirroring dev-ralf's own pooling rule for this exact stage). Exhausting it
# is `blocked`, not `failed` -- same reasoning as `MAX_SHIP_CYCLES` above.
MAX_GH_REVIEW_CYCLES = 3

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
    sync_cycles: int = 0
    ship_cycles: int = 0
    gh_review_cycles: int = 0
    total_used: int = 0

    def can_spend(self, stage: str) -> bool:
        cap = {
            "review": MAX_REVIEW_CYCLES,
            "scan": MAX_SCAN_CYCLES,
            "final": MAX_FINAL_CYCLES,
            "sync": MAX_SYNC_CYCLES,
            "ship": MAX_SHIP_CYCLES,
            "gh_review": MAX_GH_REVIEW_CYCLES,
        }[stage]
        used = {
            "review": self.review_cycles, "scan": self.scan_cycles,
            "final": self.final_cycles, "sync": self.sync_cycles,
            "ship": self.ship_cycles, "gh_review": self.gh_review_cycles,
        }[stage]
        return used < cap and self.total_used < MAX_TOTAL_FIX_CYCLES

    def spend(self, stage: str) -> None:
        if stage == "review":
            self.review_cycles += 1
        elif stage == "scan":
            self.scan_cycles += 1
        elif stage == "final":
            self.final_cycles += 1
        elif stage == "sync":
            self.sync_cycles += 1
        elif stage == "ship":
            self.ship_cycles += 1
        elif stage == "gh_review":
            self.gh_review_cycles += 1
        self.total_used += 1

    # JSON roundtrip -- the ledger (reasona_dev.ledger) checkpoints this
    # every cycle so a resumed run does not re-litigate a budget an
    # interrupted run had already spent. Every field is a plain int, so
    # this is asdict()/the constructor, named for symmetry with the other
    # two trackers below (RecurrenceTracker.escalated needs real conversion).
    def to_dict(self) -> dict:
        return {
            "review_cycles": self.review_cycles, "scan_cycles": self.scan_cycles,
            "final_cycles": self.final_cycles, "sync_cycles": self.sync_cycles,
            "ship_cycles": self.ship_cycles, "gh_review_cycles": self.gh_review_cycles,
            "total_used": self.total_used,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "FixBudget":
        return cls(
            review_cycles=d.get("review_cycles", 0), scan_cycles=d.get("scan_cycles", 0),
            final_cycles=d.get("final_cycles", 0), sync_cycles=d.get("sync_cycles", 0),
            ship_cycles=d.get("ship_cycles", 0), gh_review_cycles=d.get("gh_review_cycles", 0),
            total_used=d.get("total_used", 0),
        )


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

    def to_dict(self) -> dict:
        # `escalated` is a set -- JSON has no set literal, so it round-trips
        # as a sorted list (sorted only for a stable diff in the ledger
        # file, membership is what `decide()` actually uses).
        return {"survived": dict(self.survived), "escalated": sorted(self.escalated)}

    @classmethod
    def from_dict(cls, d: dict) -> "RecurrenceTracker":
        return cls(survived=dict(d.get("survived", {})), escalated=set(d.get("escalated", [])))


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

    def to_dict(self) -> dict:
        return {"counts": list(self.counts)}

    @classmethod
    def from_dict(cls, d: dict) -> "ConvergenceTracker":
        return cls(counts=list(d.get("counts", [])))


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
