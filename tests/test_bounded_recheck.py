import re
from pathlib import Path

from reasona_dev import pr_cycle
from reasona_dev.finding_adapter import ReviewResult, RoleStatus, parse_text_contract
from reasona_dev.model_config import ResolvedModel
from reasona_dev.pr_cycle import RoleRunResult, run_pr_cycle

_RESOLVED = {
    "dev": ResolvedModel("dev", "sonnet", "claude", "high", "default"),
    "review": ResolvedModel("review", "opus", "claude", "high", "default"),
    "recheck": ResolvedModel("recheck", "sonnet", "claude", "medium", "default"),
    "bugbot": ResolvedModel("bugbot", "deepseek-v4-pro", "kilo", "high", "default"),
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
    "\nVERDICT: FAIL\n"
)


def _recording_role_fn(script):
    calls = []

    def _fn(*, workdir, role, title, prompt, model, rundir, cycle):
        calls.append({
            "role": role, "title": title, "prompt": prompt, "model": model.model,
            "cycle": cycle,
        })
        idx = len(calls) - 1
        result = script[idx] if idx < len(script) else parse_text_contract(PASS_TEXT)
        return RoleRunResult(role=role, cycle=cycle, review_result=result, raw_output_path=Path("/dev/null"))

    _fn.calls = calls
    return _fn




_FIX_THEN_PASS = [
    parse_text_contract(MUST_FIX_TEXT),           # review c1
    ReviewResult(role_status=RoleStatus.COMPLETE),  # dev fix
    parse_text_contract(PASS_TEXT),               # review/recheck c2
    parse_text_contract(PASS_TEXT),               # bugbot
    parse_text_contract(PASS_TEXT),               # compliance
]


def test_bounded_route_uses_the_recheck_model_and_prompt(tmp_path, generic_prompts, monkeypatch):
    monkeypatch.setattr(pr_cycle, "_safe_recheck_route", lambda *a, **k: "BOUNDED")
    fn = _recording_role_fn(_FIX_THEN_PASS)

    result = run_pr_cycle(
        workdir=tmp_path, pr_title="PR 1", resolved=_RESOLVED, rundir=tmp_path / "run",
        profile="generic", run_role_fn=fn,
    )

    assert result.verdict == "PASS"
    reviewer_calls = [c for c in fn.calls if c["role"] == "reviewer"]
    assert reviewer_calls[0]["model"] == "opus"        # full review, expensive model
    assert reviewer_calls[1]["model"] == "sonnet"      # bounded recheck, cheaper model
    assert "recheck" in reviewer_calls[1]["title"]
    # the bounded prompt states the confirm+regression contract and carries
    # the exact findings being confirmed, verbatim
    assert "BOUNDED recheck" in reviewer_calls[1]["prompt"]
    assert "|| contract: c" in reviewer_calls[1]["prompt"]


def test_full_route_keeps_the_expensive_review_model(tmp_path, generic_prompts, monkeypatch):
    monkeypatch.setattr(pr_cycle, "_safe_recheck_route", lambda *a, **k: "FULL")
    fn = _recording_role_fn(_FIX_THEN_PASS)

    run_pr_cycle(
        workdir=tmp_path, pr_title="PR 1", resolved=_RESOLVED, rundir=tmp_path / "run",
        profile="generic", run_role_fn=fn,
    )

    reviewer_calls = [c for c in fn.calls if c["role"] == "reviewer"]
    assert [c["model"] for c in reviewer_calls] == ["opus", "opus"]
    assert "BOUNDED recheck" not in reviewer_calls[1]["prompt"]


def test_route_defaults_to_full_outside_a_git_repo(tmp_path):
    """`tmp_path` is not a git repo, so routing is unanswerable -- the
    conservative answer is FULL, never a narrowed review on an unverified
    premise."""
    assert pr_cycle._safe_recheck_route(tmp_path, "deadbeef", {"src/a.rs"}) == "FULL"


def test_route_defaults_to_full_when_pre_fix_head_is_unknown(tmp_path):
    assert pr_cycle._safe_recheck_route(tmp_path, None, {"src/a.rs"}) == "FULL"


def test_route_defaults_to_full_when_no_finding_files(tmp_path):
    assert pr_cycle._safe_recheck_route(tmp_path, "deadbeef", set()) == "FULL"


