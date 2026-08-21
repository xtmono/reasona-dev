"""B-1: the Open Decisions Gate -- dev-ralf's own hard blocker, ported.

worker.md refuses to start a PR unit while its plan's `## Open decisions
(human)` section carries any entry lacking an explicit `decided: <choice>`
tag, even when the choice would equal the printed default -- "deciding is
the human's step, after convergence" (plan-ralf's own SKILL.md, the
producer of this section, states the identical rule from the other side:
"plan-ralf never adds that tag"). reasona-plan's `check_plan._open_decisions()`
already implements this parsing (column-0 `-` bullets own their indented
continuation lines; a column-0 line that is not a bullet ends the entry; a
markdown table row is not an entry and is rejected, since the parser
cannot see rows as entries and every decision written as one would go
uncounted). This module ports that same parsing into reasona-dev, the
consumer side of the contract plan-ralf's own Report already describes to
the human ("reasona-dev refuses to start while this entry lacks an
explicit decided: <choice> tag") -- until now, nothing here actually
refused.

Absent entirely: a plan with no `## Open decisions (human)` section at all
has nothing to check here (a well-authored plan may legitimately need no
open decisions; `plan_report.py`'s own completeness sweep, not this gate,
is where a suspiciously-empty section for a judgment-heavy plan would be
worth flagging -- this gate only enforces the entries that DO exist).
"""

from __future__ import annotations

import re

_OD_HEADING = re.compile(r"^##\s+Open decisions\s*\(human\)\s*$", re.I)
_OD_ENTRY = re.compile(r"^-\s+\S")
_DECIDED = re.compile(r"\bdecided:\s*\S", re.I)
_KEY_TAG = re.compile(r"\[key:\s*[^\]]+\]")


def _entries(plan_text: str) -> list[str]:
    lines = plan_text.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if _OD_HEADING.match(ln.strip()):
            start = i + 1
            break
    if start is None:
        return []

    end = len(lines)
    for j in range(start, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break

    blocks: list[list[str]] = []
    cur: list[str] | None = None
    for ln in lines[start:end]:
        if _OD_ENTRY.match(ln):
            if cur is not None:
                blocks.append(cur)
            cur = [ln]
        elif cur is not None and (not ln.strip() or ln.startswith(("  ", "\t"))):
            cur.append(ln)
        elif cur is not None:
            blocks.append(cur)
            cur = None
    if cur is not None:
        blocks.append(cur)

    return ["\n".join(blk) for blk in blocks]


def undecided_entries(plan_text: str) -> list[str]:
    """Every Open-decisions entry lacking `decided: <choice>`, as its full
    (multi-line) text. Empty when the section is absent, empty, or every
    entry is already decided."""
    return [e for e in _entries(plan_text) if not _DECIDED.search(e)]


def entry_summary(entry: str) -> str:
    """A one-line label for an undecided entry in an error message -- the
    entry's own first bullet line, `[key: ...]` stripped (redundant noise
    in a "you must decide this" message), truncated so one bad entry does
    not dominate a multi-entry error."""
    first_line = entry.splitlines()[0].strip().lstrip("- ").strip()
    first_line = _KEY_TAG.sub("", first_line).strip()
    return first_line[:120]
