"""Multi-reviewer + OCR co-reviewer dispatch in `pr_cycle.run_pr_cycle`'s
FULL-route review cycle -- items 2 and 4 of the dev-ralf parity gap
(docs/ARCHITECTURE.md). Both share the same mechanism: dispatch every
requested reviewer (plus OCR when requested) sequentially, merge their
`ReviewResult`s via `finding_adapter.merge()`, and evaluate the merged
result exactly as a single reviewer's result was evaluated before.
"""

from pathlib import Path

from reasona_dev import pr_cycle
from reasona_dev.finding_adapter import parse_text_contract
from reasona_dev.model_config import ResolvedModel
from reasona_dev.pr_cycle import RoleRunResult, run_pr_cycle

_RESOLVED_BASE = {
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


def _recording_role_fn(script_by_role_key):
    """Returns findings keyed by `label or role` -- lets a test script a
    different verdict per dispatched reviewer within the SAME cycle
    (unlike `_stub_role_fn` in test_pr_cycle.py, which scripts by call
    order only). Records every (role, label, model.adapter) triple seen.
    """
    calls = []

    def _fn(*, workdir, role, title, prompt, model, rundir, cycle, label=None, port=None, files=None):
        key = label or role
        calls.append((role, label, model.adapter))
        result = script_by_role_key.get(key, parse_text_contract(PASS_TEXT))
        return RoleRunResult(role=key, cycle=cycle, review_result=result, raw_output_path=Path("/dev/null"))

    return _fn, calls


def test_single_reviewer_default_dispatches_once(tmp_path, rust_dev_prompts):
    """No `review_all` key at all (old-shape `resolved` dict, as every
    pre-existing caller still passes) -- exactly one reviewer dispatch,
    unchanged from before this feature existed.
    """
    fn, calls = _recording_role_fn({
        "reviewer": parse_text_contract(PASS_TEXT),
        "bugbot": parse_text_contract(PASS_TEXT),
        "compliance": parse_text_contract(PASS_TEXT),
    })
    result = run_pr_cycle(
        workdir=tmp_path, pr_title="PR 1", resolved=_RESOLVED_BASE, rundir=tmp_path / "run",
        profile="rust-dev", run_role_fn=fn,
    )
    assert result.verdict == "PASS"
    review_calls = [c for c in calls if c[0] == "reviewer"]
    assert len(review_calls) == 1


def test_multiple_reviewers_all_pass_merges_to_pass(tmp_path, rust_dev_prompts):
    resolved = dict(_RESOLVED_BASE)
    resolved["review_all"] = [
        ResolvedModel("review", "opus", "claude", "high", "flag"),
        ResolvedModel("review", "o1", "codex", "high", "flag"),
    ]
    resolved["review_ocr_requested"] = False
    fn, calls = _recording_role_fn({
        "reviewer": parse_text_contract(PASS_TEXT),
        "reviewer_2": parse_text_contract(PASS_TEXT),
        "bugbot": parse_text_contract(PASS_TEXT),
        "compliance": parse_text_contract(PASS_TEXT),
    })
    result = run_pr_cycle(
        workdir=tmp_path, pr_title="PR 1", resolved=resolved, rundir=tmp_path / "run",
        profile="rust-dev", run_role_fn=fn,
    )
    assert result.verdict == "PASS"
    review_calls = [c for c in calls if c[1] in ("reviewer", "reviewer_2")]
    assert [c[1] for c in review_calls] == ["reviewer", "reviewer_2"]
    assert [c[2] for c in review_calls] == ["claude", "codex"]


def test_any_reviewer_must_fix_blocks_the_merged_verdict(tmp_path, rust_dev_prompts):
    """`finding_adapter.merge()`'s own contract: ANY reviewer's MUST_FIX
    survives into the merged result, even when the other reviewer(s) came
    back clean -- this test only confirms the multi-reviewer dispatch
    actually feeds `merge()` all of them, not `merge()`'s own logic (that
    is covered by tests/test_finding_adapter.py already).
    """
    resolved = dict(_RESOLVED_BASE)
    resolved["review_all"] = [
        ResolvedModel("review", "opus", "claude", "high", "flag"),
        ResolvedModel("review", "o1", "codex", "high", "flag"),
    ]
    resolved["review_ocr_requested"] = False
    fn, calls = _recording_role_fn({
        "reviewer": parse_text_contract(PASS_TEXT),
        "reviewer_2": parse_text_contract(MUST_FIX_TEXT),
        "backend": parse_text_contract(PASS_TEXT),
    })
    result = run_pr_cycle(
        workdir=tmp_path, pr_title="PR 1", resolved=resolved, rundir=tmp_path / "run",
        profile="rust-dev", run_role_fn=fn, files=["src/a.rs"],
    )
    # A fix cycle was dispatched (the merged review carried a MUST_FIX),
    # never a bare unconditional PASS despite reviewer #1 alone passing.
    fix_calls = [c for c in calls if c[0] == "backend"]
    assert len(fix_calls) >= 1


def test_ocr_marker_dispatches_the_ocr_reviewer_once(tmp_path, rust_dev_prompts):
    resolved = dict(_RESOLVED_BASE)
    resolved["review_all"] = [ResolvedModel("review", "sonnet", "claude", "high", "flag", ocr=True)]
    resolved["review_ocr_requested"] = True
    fn, calls = _recording_role_fn({
        "reviewer": parse_text_contract(PASS_TEXT),
        "ocr_reviewer": parse_text_contract(PASS_TEXT),
        "bugbot": parse_text_contract(PASS_TEXT),
        "compliance": parse_text_contract(PASS_TEXT),
    })
    result = run_pr_cycle(
        workdir=tmp_path, pr_title="PR 1", resolved=resolved, rundir=tmp_path / "run",
        profile="rust-dev", run_role_fn=fn,
    )
    assert result.verdict == "PASS"
    ocr_calls = [c for c in calls if c[0] == "ocr_reviewer"]
    assert len(ocr_calls) == 1
    assert ocr_calls[0][2] == "ocr"


def test_no_ocr_marker_does_not_dispatch_ocr(tmp_path, rust_dev_prompts):
    resolved = dict(_RESOLVED_BASE)
    resolved["review_all"] = [ResolvedModel("review", "sonnet", "claude", "high", "flag", ocr=False)]
    resolved["review_ocr_requested"] = False
    fn, calls = _recording_role_fn({
        "reviewer": parse_text_contract(PASS_TEXT),
        "bugbot": parse_text_contract(PASS_TEXT),
        "compliance": parse_text_contract(PASS_TEXT),
    })
    run_pr_cycle(
        workdir=tmp_path, pr_title="PR 1", resolved=resolved, rundir=tmp_path / "run",
        profile="rust-dev", run_role_fn=fn,
    )
    assert not any(c[0] == "ocr_reviewer" for c in calls)


def test_bounded_route_never_fans_out_to_multiple_reviewers_or_ocr(tmp_path, rust_dev_prompts, monkeypatch):
    """The cheap bounded re-check exists to re-confirm a small,
    already-identified finding set, not to re-open full independent
    review -- so it stays single-reviewer even when `review_all` carries
    several and `,ocr` was requested.
    """
    monkeypatch.setattr(pr_cycle, "_safe_recheck_route", lambda *a, **k: "BOUNDED")
    resolved = dict(_RESOLVED_BASE)
    resolved["review_all"] = [
        ResolvedModel("review", "opus", "claude", "high", "flag"),
        ResolvedModel("review", "o1", "codex", "high", "flag"),
    ]
    resolved["review_ocr_requested"] = True
    script = [
        parse_text_contract(MUST_FIX_TEXT),  # review c1 (reviewer #1 only -- fan-out still happens on c1)
        parse_text_contract(MUST_FIX_TEXT),  # review c1 (reviewer #2)
        pr_cycle.ReviewResult(role_status=pr_cycle.RoleStatus.COMPLETE),  # dev fix
        parse_text_contract(PASS_TEXT),      # recheck c2 -- must be the ONLY c2 review-role call
        parse_text_contract(PASS_TEXT),      # bugbot
        parse_text_contract(PASS_TEXT),      # compliance
    ]
    calls = []

    def fn(*, workdir, role, title, prompt, model, rundir, cycle, label=None, port=None, files=None):
        calls.append((cycle, role, label))
        idx = len(calls) - 1
        result = script[idx] if idx < len(script) else parse_text_contract(PASS_TEXT)
        return RoleRunResult(role=label or role, cycle=cycle, review_result=result, raw_output_path=Path("/dev/null"))

    result = run_pr_cycle(
        workdir=tmp_path, pr_title="PR 1", resolved=resolved, rundir=tmp_path / "run",
        profile="rust-dev", run_role_fn=fn, files=["src/a.rs"],
    )
    assert result.verdict == "PASS"
    cycle2_reviewer_calls = [c for c in calls if c[0] == 2 and c[1] == "reviewer"]
    assert len(cycle2_reviewer_calls) == 1
    assert not any(c[1] == "ocr_reviewer" for c in calls if c[0] == 2)
