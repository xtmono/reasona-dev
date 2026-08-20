from pathlib import Path

from reasona_dev.finding_adapter import ReviewResult, RoleStatus, parse_text_contract
from reasona_dev.model_config import ResolvedModel
from reasona_dev.pr_cycle import RoleRunResult, _is_docs_only, run_pr_cycle

_RESOLVED = {
    "dev": ResolvedModel("dev", "sonnet", "claude", "high", "default"),
    "review": ResolvedModel("review", "opus", "claude", "high", "default"),
    "bugbot": ResolvedModel("bugbot", "deepseek-v4-pro", "kilo", "high", "default"),
    "recheck": ResolvedModel("recheck", "sonnet", "claude", "high", "default"),
    "compliance": ResolvedModel("compliance", "sonnet", "claude", "high", "default"),
    "dev_escalation": ResolvedModel("dev_escalation", "opus", "claude", "high", "default"),
}

PASS_TEXT = "VERDICT: PASS\n"
MUST_FIX_TEXT = (
    "MUST_FIX:\n"
    "- [HIGH] src/a.rs:10 foo\n"
    "  || contract: c\n"
    "  || scenario: s\n"
    "  || fix: f\n"
    "\n"
    "VERDICT: FAIL\n"
)


def _stub_role_fn(*, script):
    """Returns a run_role_fn stand-in that pops pre-scripted ReviewResults
    per role in call order -- keeps tests independent of any real Bernstein
    server (HTTP or otherwise).
    """
    calls = {"n": 0}

    def _fn(*, workdir, role, title, prompt, model, rundir, cycle, label=None, port=None):
        idx = calls["n"]
        calls["n"] += 1
        result = script[idx] if idx < len(script) else parse_text_contract(PASS_TEXT)
        return RoleRunResult(role=role, cycle=cycle, review_result=result, raw_output_path=Path("/dev/null"))

    return _fn




def test_clean_pass_no_fix_cycles(tmp_path, generic_prompts):
    script = [parse_text_contract(PASS_TEXT), None, None]  # review, bugbot, compliance
    script[1] = parse_text_contract(PASS_TEXT)
    script[2] = parse_text_contract(PASS_TEXT)
    result = run_pr_cycle(
        workdir=tmp_path, pr_title="PR 1", resolved=_RESOLVED, rundir=tmp_path / "run",
        profile="generic", run_role_fn=_stub_role_fn(script=script),
    )
    assert result.verdict == "PASS"
    assert result.review_cycles == 1
    assert result.scan_cycles == 1


def test_review_fix_required_then_passes(tmp_path, generic_prompts):
    script = [
        parse_text_contract(MUST_FIX_TEXT),  # review c1: FIX_REQUIRED
        ReviewResult(role_status=RoleStatus.COMPLETE),  # dev fix (ignored, empty parse)
        parse_text_contract(PASS_TEXT),  # review c2: PASS
        parse_text_contract(PASS_TEXT),  # bugbot
        parse_text_contract(PASS_TEXT),  # compliance
    ]
    result = run_pr_cycle(
        workdir=tmp_path, pr_title="PR 1", resolved=_RESOLVED, rundir=tmp_path / "run",
        profile="generic", run_role_fn=_stub_role_fn(script=script),
    )
    assert result.verdict == "PASS"
    assert result.review_cycles == 2
    # role_results carries the fix dispatch too
    assert any(r.role == "backend" for r in result.role_results)


def test_review_budget_exhausted_fails(tmp_path, generic_prompts):
    # Same finding key every cycle -> RecurrenceTracker escalates once then
    # FAILs; well within MAX_REVIEW_CYCLES so budget itself isn't the
    # limiter here -- this exercises the ESCALATE_ONCE -> FAIL path.
    script = [parse_text_contract(MUST_FIX_TEXT) for _ in range(10)]
    result = run_pr_cycle(
        workdir=tmp_path, pr_title="PR 1", resolved=_RESOLVED, rundir=tmp_path / "run",
        profile="generic", run_role_fn=_stub_role_fn(script=script),
    )
    assert result.verdict == "FAIL"
    assert result.stage == "review"


def test_missing_review_prompt_aborts(tmp_path):
    result = run_pr_cycle(
        workdir=tmp_path, pr_title="PR 1", resolved=_RESOLVED, rundir=tmp_path / "run",
        profile="nonexistent-profile", run_role_fn=_stub_role_fn(script=[]),
    )
    assert result.verdict == "ABORT"
    assert result.stage == "review"


def test_role_error_status_aborts_immediately(tmp_path, generic_prompts):
    script = [ReviewResult(role_status=RoleStatus.ERROR)]
    result = run_pr_cycle(
        workdir=tmp_path, pr_title="PR 1", resolved=_RESOLVED, rundir=tmp_path / "run",
        profile="generic", run_role_fn=_stub_role_fn(script=script),
    )
    # cycle_gate.evaluate(): ERROR (role/model unavailable) -> abort action ->
    # CycleResult ABORT, distinct from FAIL -- orchestrate.py reports this as
    # a `blocked` unit outcome, not `failed` (§3.7.11.1): the role never
    # actually ran, so nothing about the code was judged deficient.
    assert result.verdict == "ABORT"
    assert result.stage == "review"


