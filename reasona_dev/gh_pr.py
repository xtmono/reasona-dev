"""Ports `/gh-pr` (`~/repository/tas-dev-plugins/plugins/dev/skills/gh-pr/
SKILL.md`): issue -> branch rename -> push -> PR, with the same structural
title/body validation that skill enforces.

**What is NOT ported, and why.** `/gh-pr` §4 re-runs `make ci`/
`make lint-md` before creating the PR unconditionally, for any
source-touching change. reasona-dev has no such unconditional gate
anywhere -- its equivalent, `acceptance.py`, is opt-in PER PLAN UNIT (a
unit that declares no `acceptance:` passes with a warning, not a failure;
see `acceptance.py`'s own docstring and docs/ARCHITECTURE.md §3.7.3). This
module does not re-add a build/test gate of its own: when the plan DID
declare acceptance criteria, `ship_gate` already ran them against the
exact code being shipped, and re-running here would duplicate that; when
the plan did NOT declare any, that is an under-specified plan (the
author's responsibility, not something this module should silently paper
over by inventing its own build command reasona-dev cannot know is
correct for this repo).

**Branch handling differs from the original skill on purpose.** `/gh-pr`
creates its own branch (`issue/<N>-<slug>`) because it can be invoked
standalone, against whatever the caller already has checked out --
`checkout -b` from base, or `branch -m` in place on a feature branch. This
module is never invoked standalone: by the time it runs, the unit already
has its own dedicated worktree (`reasona_dev.worktree`), checked out on a
unit-named branch, from before cycle-0 even started. There is no "on base"
case to handle -- the worktree's branch is never literally base -- so this
always takes the `/gh-pr` skill's "on a feature/temp branch: rename in
place" path (`rename_branch_for_pr()`), never `checkout -b`.

**Title/body are built deterministically, then independently re-checked --
the same `build()`/`guard()` split `reasona_dev.squash` already uses.**
`build_pr_title()`/`build_pr_body()` sanitize the plan's own freeform
`## PR <index>: <title>` heading text (strip a stray `#N` prefix, a
trailing period, an unrecognized type) the same way `squash.build()`
sanitizes commit body lines, so a P1-P7 violation on a fresh build should
be vanishingly rare. `validate_pr_meta()` re-derives the same P1-P7 checks
`/gh-pr` SKILL.md §8 specifies, independently of the builder, and
`repair_pr()` pushes a corrected title/body via `gh pr edit` (never
`gh pr create` again) up to `MAX_PR_REPAIR_ATTEMPTS` times before the unit
is reported `blocked` -- not `failed`: a PR-metadata violation is not a
judgment about the code's quality.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from reasona_dev import _shell, ci_gate, config_file, final_phase, ledger
from reasona_dev.model_config import ResolvedModel
from reasona_dev.plan_compile import PRUnit

MAX_PR_REPAIR_ATTEMPTS = 3

_CC_TYPES = frozenset(
    {"feat", "fix", "docs", "style", "refactor", "test", "chore", "perf", "build", "ci", "deps", "revert"}
)
_TITLE_HASH_PREFIX_RE = re.compile(r"^#\d+\s+")
_TITLE_TRAILING_PERIOD_RE = re.compile(r"\.$")
_TITLE_CC_RE = re.compile(
    r"^(feat|fix|docs|style|refactor|test|chore|perf|build|ci|deps|revert)"
    r"(\([a-z0-9._/-]+\))?:\s+\S"
)
_BODY_CLOSES_RE = re.compile(r"\bcloses\s+#(?P<n>\d+)\b", re.IGNORECASE)
_BODY_CHANGES_RE = re.compile(r"^## Changes\s*$", re.MULTILINE)
_BODY_WHY_RE = re.compile(r"^## Why we need this\s*$", re.MULTILINE)
_BODY_TEST_RE = re.compile(r"^## Test\s*$", re.MULTILINE)


@dataclass
class GhPrResult:
    passed: bool
    reason: str
    pr_url: str | None = None
    pr_num: int | None = None
    issue_num: int | None = None
    branch: str | None = None
    duplicate: bool = False
    # The title actually used for the PR -- LLM-generated (from `generate_pr_summary()`'s
    # `title` key) when available and structurally valid, else `build_pr_title()`'s
    # deterministic fallback. Exposed so `final_phase.py`'s squash-merge message uses
    # the SAME title the PR itself carries, rather than the plan's own possibly-stale
    # one -- see `docs/ARCHITECTURE.md` on why both are LLM-generated now.
    title: str | None = None


def _kebab(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "unit"


def resolve_type_subject(unit: PRUnit) -> tuple[str, str]:
    """`unit.unit_type`/`unit.title` -- already resolved from the plan
    document's own `## PR <index>: <title>` heading and `type:` line by
    `plan_compile.py`, so there is no diff-inference heuristic to run here
    the way `/gh-pr` needs one for a standalone invocation with no plan
    context. `unit_type` falls back to `"feat"` when the plan left it
    unset, matching `squash.build()`'s own default.
    """
    return unit.unit_type or "feat", unit.title


def build_pr_title(unit_type: str, subject: str) -> str:
    """Deterministic construction, sanitized the same way `squash.build()`
    sanitizes commit text -- never emits a leading `#N` or trailing period,
    and only ever a recognized Conventional Commits type."""
    subject = _TITLE_HASH_PREFIX_RE.sub("", subject.strip())
    subject = _TITLE_TRAILING_PERIOD_RE.sub("", subject).strip()
    cc_type = unit_type if unit_type in _CC_TYPES else "feat"
    return f"{cc_type}: {subject}"


_SUMMARY_LABEL_RE = re.compile(r"(?im)^\s*(TITLE|CHANGES|WHY|TEST)\s*:\s*")


def _pr_summary_prompt(*, unit: PRUnit, plan_name: str, cc_types: frozenset[str]) -> str:
    """Asks the dispatched role to describe what this unit's diff ACTUALLY
    did, not what the plan originally intended -- the two can diverge (a
    fix made mid-review, a scope correction), and the plan's own prose is
    only the starting intent, not necessarily the final shape of the
    change. See `generate_pr_summary()`'s docstring for why this exists."""
    types = ", ".join(sorted(cc_types))
    return (
        "You are writing the title and description for a pull request this "
        "pipeline is about to open, AND the commit message it will squash "
        "onto the default branch as (same title, one of the two carries a "
        "short body too -- your TITLE line drives both). Look at the "
        "ACTUAL change in the current worktree -- `git log <base>..HEAD "
        "--oneline` and `git diff <base>...HEAD` (against this repo's "
        "default base branch) -- to see what this PR really did. If it "
        "diverged from the plan's original intent below, describe what "
        "actually happened, not the original plan.\n\n"
        f"The plan's own `## PR {unit.index}:` section for this unit "
        "(starting intent, for reference only):\n\n"
        f"{unit.section.strip() or '(no plan section)'}\n\n"
        "Write exactly four labeled sections, output ONLY this, nothing "
        "before or after:\n\n"
        f"TITLE: <a single Conventional Commits line, `type: subject` -- "
        f"type must be exactly one of: {types}; subject imperative mood, "
        "under 60 chars, no trailing period, no leading `#N`, no `Closes "
        "#N` -- describing what this PR ACTUALLY did, based on the diff/"
        "commits you just read, not the plan's original title if it "
        "diverged>\n"
        "CHANGES: <what this PR actually changed, in your own words, based "
        "on the diff/commits you just read -- plain prose, concise>\n"
        "WHY: <why this change is needed -- plain prose, concise>\n"
        "TEST: <how this change was verified -- cite the actual test/review "
        "evidence you find (test files, CI config, review artifacts), not a "
        "generic claim -- plain prose, concise>"
    )


