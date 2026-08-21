"""Plan-level teardown reporting -- what a finished run's plan promised but
the repo never got, and what its PR units touched beyond what they declared.

Ports the two reporters dev-ralf runs once at *Teardown & final report*
(`tools/completeness.py` and `tools/scope_report.py`). Both **report and
never block**: they run after every unit has already reached a terminal
outcome, so there is nothing left for them to gate. reasona-dev had no
plan-level pass at all before this -- `orchestrate.run_plan()` returned
straight out of its per-unit loop and the CLI printed a status tally -- so
a plan that named a symbol nobody implemented, or a unit that edited files
it never declared, produced no signal anywhere.

**Why plan-level rather than per-PR.** dev-ralf measured the per-PR variant
and abandoned it: matching each PR's own section names against only that
PR's diff flagged 19.7% of names with zero real findings, because a plan
legitimately names things a LATER unit builds. Deferring the same check to
the whole plan, against the whole repo, dropped that to a 0% noise floor on
its corpus. This module follows that finding rather than re-deriving it --
`completeness()` only ever asks "did ANY unit of this plan produce this
name, anywhere in the repo".

**Why `changed_files` is captured earlier, not here.** A merged unit's
worktree is removed by `orchestrate.py` the moment it ships, so its branch
diff is gone by teardown. `final_phase.capture_changed_files()` records it
just before the squash-merge and carries it on `TailResult.changed_files`
-- the same ordering constraint worker.md states in its own *Squash merge*
section.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# Backtick-quoted spans are how a plan document names a concrete artifact.
# Bounded length so a backticked prose sentence is not mistaken for an
# identifier.
_BACKTICK_RE = re.compile(r"`([^`\n]{1,120})`")

# A token worth checking: a path-like name, a dotted/qualified name, or a
# bare identifier long enough not to be an English word by accident.
_PATHLIKE_RE = re.compile(r"^[\w./-]+\.[A-Za-z0-9]{1,8}$")
_QUALIFIED_RE = re.compile(r"^[A-Za-z_][\w]*(?:[.:][A-Za-z_][\w]*)+$")
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{3,}$")

# Extensions worth searching for a promised name. Mirrors dev-ralf
# `completeness.py`'s CORPUS_EXT.
_CORPUS_EXT = (
    ".rs", ".py", ".go", ".ts", ".tsx", ".js", ".toml", ".yaml", ".yml",
    ".json", ".md", ".sh", ".sql", ".proto",
)

# Source-ish extensions for the scope report -- a docs or config file
# touched outside a unit's declared set is not the signal this is after
# (dev-ralf `scope_report.py`'s SOURCE_EXT).
_SOURCE_EXT = (
    ".rs", ".py", ".go", ".ts", ".tsx", ".js", ".java", ".kt", ".rb",
    ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".swift", ".scala",
)

# The plan documents themselves are not evidence that a plan was
# implemented -- a name appearing only in the plan that promised it is
# exactly the absence being looked for. dev-ralf hardcodes `docs/plans/`
# because that is where its plans live; reasona-dev takes an arbitrary
# `--plan` path, so the file actually being reported on is excluded BY
# PATH too (`completeness(..., plan_path=...)`). Without that, a plan kept
# anywhere else is its own evidence and the report is silently always
# clean -- caught by running the report against a real repo rather than
# only against fixtures that happened to use the conventional directory.
_EXCLUDE_PREFIXES = ("docs/plans/",)


@dataclass
class CompletenessReport:
    """Names a plan promised that no file in the repo contains."""

    absent: dict[str, list[str]] = field(default_factory=dict)  # unit index -> names
    checked: int = 0

    @property
    def clean(self) -> bool:
        return not self.absent


@dataclass
class ScopeReport:
    """Source files a unit changed without declaring them in its `files:`."""

    undeclared: dict[str, list[str]] = field(default_factory=dict)  # unit index -> paths
    undeclared_units: int = 0
    measured_units: int = 0

    @property
    def clean(self) -> bool:
        return not self.undeclared


def _candidate_names(section: str) -> set[str]:
    """Backticked tokens from a plan section that look like a real artifact."""
    names: set[str] = set()
    for raw in _BACKTICK_RE.findall(section):
        tok = raw.strip().strip("(),;:")
        if not tok or " " in tok:
            continue
        # A call/expression like `foo()` names `foo`.
        tok = tok.split("(", 1)[0].rstrip(".")
        if not tok:
            continue
        if _PATHLIKE_RE.match(tok) or _QUALIFIED_RE.match(tok) or _IDENT_RE.match(tok):
            names.add(tok)
    return names


def _corpus_text(workdir: Path, plan_path: Path | None = None) -> str:
    """Every searchable file's text, concatenated once.

    One pass and one big string rather than a grep per name: a plan names
    on the order of hundreds of tokens, and re-walking the tree for each
    would dominate a teardown step that is supposed to be cheap.
    """
    chunks: list[str] = []
    for path in workdir.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in _CORPUS_EXT:
            continue
        try:
            rel = path.relative_to(workdir).as_posix()
        except ValueError:  # pragma: no cover -- rglob always yields children
            continue
        if rel.startswith(".git/") or "/.worktrees/" in f"/{rel}" or rel.startswith(".worktrees/"):
            continue
        if any(rel.startswith(p) for p in _EXCLUDE_PREFIXES):
            continue
        if plan_path is not None and path.resolve() == plan_path:
            continue
        try:
            chunks.append(path.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
        chunks.append(rel)  # a promised FILE counts as present if the path exists
    return "\n".join(chunks)


def completeness(workdir: str | Path, units, *, plan_path: str | Path | None = None) -> CompletenessReport:
    """Which backticked names a plan's units promised that the repo lacks.

    `units` is a list of `plan_compile.PRUnit`. Reports; never blocks. A
    name is "present" if it appears anywhere in the searchable corpus, by
    ANY unit -- see the module docstring on why this is plan-level.
    """
    workdir = Path(workdir)
    resolved_plan = Path(plan_path).resolve() if plan_path is not None else None
    corpus = _corpus_text(workdir, resolved_plan)
    report = CompletenessReport()
    for unit in units:
        names = _candidate_names(unit.section or "")
        report.checked += len(names)
        missing = sorted(n for n in names if n not in corpus)
        if missing:
            report.absent[unit.index] = missing
    return report


def scope_divergence(outcomes) -> ScopeReport:
    """Source files each unit changed but never declared in its `files:`.

    `outcomes` is `orchestrate.PlanRunResult.outcomes`. Only units that
    actually recorded `changed_files` are measured -- a unit that never
    reached the merge step has nothing to compare, and is counted out
    rather than silently reported clean.
    """
    report = ScopeReport()
    for outcome in outcomes:
        tail = getattr(outcome, "tail", None)
        changed = list(getattr(tail, "changed_files", []) or []) if tail else []
        if not changed:
            continue
        report.measured_units += 1
        declared = set(getattr(getattr(outcome, "unit", None), "files", None) or [])
        extra = sorted(
            p for p in changed
            if Path(p).suffix.lower() in _SOURCE_EXT and p not in declared
        )
        if extra:
            report.undeclared[outcome.stage_name] = extra
            report.undeclared_units += 1
    return report


def render(completeness_report: CompletenessReport, scope_report: ScopeReport) -> str:
    """One compact block for the end of a run. Never raises."""
    lines = ["plan report (reporting only -- nothing here blocked a merge):"]

    if completeness_report.clean:
        lines.append(
            f"  completeness: {completeness_report.checked} promised name(s) checked, all present"
        )
    else:
        total = sum(len(v) for v in completeness_report.absent.values())
        lines.append(
            f"  completeness: {total} promised name(s) not found in the repo "
            f"(of {completeness_report.checked} checked)"
        )
        for index, names in sorted(completeness_report.absent.items()):
            lines.append(f"    PR {index}: {', '.join(names)}")

    if scope_report.measured_units == 0:
        lines.append("  scope: no unit recorded its changed files -- nothing to compare")
    elif scope_report.clean:
        lines.append(
            f"  scope: {scope_report.measured_units} unit(s) measured, none touched an undeclared source file"
        )
    else:
        lines.append(
            f"  scope: {scope_report.undeclared_units} of {scope_report.measured_units} "
            "unit(s) touched a source file they did not declare"
        )
        for stage_name, paths in sorted(scope_report.undeclared.items()):
            lines.append(f"    {stage_name}: {', '.join(paths)}")

    return "\n".join(lines)


def build(workdir: str | Path, units, outcomes, *, plan_path: str | Path | None = None) -> str:
    """`completeness` + `scope_divergence`, rendered. Never raises.

    Wrapped defensively because this runs at the very end of a real run:
    a reporting bug must not turn a successful plan into a traceback after
    every unit has already merged.
    """
    try:
        return render(
            completeness(workdir, units, plan_path=plan_path), scope_divergence(outcomes)
        )
    except Exception as exc:  # noqa: BLE001 -- reporting only, never fatal
        return f"plan report: skipped ({type(exc).__name__}: {exc})"
