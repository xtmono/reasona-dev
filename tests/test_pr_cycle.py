from pathlib import Path

from reasona_dev.finding_adapter import ReviewResult, RoleStatus, parse_text_contract
from reasona_dev.model_config import ResolvedModel
from reasona_dev.pr_cycle import RoleRunResult, run_pr_cycle

_RESOLVED = {
    "dev": ResolvedModel("dev", "sonnet", "claude", "high", "default"),
    "review": ResolvedModel("review", "opus", "claude", "high", "default"),
    "bugbot": ResolvedModel("bugbot", "deepseek-v4-pro", "kilo", "high", "default"),
    "recheck": ResolvedModel("recheck", "sonnet", "claude", "high", "default"),
    "verify": ResolvedModel("verify", "sonnet", "claude", "high", "default"),
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

    def _fn(*, server, workdir, role, title, prompt, model, rundir, cycle):
        idx = calls["n"]
        calls["n"] += 1
        result = script[idx] if idx < len(script) else parse_text_contract(PASS_TEXT)
        return RoleRunResult(role=role, cycle=cycle, review_result=result, raw_output_path=Path("/dev/null"))

    return _fn


def _noop_start_server(workdir, *, port):
    return None


def _noop_stop_server(server, *, workdir):
    pass


def test_clean_pass_no_fix_cycles(tmp_path, generic_prompts):
    script = [parse_text_contract(PASS_TEXT), None, None]  # review, bugbot, compliance
    script[1] = parse_text_contract(PASS_TEXT)
    script[2] = parse_text_contract(PASS_TEXT)
    result = run_pr_cycle(
        workdir=tmp_path, pr_title="PR 1", resolved=_RESOLVED, rundir=tmp_path / "run",
        profile="generic", run_role_fn=_stub_role_fn(script=script),
        start_server_fn=_noop_start_server, stop_server_fn=_noop_stop_server,
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
        start_server_fn=_noop_start_server, stop_server_fn=_noop_stop_server,
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
        start_server_fn=_noop_start_server, stop_server_fn=_noop_stop_server,
    )
    assert result.verdict == "FAIL"
    assert result.stage == "review"


def test_missing_review_prompt_aborts(tmp_path):
    result = run_pr_cycle(
        workdir=tmp_path, pr_title="PR 1", resolved=_RESOLVED, rundir=tmp_path / "run",
        profile="nonexistent-profile", run_role_fn=_stub_role_fn(script=[]),
        start_server_fn=_noop_start_server, stop_server_fn=_noop_stop_server,
    )
    assert result.verdict == "ABORT"
    assert result.stage == "review"


def test_role_error_status_aborts_immediately(tmp_path, generic_prompts):
    script = [ReviewResult(role_status=RoleStatus.ERROR)]
    result = run_pr_cycle(
        workdir=tmp_path, pr_title="PR 1", resolved=_RESOLVED, rundir=tmp_path / "run",
        profile="generic", run_role_fn=_stub_role_fn(script=script),
        start_server_fn=_noop_start_server, stop_server_fn=_noop_stop_server,
    )
    assert result.verdict == "FAIL"  # cycle_gate.evaluate(): ERROR -> abort action -> CycleResult FAIL
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
        start_server_fn=_noop_start_server, stop_server_fn=_noop_stop_server,
    )
    assert result.verdict == "PASS"
