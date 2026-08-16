"""Deterministic cycle control: recheck routing, escalation, budget, fingerprints.

Zero-LLM logic. This is the module `on_pre_task_create` (verified real hookspec
in Bernstein 3.15.1, `plugins/hookspecs.py`) calls before a fix/recheck task is
allowed to spawn. Ports dev-ralf's `cycle_gate.py` + the renewal contract
(dev-ralf-renewal-claude.md §3.5, §3.8, §3.9) onto Bernstein's task model.
"""

from __future__ import annotations

import hashlib
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


def three_dot_diff_hash(repo: str, base: str = "origin/main", head: str = "HEAD") -> str:
    """diff_hash = hash(git diff --binary base...head), index lines stripped.

    Stripping the `index <blob>..<blob>` header line makes the hash tolerant
    of unrelated blob-id churn (dev-ralf-renewal-claude.md §3.6 implementation
    note).
    """
    out = subprocess.run(
        ["git", "-C", repo, "diff", "--binary", f"{base}...{head}"],
        capture_output=True, text=True, check=True,
    ).stdout
    filtered = "\n".join(
        line for line in out.splitlines() if not line.startswith("index ")
    )
    return hashlib.sha256(filtered.encode("utf-8", errors="surrogateescape")).hexdigest()


def base_scope_hash(repo: str, changed_paths: list[str], base: str = "origin/main") -> str:
    """hash of the origin/main blobs for every path the PR touches.

    Distinguishes "main changed an unrelated file" (reusable) from "main
    changed a file this PR also touches" (invalidates review -- semantic
    conflict risk even when the PR's own diff is unchanged).
    """
    h = hashlib.sha256()
    for path in sorted(changed_paths):
        blob = subprocess.run(
            ["git", "-C", repo, "rev-parse", f"{base}:{path}"],
            capture_output=True, text=True,
        )
        h.update(path.encode())
        h.update((blob.stdout.strip() if blob.returncode == 0 else "MISSING").encode())
    return h.hexdigest()


@dataclass
class Fingerprint:
    plan_unit_hash: str
    patch_hash: str
    base_scope_hash: str
    head_sha: str
    base_sha: str

    def matches(self, other: "Fingerprint") -> bool:
        return (
            self.plan_unit_hash == other.plan_unit_hash
            and self.patch_hash == other.patch_hash
            and self.base_scope_hash == other.base_scope_hash
        )


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
) -> GateDecision:
    """The single entry point `on_pre_task_create` calls before spawning a fix task."""
    gate = result.gate()

    if gate == "INCONCLUSIVE":
        if inconclusive_attempts >= 3:
            return GateDecision("abort", "inconclusive retry budget exhausted (3 attempts)")
        return GateDecision("inconclusive_retry", f"attempt {inconclusive_attempts + 1}/3")

    if gate == "ERROR":
        return GateDecision("abort", "role/model unavailable -- hard blocker, never swap")

    if gate in ("PASS", "PASS_WITH_NOTES"):
        return GateDecision("pass", gate)

    # FIX_REQUIRED
    if not budget.can_spend(stage):
        return GateDecision("fail", f"{stage} budget exhausted ({budget.total_used}/{MAX_TOTAL_FIX_CYCLES})")

    decisions = {recurrence.decide(f.key()) for f in result.must_fix}
    if "FAIL" in decisions:
        return GateDecision("fail", "MUST_FIX key survived escalated fix -- stop-the-world")
    if "ESCALATE_ONCE" in decisions:
        budget.spend(stage)
        return GateDecision(
            "spawn_fix_escalated",
            "recurring finding -- one bounded escalation before FAIL",
            escalated_model=escalation_model,
        )

    budget.spend(stage)
    return GateDecision("spawn_fix", f"{len(result.must_fix)} MUST_FIX finding(s)")
