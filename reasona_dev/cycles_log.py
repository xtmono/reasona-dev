"""Append-only per-cycle finding log -- the measurement substrate every
later budget decision rests on.

**Why this exists.** dev-ralf's 3.5-month production record showed a 30%
follow-up-plan rate despite a 16-fix-cycle budget spread over five review
roles, which means the marginal return of the last reviewer was already
near zero -- but there was no way to tell WHICH role was carrying its
weight, because nothing recorded which gate first caught a finding that
turned out to matter. Cutting a reviewer without that record would be the
same guess that produced the current allocation. reasona-dev has zero
production history, so instrumenting BEFORE the first real run costs
nothing and is the only moment it is free.

**What a record is.** One line per role dispatch (not per cycle) -- a scan
cycle runs bugbot and compliance, so it emits two lines sharing a
`(stage, cycle)`. Every MUST_FIX/ADVISORY finding is flattened into the
line with its `Finding.key()`, the same stable identity
`RecurrenceTracker` uses, so a finding can be followed across cycles and
across roles without re-deriving anything.

**What it is NOT.** Not a debug log and not a transcript -- the agent's raw
output already lives at `RoleRunResult.raw_output_path`. This carries only
the fields an attribution query needs, so it stays small enough to keep
forever and machine-readable enough that no parsing heuristics are needed
to query it later.

**The query it is built for.** Define an *effective* finding as one whose
path+symbol is touched again by a fix commit within N days. Then group by
`role` to get, per gate, how many effective findings it caught FIRST. That
distribution -- not an opinion about how many reviewers feel necessary --
is what makes it possible to decide which of review/bugbot/compliance/
final_audit to drop. Once `reasona_dev.acceptance` lands, its AC ids join
here on the same `(stage_name, cycle)` key, splitting findings four ways:
caught by a gate only, by an AC only, by both, or by neither (a post-merge
defect, i.e. a hole in the AC design itself).

`.reasona/memory/*.md` is generated FROM this file, never hand-written --
see `reasona_dev.memory`. That is what keeps the memory surface from
drifting into the prose bloat it exists to replace.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from reasona_dev.finding_adapter import ReviewResult

SCHEMA_VERSION = 1


def _head_sha(workdir: Path) -> str | None:
    """Current HEAD, so a record can be correlated with the commits that
    followed it. Returns None outside a git repo rather than raising --
    losing a measurement is never worth failing a PR cycle over.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(workdir), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None
    return out.stdout.strip() or None


def cycles_path(workdir: str | Path) -> Path:
    """`<workdir>/.reasona/cycles.jsonl` -- the same `.reasona/` convention
    `model_config.json` / `review-<stage>.json` / `model_divergence.jsonl`
    already use, anchored to the TARGET repo (never reasona-dev's own
    install location).
    """
    return Path(workdir) / ".reasona" / "cycles.jsonl"


# Contract text is stored truncated. It has to be stored at all because
# `reasona_dev.memory` groups recurrences by it -- the finding key alone
# cannot, since it hashes path+symbol+contract together and so never matches
# across two different files. It has to be truncated because this file is
# meant to be kept indefinitely, and an untruncated model-written paragraph
# per finding is what would eventually make that impractical.
_MAX_CONTRACT_CHARS = 300


def _finding_rows(result: ReviewResult) -> list[dict]:
    return [
        {
            "key": f.key(),
            "disposition": f.disposition.value,
            "severity": f.severity.value if f.severity else None,
            "path": f.path,
            "line": f.line,
            "symbol": f.symbol,
            "contract": (f.contract or "")[:_MAX_CONTRACT_CHARS] or None,
            # Carried because a MUST_FIX with incomplete evidence is a
            # different quality signal than one with full contract/scenario/
            # fix -- attribution should be able to tell them apart rather
            # than counting both as "the role found something".
            "contract_incomplete": f.contract_incomplete,
        }
        for f in result.findings
    ]


def record_dispatch(
    *,
    workdir: str | Path,
    stage_name: str,
    stage: str,
    cycle: int,
    role: str,
    model: str,
    adapter: str,
    result: ReviewResult,
) -> None:
    """Append one role dispatch's outcome. Never raises.

    Called from `pr_cycle.run_pr_cycle()` after every `run_role_fn()`
    return, including dev fix dispatches (whose `ReviewResult` is normally
    empty -- the line still matters, because it is what marks the boundary
    between "findings before this fix" and "findings after it").

    Instrumentation must never be able to fail a PR cycle, so every error
    is swallowed. A missing measurement is a cost; a cycle aborted by its
    own logger is a defect.
    """
    try:
        path = cycles_path(workdir)
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "schema_version": SCHEMA_VERSION,
            "ts": time.time(),
            "head_sha": _head_sha(Path(workdir)),
            "stage_name": stage_name,
            "stage": stage,
            "cycle": cycle,
            "role": role,
            "model": model,
            "adapter": adapter,
            "role_status": result.role_status.value,
            "gate": result.gate(),
            "must_fix_count": len(result.must_fix),
            "advisory_count": len(result.advisory),
            "contract_mismatch": result.contract_mismatch,
            "findings": _finding_rows(result),
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 -- see docstring: never fail a cycle
        pass


def record_decision(
    *,
    workdir: str | Path,
    stage_name: str,
    stage: str,
    cycle: int,
    action: str,
    reason: str,
    escalated_model: str | None = None,
) -> None:
    """Append the deterministic gate decision that followed a cycle's
    dispatches.

    Separate from `record_dispatch` because the decision is a property of
    the CYCLE, not of any one role -- and because "which rule ended this
    PR" (budget exhaustion vs. recurrence vs. non-convergence) is exactly
    what a later budget-shape review needs, and it is not recoverable from
    the finding rows alone.
    """
    try:
        path = cycles_path(workdir)
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "schema_version": SCHEMA_VERSION,
            "ts": time.time(),
            "stage_name": stage_name,
            "stage": stage,
            "cycle": cycle,
            "kind": "decision",
            "action": action,
            "reason": reason,
            "escalated_model": escalated_model,
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


def read_records(workdir: str | Path) -> list[dict]:
    """Every record, in append order. Malformed lines are skipped rather
    than raising -- a partially-written final line (killed mid-run) must
    not make the whole history unreadable.
    """
    path = cycles_path(workdir)
    if not path.is_file():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows
