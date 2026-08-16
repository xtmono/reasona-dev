"""Deterministic text-contract adapter for reviewer output.

Parses the reviewer text contract (dev-ralf-renewal §3.4 / §4):

    MUST_FIX:
    - [CRITICAL] src/session.rs:142 rotate_token
      || contract: the previous refresh token must be rejected after rotation
      || scenario: two requests refresh successfully with the same token
      || fix: include the previous-token state in the atomic update condition

    ADVISORY:
    - [MEDIUM] src/util.rs:88 parse_ttl
      || note: boundary handling is missing

    VERDICT: PASS|FAIL

Rules this module enforces (not the reviewer's prompt -- the parser is the
source of truth regardless of what the model actually wrote):

- Section membership (MUST_FIX / ADVISORY) is the only authoritative
  disposition. The bracketed [SEVERITY] tag is a label, never a gate input.
- The trailing ``VERDICT:`` line is a parsing anchor only. A mismatch between
  VERDICT and section membership is recorded, never trusted.
- ``line`` is evidence, never identity. The finding key excludes it so a fix
  that shifts line numbers does not spuriously read as a new finding.
- OCR's JSON (``{status, comments[], failed[]}``) normalizes directly into
  the same canonical shape -- no separate LLM adapter, no re-prompting.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum


class Disposition(str, Enum):
    MUST_FIX = "MUST_FIX"
    ADVISORY = "ADVISORY"


class Severity(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class RoleStatus(str, Enum):
    """Verification-execution status. Never a code finding (dev-ralf-renewal §6)."""

    COMPLETE = "COMPLETE"
    INCONCLUSIVE = "INCONCLUSIVE"
    ERROR = "ERROR"


# OCR's existing severity mapping is reused verbatim -- it already coincides
# with the four-tier scheme (dev-ralf-renewal-claude.md §3.4).
_OCR_SEVERITY_TO_DISPOSITION = {
    "critical": (Disposition.MUST_FIX, Severity.CRITICAL),
    "high": (Disposition.MUST_FIX, Severity.HIGH),
    "medium": (Disposition.ADVISORY, Severity.MEDIUM),
    "low": (Disposition.ADVISORY, Severity.LOW),
}


@dataclass
class Finding:
    disposition: Disposition
    severity: Severity | None
    path: str
    line: int | None
    symbol: str | None
    contract: str | None = None
    scenario: str | None = None
    fix: str | None = None
    note: str | None = None
    raw: str = ""
    contract_incomplete: bool = False

    def key(self) -> str:
        """Stable identity: path + symbol + normalized description.

        Line numbers are excluded on purpose -- a fix commit shifts them,
        and doing otherwise makes recurrence detection spuriously blind
        (dev-ralf-renewal §5 / dev-ralf-renewal-claude.md §3.5).
        """
        desc = self.contract or self.note or ""
        norm = re.sub(r"\s+", " ", desc.strip().lower())
        parts = [self.path, self.symbol or "", norm]
        return hashlib.sha256("||".join(parts).encode("utf-8")).hexdigest()[:16]

    def is_evidence_complete(self) -> bool:
        if self.disposition is not Disposition.MUST_FIX:
            return True
        return bool(self.contract and self.scenario and self.fix)


@dataclass
class ReviewResult:
    role_status: RoleStatus
    findings: list[Finding] = field(default_factory=list)
    verdict_tail: str | None = None  # parsing anchor only, never authoritative
    contract_mismatch: bool = False
    schema_version: int = 2

    @property
    def must_fix(self) -> list[Finding]:
        return [f for f in self.findings if f.disposition is Disposition.MUST_FIX]

    @property
    def advisory(self) -> list[Finding]:
        return [f for f in self.findings if f.disposition is Disposition.ADVISORY]

    def gate(self) -> str:
        """Deterministic gate -- dev-ralf-renewal-claude.md §3.2.

        The model never declares this. It is computed, always, from the
        parsed section membership.
        """
        if self.role_status is RoleStatus.INCONCLUSIVE:
            return "INCONCLUSIVE"
        if self.role_status is RoleStatus.ERROR:
            return "ERROR"
        if self.must_fix:
            return "FIX_REQUIRED"
        if self.advisory:
            return "PASS_WITH_NOTES"
        return "PASS"


_SECTION_RE = re.compile(r"^(MUST_FIX|ADVISORY):\s*$", re.MULTILINE)
_ITEM_RE = re.compile(
    r"^-\s*\[(?P<severity>CRITICAL|HIGH|MEDIUM|LOW)\]\s*"
    r"(?P<path>\S+?)(?::(?P<line>\d+))?"
    r"(?:\s+(?P<symbol>[A-Za-z_][\w:]*))?\s*$"
)
_EVIDENCE_RE = re.compile(r"^\s*\|\|\s*(?P<key>contract|scenario|fix|note):\s*(?P<val>.+)$")
_VERDICT_RE = re.compile(r"^VERDICT:\s*(PASS|FAIL)\s*$", re.MULTILINE)


def parse_text_contract(text: str) -> ReviewResult:
    """Parse the ``||``-delimited text contract (dev-ralf-renewal-claude.md §3.4).

    Never uses a secondary LLM to extract structure -- deterministic only.
    A v1 fallback (bare ``BLOCKING``/``NON_BLOCKING`` + trailing ``VERDICT``,
    no evidence fields) is accepted and mapped onto MUST_FIX/ADVISORY with
    ``contract_incomplete=True`` on every MUST_FIX item, since v1 output
    carries no evidence fields to parse.
    """
    lines = text.splitlines()
    findings: list[Finding] = []
    current_disposition: Disposition | None = None
    current: Finding | None = None

    def _flush() -> None:
        if current is not None:
            findings.append(current)

    for raw_line in lines:
        sect = _SECTION_RE.match(raw_line)
        if sect:
            _flush()
            current = None
            current_disposition = Disposition(sect.group(1))
            continue
        # v1 fallback headings
        if raw_line.strip() in ("## BLOCKING", "BLOCKING:"):
            _flush()
            current = None
            current_disposition = Disposition.MUST_FIX
            continue
        if raw_line.strip() in ("## NON_BLOCKING", "NON_BLOCKING:"):
            _flush()
            current = None
            current_disposition = Disposition.ADVISORY
            continue

        item = _ITEM_RE.match(raw_line)
        if item and current_disposition is not None:
            _flush()
            current = Finding(
                disposition=current_disposition,
                severity=Severity(item.group("severity")),
                path=item.group("path"),
                line=int(item.group("line")) if item.group("line") else None,
                symbol=item.group("symbol"),
                raw=raw_line,
            )
            continue

        ev = _EVIDENCE_RE.match(raw_line)
        if ev and current is not None:
            key, val = ev.group("key"), ev.group("val").strip()
            setattr(current, key, val)
            current.raw += "\n" + raw_line
            continue

    _flush()

    for f in findings:
        if f.disposition is Disposition.MUST_FIX and not f.is_evidence_complete():
            f.contract_incomplete = True

    verdict_match = _VERDICT_RE.search(text)
    verdict_tail = verdict_match.group(1) if verdict_match else None

    # Section membership is authoritative; a VERDICT that disagrees is logged,
    # never trusted (dev-ralf-renewal-claude.md §3.4).
    section_says_fail = any(f.disposition is Disposition.MUST_FIX for f in findings)
    contract_mismatch = bool(
        verdict_tail is not None
        and ((verdict_tail == "FAIL") != section_says_fail)
    )

    return ReviewResult(
        role_status=RoleStatus.COMPLETE,
        findings=findings,
        verdict_tail=verdict_tail,
        contract_mismatch=contract_mismatch,
    )


def parse_ocr_result(payload: dict) -> ReviewResult:
    """Normalize OCR's JSON (``{status, comments[], failed[]}``) directly.

    worker.md -> *`ocr` reviewers*: any non-empty ``failed[]`` is a
    verification failure (INCONCLUSIVE), never a synthetic finding -- this
    was the actual bug in the pre-renewal contract (dev-ralf-renewal-claude.md
    §1.3).
    """
    failed = payload.get("failed") or []
    if failed or payload.get("status") != "success":
        return ReviewResult(role_status=RoleStatus.INCONCLUSIVE, findings=[])

    findings: list[Finding] = []
    for c in payload.get("comments", []):
        disposition, severity = _OCR_SEVERITY_TO_DISPOSITION[c["severity"]]
        findings.append(
            Finding(
                disposition=disposition,
                severity=severity,
                path=c["path"],
                line=c.get("start_line"),
                symbol=None,
                note=c.get("content"),
                raw=str(c),
            )
        )
    return ReviewResult(role_status=RoleStatus.COMPLETE, findings=findings)


def merge(*results: ReviewResult) -> ReviewResult:
    """Merge across reviewers.

    Any MUST_FIX from ANY reviewer blocks -- equivalent to Bernstein's
    ``review --pipeline`` ``strategy: all`` aggregator (verified against
    installed 3.15.1 source: ``core/quality/review_pipeline/verdict.py``).
    This function exists for the case where the pipeline's own aggregator
    cannot be used (e.g. merging OCR's JSON output with a text-contract
    reviewer's output ahead of a single ``strategy: all`` stage).
    """
    if any(r.role_status is RoleStatus.INCONCLUSIVE for r in results):
        return ReviewResult(role_status=RoleStatus.INCONCLUSIVE, findings=[])
    if any(r.role_status is RoleStatus.ERROR for r in results):
        return ReviewResult(role_status=RoleStatus.ERROR, findings=[])

    seen: dict[str, Finding] = {}
    for r in results:
        for f in r.findings:
            seen.setdefault(f.key(), f)

    return ReviewResult(
        role_status=RoleStatus.COMPLETE,
        findings=list(seen.values()),
        contract_mismatch=any(r.contract_mismatch for r in results),
    )
