"""worker.md -> *Incomplete evidence*: a MUST_FIX finding reported without a
complete contract/scenario/fix earns ONE re-query before it ever reaches a
dev-fix or recheck prompt -- never a silent downgrade to ADVISORY. Item 2 of
the dev-ralf source-level parity re-check (docs/ARCHITECTURE.md §3.14.6).
"""

from pathlib import Path

from reasona_dev.finding_adapter import parse_text_contract
from reasona_dev.model_config import ResolvedModel
from reasona_dev.pr_cycle import RoleRunResult, run_pr_cycle

_RESOLVED = {
    "dev": ResolvedModel("dev", "sonnet", "claude", "high", "default"),
    "review": ResolvedModel("review", "opus", "claude", "high", "default"),
    "bugbot": ResolvedModel("bugbot", "deepseek-v4-pro", "kilo", "high", "default"),
    "recheck": ResolvedModel("recheck", "sonnet", "claude", "high", "default"),
    "compliance": ResolvedModel("compliance", "sonnet", "claude", "high", "default"),
    "dev_escalation": ResolvedModel("dev_escalation", "opus", "claude", "high", "default"),
}

PASS_TEXT = "VERDICT: PASS\n"
# A MUST_FIX with NO || evidence fields at all -- parse_text_contract()
# flags this contract_incomplete=True (finding_adapter.py's own v1-fallback
# and is_evidence_complete() rules).
INCOMPLETE_MUST_FIX_TEXT = (
    "MUST_FIX:\n"
    "- [HIGH] src/a.rs:10 foo\n"
    "\n"
    "VERDICT: FAIL\n"
)
COMPLETE_MUST_FIX_TEXT = (
    "MUST_FIX:\n"
    "- [HIGH] src/a.rs:10 foo\n"
    "  || contract: real contract\n"
    "  || scenario: real scenario\n"
    "  || fix: real fix\n"
    "\n"
    "VERDICT: FAIL\n"
)
CORRECTION_REPLY_TEXT = (
    "MUST_FIX:\n"
    "- [HIGH] src/a.rs:10 foo\n"
    "  || contract: filled-in contract\n"
    "  || scenario: filled-in scenario\n"
    "  || fix: filled-in fix\n"
)
CORRECTION_CANNOT_SUPPLY_TEXT = "no evidence available\n"  # no MUST_FIX bullet at all


def _fn(script_by_key):
    calls = []

    def fn(*, workdir, role, title, prompt, model, rundir, cycle, label=None, port=None):
        key = label or role
        calls.append(key)
        result = script_by_key.get(key, parse_text_contract(PASS_TEXT))
        return RoleRunResult(role=key, cycle=cycle, review_result=result, raw_output_path=Path("/dev/null"))

    return fn, calls


def test_an_incomplete_finding_triggers_one_correction_dispatch(tmp_path, generic_prompts):
    fn, calls = _fn({
        "reviewer": parse_text_contract(INCOMPLETE_MUST_FIX_TEXT),
        "reviewer-evidence-correction-1": parse_text_contract(CORRECTION_REPLY_TEXT),
        "bugbot": parse_text_contract(PASS_TEXT),
        "compliance": parse_text_contract(PASS_TEXT),
        "backend": parse_text_contract(PASS_TEXT),
    })
    run_pr_cycle(
        workdir=tmp_path, pr_title="PR 1", resolved=_RESOLVED, rundir=tmp_path / "run",
        profile="generic", run_role_fn=fn, files=["src/a.rs"],
    )
    assert "reviewer-evidence-correction-1" in calls


def test_a_complete_finding_never_triggers_a_correction_dispatch(tmp_path, generic_prompts):
    fn, calls = _fn({
        "reviewer": parse_text_contract(COMPLETE_MUST_FIX_TEXT),
        "bugbot": parse_text_contract(PASS_TEXT),
        "compliance": parse_text_contract(PASS_TEXT),
        "backend": parse_text_contract(PASS_TEXT),
    })
    run_pr_cycle(
        workdir=tmp_path, pr_title="PR 1", resolved=_RESOLVED, rundir=tmp_path / "run",
        profile="generic", run_role_fn=fn, files=["src/a.rs"],
    )
    assert not any(c.startswith("reviewer-evidence-correction") for c in calls)


def test_a_successful_correction_fills_in_the_evidence_fields_sent_to_dev(tmp_path, generic_prompts):
    """The corrected contract/scenario/fix must be what actually reaches
    the dev-fix prompt -- proving the correction round's output is not
    merely dispatched and discarded."""
    fn, calls = _fn({
        "reviewer": parse_text_contract(INCOMPLETE_MUST_FIX_TEXT),
        "reviewer-evidence-correction-1": parse_text_contract(CORRECTION_REPLY_TEXT),
        "bugbot": parse_text_contract(PASS_TEXT),
        "compliance": parse_text_contract(PASS_TEXT),
        "backend": parse_text_contract(PASS_TEXT),
    })
    fix_prompts = []

    def recording_fn(*, workdir, role, title, prompt, model, rundir, cycle, label=None, port=None):
        if role == "backend":
            fix_prompts.append(prompt)
        return fn(workdir=workdir, role=role, title=title, prompt=prompt, model=model,
                   rundir=rundir, cycle=cycle, label=label, port=port)

    run_pr_cycle(
        workdir=tmp_path, pr_title="PR 1", resolved=_RESOLVED, rundir=tmp_path / "run",
        profile="generic", run_role_fn=recording_fn, files=["src/a.rs"],
    )
    assert fix_prompts
    assert "filled-in contract" in fix_prompts[0]
    assert "filled-in scenario" in fix_prompts[0]
    assert "filled-in fix" in fix_prompts[0]