def _parse_pr_summary(text: str) -> dict[str, str] | None:
    """Split on `TITLE:`/`CHANGES:`/`WHY:`/`TEST:` labels, in any order,
    tolerating extra prose around them -- real LLM output is not guaranteed
    to match a single rigid template. `None` (not a partial dict) when any
    of `changes`/`why`/`test` is missing or empty, so the caller has one
    unambiguous fallback condition rather than three. `title` is optional --
    its own structural validity (Conventional Commits form) is re-checked
    independently by `validate_pr_meta()`/`repair_pr()` downstream, the same
    build/guard split every other title source in this module already goes
    through, so a malformed or missing `TITLE:` here just falls back to the
    deterministic `build_pr_title()` rather than invalidating the whole
    summary."""
    matches = list(_SUMMARY_LABEL_RE.finditer(text))
    if not matches:
        return None
    sections: dict[str, str] = {}
    for i, m in enumerate(matches):
        label = m.group(1).upper()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections[label] = text[start:end].strip()
    changes, why, test = sections.get("CHANGES"), sections.get("WHY"), sections.get("TEST")
    if not (changes and why and test):
        return None
    result = {"changes": changes, "why": why, "test": test}
    if sections.get("TITLE"):
        result["title"] = sections["TITLE"]
    return result


