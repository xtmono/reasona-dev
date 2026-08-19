from pathlib import Path

from reasona_dev import cycles_log, memory, pr_cycle
from reasona_dev.finding_adapter import (
    Disposition,
    Finding,
    ReviewResult,
    RoleStatus,
    Severity,
    parse_text_contract,
)
from reasona_dev.model_config import ResolvedModel
from reasona_dev.pr_cycle import RoleRunResult, run_pr_cycle

_RESOLVED = {
    "dev": ResolvedModel("dev", "sonnet", "claude", "high", "default"),
    "review": ResolvedModel("review", "opus", "claude", "high", "default"),
    "recheck": ResolvedModel("recheck", "sonnet", "claude", "medium", "default"),
    "bugbot": ResolvedModel("bugbot", "deepseek-v4-pro", "kilo", "high", "default"),
    "compliance": ResolvedModel("compliance", "sonnet", "claude", "high", "default"),
    "dev_escalation": ResolvedModel("dev_escalation", "opus", "claude", "high", "default"),
}

PASS_TEXT = "VERDICT: PASS\n"


# --- memory wiring ----------------------------------------------------------

def _recording_role_fn():
    calls = []

    def _fn(*, workdir, role, title, prompt, model, rundir, cycle):
        calls.append({"role": role, "prompt": prompt})
        return RoleRunResult(
            role=role, cycle=cycle, review_result=parse_text_contract(PASS_TEXT),
            raw_output_path=Path("/dev/null"),
        )

    _fn.calls = calls
    return _fn




def _seed_recurrence(workdir, path="crates/flow/src/a.rs"):
    for unit in ("pr-1", "pr-2"):
        cycles_log.record_dispatch(
            workdir=workdir, stage_name=unit, stage="review", cycle=1,
            role="reviewer", model="opus", adapter="claude",
            result=ReviewResult(
                role_status=RoleStatus.COMPLETE,
                findings=[Finding(
                    disposition=Disposition.MUST_FIX, severity=Severity.HIGH, path=path,
                    line=1, symbol="foo", contract="missing negative test", scenario="s", fix="x",
                )],
            ),
        )
    memory.regenerate(workdir)


def test_matching_priors_are_injected_into_review_and_scan_prompts(tmp_path, generic_prompts):
    _seed_recurrence(tmp_path)
    fn = _recording_role_fn()

    run_pr_cycle(
        workdir=tmp_path, pr_title="PR 3", resolved=_RESOLVED, rundir=tmp_path / "run",
        profile="generic", files=["crates/flow/src/b.rs"], run_role_fn=fn,
    )

    by_role = {c["role"]: c["prompt"] for c in fn.calls}
    assert "PRIOR OBSERVATIONS" in by_role["reviewer"]
    assert "PRIOR OBSERVATIONS" in by_role["bugbot"]
    assert "PRIOR OBSERVATIONS" in by_role["compliance"]


def test_unrelated_files_get_an_unchanged_prompt(tmp_path, generic_prompts):
    _seed_recurrence(tmp_path)
    fn = _recording_role_fn()

    run_pr_cycle(
        workdir=tmp_path, pr_title="PR 3", resolved=_RESOLVED, rundir=tmp_path / "run",
        profile="generic", files=["crates/unrelated/src/z.rs"], run_role_fn=fn,
    )

    assert all("PRIOR OBSERVATIONS" not in c["prompt"] for c in fn.calls)


def test_unit_declaring_no_files_gets_an_unchanged_prompt(tmp_path, generic_prompts):
    """No declared files means no retrieval key, and an unscoped memory
    would become a preamble on every prompt."""
    _seed_recurrence(tmp_path)
    fn = _recording_role_fn()

    run_pr_cycle(
        workdir=tmp_path, pr_title="PR 3", resolved=_RESOLVED, rundir=tmp_path / "run",
        profile="generic", run_role_fn=fn,
    )

    assert all("PRIOR OBSERVATIONS" not in c["prompt"] for c in fn.calls)


def test_fresh_repo_gets_an_unchanged_prompt(tmp_path, generic_prompts):
    fn = _recording_role_fn()
    run_pr_cycle(
        workdir=tmp_path, pr_title="PR 1", resolved=_RESOLVED, rundir=tmp_path / "run",
        profile="generic", files=["crates/flow/src/a.rs"], run_role_fn=fn,
    )
    assert all("PRIOR OBSERVATIONS" not in c["prompt"] for c in fn.calls)


def test_memory_is_regenerated_after_the_cycle(tmp_path, generic_prompts):
    """This cycle's findings must be available as the NEXT unit's priors."""
    cycles_log.record_dispatch(
        workdir=tmp_path, stage_name="pr-1", stage="review", cycle=1,
        role="reviewer", model="opus", adapter="claude",
        result=ReviewResult(
            role_status=RoleStatus.COMPLETE,
            findings=[Finding(
                disposition=Disposition.MUST_FIX, severity=Severity.HIGH,
                path="crates/flow/src/a.rs", line=1, symbol="foo",
                contract="missing negative test", scenario="s", fix="x",
            )],
        ),
    )
    assert memory.derive(tmp_path) == []  # one unit only, below threshold

    def _fn(*, workdir, role, title, prompt, model, rundir, cycle):
        result = (
            ReviewResult(
                role_status=RoleStatus.COMPLETE,
                findings=[Finding(
                    disposition=Disposition.MUST_FIX, severity=Severity.HIGH,
                    path="crates/flow/src/a.rs", line=9, symbol="foo",
                    contract="missing negative test", scenario="s", fix="x",
                )],
            )
            if role == "reviewer" and cycle == 1
            else parse_text_contract(PASS_TEXT)
        )
        return RoleRunResult(role=role, cycle=cycle, review_result=result, raw_output_path=Path("/dev/null"))

    run_pr_cycle(
        workdir=tmp_path, pr_title="PR 2", resolved=_RESOLVED, rundir=tmp_path / "run",
        profile="generic", stage_name="pr-2", files=["crates/flow/src/a.rs"], run_role_fn=_fn,
    )

    # now observed in pr-1 and pr-2 -> a memory file exists on disk
    written = list(memory.memory_dir(tmp_path).glob("recurring-*.md"))
    assert written
    assert memory.select(tmp_path, ["crates/flow/src/a.rs"])


def test_memory_regeneration_failure_never_fails_the_cycle(tmp_path, generic_prompts, monkeypatch):
    monkeypatch.setattr(memory, "regenerate", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    fn = _recording_role_fn()
    result = run_pr_cycle(
        workdir=tmp_path, pr_title="PR 1", resolved=_RESOLVED, rundir=tmp_path / "run",
        profile="generic", run_role_fn=fn,
    )
    assert result.verdict == "PASS"
