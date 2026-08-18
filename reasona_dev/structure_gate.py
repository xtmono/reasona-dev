"""Deterministic structural checks -- the category an LLM reviewer cannot
decide, by construction.

**Why this is a separate gate and not another reviewer role.** Every review
role in this pipeline reads a DIFF. Module size, duplication across files,
and dependency direction are properties of the WHOLE tree, so a reviewer
looking at a 200-line diff has no way to reach them: an 11,288-line file
grows 200 lines at a time, and no single one of those diffs is refusable.
dev-ralf's production record shows exactly this outcome -- two files past
10,000 lines with a five-role review stack running on every PR, and a
separate refactoring plan eventually scheduled after the fact to deal with
them. Adding a sixth reviewer would not have caught it; the judgment simply
is not available at the diff level.

That makes this the same architectural move the rest of this project is
built on (`gate_check.py`, `cycle_gate.py`, `finding_adapter.py`): take a
judgment out of the model and make it a computation. This module is the
missing member of that family, not an addition to the review stack.

**Checks are exact, never heuristic.** Every check here is either a line
count, a regex the operator declared, or a hash comparison. Nothing infers
"code smell". A check that could produce a false positive the operator
cannot predict from its own config does not belong here -- it belongs in a
review prompt, where a human-legible finding can be argued with. The
consequence is that the check set is smaller than a typical linter's, and
deliberately so: this gate FAILS a PR, so its verdicts have to be
mechanically defensible.

**Config** lives under `structure-gate:` in `reasona.yaml`, using the same
global-then-project cascade as `dev-models:`
(`reasona_dev.config_file`), so thresholds are per-repo (a Rust workspace
and a Python package do not share a sensible file-size limit) with an
operator-wide default underneath:

    structure-gate:
      max_file_lines:
        limit: 1200
        include: ["**/*.rs", "**/*.py"]
      max_added_lines_per_file:
        limit: 400
      duplicate_block:
        window: 30
        include: ["**/*.rs"]
      forbidden_dependency:
        - name: "domain must not import infra"
          in: ["crates/domain/**/*.rs"]
          pattern: '^\\s*use\\s+crate::infra::'
      max_public_api_growth:
        limit: 15
        pattern: '^\\+\\s*pub\\s+(fn|struct|enum|trait|type|const|static)\\s'
      waivers:
        - check: max_file_lines
          path: crates/tas-plan/src/validator.rs
          reason: "scheduled for split in PR 24"

**Waivers are explicit, pathed, and reasoned.** An exception is a recorded
decision, not a silent threshold bump -- which is the difference between a
gate that shapes the codebase and one that gets raised every time it fires.
A waiver with no `reason` is rejected.

**Absent config disables the gate.** A repo that has declared no
`structure-gate:` block passes unconditionally rather than inheriting
someone else's limits. Enforcement has to be opted into per repo, because
a threshold that is wrong for a codebase produces exactly the reflexive
waivers this design is trying to avoid.
"""

from __future__ import annotations

import fnmatch
import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Violation:
    check: str
    path: str
    detail: str
    actual: int | None = None
    limit: int | None = None

    def render(self) -> str:
        bound = ""
        if self.actual is not None and self.limit is not None:
            bound = f" ({self.actual} > {self.limit})"
        return f"[{self.check}] {self.path}: {self.detail}{bound}"


def load_config(workdir: str | Path) -> dict:
    """`structure-gate:` from the project layer, else the global layer.

    Whole-block override, not a per-key merge: a repo that declares the
    block owns its limits completely. Merging would silently reintroduce a
    global limit the repo thought it had replaced -- the same
    CONDUCTOR-COLLAPSE failure `model_config.py` avoids by making every
    resolution report its own source layer.
    """
    from reasona_dev import config_file

    project = config_file.load_project(workdir).get("structure-gate")
    if isinstance(project, dict):
        return project
    glob = config_file.load_global().get("structure-gate")
    return glob if isinstance(glob, dict) else {}


def _matches_any(path: str, patterns: list[str]) -> bool:
    """`fnmatch` with the `**/` prefix people actually expect.

    `fnmatch`'s `*` already crosses `/`, so a literal `**/*.rs` pattern
    demands at least one directory separator and silently fails to match a
    top-level `a.rs` -- which would exempt exactly the files most likely to
    be a repo's oversized ones. Trying the pattern again with `**/` stripped
    makes `**/*.rs` mean "any .rs at any depth, including the root", which
    is what every operator writing that pattern intends.
    """
    for p in patterns:
        if fnmatch.fnmatch(path, p):
            return True
        if p.startswith("**/") and fnmatch.fnmatch(path, p[3:]):
            return True
    return False