def test_scan_bounded_route_restricts_scope_in_the_prompt(tmp_path, generic_prompts, monkeypatch):
    monkeypatch.setattr(pr_cycle, "_safe_recheck_route", lambda *a, **k: "BOUNDED")
    monkeypatch.setattr(pr_cycle, "_changed_files", lambda *a, **k: {"src/a.rs"})
    script = [
        parse_text_contract(PASS_TEXT),   # review c1 passes straight away
        parse_text_contract(MUST_FIX_TEXT),  # bugbot c1
        parse_text_contract(PASS_TEXT),   # compliance c1
        ReviewResult(role_status=RoleStatus.COMPLETE),  # dev fix
        parse_text_contract(PASS_TEXT),   # bugbot c2
        parse_text_contract(PASS_TEXT),   # compliance c2
    ]
    fn = _recording_role_fn(script)

    run_pr_cycle(
        workdir=tmp_path, pr_title="PR 1", resolved=_RESOLVED, rundir=tmp_path / "run",
        profile="generic", run_role_fn=fn,
    )

    bugbot_calls = [c for c in fn.calls if c["role"] == "bugbot"]
    assert "SCOPE RESTRICTION" not in bugbot_calls[0]["prompt"]
    assert "SCOPE RESTRICTION" in bugbot_calls[1]["prompt"]
    assert "- src/a.rs" in bugbot_calls[1]["prompt"]


def test_cycles_log_is_written_for_every_dispatch_and_decision(tmp_path, generic_prompts, monkeypatch):
    from reasona_dev import cycles_log

    monkeypatch.setattr(pr_cycle, "_safe_recheck_route", lambda *a, **k: "FULL")
    fn = _recording_role_fn(_FIX_THEN_PASS)

    run_pr_cycle(
        workdir=tmp_path, pr_title="PR 1", resolved=_RESOLVED, rundir=tmp_path / "run",
        profile="generic", run_role_fn=fn,
    )

    rows = cycles_log.read_records(tmp_path)
    dispatches = [r for r in rows if r.get("kind") != "decision"]
    decisions = [r for r in rows if r.get("kind") == "decision"]

    assert [r["role"] for r in dispatches] == [
        "reviewer", "backend", "reviewer", "bugbot", "compliance",
    ]
    assert [d["action"] for d in decisions] == ["spawn_fix", "pass", "pass"]
    # the review that found something records its finding key, so attribution
    # can later ask which role caught it first
    first = dispatches[0]
    assert first["must_fix_count"] == 1
    assert first["findings"][0]["path"] == "src/a.rs"


def test_the_path_given_to_the_agent_is_absolute_and_carries_a_turn_budget(tmp_path, monkeypatch):
    """Two live regressions in one dispatch.

    The agent runs in a per-task worktree, so a relative path in its
    instructions resolves against THAT tree -- observed: it wrote into its own
    worktree, spent its remaining turns hunting for the file the driver was
    asking about, and died on `error_max_turns` while the driver reported
    ERROR. The turn budget travels as `scope`: the plan-step schema has no
    `max_turns` field, and `complexity` -- the obvious substitute -- maps to
    a budget through a function Bernstein never calls.
    """
    from reasona_dev import bernstein_dispatch, pr_cycle as pc

    seen = {}

    def fake_write_plan(*, path, role, title, description, model, effort, cli, scope):
        seen["description"] = description
        seen["scope"] = scope

    def fake_run(plan_path, workdir, *, port=8052, timeout=3600):
        marker = "to the file `"
        start = seen["description"].index(marker) + len(marker)
        seen["path"] = seen["description"][start:seen["description"].index("`", start)]
        Path(seen["path"]).write_text(PASS_TEXT)
        return bernstein_dispatch.DispatchResult(returncode=0, stderr_tail="")

    monkeypatch.setattr(pc, "write_role_plan", fake_write_plan)
    monkeypatch.setattr(pc, "run_plan_file", fake_run)
    monkeypatch.chdir(tmp_path)

    r = pc.run_role(
        workdir=Path("."), role="reviewer", title="t", prompt="p",
        model=_RESOLVED["review"], rundir=Path("./run"), cycle=1,
    )

    assert Path(seen["path"]).is_absolute()
    assert seen["scope"] == "large"
    assert r.review_result.gate() == "PASS"


def test_a_missing_output_file_records_why_not_just_that(tmp_path, monkeypatch):
    """`cycle_gate` collapses every ERROR into the same abort string, so an
    agent that died on its turn budget and one whose adapter was
    misconfigured read identically. The run's exit code and stderr tail are
    the only diagnostic available once the artifact is absent."""
    from reasona_dev import bernstein_dispatch, pr_cycle as pc

    monkeypatch.setattr(pc, "write_role_plan", lambda **k: None)
    monkeypatch.setattr(
        pc, "run_plan_file",
        lambda *a, **k: bernstein_dispatch.DispatchResult(
            returncode=1, stderr_tail="agent exhausted its turn budget"),
    )

    r = pc.run_role(
        workdir=tmp_path, role="reviewer", title="t", prompt="p",
        model=_RESOLVED["review"], rundir=tmp_path / "run", cycle=1,
    )

    assert r.review_result.role_status.value == "ERROR"
    assert "exit=1" in r.error_detail
    assert "turn budget" in r.error_detail
