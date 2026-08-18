"""Repo-scoped memory, GENERATED from `cycles.jsonl` -- never hand-written.

**Why generation is the whole design.** A memory directory is the same kind
of surface a skill document is, and skill documents bloat for a reason that
has nothing to do with their format: every entry is easy to add and nobody
owns deleting one. dev-ralf's own SKILL.md reached 472 lines, a large share
of it prose explaining why superseded revisions were wrong, and all of it
loaded into every agent's context on every run. Relocating that habit into
`.reasona/memory/*.md` would reproduce it exactly.

So memory here is not a notebook. It is a projection of measurement:
`cycles_log` records what every gate actually found, and this module
derives, from those records alone, the patterns that have recurred across
DISTINCT PR units. Three properties follow directly, and none of them
require anyone's discipline:

- *No drift.* A memory cannot disagree with what happened, because it is
  computed from what happened.
- *Automatic decay.* Generation reads only the last `window_units` PR
  units, so a pattern that stops recurring stops being written. Nobody has
  to notice it became obsolete.
- *Bounded size.* Entries exist only above a recurrence threshold, and
  retrieval caps how many reach a prompt.

**Clustering is exact, never inferred.** Two grouping keys are used, both
of which either match or do not:

    location   the same (path, symbol) flagged in >= N distinct PR units
    contract   the same normalized contract text in >= N distinct units

Neither attempts to recognize a paraphrase. That under-clusters -- two
findings describing one problem in different words stay separate -- and
that is the intended trade: a memory injected into a review prompt shapes
what the next reviewer looks for, so a wrong grouping actively misdirects
attention. Missing a pattern costs less than inventing one.

**Retrieval is file-scoped.** A memory carries the paths it was observed
in; a PR unit already declares `files:` in its manifest. Injecting only the
memories whose scope intersects the unit's files is what keeps this from
becoming a growing preamble on every prompt -- and the retrieval key costs
nothing, because both halves already exist.

**What does NOT belong here.** Anything a program can enforce. A rule that
`structure_gate` or an acceptance criterion can check is not a memory; it
is a check, and writing it here instead would be choosing to remind a model
of something the pipeline could simply guarantee.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from reasona_dev import cycles_log

DEFAULT_MIN_OCCURRENCES = 2
DEFAULT_WINDOW_UNITS = 10
DEFAULT_INJECT_LIMIT = 5

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(?P<body>.*?)\n---\s*\n(?P<rest>.*)\Z", re.DOTALL)


def memory_dir(workdir: str | Path) -> Path:
    return Path(workdir) / ".reasona" / "memory"


@dataclass
class Memory:
    name: str
    description: str
    scope_files: list[str] = field(default_factory=list)
    observed: list[str] = field(default_factory=list)
    body: str = ""

    def render(self) -> str:
        scope = "".join(f"\n  - {p}" for p in self.scope_files)
        observed = "".join(f"\n  - {u}" for u in self.observed)
        return (
            "---\n"
            f"name: {self.name}\n"
            f"description: {self.description}\n"
            f"scope_files:{scope}\n"
            f"observed:{observed}\n"
            "generated_from: cycles.jsonl\n"
            "---\n\n"
            f"{self.body}\n"
        )


def _slug(text: str, limit: int = 60) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s[:limit].rstrip("-")) or "unnamed"


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _scope_of(path: str) -> str:
    """The directory a finding is attributed to.

    Directory rather than exact file: a pattern that recurs in a module is
    about the module, and scoping to one file would fail to match the next
    PR that touches its sibling.
    """
    parent = str(Path(path).parent)
    return "" if parent in (".", "") else parent


def _recent_units(records: list[dict], window_units: int) -> set[str]:
    """The last `window_units` distinct stage names, in append order.

    This is what makes decay automatic: a pattern outside the window is
    simply not seen by generation, so its file stops being written without
    anyone deciding it is obsolete.
    """
    seen: list[str] = []
    for r in records:
        name = r.get("stage_name")
        if name and name not in seen:
            seen.append(name)
    return set(seen[-window_units:]) if window_units else set(seen)


def derive(
    workdir: str | Path,
    *,
    min_occurrences: int = DEFAULT_MIN_OCCURRENCES,
    window_units: int = DEFAULT_WINDOW_UNITS,
) -> list[Memory]:
    """Compute memories from `cycles.jsonl`. Pure -- writes nothing."""
    records = cycles_log.read_records(workdir)
    in_window = _recent_units(records, window_units)

    # key -> {units, paths, sample description}
    by_location: dict[tuple[str, str], dict] = {}
    by_contract: dict[tuple[str, str], dict] = {}

    for r in records:
        unit = r.get("stage_name")
        if not unit or unit not in in_window or r.get("kind") == "decision":
            continue
        for f in r.get("findings") or []:
            if f.get("disposition") != "MUST_FIX":
                continue
            path = f.get("path") or ""
            if not path:
                continue
            scope = _scope_of(path)

            symbol = f.get("symbol")
            if symbol:
                key = (path, symbol)
                slot = by_location.setdefault(key, {"units": set(), "paths": set(), "roles": set()})
                slot["units"].add(unit)
                slot["paths"].add(scope or path)
                slot["roles"].add(r.get("role", "?"))

            contract = _normalize(f.get("contract") or "")
            if contract:
                key = (scope, contract)
                slot = by_contract.setdefault(key, {"units": set(), "paths": set(), "roles": set()})
                slot["units"].add(unit)
                slot["paths"].add(scope or path)
                slot["roles"].add(r.get("role", "?"))

    memories: list[Memory] = []

    for (path, symbol), slot in sorted(by_location.items()):
        if len(slot["units"]) < min_occurrences:
            continue
        units = sorted(slot["units"])
        memories.append(
            Memory(
                name=f"recurring-location-{_slug(path + '-' + symbol)}",
                description=f"{path} {symbol} was flagged MUST_FIX in {len(units)} distinct PR units",
                scope_files=sorted(slot["paths"]),
                observed=units,
                body=(
                    f"`{symbol}` in `{path}` has been reported as MUST_FIX across "
                    f"{len(units)} separate PR units ({', '.join(units)}), by "
                    f"{', '.join(sorted(slot['roles']))}.\n\n"
                    "Treat this location as historically defect-prone when reviewing "
                    "a change that touches it."
                ),
            )
        )

    for (scope, contract), slot in sorted(by_contract.items()):
        if len(slot["units"]) < min_occurrences:
            continue
        units = sorted(slot["units"])
        shown = contract if len(contract) <= 160 else contract[:157] + "..."
        memories.append(
            Memory(
                name=f"recurring-contract-{_slug((scope or 'root') + '-' + contract)}",
                description=f"the same contract violation recurred in {len(units)} PR units under {scope or 'the repo root'}",
                scope_files=[scope] if scope else [],
                observed=units,
                body=(
                    f"The same violated contract has been reported in {len(units)} "
                    f"separate PR units ({', '.join(units)}) under `{scope or '.'}`:\n\n"
                    f"> {shown}\n\n"
                    "Check for this specific condition when reviewing changes in this area."
                ),
            )
        )

    return memories


def regenerate(
    workdir: str | Path,
    *,
    min_occurrences: int = DEFAULT_MIN_OCCURRENCES,
    window_units: int = DEFAULT_WINDOW_UNITS,
) -> list[Memory]:
    """Rewrite `.reasona/memory/` to exactly match what `derive()` computes.

    Removes every generated file that no longer corresponds to a current
    memory -- that removal IS the decay mechanism, so it must not be
    softened into a merge. Hand-authored files are not preserved because
    hand-authored files are not supposed to exist here (see module
    docstring); anything under this directory is owned by generation.
    """
    memories = derive(workdir, min_occurrences=min_occurrences, window_units=window_units)
    d = memory_dir(workdir)
    d.mkdir(parents=True, exist_ok=True)

    wanted = {f"{m.name}.md" for m in memories}
    for existing in d.glob("*.md"):
        if existing.name not in wanted:
            existing.unlink()
    for m in memories:
        (d / f"{m.name}.md").write_text(m.render(), encoding="utf-8")
    return memories


def load_all(workdir: str | Path) -> list[Memory]:
    d = memory_dir(workdir)
    if not d.is_dir():
        return []
    out: list[Memory] = []
    for path in sorted(d.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        m = _FRONTMATTER_RE.match(text)
        if not m:
            continue
        meta: dict[str, list[str] | str] = {}
        current_key: str | None = None
        for line in m.group("body").splitlines():
            if line.startswith("  - ") and current_key:
                meta.setdefault(current_key, [])
                if isinstance(meta[current_key], list):
                    meta[current_key].append(line[4:].strip())
                continue
            if ":" in line:
                k, _, v = line.partition(":")
                current_key = k.strip()
                v = v.strip()
                meta[current_key] = v if v else []
        out.append(
            Memory(
                name=str(meta.get("name") or path.stem),
                description=str(meta.get("description") or ""),
                scope_files=list(meta.get("scope_files") or []),
                observed=list(meta.get("observed") or []),
                body=m.group("rest").strip(),
            )
        )
    return out


def select(
    workdir: str | Path,
    files: list[str],
    *,
    limit: int = DEFAULT_INJECT_LIMIT,
) -> list[Memory]:
    """Memories whose scope intersects `files`, most-observed first.

    A memory with no scope matches nothing rather than everything --
    "applies everywhere" is how a preamble starts growing, and an unscoped
    memory carries no evidence that it applies to the unit at hand.
    """
    if not files:
        return []
    picked: list[Memory] = []
    for m in load_all(workdir):
        if not m.scope_files:
            continue
        if any(f == s or f.startswith(s.rstrip("/") + "/") for f in files for s in m.scope_files):
            picked.append(m)
    picked.sort(key=lambda m: (-len(m.observed), m.name))
    return picked[:limit]


def render_for_prompt(memories: list[Memory]) -> str:
    """The block appended to a role prompt. Empty when nothing was selected.

    Labelled as observation, not instruction, and explicitly non-binding:
    these are priors about where defects have appeared, and a reviewer that
    treats them as a checklist would stop looking anywhere else.
    """
    if not memories:
        return ""
    lines = [
        "",
        "---",
        "PRIOR OBSERVATIONS for the files this PR touches, derived from this "
        "repository's own recorded review history. These are evidence about "
        "where defects have recurred, NOT a checklist and NOT a limit on what "
        "to examine. A prior that does not apply to this change should be "
        "ignored without comment.",
        "",
    ]
    for m in memories:
        lines.append(f"- {m.description}")
    return "\n".join(lines) + "\n"
