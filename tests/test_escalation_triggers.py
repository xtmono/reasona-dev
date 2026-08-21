"""End-to-end coverage for worker.md's `cross_reviewer_convergence` and
`scope_exceeded` escalation triggers through `pr_cycle.run_pr_cycle()` --
item 3 of the dev-ralf source-level parity re-check
(docs/ARCHITECTURE.md §3.14.6). `observed_recurrence` (the third trigger)
was already covered by the pre-existing recurrence tests.
"""

from pathlib import Path

from reasona_dev import pr_cycle
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
MUST_FIX_TEXT = (
    "MUST_FIX:\n"
    "- [HIGH] src/a.rs:10 foo\n"
    "  || contract: c\n"
    "  || scenario: s\n"
    "  || fix: f\n"
    "\nVERDICT: FAIL\n"
)


def test_two_independent_reviewers_agreeing_escalates_on_the_first_cycle(tmp_path, rust_dev_prompts):
    """worker.md's `cross_reviewer_convergence`: this is the ONLY trigger
    that can fire on cycle 1 -- `observed_recurrence` needs a prior
    completed fix to have already happened."""
    resolved = dict(_RESOLVED)
    resolved["review_all"] = [
        ResolvedModel("review", "opus", "claude", "high", "flag"),
        ResolvedModel("review", "o1", "codex", "high", "flag"),
    ]
    resolved["review_ocr_requested"] = False

    fix_models = []

    def fn(*, workdir, role, title, prompt, model, rundir, cycle, label=None, port=None):
        if role == "backend":
            fix_models.append(model.model)
            return RoleRunResult(role=role, cycle=cycle,
                                 review_result=parse_text_contract("VERDICT: PASS\n"),
                                 raw_output_path=Path("/dev/null"))
        key = label or role
        if key in ("reviewer", "reviewer_2"):
            # BOTH reviewers independently flag the exact same finding
            return RoleRunResult(role=key, cycle=cycle,
                                 review_result=parse_text_contract(MUST_FIX_TEXT),
                                 raw_output_path=Path("/dev/null"))
        return RoleRunResult(role=key, cycle=cycle, review_result=parse_text_contract(PASS_TEXT),
                             raw_output_path=Path("/dev/null"))

    run_pr_cycle(
        workdir=tmp_path, pr_title="PR 1", resolved=resolved, rundir=tmp_path / "run",
        profile="rust-dev", run_role_fn=fn, files=["src/a.rs"],
    )
    assert fix_models
    assert fix_models[0] == "opus"  # dev_escalation model, on the VERY FIRST fix dispatch


def test_a_single_reviewer_finding_never_escalates_on_the_first_cycle(tmp_path, rust_dev_prompts):
    """Control: the SAME finding from only ONE reviewer must not trigger
    cross_reviewer_convergence -- proving the test above is measuring
    agreement, not merely "a MUST_FIX exists"."""
    fix_models = []

    def fn(*, workdir, role, title, prompt, model, rundir, cycle, label=None, port=None):
        if role == "backend":
            fix_models.append(model.model)
            return RoleRunResult(role=role, cycle=cycle,
                                 review_result=parse_text_contract("VERDICT: PASS\n"),
                                 raw_output_path=Path("/dev/null"))
        if role == "reviewer" and cycle == 1:
            return RoleRunResult(role=role, cycle=cycle,
                                 review_result=parse_text_contract(MUST_FIX_TEXT),
                                 raw_output_path=Path("/dev/null"))
        return RoleRunResult(role=role, cycle=cycle, review_result=parse_text_contract(PASS_TEXT),
                             raw_output_path=Path("/dev/null"))

    run_pr_cycle(
        workdir=tmp_path, pr_title="PR 1", resolved=_RESOLVED, rundir=tmp_path / "run",
        profile="rust-dev", run_role_fn=fn, files=["src/a.rs"],
    )
    assert fix_models
    assert fix_models[0] == "sonnet"  # the ordinary dev model, not escalated


