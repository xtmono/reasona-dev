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
# Every value matched against `~/repository/tas-dev-plugins/plugins/dev/
# skills/dev-ralf/tools/budget.py`'s `STAGE_CAPS` (the single authority
# there since it absorbed the arithmetic worker.md used to restate) and
# worker.md's own section headings: review 8, scan 8, final 3, sync 3,
# ship 3, total 16.
#
# `final` moved 3 -> 2 -> 3 across two parity passes and is worth reading
# as one story rather than three: reasona-dev ran 3, a source-level check
# found worker.md at 2 and aligned this to 2 (docs/ARCHITECTURE.md
# §3.14.6), and both projects were then raised back to 3 by the same
# operator decision -- dev-ralf in `5ff9641`, reasona-dev here (§3.14.8).
# The two are consistent at 3; nothing here is a deliberate divergence.
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

# `/gh-review`'s own default `--max-cycle`. NOT a budget stage of its own --
# a ceiling on how many fix cycles ONE `/gh-review` invocation may run, the
# same shape as dev-ralf's `budget.py` `GH_REVIEW_MAX_CYCLE`, which likewise
# sits beside `STAGE_CAPS` rather than inside it. The effective value is
# `gh_review_cap()` below; the cycles themselves are charged to the `review`
# stage (worker.md -> *Fix budget accounting*: "After `/gh-review` returns,
# call `spend --stage review` once per reported `FIX_COMMITS`"). This used
# to be a sixth `gh_review` stage in `FixBudget` with its own cap, which
# handed gh-review 3 cycles ON TOP of review's 8 instead of out of them.
# Exhausting it is `blocked`, not `failed` -- same reasoning as
# `MAX_SHIP_CYCLES` above.
GH_REVIEW_MAX_CYCLE = 3

