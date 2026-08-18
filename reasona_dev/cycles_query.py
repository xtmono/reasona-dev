"""Queries over `cycles.jsonl` -- the half of the measurement that turns a
log into a decision.

**Why this module is not optional.** `cycles_log` records; without a query
the record is inert, and every question it was built to answer stays
answerable only by opinion. The deferred decisions in this project are
explicitly conditioned on "once measurement exists" -- which of
review/bugbot/compliance to drop, whether an undeclared acceptance criterion
should become a refusal, whether the 8/8/16 cycle caps are anywhere near the
observed distribution. A log with no query cannot discharge any of them, so
the deferral would be permanent by construction rather than by evidence.

**Every query here is exact.** Counting, grouping, set membership. Nothing
estimates, and nothing that requires a judgment call is computed -- the
output is a table for a person to decide from, never a recommendation.

**The one approximation is isolated and labelled.** `effective_findings()`
asks whether a finding mattered, using the proxy "a later commit touched the
same file" (`--effective` in the CLI). That is a genuine heuristic: a commit
can touch a file for unrelated reasons, and a finding can matter without
producing a commit at all. The original analysis this project draws on ran
into precisely this and warned that a comparable measure carried a base rate
high enough to be uninformative on its own -- so it is off by default,
reported separately, and never mixed into the exact counts.

**Reading the output.** The question the numbers are for is not "which role
found the most" -- a role that finds many findings nobody acts on is not
carrying its weight. It is:

    first_catch    findings this role reported before any other role did
    duplicate      findings another role also reported (redundancy)
    unique         findings ONLY this role reported (irreplaceability)

A role with high `duplicate` and near-zero `unique` is the candidate to
drop, and that is a conclusion the table supports directly rather than one
requiring interpretation.
"""

from __future__ import annotations

import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from reasona_dev import cycles_log


@dataclass
class RoleAttribution:
    role: str
    first_catch: int = 0
    duplicate: int = 0
    unique: int = 0
    total_reported: int = 0


@dataclass
class BudgetReport:
    units: int = 0
    review_cycles: dict[str, int] = field(default_factory=dict)
    scan_cycles: dict[str, int] = field(default_factory=dict)
    terminal_reasons: dict[str, int] = field(default_factory=dict)
    escalations: int = 0


@dataclass
class AcceptanceCoverage:
    units_total: int = 0
    units_declaring: int = 0
    units_passing: int = 0
    units_failing: int = 0

    @property
    def coverage_pct(self) -> float:
        return 100.0 * self.units_declaring / self.units_total if self.units_total else 0.0


def _dispatches(records: list[dict]) -> list[dict]:
    return [r for r in records if r.get("kind") is None]


def attribution(workdir: str | Path) -> list[RoleAttribution]:
    """Per-role first-catch / duplicate / unique counts over distinct findings.

    "First" is resolved by append order, which is the dispatch order the
    driver actually used -- within a scan cycle bugbot is dispatched before
    compliance, so a finding both report is credited to bugbot. That is a
    real ordering bias and it is why `unique` matters more than
    `first_catch` when comparing two roles that run in the same cycle.
    """
    records = _dispatches(cycles_log.read_records(workdir))

    first_by_key: dict[str, str] = {}
    roles_by_key: dict[str, set[str]] = defaultdict(set)
    reported_by_role: dict[str, int] = defaultdict(int)

    for r in records:
        role = r.get("role") or "?"
        for f in r.get("findings") or []:
            key = f.get("key")
            if not key:
                continue
            reported_by_role[role] += 1
            roles_by_key[key].add(role)
            first_by_key.setdefault(key, role)

    out: dict[str, RoleAttribution] = {
        role: RoleAttribution(role=role, total_reported=n)
        for role, n in reported_by_role.items()
    }
    for key, roles in roles_by_key.items():
        first = first_by_key[key]
        out.setdefault(first, RoleAttribution(role=first)).first_catch += 1
        for role in roles:
            entry = out.setdefault(role, RoleAttribution(role=role))
            if len(roles) == 1:
                entry.unique += 1
            else:
                entry.duplicate += 1
    return sorted(out.values(), key=lambda a: (-a.unique, -a.first_catch, a.role))