def test_scope_exceeded_escalates_when_the_full_route_follows_a_fix(tmp_path, rust_dev_prompts, monkeypatch):
    """worker.md's `scope_exceeded`: the recheck route came back FULL
    (the previous fix's diff spilled outside the files its findings
    named) -- this cycle's fix earns the same one-time escalation."""
    monkeypatch.setattr(pr_cycle, "_safe_recheck_route", lambda *a, **k: "FULL")

    fix_models = []
    calls = {"n": 0}

    def fn(*, workdir, role, title, prompt, model, rundir, cycle, label=None, port=None):
        if role == "backend":
            fix_models.append(model.model)
            return RoleRunResult(role=role, cycle=cycle,
                                 review_result=parse_text_contract("VERDICT: PASS\n"),
                                 raw_output_path=Path("/dev/null"))
        calls["n"] += 1
        # cycle 1: a fresh finding (triggers the first, non-escalated fix).
        # cycle 2: a DIFFERENT key, with the route forced FULL above.
        #
        # NOTE: this does NOT isolate `scope_exceeded` from
        # `observed_recurrence`. An earlier version of this comment claimed
        # a different key each cycle rules the other trigger out; that is
        # false here, because `RecurrenceTracker.record_post_fix()` counts
        # EVERY must_fix present from cycle 2 on as "survived", including a
        # brand-new one (dev-ralf's own `finding_merge.escalate` instead
        # intersects the current keys with the PRIOR cycle's). So both
        # triggers fire together on cycle 2 and this asserts only that an
        # escalation happens, not which signal produced it -- see
        # docs/ARCHITECTURE.md §3.14.7.
        text = MUST_FIX_TEXT if cycle == 1 else MUST_FIX_TEXT.replace("foo", "bar")
        return RoleRunResult(role="reviewer", cycle=cycle, review_result=parse_text_contract(text),
                             raw_output_path=Path("/dev/null"))

    run_pr_cycle(
        workdir=tmp_path, pr_title="PR 1", resolved=_RESOLVED, rundir=tmp_path / "run",
        profile="rust-dev", run_role_fn=fn, files=["src/a.rs"],
    )
    assert len(fix_models) >= 2
    assert fix_models[0] == "sonnet"  # cycle 1: no prior fix yet, not scope_exceeded
    assert fix_models[1] == "opus"    # cycle 2: FULL route after a fix -- scope_exceeded escalates


def test_escalation_from_equals_escalation_to_still_fixes_on_a_convergence_trigger(tmp_path, rust_dev_prompts):
    """worker.md: when `--dev-escalation` is configured down to match
    `--dev`, the tier-collision guard skips the ESCALATED dispatch -- but
    only routes straight to FAIL for observed_recurrence. Here the trigger
    is cross_reviewer_convergence (two reviewers agree on cycle 1), whose
    non-escalated outcome is an ordinary fix, so the PR still gets ONE dev
    dispatch; the eventual FAIL comes from the SAME key surviving that fix
    on cycle 2 (already_escalated + recurring -- a genuinely separate exit),
    not from the tier comparison itself."""
    resolved = dict(_RESOLVED)
    resolved["dev_escalation"] = ResolvedModel("dev_escalation", "sonnet", "claude", "high", "flag")
    resolved["review_all"] = [
        ResolvedModel("review", "opus", "claude", "high", "flag"),
        ResolvedModel("review", "o1", "codex", "high", "flag"),
    ]
    resolved["review_ocr_requested"] = False

    fix_calls = []

    def fn(*, workdir, role, title, prompt, model, rundir, cycle, label=None, port=None):
        if role == "backend":
            fix_calls.append(1)
            return RoleRunResult(role=role, cycle=cycle,
                                 review_result=parse_text_contract("VERDICT: PASS\n"),
                                 raw_output_path=Path("/dev/null"))
        key = label or role
        if key in ("reviewer", "reviewer_2"):
            return RoleRunResult(role=key, cycle=cycle,
                                 review_result=parse_text_contract(MUST_FIX_TEXT),
                                 raw_output_path=Path("/dev/null"))
        return RoleRunResult(role=key, cycle=cycle, review_result=parse_text_contract(PASS_TEXT),
                             raw_output_path=Path("/dev/null"))

    result = run_pr_cycle(
        workdir=tmp_path, pr_title="PR 1", resolved=resolved, rundir=tmp_path / "run",
        profile="rust-dev", run_role_fn=fn, files=["src/a.rs"],
    )
    assert result.verdict == "FAIL"
    assert fix_calls == [1]  # cycle 1 DID dispatch a normal (non-escalated) fix