def _tracked_files(workdir: Path) -> list[str]:
    try:
        out = subprocess.run(
            ["git", "-C", str(workdir), "ls-files"],
            capture_output=True, text=True, check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def _diff_numstat(workdir: Path, base: str, head: str) -> dict[str, int]:
    """path -> added line count, from `git diff --numstat base...head`."""
    try:
        out = subprocess.run(
            ["git", "-C", str(workdir), "diff", "--numstat", f"{base}...{head}"],
            capture_output=True, text=True, check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return {}
    added: dict[str, int] = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 3 or parts[0] == "-":  # "-" marks a binary file
            continue
        try:
            added[parts[2].strip()] = int(parts[0])
        except ValueError:
            continue
    return added


def _diff_text(workdir: Path, base: str, head: str) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(workdir), "diff", f"{base}...{head}"],
            capture_output=True, text=True, check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return ""


def check_max_file_lines(workdir: Path, cfg: dict) -> list[Violation]:
    """The headline check: no tracked file exceeds `limit` lines.

    Whole-file, not diff-scoped, on purpose -- a file is over the limit
    regardless of which PR pushed it over, and scoping to the diff would
    let a file cross the threshold in whichever PR happens to be smallest.
    """
    limit = cfg.get("limit")
    if not isinstance(limit, int):
        return []
    include = cfg.get("include") or ["**/*"]
    violations: list[Violation] = []
    for rel in _tracked_files(workdir):
        if not _matches_any(rel, include):
            continue
        f = workdir / rel
        if not f.is_file():
            continue
        try:
            n = sum(1 for _ in f.open("r", encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if n > limit:
            violations.append(
                Violation("max_file_lines", rel, "file exceeds line limit", n, limit)
            )
    return violations


def check_max_added_lines_per_file(workdir: Path, cfg: dict, base: str, head: str) -> list[Violation]:
    """Growth check: no single file gains more than `limit` lines in one PR.

    Complements `max_file_lines` rather than duplicating it -- this one
    fires on a 900-line addition to a 200-line file, which is still under
    any sane absolute limit but is the shape that produces an unreviewable
    diff.
    """
    limit = cfg.get("limit")
    if not isinstance(limit, int):
        return []
    include = cfg.get("include") or ["**/*"]
    return [
        Violation("max_added_lines_per_file", path, "single-PR growth exceeds limit", added, limit)
        for path, added in sorted(_diff_numstat(workdir, base, head).items())
        if _matches_any(path, include) and added > limit
    ]


_COMMENT_OR_BLANK = re.compile(r"^\s*(//|#|/\*|\*|$)")


def _normalized_windows(text: str, window: int) -> list[tuple[str, int, int]]:
    """(hash, source_line, window_index) for each sliding window of `window`
    significant lines. Whitespace is collapsed and comment/blank lines are
    dropped, so reindentation and comment edits do not hide a duplicate --
    and do not invent one either.

    `window_index` is the position in the SIGNIFICANT-line sequence, not the
    source line, because that is the axis overlapping windows have to be
    collapsed along (source lines are non-contiguous once comments are
    dropped).
    """
    lines = [
        (i + 1, re.sub(r"\s+", " ", ln.strip()))
        for i, ln in enumerate(text.splitlines())
        if not _COMMENT_OR_BLANK.match(ln)
    ]
    out: list[tuple[str, int, int]] = []
    for i in range(len(lines) - window + 1):
        chunk = "\n".join(ln for _, ln in lines[i : i + window])
        out.append((hashlib.sha256(chunk.encode()).hexdigest()[:16], lines[i][0], i))
    return out


def check_duplicate_block(workdir: Path, cfg: dict) -> list[Violation]:
    """Exact duplicate spans of `window` significant lines, across all
    included files.

    Reports only the SECOND and later occurrence of each hash, so the
    original is never blamed for its own copy.

    Overlapping windows inside one duplicated region collapse to a single
    violation: after reporting a window starting at significant-line index
    `i`, every window that starts before `i + window` is suppressed. Without
    this a 60-line copy at window=30 reports 31 near-identical violations
    -- each with a DIFFERENT hash, so deduplicating by hash (the obvious
    approach) does not collapse them at all.
    """
    window = cfg.get("window")
    if not isinstance(window, int) or window < 2:
        return []
    include = cfg.get("include") or ["**/*"]
    seen: dict[str, tuple[str, int]] = {}
    violations: list[Violation] = []
    for rel in sorted(_tracked_files(workdir)):
        if not _matches_any(rel, include):
            continue
        f = workdir / rel
        if not f.is_file():
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        suppress_before = -1
        for h, start, idx in _normalized_windows(text, window):
            if h not in seen:
                seen[h] = (rel, start)
                continue
            if idx < suppress_before:
                continue
            suppress_before = idx + window
            origin_path, origin_line = seen[h]
            violations.append(
                Violation(
                    "duplicate_block",
                    f"{rel}:{start}",
                    f"{window} significant lines duplicated from {origin_path}:{origin_line}",
                )
            )
    return violations


def check_forbidden_dependency(workdir: Path, cfg: list) -> list[Violation]:
    """Operator-declared dependency-direction rules.

    Each rule is `{name, in: [globs], pattern: <regex>}` -- a file matching
    `in` must contain no line matching `pattern`. This is the layering
    constraint an architecture doc states in prose and nothing enforces;
    stating it as a regex the operator wrote keeps the check exact and its
    failures explicable.
    """
    if not isinstance(cfg, list):
        return []
    violations: list[Violation] = []
    tracked = _tracked_files(workdir)
    for rule in cfg:
        if not isinstance(rule, dict):
            continue
        name = str(rule.get("name") or "forbidden dependency")
        globs = rule.get("in") or []
        raw = rule.get("pattern")
        if not globs or not isinstance(raw, str):
            continue
        try:
            pattern = re.compile(raw, re.MULTILINE)
        except re.error:
            continue
        for rel in tracked:
            if not _matches_any(rel, globs):
                continue
            f = workdir / rel
            if not f.is_file():
                continue
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for i, line in enumerate(text.splitlines(), start=1):
                if pattern.search(line):
                    violations.append(
                        Violation("forbidden_dependency", f"{rel}:{i}", name)
                    )
    return violations


def check_max_public_api_growth(workdir: Path, cfg: dict, base: str, head: str) -> list[Violation]:
    """Count added public declarations in the PR's diff against `limit`.

    Surface area is the thing a reviewer least reliably tracks and the
    thing hardest to remove later, since every added symbol is a
    compatibility obligation the moment it merges. `pattern` matches
    ADDED diff lines (leading `+`), so the operator declares what "public"
    means in their language rather than this module guessing.
    """
    limit = cfg.get("limit")
    raw = cfg.get("pattern")
    if not isinstance(limit, int) or not isinstance(raw, str):
        return []
    try:
        pattern = re.compile(raw)
    except re.error:
        return []
    diff = _diff_text(workdir, base, head)
    count = sum(
        1 for line in diff.splitlines()
        if line.startswith("+") and not line.startswith("+++") and pattern.search(line)
    )
    if count > limit:
        return [
            Violation(
                "max_public_api_growth", "<diff>",
                "added public declarations exceed limit", count, limit,
            )
        ]
    return []


def _waived(v: Violation, waivers: list) -> bool:
    """A waiver matches on (check, path-glob) and MUST carry a reason.

    Matching the path as a glob lets one waiver cover a directory that is
    knowingly exempt; requiring `reason` is what keeps a waiver a recorded
    decision rather than a silent threshold bump.
    """
    if not isinstance(waivers, list):
        return False
    bare_path = v.path.split(":", 1)[0]
    for w in waivers:
        if not isinstance(w, dict):
            continue
        if not str(w.get("reason") or "").strip():
            continue
        if w.get("check") != v.check:
            continue
        wp = w.get("path")
        if not isinstance(wp, str):
            continue
        if bare_path == wp or fnmatch.fnmatch(bare_path, wp):
            return True
    return False


def evaluate(workdir: str | Path, *, base: str = "origin/main", head: str = "HEAD") -> list[Violation]:
    """Run every configured check. Empty list means the gate passes.

    An empty/absent `structure-gate:` block returns no violations -- see the
    module docstring on why enforcement is opt-in per repo.
    """
    workdir = Path(workdir)
    cfg = load_config(workdir)
    if not cfg:
        return []

    violations: list[Violation] = []
    if isinstance(cfg.get("max_file_lines"), dict):
        violations += check_max_file_lines(workdir, cfg["max_file_lines"])
    if isinstance(cfg.get("max_added_lines_per_file"), dict):
        violations += check_max_added_lines_per_file(workdir, cfg["max_added_lines_per_file"], base, head)
    if isinstance(cfg.get("duplicate_block"), dict):
        violations += check_duplicate_block(workdir, cfg["duplicate_block"])
    if cfg.get("forbidden_dependency"):
        violations += check_forbidden_dependency(workdir, cfg["forbidden_dependency"])
    if isinstance(cfg.get("max_public_api_growth"), dict):
        violations += check_max_public_api_growth(workdir, cfg["max_public_api_growth"], base, head)

    waivers = cfg.get("waivers") or []
    return [v for v in violations if not _waived(v, waivers)]


def main(argv: list[str]) -> int:
    """CLI, same shape as `gate_check.py`: exit 0 passes, exit 1 fails.

        python3 -m reasona_dev.structure_gate [workdir] [base] [head]
    """
    import sys

    workdir = argv[0] if argv else "."
    base = argv[1] if len(argv) > 1 else "origin/main"
    head = argv[2] if len(argv) > 2 else "HEAD"
    violations = evaluate(workdir, base=base, head=head)
    if not violations:
        print("reasona-dev structure gate: PASS", file=sys.stderr)
        return 0
    print(f"reasona-dev structure gate: FAIL ({len(violations)} violation(s))", file=sys.stderr)
    for v in violations:
        print("  " + v.render(), file=sys.stderr)
    return 1


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