def test_the_finding_stays_must_fix_even_when_the_correction_cannot_supply_evidence(tmp_path, generic_prompts):
    """worker.md: 'regardless of the response, the ORIGINAL finding stays
    in must_fix' -- a correction reply that supplies no evidence must
    never downgrade the finding to ADVISORY or drop it."""
    fn, calls = _fn({
        "reviewer": parse_text_contract(INCOMPLETE_MUST_FIX_TEXT),
        "reviewer-evidence-correction-1": parse_text_contract(CORRECTION_CANNOT_SUPPLY_TEXT),
        "bugbot": parse_text_contract(PASS_TEXT),
        "compliance": parse_text_contract(PASS_TEXT),
        "backend": parse_text_contract(PASS_TEXT),
    })
    fix_calls = []

    def recording_fn(*, workdir, role, title, prompt, model, rundir, cycle, label=None, port=None):
        if role == "backend":
            fix_calls.append(prompt)
        return fn(workdir=workdir, role=role, title=title, prompt=prompt, model=model,
                   rundir=rundir, cycle=cycle, label=label, port=port)

    run_pr_cycle(
        workdir=tmp_path, pr_title="PR 1", resolved=_RESOLVED, rundir=tmp_path / "run",
        profile="generic", run_role_fn=recording_fn, files=["src/a.rs"],
    )
    # A dev-fix dispatch happened -- the finding was NOT silently dropped.
    assert fix_calls
    assert "src/a.rs" in fix_calls[0]


def test_the_scan_stage_also_corrects_incomplete_bugbot_findings(tmp_path, generic_prompts):
    """Unlike dev-ralf's external tas-bugbot (a KV shape with no evidence
    fields at all), this project's packaged `generic` profile asks bugbot
    for the same evidence contract as review -- so the correction round
    applies here too (see the code comment at the scan-cycle call site)."""
    fn, calls = _fn({
        "reviewer": parse_text_contract(PASS_TEXT),
        "bugbot": parse_text_contract(INCOMPLETE_MUST_FIX_TEXT),
        "bugbot-evidence-correction-1": parse_text_contract(CORRECTION_REPLY_TEXT),
        "compliance": parse_text_contract(PASS_TEXT),
        "backend": parse_text_contract(PASS_TEXT),
    })
    run_pr_cycle(
        workdir=tmp_path, pr_title="PR 1", resolved=_RESOLVED, rundir=tmp_path / "run",
        profile="generic", run_role_fn=fn, files=["src/a.rs"],
    )
    assert "bugbot-evidence-correction-1" in calls


def test_the_bounded_recheck_route_also_corrects_incomplete_findings(tmp_path, generic_prompts, monkeypatch):
    from reasona_dev import pr_cycle
    monkeypatch.setattr(pr_cycle, "_safe_recheck_route", lambda *a, **k: "BOUNDED")

    from reasona_dev.finding_adapter import ReviewResult, RoleStatus
    script = [
        parse_text_contract(
            "MUST_FIX:\n- [HIGH] src/a.rs:10 foo\n  || contract: c\n  || scenario: s\n  || fix: f\n\nVERDICT: FAIL\n"
        ),  # review c1
        ReviewResult(role_status=RoleStatus.COMPLETE),  # dev fix
        parse_text_contract(INCOMPLETE_MUST_FIX_TEXT),  # recheck c2 -- new, incomplete finding
        parse_text_contract(CORRECTION_REPLY_TEXT),  # correction for the recheck's finding
        ReviewResult(role_status=RoleStatus.COMPLETE),  # dev fix c2
        parse_text_contract(PASS_TEXT),  # review c3
        parse_text_contract(PASS_TEXT),  # bugbot
        parse_text_contract(PASS_TEXT),  # compliance
    ]
    calls = []

    def fn(*, workdir, role, title, prompt, model, rundir, cycle, label=None, port=None):
        calls.append((cycle, role, label))
        idx = len(calls) - 1
        result = script[idx] if idx < len(script) else parse_text_contract(PASS_TEXT)
        return RoleRunResult(role=label or role, cycle=cycle, review_result=result, raw_output_path=Path("/dev/null"))

    run_pr_cycle(
        workdir=tmp_path, pr_title="PR 1", resolved=_RESOLVED, rundir=tmp_path / "run",
        profile="generic", run_role_fn=fn, files=["src/a.rs"],
    )
    assert any(label == "reviewer-evidence-correction-1" for _, _, label in calls)