def test_scan_stage_runs_bugbot_and_compliance_in_kv_shape(tmp_path, generic_prompts):
    kv_pass = (
        "=== ext-bugbot RESULT ===\nVERDICT: PASS\nBLOCKING_JSON=[]\nNON_BLOCKING_JSON=[]\n=== END ===\n"
    )
    from reasona_dev.finding_adapter import parse_kv_contract

    script = [
        parse_text_contract(PASS_TEXT),  # review
        parse_kv_contract(kv_pass),  # bugbot
        parse_kv_contract(kv_pass),  # compliance
    ]
    result = run_pr_cycle(
        workdir=tmp_path, pr_title="PR 1", resolved=_RESOLVED, rundir=tmp_path / "run",
        profile="generic", run_role_fn=_stub_role_fn(script=script),
    )
    assert result.verdict == "PASS"


def test_is_docs_only_true_when_every_file_matches():
    assert _is_docs_only(["docs/a.md", "config.yaml", "pyproject.toml", "data.json"])


def test_is_docs_only_false_with_any_source_file():
    assert not _is_docs_only(["docs/a.md", "src/a.rs"])


def test_is_docs_only_false_with_no_declared_files():
    assert not _is_docs_only(None)
    assert not _is_docs_only([])


def test_is_docs_only_extension_match_is_case_insensitive():
    assert _is_docs_only(["README.MD"])


def test_docs_only_unit_skips_bugbot_but_still_runs_compliance(tmp_path, generic_prompts):
    """worker.md's Bug + compliance scan: tas-bugbot only runs when the PR
    changes code -- no source path in the declared files (docs/config-only:
    .md/.toml/.yaml/.json) skips it. compliance always runs regardless."""
    calls = []

    def fn(*, workdir, role, title, prompt, model, rundir, cycle, label=None, port=None):
        calls.append(role)
        return RoleRunResult(role=role, cycle=cycle, review_result=parse_text_contract(PASS_TEXT),
                             raw_output_path=Path("/dev/null"))

    result = run_pr_cycle(
        workdir=tmp_path, pr_title="PR 1", resolved=_RESOLVED, rundir=tmp_path / "run",
        profile="generic", run_role_fn=fn, files=["docs/readme.md", "config.yaml"],
    )
    assert result.verdict == "PASS"
    assert "bugbot" not in calls
    assert "compliance" in calls


def test_a_unit_with_any_source_file_still_dispatches_bugbot(tmp_path, generic_prompts):
    calls = []

    def fn(*, workdir, role, title, prompt, model, rundir, cycle, label=None, port=None):
        calls.append(role)
        return RoleRunResult(role=role, cycle=cycle, review_result=parse_text_contract(PASS_TEXT),
                             raw_output_path=Path("/dev/null"))

    run_pr_cycle(
        workdir=tmp_path, pr_title="PR 1", resolved=_RESOLVED, rundir=tmp_path / "run",
        profile="generic", run_role_fn=fn, files=["docs/readme.md", "src/a.rs"],
    )
    assert "bugbot" in calls


def test_no_declared_files_is_not_treated_as_docs_only(tmp_path, generic_prompts):
    """A unit with no `files:` metadata at all is not KNOWN to be
    docs-only -- bugbot still runs, the safe default."""
    calls = []

    def fn(*, workdir, role, title, prompt, model, rundir, cycle, label=None, port=None):
        calls.append(role)
        return RoleRunResult(role=role, cycle=cycle, review_result=parse_text_contract(PASS_TEXT),
                             raw_output_path=Path("/dev/null"))

    run_pr_cycle(
        workdir=tmp_path, pr_title="PR 1", resolved=_RESOLVED, rundir=tmp_path / "run",
        profile="generic", run_role_fn=fn,
    )
    assert "bugbot" in calls


def test_port_reaches_every_run_role_fn_dispatch(tmp_path, generic_prompts):
    """`run_pr_cycle(port=...)` used to accept the argument and never use
    it -- every dispatch (review, scan, dev fix) silently fell back to
    `run_role`'s own default (8052) regardless of what was passed here.
    With concurrent unit dispatch (`orchestrate.run_plan(job=...)`), two
    units running at once need genuinely different ports, so every
    dispatch inside one unit's cycle must actually carry the port it was
    given.
    """
    seen_ports = []
    calls = {"n": 0}

    def fn(*, workdir, role, title, prompt, model, rundir, cycle, label=None, port=None):
        seen_ports.append(port)
        idx = calls["n"]
        calls["n"] += 1
        script = [
            parse_text_contract(MUST_FIX_TEXT),  # review c1 -- forces a dev fix + review c2
            ReviewResult(role_status=RoleStatus.COMPLETE),  # dev fix
            parse_text_contract(PASS_TEXT),  # review c2
            parse_text_contract(PASS_TEXT),  # bugbot
            parse_text_contract(PASS_TEXT),  # compliance
        ]
        result = script[idx] if idx < len(script) else parse_text_contract(PASS_TEXT)
        return RoleRunResult(role=label or role, cycle=cycle, review_result=result, raw_output_path=Path("/dev/null"))

    result = run_pr_cycle(
        workdir=tmp_path, pr_title="PR 1", resolved=_RESOLVED, rundir=tmp_path / "run",
        profile="generic", run_role_fn=fn, port=19999,
    )
    assert result.verdict == "PASS"
    assert seen_ports  # at least one dispatch happened
    assert all(p == 19999 for p in seen_ports)
