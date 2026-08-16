"""Deterministic squash-merge message builder + guard.

Ports dev-ralf's `squash_build.py` / `squash_guard.py` split: the builder is
the only thing allowed to construct a message, the guard re-checks the
builder's own output by re-deriving it independently. A violation means the
two disagree with each other, never "go fix the message by hand"
(dev-ralf worker.md -> *Squash merge*).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_TITLE_RE = re.compile(
    r"^(?P<type>feat|fix|docs|refactor|test|chore|perf|build|ci)"
    r"(?P<scope>\([\w./-]+\))?: (?P<subject>.+)$"
)
_FORBIDDEN_IN_TITLE = (
    re.compile(r"^#\d+"),           # no leading issue-number prefix
    re.compile(r"#\d+"),            # no closing-ref anywhere
    re.compile(r"(?i)co-authored-by"),
    re.compile(r"(?i)generated (with|by)"),
)


@dataclass
class SquashMessage:
    title: str
    body: str


def build(pr_type: str, subject: str, body_lines: list[str], issue_num: int | None = None) -> SquashMessage:
    """The ONLY place a squash message is constructed.

    Title MUST be `<type>: <subject>` with no `#NUM` prefix -- the
    compliance bot in this org non-deterministically FAILs `#NUM type:`
    (dev-ralf worker.md -> *Ship via /gh-pr*).
    """
    subject = subject.strip()
    title = f"{pr_type}: {subject}"
    body = "\n".join(line for line in body_lines if line.strip())
    # GitHub appends ` (#<pr_num>)` on squash merge automatically -- never
    # add it here ourselves, that would double it.
    return SquashMessage(title=title, body=body)


def guard(msg: SquashMessage) -> list[str]:
    """Re-derive validity independently of `build()`. Returns violation codes.

    `T#` codes: the title itself is malformed -- verdict=FAIL, do not merge.
    `B#` codes: body-only violations -- merge with title-only, record codes.
    """
    violations: list[str] = []

    m = _TITLE_RE.match(msg.title)
    if not m:
        violations.append("T1: title does not match `<type>(<scope>)?: <subject>`")
    for pattern in _FORBIDDEN_IN_TITLE:
        if pattern.search(msg.title):
            violations.append(f"T2: forbidden pattern in title: {pattern.pattern}")

    for pattern in _FORBIDDEN_IN_TITLE[2:]:  # co-authored-by / generated-by also banned in body
        if pattern.search(msg.body):
            violations.append(f"B1: forbidden pattern in body: {pattern.pattern}")

    return violations


def classify(violations: list[str]) -> str:
    """PASS | TITLE_ONLY | FAIL -- worker.md -> *Squash merge* -- Errors table."""
    if not violations:
        return "PASS"
    if any(v.startswith("T") for v in violations):
        return "FAIL"
    return "TITLE_ONLY"