def budget(workdir: str | Path) -> BudgetReport:
    """How many cycles units actually consumed, and which rule ended them.

    The caps are 8/8/16. If the observed distribution never approaches them,
    the caps are not what constrains cost and lowering them changes nothing;
    if units routinely hit them, they are. Either finding is actionable and
    neither is available without this count.
    """
    records = cycles_log.read_records(workdir)
    report = BudgetReport()
    seen_units: set[str] = set()
    max_cycle: dict[tuple[str, str], int] = {}

    for r in records:
        unit = r.get("stage_name")
        if unit:
            seen_units.add(unit)
        stage, cycle = r.get("stage"), r.get("cycle")
        if unit and stage and isinstance(cycle, int):
            k = (unit, stage)
            max_cycle[k] = max(max_cycle.get(k, 0), cycle)
        if r.get("kind") == "decision":
            action = r.get("action") or "?"
            if action in ("fail", "abort"):
                report.terminal_reasons[r.get("reason") or "?"] = (
                    report.terminal_reasons.get(r.get("reason") or "?", 0) + 1
                )
            if action == "spawn_fix_escalated":
                report.escalations += 1

    report.units = len(seen_units)
    for (unit, stage), n in max_cycle.items():
        target = report.review_cycles if stage == "review" else report.scan_cycles
        target[unit] = n
    return report


def acceptance_coverage(workdir: str | Path) -> AcceptanceCoverage:
    """How many PR units declare executable criteria, and how they fared.

    This is the number that decides when an undeclared unit should stop
    passing with a warning and start being refused. Flipping that switch on
    a guess would block every plan written before the field existed;
    flipping it once coverage is high is a formality.
    """
    records = cycles_log.read_records(workdir)
    cov = AcceptanceCoverage()
    units: set[str] = set()
    seen_acceptance: dict[str, dict] = {}

    for r in records:
        unit = r.get("stage_name")
        if unit:
            units.add(unit)
        if r.get("kind") == "acceptance" and unit:
            seen_acceptance[unit] = r  # last row per unit wins

    cov.units_total = len(units)
    for row in seen_acceptance.values():
        if not row.get("declared"):
            continue
        cov.units_declaring += 1
        if row.get("passed"):
            cov.units_passing += 1
        else:
            cov.units_failing += 1
    return cov


def gate_vs_acceptance(workdir: str | Path) -> dict[str, int]:
    """The four-way split, per PR unit.

    Answers the question the review budget actually turns on: did the review
    stack catch anything the executable criteria would not have caught on
    their own?

        gate_only        review found MUST_FIX, acceptance passed
        acceptance_only  review found nothing, acceptance failed
        both             both fired
        neither          both clean
    """
    records = cycles_log.read_records(workdir)
    had_must_fix: dict[str, bool] = defaultdict(bool)
    ac_failed: dict[str, bool] = {}

    for r in records:
        unit = r.get("stage_name")
        if not unit:
            continue
        if r.get("kind") is None and (r.get("must_fix_count") or 0) > 0:
            had_must_fix[unit] = True
        if r.get("kind") == "acceptance" and r.get("declared"):
            ac_failed[unit] = not r.get("passed")

    out = {"gate_only": 0, "acceptance_only": 0, "both": 0, "neither": 0}
    # Only units with DECLARED criteria are counted -- a unit with none
    # cannot contribute evidence about what acceptance would have caught,
    # and counting it as "acceptance found nothing" would understate the
    # criteria's value with a number that reflects plan coverage instead.
    for unit, failed in ac_failed.items():
        gate = had_must_fix[unit]
        if gate and failed:
            out["both"] += 1
        elif gate:
            out["gate_only"] += 1
        elif failed:
            out["acceptance_only"] += 1
        else:
            out["neither"] += 1
    return out