def generate_pr_summary(
    *, workdir: Path, unit: PRUnit, plan_name: str, model: ResolvedModel,
    rundir: str | Path, port: int = 8052, run_role_fn=None,
) -> dict[str, str] | None:
    """Dispatch a role against THIS unit's own worktree to summarize what
    its diff/commits actually did, for the issue/PR bodies about to be
    created -- instead of dumping the plan's own pre-development prose
    verbatim (`build_pr_body()`'s old, and still fallback, behavior).

    **Why this exists.** A static template can only ever describe the
    plan's original INTENT (`unit.section`), never what the unit's diff
    actually ended up doing -- and the two are not guaranteed to match (a
    fix made mid-review can change scope, correct an approach, or touch
    something the plan didn't anticipate). dev-ralf's own worker used an
    agent to write this summary at PR-creation time for exactly this
    reason; this restores that behavior for reasona-dev's static-template
    replacement (`docs/ARCHITECTURE.md` -- gh-pr body generation).

    Reuses the `"backend"` Bernstein role (already whitelisted in every
    `bernstein.yaml` this project ships/consumes) rather than a new role
    name of its own -- introducing a new role would require every target
    repo's `bernstein-template.yaml` (this project's own, AND every
    consumer repo's, e.g. TAS's hand-authored union template) to add an
    entry before task creation would even succeed, for what is a
    text-summarization task well within `"backend"`'s existing remit.
    `label="pr_body"` still keeps this dispatch's raw-output filename and
    `cycles_log` entry distinct from any other `"backend"` dispatch in the
    same cycle.

    Returns `None` -- never raises -- on any dispatch failure or unparsable
    output; `run_gh_pr()` falls back to `build_pr_body()`'s deterministic
    plan-section dump in that case, so a flaky/exhausted model never blocks
    PR creation outright. The optional `title` key in the returned dict (see
    `_parse_pr_summary()`) similarly falls back to `build_pr_title()` when
    absent or structurally invalid -- `validate_pr_meta()`/`repair_pr()`
    catch that the same way they already catch any other title violation.
    """
    if run_role_fn is None:
        from reasona_dev.pr_cycle import run_role as run_role_fn
    try:
        result = run_role_fn(
            workdir=workdir, role="backend", label="pr_body",
            title=f"pr-body summary: {unit.title}",
            prompt=_pr_summary_prompt(unit=unit, plan_name=plan_name, cc_types=_CC_TYPES),
            model=model, rundir=Path(rundir), cycle=1, port=port,
        )
    except Exception:
        return None
    if result.error_detail or not result.raw_output_path.is_file():
        return None
    text = result.raw_output_path.read_text(encoding="utf-8")
    return _parse_pr_summary(text)


def build_pr_body(*, issue_num: int, plan_name: str, unit: PRUnit, summary: dict[str, str] | None = None) -> str:
    """The three sections `/gh-pr` SKILL.md §8 requires (`## Changes`,
    `## Why we need this`, `## Test`) plus the `Closes #N` line P4 checks
    for.

    `summary`, when given (`generate_pr_summary()`'s parsed output), fills
    all three sections with the actual-diff description an LLM wrote.
    Omitted (or `None`, e.g. the dispatch failed or wasn't configured),
    "Why"/"Test" fall back to what this pipeline knows without a model
    call: this unit's plan section as the change description, and the fact
    that review/scan/ship_gate's acceptance axis already passed.
    """
    if summary:
        changes, why, test = summary["changes"], summary["why"], summary["test"]
    else:
        changes = unit.section.strip() or f"See PR {unit.index} of plan `{plan_name}`."
        why = f"Implements PR {unit.index} of plan `{plan_name}`."
        test = (
            "Verified by this pipeline's review/scan cycle and the plan's "
            "own executable acceptance criteria (ship_gate) before this PR "
            "was opened."
        )
    return "\n\n".join(
        [f"Closes #{issue_num}", "## Changes", changes, "## Why we need this", why, "## Test", test]
    )