# A sync conflict `run_sync_cycle()` classifies as SUBSTANTIVE (the dev
# role's own self-report -- see `final_phase.parse_conflict_kind()`) means
# the resolution changed code review/scan never saw, so `orchestrate.py`
# re-enters `pr_cycle.run_pr_cycle()` from scratch before the final stage
# is allowed to proceed to gh-pr/gh-review/squash-merge (worker.md's own
# mechanical/substantive distinction for conflict resolution -- a
# MECHANICAL resolution, e.g. import order or formatting, does not earn
# this; see `final_phase.NEEDS_REVIEW`).
#
# Unconditional, not round-capped -- matches dev-ralf exactly (worker.md
# §228/§277 have no numbered bound here, just "re-enter ... loop back to
# retry"). reasona-dev used to bound this at a `MAX_SUBSTANTIVE_RESYNC_ROUNDS`
# retry count (removed) AND reset the whole shared fix-cycle budget on every
# retry (also fixed -- `pr_cycle.run_pr_cycle()`'s `carried_budget` param) --
# a double divergence from dev-ralf's design that let a unit spend multiple
# fresh 16-cycle budgets instead of one 16-cycle budget for its whole life
# (`BUDGET_STATE` "is never reset mid-PR", worker.md's *Result block*). The
# real backstop against runaway retries is the same one dev-ralf relies on:
# the shared budget itself running out (`run_sync_cycle()` refuses a
# conflict-fix dispatch once `can_spend("sync")` is False, returning
# `blocked`, never another `needs_review`) -- see `docs/ARCHITECTURE.md`
# §3.23 for the full incident.

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
    total_used: int = 0

    def can_spend(self, stage: str) -> bool:
        cap = {
            "review": MAX_REVIEW_CYCLES,
            "scan": MAX_SCAN_CYCLES,
            "final": MAX_FINAL_CYCLES,
            "sync": MAX_SYNC_CYCLES,
            "ship": MAX_SHIP_CYCLES,
        }[stage]
        used = {
            "review": self.review_cycles, "scan": self.scan_cycles,
            "final": self.final_cycles, "sync": self.sync_cycles,
            "ship": self.ship_cycles,
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
        else:
            raise KeyError(f"unknown fix-budget stage {stage!r}")
        self.total_used += 1

    def gh_review_cap(self) -> int:
        """How many fix cycles ONE `/gh-review` invocation may run --
        dev-ralf `budget.py`'s `gh_review_cap()` verbatim:
        `max(0, min(GH_REVIEW_MAX_CYCLE, total - total_used))`. Floored at
        zero so an already-exhausted pool yields 0 rather than a negative
        cap the caller would have to special-case.
        """
        return max(0, min(GH_REVIEW_MAX_CYCLE, MAX_TOTAL_FIX_CYCLES - self.total_used))

    # JSON roundtrip -- the ledger (reasona_dev.ledger) checkpoints this
    # every cycle so a resumed run does not re-litigate a budget an
    # interrupted run had already spent. Every field is a plain int, so
    # this is asdict()/the constructor, named for symmetry with the other
    # two trackers below (RecurrenceTracker.escalated needs real conversion).
    def to_dict(self) -> dict:
        return {
            "review_cycles": self.review_cycles, "scan_cycles": self.scan_cycles,
            "final_cycles": self.final_cycles, "sync_cycles": self.sync_cycles,
            "ship_cycles": self.ship_cycles,
            "total_used": self.total_used,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "FixBudget":
        return cls(
            review_cycles=d.get("review_cycles", 0), scan_cycles=d.get("scan_cycles", 0),
            final_cycles=d.get("final_cycles", 0), sync_cycles=d.get("sync_cycles", 0),
            ship_cycles=d.get("ship_cycles", 0),
            # A ledger written while gh-review had its own sixth stage
            # carries `gh_review_cycles`; those cycles are already inside
            # `total_used`, so dropping the field loses no accounting.
            total_used=d.get("total_used", 0),
        )


@dataclass
class RecurrenceTracker:
    """Tracks how many completed fixes a MUST_FIX key has survived."""

    survived: dict[str, int] = field(default_factory=dict)
    # ONE escalation per PR, not one per key -- worker.md -> *Deterministic
    # dev escalation*: "ONE escalation per PR, and it is capped ... not an
    # uncapped ladder", and the scan section's "the SAME escalation budget
    # applies here as in review -- one per PR total, not one per stage".
    # dev-ralf enforces this with a single `already_escalated` boolean
    # (`finding_merge.escalation_decision`); this used to be a `set[str]` of
    # escalated keys here, which let a PR escalate once per DISTINCT key --
    # exactly the uncapped ladder that wording forbids.
    escalated: bool = False
    # Last cycle's MUST_FIX keys. `observed_recurrence` is the INTERSECTION
    # of this cycle's keys with the previous cycle's
    # (`finding_merge.escalation_decision`: `set(current) & set(prior)`) --
    # `record_cycle()` used to increment `survived` for every MUST_FIX
    # present from cycle 2 on, counting a brand-new finding as one that had
    # "survived" a fix it was never subject to.
    previous_keys: set[str] = field(default_factory=set)

    def record_cycle(self, must_fix: list[Finding]) -> None:
        """Call once per cycle, BEFORE `evaluate()`, on EVERY cycle.

        A key only counts as having survived a fix when it was present in
        the immediately-preceding cycle too. Calling this every cycle (not
        only from cycle 2 on) is what keeps `previous_keys` accurate across
        a stage boundary: review exits with an empty MUST_FIX list, so the
        scan stage's first cycle intersects against an empty set and cannot
        inherit a review finding as a spurious recurrence.
        """
        current = {f.key() for f in must_fix}
        for key in current & self.previous_keys:
            self.survived[key] = self.survived.get(key, 0) + 1
        self.previous_keys = current

    def clear(self, key: str) -> None:
        self.survived.pop(key, None)
        self.previous_keys.discard(key)

    def escalation_decision(
        self, must_fix: list[Finding], *,
        convergent_keys: set[str] | None = None,
        route_full: bool = False,
    ) -> tuple[str, str | None]:
        """`("proceed"|"escalate"|"fail", trigger)` -- a whole-cycle decision,
        mirroring `finding_merge.escalation_decision` exactly, including its
        priority order and its refusal to escalate twice.

        The three triggers, highest priority first (worker.md ->
        *Deterministic dev escalation*), all OBSERVED signals, never a
        reviewer's own severity label:

        1. `cross_reviewer_convergence` -- >=2 independently dispatched
           reviewers flagged the same key THIS cycle
           (`finding_adapter.convergent_keys()`).
        2. `observed_recurrence` -- a key present now was ALSO present at
           the end of the prior cycle, i.e. it survived that cycle's fix
           (`record_cycle()` above).
        3. `scope_exceeded` -- the recheck route came back FULL, so the
           last fix's diff spilled outside the files its findings named.

        Once the PR's single escalation is spent, a key that survives the
        escalated fix is stop-the-world (`"fail"`), never a second
        escalation; anything else simply proceeds with an ordinary fix.
        """
        current = {f.key() for f in must_fix}
        recurring = {k for k in current if self.survived.get(k, 0) >= 1}

        if self.escalated:
            return ("fail", None) if recurring else ("proceed", None)
        if convergent_keys and (convergent_keys & current):
            self.escalated = True
            return "escalate", "cross_reviewer_convergence"
        if recurring:
            self.escalated = True
            return "escalate", "observed_recurrence"
        if route_full:
            self.escalated = True
            return "escalate", "scope_exceeded"
        return "proceed", None

    def to_dict(self) -> dict:
        # Sets have no JSON literal, so `previous_keys` round-trips as a
        # sorted list (sorted only for a stable ledger diff -- membership is
        # what `record_cycle()` actually uses).
        return {
            "survived": dict(self.survived),
            "escalated": self.escalated,
            "previous_keys": sorted(self.previous_keys),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RecurrenceTracker":
        # `escalated` was a LIST of per-key escalations before it became a
        # per-PR boolean; a ledger written by that older build still
        # deserializes -- a non-empty list means the one escalation this PR
        # gets was already spent.
        raw = d.get("escalated", False)
        escalated = bool(raw) if isinstance(raw, bool) else len(raw) > 0
        return cls(
            survived=dict(d.get("survived", {})),
            escalated=escalated,
            previous_keys=set(d.get("previous_keys", [])),
        )


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

    **An EMPTY `fix_files` is never BOUNDED, even though the empty set is
    trivially a subset of `finding_files`.** A real incident (TAS plan 49
    PR2, 2026-08-22) hit exactly this: the dev-fix dispatch's own agent
    committed inside its OWN Bernstein-managed sub-worktree, but that
    commit never landed on the unit branch `pre_fix_head..HEAD` is
    diffed against (Bernstein's own "spawn, execute, merge" sequence --
    `bernstein_dispatch.run_plan_file()`'s docstring -- did not merge it
    back; the commit was later found dangling via `git fsck
    --unreachable`, unreachable from any ref). `fix_files <= finding_files`
    is `True` for `fix_files = set()` regardless of WHY the diff is empty,
    so this silently routed a completely unfixed PR to `"BOUNDED"` --
    "confirm the narrow fix landed" on code that never changed at all,
    which would have had the SAME reviewer re-confirm a finding that was
    never addressed, misread as recurrence rather than as an infra failure
    with nothing to route on either narrowly OR broadly. `finding_files`
    itself being empty already returns `"FULL"` via `_safe_recheck_route()`
    before this function is even called; this closes the analogous gap for
    an empty `fix_files`.
    """
    out = subprocess.run(
        ["git", "-C", repo, "diff", "--name-only", f"{pre_fix_head}..HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout
    fix_files = {line.strip() for line in out.splitlines() if line.strip()}
    return "BOUNDED" if fix_files and fix_files <= finding_files else "FULL"


@dataclass
class GateDecision:
    action: str  # "spawn_fix" | "spawn_fix_escalated" | "pass" | "fail" | "inconclusive_retry" | "abort"
    reason: str
    escalated_model: str | None = None
    # Which of worker.md's three deterministic triggers produced an
    # escalation -- `cross_reviewer_convergence` | `observed_recurrence` |
    # `scope_exceeded`, None when nothing escalated. dev-ralf's result block
    # requires this (`cycle_gate.ESCALATION_TRIGGERS`, validated by its
    # `check_v2`); reasona-dev records it through `cycles_log.record_decision`
    # so an escalation is attributable after the fact rather than only
    # visible as a model swap.
    escalation_trigger: str | None = None


def evaluate(
    result: ReviewResult,
    budget: FixBudget,
    stage: str,
    recurrence: RecurrenceTracker,
    inconclusive_attempts: int,
    escalation_model: str = "opus",
    convergence: ConvergenceTracker | None = None,
    convergence_window: int = 3,
    convergent_keys: set[str] | None = None,
    route_full: bool = False,
    dev_model: str | None = None,
) -> GateDecision:
    """The single entry point `pr_cycle` calls before spawning a fix task.

    `convergence` is optional so existing callers keep their exact
    behaviour; when supplied, it adds the non-convergence exit described in
    `ConvergenceTracker` -- a PR that stops improving fails at
    `convergence_window` cycles instead of at the stage cap.

    `convergent_keys` and `route_full` are two of worker.md's three
    escalation triggers, passed separately rather than pre-unioned so
    `RecurrenceTracker.escalation_decision()` can apply dev-ralf's own
    PRIORITY ORDER and name which one fired: `convergent_keys` is
    `cross_reviewer_convergence` (`finding_adapter.convergent_keys()` --
    >=2 independently dispatched reviewers flagged the same key THIS
    cycle), `route_full` is `scope_exceeded` (this cycle's
    `recheck_route()` came back FULL after an actual prior fix). The third,
    `observed_recurrence`, comes from the tracker's own `survived` counts.

    `dev_model`, when given, is compared against `escalation_model`
    verbatim (worker.md: "compare `escalation_from` and `escalation_to`
    verbatim"). If they resolve to the SAME `tool:model:effort` string, an
    "escalated" dispatch would be an identical re-run at the same tier --
    no capability increase, one wasted fix-budget cycle to prove what the
    comparison already showed. In that case the escalation is still
    recorded (`escalation_decision()` already marked this PR's single
    escalation spent), but the dispatch itself is skipped -- straight to
    the outcome a non-escalated fix reaching this key again would produce:
    `fail`, without spending the stage budget on the skipped cycle.
    Omitting `dev_model` keeps the escalated dispatch unconditional.
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

    action, trigger = recurrence.escalation_decision(
        result.must_fix, convergent_keys=convergent_keys, route_full=route_full,
    )
    if action == "fail":
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

    if action == "escalate":
        if dev_model is not None and dev_model == escalation_model:
            # worker.md: "skip the redundant dispatch and go straight to
            # the outcome a NON-ESCALATED fix would have reached" -- that
            # outcome depends on WHICH trigger fired. For observed_recurrence
            # the non-escalated outcome is the key's second unresolved
            # occurrence, i.e. immediate FAIL. For cross_reviewer_convergence
            # and scope_exceeded, a non-escalated fix would have been an
            # ordinary spawn_fix (worker.md never routes those two straight
            # to FAIL) -- so THIS guard must fall through to the same,
            # spending budget normally, rather than failing the PR on a
            # trigger that was never a stop-the-world signal on its own.
            if trigger == "observed_recurrence":
                return GateDecision(
                    "fail",
                    f"escalation_from == escalation_to ({escalation_model}) -- no capability "
                    "increase, skipping the redundant dispatch",
                    escalation_trigger=trigger,
                )
            budget.spend(stage)
            return GateDecision(
                "spawn_fix",
                f"{trigger} -- escalation_from == escalation_to ({escalation_model}), "
                "no capability increase: dispatching a normal (non-escalated) fix instead",
                escalation_trigger=trigger,
            )
        budget.spend(stage)
        return GateDecision(
            "spawn_fix_escalated",
            f"{trigger} -- one bounded escalation before FAIL",
            escalated_model=escalation_model,
            escalation_trigger=trigger,
        )

    budget.spend(stage)
    return GateDecision("spawn_fix", f"{len(result.must_fix)} MUST_FIX finding(s)")