def effective_findings(workdir: str | Path, *, window_days: int = 7) -> dict[str, int]:
    """APPROXIMATE. Per-role count of findings whose file was touched again
    by a commit within `window_days`.

    The proxy is weak in both directions -- a later commit may touch the file
    for unrelated reasons, and a finding can matter without ever producing a
    commit. The analysis this measurement descends from hit exactly this and
    found a comparable "fix touched a recent file" rate of 84% against a
    control-group base rate of 77%, i.e. almost entirely base rate. Reported
    separately from the exact counts and never merged into them; treat a
    difference between roles as a hypothesis, not a result.
    """
    workdir = Path(workdir)
    records = _dispatches(cycles_log.read_records(workdir))
    window_s = window_days * 86400
    out: dict[str, int] = defaultdict(int)

    for r in records:
        ts, role = r.get("ts"), r.get("role") or "?"
        if not ts:
            continue
        for f in r.get("findings") or []:
            path = f.get("path")
            if not path:
                continue
            try:
                res = subprocess.run(
                    ["git", "-C", str(workdir), "log", "--format=%ct", "--",
                     path],
                    capture_output=True, text=True, check=True,
                )
            except (subprocess.CalledProcessError, FileNotFoundError, OSError):
                continue
            for line in res.stdout.splitlines():
                try:
                    commit_ts = int(line.strip())
                except ValueError:
                    continue
                if ts < commit_ts <= ts + window_s:
                    out[role] += 1
                    break
    return dict(out)


def render(workdir: str | Path, *, include_effective: bool = False) -> str:
    rows = attribution(workdir)
    b = budget(workdir)
    cov = acceptance_coverage(workdir)
    split = gate_vs_acceptance(workdir)

    lines = [f"cycles report for {workdir}", ""]

    if not rows:
        lines.append("no records yet -- run a PR cycle first")
        return "\n".join(lines)

    lines += ["role attribution (exact)", "  role          first  dup  uniq  total"]
    for a in rows:
        lines.append(
            f"  {a.role:<12} {a.first_catch:>5}  {a.duplicate:>3}  {a.unique:>4}  {a.total_reported:>5}"
        )

    lines += ["", f"budget: {b.units} unit(s), {b.escalations} escalation(s)"]
    if b.review_cycles:
        vals = sorted(b.review_cycles.values())
        lines.append(f"  review cycles used: min={vals[0]} max={vals[-1]} (cap 8)")
    if b.scan_cycles:
        vals = sorted(b.scan_cycles.values())
        lines.append(f"  scan cycles used:   min={vals[0]} max={vals[-1]} (cap 8)")
    for reason, n in sorted(b.terminal_reasons.items(), key=lambda kv: -kv[1]):
        lines.append(f"  terminal x{n}: {reason}")

    lines += [
        "",
        f"acceptance coverage: {cov.units_declaring}/{cov.units_total} units declare criteria "
        f"({cov.coverage_pct:.0f}%), {cov.units_passing} passed, {cov.units_failing} failed",
        "",
        "gate vs acceptance (units with declared criteria only)",
        f"  gate_only={split['gate_only']}  acceptance_only={split['acceptance_only']}  "
        f"both={split['both']}  neither={split['neither']}",
    ]

    if include_effective:
        eff = effective_findings(workdir)
        lines += ["", "APPROXIMATE -- findings whose file was touched again within 7d:"]
        if eff:
            for role, n in sorted(eff.items(), key=lambda kv: -kv[1]):
                lines.append(f"  {role:<12} {n}")
        else:
            lines.append("  (none, or not a git repo)")
        lines.append("  base-rate caveat applies -- see effective_findings() docstring")

    return "\n".join(lines)


def main(argv: list[str]) -> int:
    import sys

    workdir = argv[0] if argv and not argv[0].startswith("-") else "."
    print(render(workdir, include_effective="--effective" in argv))
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
