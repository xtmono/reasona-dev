"""CLI entry point for a `completion_signals: [{type: test_passes, ...}]` check.

Investigation finding (see docs/ARCHITECTURE.md §3): Bernstein's hookspecs
expose exactly two hooks that can block by raising -- `on_pre_task_create`
and `on_pre_tool_use`. There is no `on_pre_merge` / `on_pre_task_complete`.
Gating whether a task's result is good enough to proceed toward PR/merge is
therefore NOT a hook concern at all -- it is a `test_passes` completion
signal, exactly like `make ci-fast` or `make lint-md` are today in dev-ralf.

Usage (wired by plan_compile.py onto each generated step):

    completion_signals:
      - type: test_passes
        command: "python3 -m reasona_dev.gate_check <review_result.json>"

Exit 0 -> PASS or PASS_WITH_NOTES (janitor accepts).
Exit 1 -> FIX_REQUIRED, INCONCLUSIVE, or ERROR (janitor rejects; Bernstein's
own retry/escalation loop -- NOT reasona_dev's -- decides what happens next
at the task level; reasona_dev's own fix-cycle loop is a separate, explicit
re-dispatch driven by reasona_dev/cycle_gate.py + plugin.py, not by this
exit code alone).
"""

from __future__ import annotations

import json
import sys

from reasona_dev.finding_adapter import ReviewResult, RoleStatus, Disposition, Severity


def _load(path: str) -> ReviewResult:
    raw = json.loads(open(path, encoding="utf-8").read())
    from reasona_dev.finding_adapter import Finding

    parsed_findings = [
        Finding(
            disposition=Disposition(f["disposition"]),
            severity=Severity(f["severity"]) if f.get("severity") else None,
            path=f["path"],
            line=f.get("line"),
            symbol=f.get("symbol"),
            contract=f.get("contract"),
            scenario=f.get("scenario"),
            fix=f.get("fix"),
            note=f.get("note"),
        )
        for f in raw.get("findings", [])
    ]
    return ReviewResult(
        role_status=RoleStatus(raw["role_status"]),
        findings=parsed_findings,
        verdict_tail=raw.get("verdict_tail"),
        contract_mismatch=raw.get("contract_mismatch", False),
    )


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python3 -m reasona_dev.gate_check <review_result.json>", file=sys.stderr)
        return 2
    try:
        result = _load(argv[0])
    except FileNotFoundError:
        # A traceback here reads as a crash in the gate; it is an absent
        # input. Exit non-zero (never a silent pass) with the path, so the
        # operator can tell "nothing wrote a verdict" apart from "the
        # verdict was FAIL".
        print(f"reasona-dev gate: no review result at {argv[0]}", file=sys.stderr)
        return 1
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        print(f"reasona-dev gate: malformed review result at {argv[0]}: {exc}", file=sys.stderr)
        return 1
    gate = result.gate()
    print(f"reasona-dev gate: {gate}", file=sys.stderr)
    if result.contract_mismatch:
        print("reasona-dev: VERDICT tail disagreed with section membership -- section wins", file=sys.stderr)
    return 0 if gate in ("PASS", "PASS_WITH_NOTES") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
