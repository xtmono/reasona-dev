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
)

# dev-ralf squash_build.py / squash_guard.py B2_RE -- the full trailer list.
# `co-authored-by` and `generated (with|by)` alone missed `signed-off-by`,
# `assisted-by`, `created-by`, `made-with`, and the bare tool name `cursor`.
_TRAILER_WORDS = (
    r"co-authored-by|made-with|generated(-|\s)(with|by)|assisted-by"
    r"|created-by|signed-off-by|cursor"
)
_TRAILER_LINE = re.compile(rf"(?i)^\s*({_TRAILER_WORDS})\b.*$")
_TRAILER_INLINE = re.compile(rf"(?i)({_TRAILER_WORDS})\b[^\n]*")
# dev-ralf squash_build.py _CLOSING / squash_guard.py B1_RE.
_CLOSING_REF = re.compile(r"\b(close[sd]?|fix(es|ed)?|resolve[sd]?)\s+#\d+", re.I)

# dev-ralf squash_build.py BODY_LIMIT -- squash.md B3: codepoints per line.
_BODY_LINE_LIMIT = 100


@dataclass
class SquashMessage:
    title: str
    body: str


def _strip_noise(line: str) -> str:
    """dev-ralf squash_build.py `_strip_noise` -- drop a trailer or closing-ref
    embedded mid-line, without dropping the line itself."""
    line = _TRAILER_INLINE.sub("", line)
    line = _CLOSING_REF.sub("", line)
    return re.sub(r"\s{2,}", " ", line).strip(" \t,;:-")


def _wrap(text: str, first: str = "- ", cont: str = "  ") -> list[str]:
    """Fit `text` under `_BODY_LINE_LIMIT` by continuing it onto indented
    lines, never truncating -- dev-ralf squash_build.py `_wrap`. Truncation
    would silently drop what the line actually said."""
    out: list[str] = []
    prefix, budget = first, _BODY_LINE_LIMIT - len(first)
    line = ""
    for word in text.split():
        while len(word) > budget:
            if line:
                out.append(prefix + line)
                prefix, budget = cont, _BODY_LINE_LIMIT - len(cont)
                line = ""
                continue
            out.append(prefix + word[:budget])
            word = word[budget:]
            prefix, budget = cont, _BODY_LINE_LIMIT - len(cont)
        if not line:
            line = word
        elif len(line) + 1 + len(word) <= budget:
            line += " " + word
        else:
            out.append(prefix + line)
            prefix, budget = cont, _BODY_LINE_LIMIT - len(cont)
            line = word
    if line:
        out.append(prefix + line)
    return out


def build(pr_type: str, subject: str, body_lines: list[str], issue_num: int | None = None) -> SquashMessage:
    """The ONLY place a squash message is constructed.

    Title MUST be `<type>: <subject>` with no `#NUM` prefix -- the
    compliance bot in this org non-deterministically FAILs `#NUM type:`
    (dev-ralf worker.md -> *Ship via /gh-pr*).

    Body lines carrying a trailer (Co-Authored-By, Signed-off-by, ...) are
    dropped outright; a closing reference embedded in an otherwise-kept line
    is stripped in place; every surviving line is wrapped at
    `_BODY_LINE_LIMIT` codepoints instead of being merged in raw -- dev-ralf
    squash_build.py `clean_body`/`_commit_bullets`/`_wrap`.
    """
    subject = subject.strip()
    title = f"{pr_type}: {subject}"

    wrapped: list[str] = []
    for raw in body_lines:
        if not raw.strip() or _TRAILER_LINE.match(raw):
            continue
        text = _strip_noise(raw)
        if text:
            wrapped.extend(_wrap(text))
    body = "\n".join(wrapped)
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
    if _TRAILER_INLINE.search(msg.title):
        violations.append("T2: forbidden trailer in title")

    if _CLOSING_REF.search(msg.body):
        violations.append("B1: body carries a closing reference (Closes/Fixes/Resolves #N)")
    if _TRAILER_INLINE.search(msg.body):
        violations.append("B2: forbidden trailer in body")
    for line in msg.body.splitlines():
        if len(line) > _BODY_LINE_LIMIT:
            violations.append(
                f"B3: body line {len(line)} codepoints > {_BODY_LINE_LIMIT}: {line[:40]!r}"
            )
            break

    return violations


def classify(violations: list[str]) -> str:
    """PASS | TITLE_ONLY | FAIL -- worker.md -> *Squash merge* -- Errors table."""
    if not violations:
        return "PASS"
    if any(v.startswith("T") for v in violations):
        return "FAIL"
    return "TITLE_ONLY"