def _title_from_summary(summary: dict[str, str] | None) -> str | None:
    """Extract and sanitize an LLM-proposed `TITLE:` line, splitting `type:
    subject` the same way a caller building one by hand would, then reusing
    `build_pr_title()`'s own sanitization (strip a stray `#N` prefix, a
    trailing period, an unrecognized type) rather than duplicating it.
    Returns `None` -- the caller's cue to keep the deterministic
    `build_pr_title()` title -- when `summary` has no title, or the line
    has no `:` to split into type/subject at all."""
    if not summary or not summary.get("title"):
        return None
    raw = summary["title"].strip()
    if ":" not in raw:
        return None
    cc_type, _, subject = raw.partition(":")
    subject = subject.strip()
    if not subject:
        return None
    return build_pr_title(cc_type.strip().lower(), subject)


def validate_pr_meta(*, title: str, body: str, issue_num: int) -> list[str]:
    """Independent re-derivation of `/gh-pr` SKILL.md §8's P1-P7 checks --
    never consults `build_pr_title()`/`build_pr_body()`'s own logic, the
    same `squash.guard()` re-derives `squash.build()`'s output rather than
    trusting it. Returns the violated check codes, `[]` if clean."""
    violations = []
    if _TITLE_HASH_PREFIX_RE.match(title):
        violations.append("P1")
    if not _TITLE_CC_RE.match(title):
        violations.append("P2")
    if _TITLE_TRAILING_PERIOD_RE.search(title):
        violations.append("P3")
    m = _BODY_CLOSES_RE.search(body)
    if not m or int(m.group("n")) != issue_num:
        violations.append("P4")
    if not _BODY_CHANGES_RE.search(body):
        violations.append("P5")
    if not _BODY_WHY_RE.search(body):
        violations.append("P6")
    if not _BODY_TEST_RE.search(body):
        violations.append("P7")
    return violations


def find_duplicate_pr(workdir: Path, *, title: str) -> tuple[int | None, str | None]:
    """worker.md's DUP-WORKER guard: before creating anything, search open
    PRs for this unit's EXACT `<type>: <subject>` title. Returns
    `(pr_number, pr_url)` on a match, else `(None, None)`.

    Guards against `create_pr()`'s own idempotency check
    (`existing_pr_url()`, branch-scoped: "does THIS branch already have an
    open PR") missing a duplicate opened under a DIFFERENT branch for the
    SAME logical unit -- the case a `--restart` after a lost/cleared ledger
    can produce, when a prior run's PR still exists on GitHub but this
    run's ledger no longer remembers it.
    """
    code, out, _ = _shell.run(
        ["gh", "pr", "list", "--state", "open", "--search", f"{title} in:title",
         "--json", "number,title,url"],
        workdir, timeout=60,
    )
    if code != 0 or not out.strip():
        return None, None
    try:
        items = json.loads(out)
    except json.JSONDecodeError:
        return None, None
    for item in items:
        if item.get("title") == title:
            return item.get("number"), item.get("url")
    return None, None


def list_merged_pr_titles(workdir: Path, *, limit: int = 200) -> dict[str, tuple[int, str]]:
    """B-4 (a scoped-down GitHub-state sweep): title -> (pr_number, pr_url)
    for every recently-merged PR, in ONE `gh` call for a whole plan run.

    Reused deliberately as a single batch fetch rather than dev-ralf's own
    per-PR title-normalization + body-scoring heuristic (execution-plan.md)
    AND rather than one `gh pr list` search per unit: this project's local
    `ledger.json` is a disk file, not context an LLM scheduler can lose to
    compaction, so the only real gap this closes is a LOST or
    `--restart`-cleared ledger re-developing a unit GitHub already shows as
    done -- an exact-title match against one batch listing is enough for
    that case, and costs the SAME one network round trip per run regardless
    of plan size (an N-unit plan making N separate `gh pr list --search`
    calls, every single run including a completely fresh one with nothing
    to find yet, was the wrong shape for what is meant to be a rare-path
    safety net, not a per-unit step).

    Empty dict (never raises) on an unreachable `gh`/empty result -- the
    caller degrades to "ledger is the only source of truth", the behavior
    before this existed.
    """
    code, out, _ = _shell.run(
        ["gh", "pr", "list", "--state", "merged", "--limit", str(limit), "--json", "number,title,url"],
        workdir, timeout=60,
    )
    if code != 0 or not out.strip():
        return {}
    try:
        items = json.loads(out)
    except json.JSONDecodeError:
        return {}
    return {
        item["title"]: (item.get("number"), item.get("url"))
        for item in items if item.get("title")
    }


def create_issue(workdir: Path, *, title: str, body: str) -> tuple[int | None, str]:
    code, out, err = _shell.run(
        ["gh", "issue", "create", "--title", title, "--body", body], workdir, timeout=120,
    )
    if code != 0:
        return None, f"gh issue create failed: {err.strip()[:200]}"
    # `gh issue create` prints the issue URL on its last stdout line.
    line = out.strip().splitlines()[-1] if out.strip() else ""
    m = re.search(r"/issues/(\d+)\s*$", line)
    if not m:
        return None, f"could not parse issue number from gh output: {line[:200]!r}"
    return int(m.group(1)), "issue created"


def rename_branch_for_pr(workdir: Path, *, issue_num: int, subject: str) -> tuple[str | None, str]:
    """`git branch -m issue/<N>-<slug>` -- always the rename path (see
    module docstring on why `checkout -b` never applies here)."""
    branch = f"issue/{issue_num}-{_kebab(subject)}"
    code, out, err = _shell.run(["git", "branch", "-m", branch], workdir, timeout=30)
    if code != 0:
        return None, f"git branch -m failed: {(err or out).strip()[:200]}"
    return branch, "branch renamed"


def repair_pr(
    workdir: Path, *, pr_url: str, title: str, body: str, issue_num: int,
    max_attempts: int = MAX_PR_REPAIR_ATTEMPTS,
) -> tuple[bool, str]:
    """Push the correctly-built title/body via `gh pr edit` (never
    `gh pr create` again), re-validating after each attempt. Bounded --
    still failing after `max_attempts` is reported to the caller as
    `blocked`, not retried forever."""
    for attempt in range(1, max_attempts + 1):
        code, _, err = _shell.run(
            ["gh", "pr", "edit", pr_url, "--title", title, "--body", body], workdir, timeout=60,
        )
        if code != 0:
            return False, f"gh pr edit failed (attempt {attempt}): {err.strip()[:200]}"
        violations = validate_pr_meta(title=title, body=body, issue_num=issue_num)
        if not violations:
            return True, f"repaired after {attempt} attempt(s)"
    return False, f"pr-meta violation persisted after {max_attempts} repair attempt(s): {violations}"


def run_gh_pr(
    *,
    workdir: str | Path,
    stage_name: str,
    unit: PRUnit,
    plan_name: str | None,
    base: str = "origin/main",
    resolved: dict[str, ResolvedModel] | None = None,
    rundir: str | Path | None = None,
    port: int = 8052,
    run_role_fn=None,
) -> GhPrResult:
    """`duplicate check -> summarize actual diff -> create issue -> rename
    branch -> push + create PR -> validate/repair`.

    `plan_name`, when given, is the same resume flag `final_phase.py`
    already threads through: a known issue number from an earlier,
    interrupted run of this unit is reused (never a second throwaway issue
    for the same unit), and a newly created issue's number is recorded back
    for the next resume, mirroring `ledger.known_pr_url()`/
    `mark_pr_created()`'s existing pattern for the PR itself.

    `resolved`/`rundir`, when both given, drive one `generate_pr_summary()`
    dispatch -- reused for BOTH the issue body and the PR body, since by
    the time this function runs (after review/scan/dev cycles already
    completed), the same actual-diff summary describes what a freshly
    created issue is documenting and what the PR that closes it changed.
    Omitted (either is `None`, e.g. an older caller/test not yet passing
    them), both bodies fall back to `build_pr_body()`'s deterministic
    plan-section dump -- unchanged from this function's behavior before
    LLM-generated bodies existed.

    **Not ported: worker.md's OTHER guard** ("Pre-/gh-pr guard": before
    creating anything, check whether this unit's OWN temp branch already
    exists on the remote -- evidence some other role pushed it, an
    overstep this function alone should own). That guard protects against
    dev-ralf's independently-scheduled subagents racing each other; it has
    no analogue here -- `create_pr()` (via `final_phase.py`) is the ONLY
    code path in this project that ever runs `git push`/`gh pr create` for
    a unit, called exactly once per unit per run by this single-process
    orchestrator, so the race that guard exists to catch cannot occur in
    this architecture.
    """
    workdir = Path(workdir)
    unit_type, subject = resolve_type_subject(unit)
    base_branch = base.split("/", 1)[1] if "/" in base else base

    title = build_pr_title(unit_type, subject)
    dup_num, dup_url = find_duplicate_pr(workdir, title=title)
    if dup_num is not None:
        return GhPrResult(
            passed=False,
            reason=f"duplicate: PR #{dup_num} already open for this unit -- not creating a second one",
            pr_url=dup_url, pr_num=dup_num, duplicate=True,
        )

    summary = None
    if resolved is not None and rundir is not None:
        summary = generate_pr_summary(
            workdir=workdir, unit=unit, plan_name=plan_name or "", model=resolved["dev"],
            rundir=rundir, port=port, run_role_fn=run_role_fn,
        )
        llm_title = _title_from_summary(summary)
        if llm_title:
            title = llm_title

    known_issue = ledger.known_issue_number(workdir, plan_name, stage_name) if plan_name else None
    if known_issue is not None:
        issue_num = known_issue
    else:
        issue_title = title  # same title the PR itself will carry (LLM-generated when available)
        if summary:
            issue_body = f"{summary['changes']}\n\n{summary['why']}"
        else:
            issue_body = unit.section.strip() or issue_title
        issue_num, issue_reason = create_issue(workdir, title=issue_title, body=issue_body)
        if issue_num is None:
            return GhPrResult(passed=False, reason=issue_reason)
        if plan_name:
            ledger.mark_issue_created(workdir, plan_name, stage_name, issue_num)

    branch, branch_reason = rename_branch_for_pr(workdir, issue_num=issue_num, subject=subject)
    if branch is None:
        return GhPrResult(passed=False, reason=branch_reason, issue_num=issue_num)

    body = build_pr_body(issue_num=issue_num, plan_name=plan_name or "", unit=unit, summary=summary)

    # B-5: the full CI gate, worker.md §4's placement -- once, right before
    # a PR is created, never per fix cycle (that is `ci.fast`'s job, inside
    # `pr_cycle._run_dev_fix()`). No-op when `ci.full` is unconfigured.
    ci_full_command = config_file.resolve_ci_command(
        "full", config_file.load_project(workdir), config_file.load_global(),
    )
    ci_ok, ci_tail = ci_gate.run_full(workdir, ci_full_command)
    if not ci_ok:
        return GhPrResult(
            passed=False, reason=f"full CI failed, refusing to open a PR: {ci_tail[-500:]}",
            issue_num=issue_num, branch=branch,
        )

    known_pr_url = ledger.known_pr_url(workdir, plan_name, stage_name) if plan_name else None
    url, pr_reason = final_phase.create_pr(
        workdir, title=title, body=body, head=branch, base=base_branch, known_pr_url=known_pr_url,
    )
    if url is None:
        return GhPrResult(passed=False, reason=pr_reason, issue_num=issue_num, branch=branch)
    if plan_name:
        ledger.mark_pr_created(workdir, plan_name, stage_name, url)

    pr_num_match = re.search(r"/pull/(\d+)\s*$", url)
    pr_num = int(pr_num_match.group(1)) if pr_num_match else None

    violations = validate_pr_meta(title=title, body=body, issue_num=issue_num)
    if violations:
        repaired, repair_reason = repair_pr(workdir, pr_url=url, title=title, body=body, issue_num=issue_num)
        if not repaired:
            return GhPrResult(
                passed=False, reason=f"pr-meta violation: {violations}; {repair_reason}",
                pr_url=url, pr_num=pr_num, issue_num=issue_num, branch=branch,
            )

    return GhPrResult(
        passed=True, reason=pr_reason, pr_url=url, pr_num=pr_num, issue_num=issue_num, branch=branch,
        title=title,
    )
