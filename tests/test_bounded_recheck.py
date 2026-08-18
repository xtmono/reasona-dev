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

    def _fn(*, server, workdir, role, title, prompt, model, rundir, cycle, approval_required=False):
        calls.append({
            "role": role, "title": title, "prompt": prompt, "model": model.model,
            "cycle": cycle, "approval_required": approval_required,
        })
        idx = len(calls) - 1
        result = script[idx] if idx < len(script) else parse_text_contract(PASS_TEXT)
        return RoleRunResult(role=role, cycle=cycle, review_result=result, raw_output_path=Path("/dev/null"))

    _fn.calls = calls
    return _fn


def _noop_start(workdir, *, port):
    return None


def _noop_stop(server, *, workdir):
    pass


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
        start_server_fn=_noop_start, stop_server_fn=_noop_stop,
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
        start_server_fn=_noop_start, stop_server_fn=_noop_stop,
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


def test_approval_flag_reaches_dev_fix_dispatch_only(tmp_path, generic_prompts, monkeypatch):
    monkeypatch.setattr(pr_cycle, "_safe_recheck_route", lambda *a, **k: "FULL")
    fn = _recording_role_fn(_FIX_THEN_PASS)

    run_pr_cycle(
        workdir=tmp_path, pr_title="PR 1", resolved=_RESOLVED, rundir=tmp_path / "run",
        profile="generic", approval_required=True, run_role_fn=fn,
        start_server_fn=_noop_start, stop_server_fn=_noop_stop,
    )

    by_role = {c["role"]: c["approval_required"] for c in fn.calls}
    assert by_role["backend"] is True      # the dispatch that changes code
    assert by_role["reviewer"] is False    # read-only roles are never gated
    assert by_role["bugbot"] is False


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
        start_server_fn=_noop_start, stop_server_fn=_noop_stop,
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
        start_server_fn=_noop_start, stop_server_fn=_noop_stop,
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


def test_raw_output_path_given_to_the_agent_is_absolute(tmp_path, monkeypatch):
    """The agent runs in a per-task worktree, so a relative path in its
    instructions resolves against that tree and the driver never sees the
    file. Live-observed: the agent wrote into its worktree, burned its turns
    looking for it, and the role came back ERROR."""
    import os

    from reasona_dev import bernstein_server, pr_cycle as pc

    seen = {}

    def fake_dispatch(handle, *, role, title, description, model, effort, cli,
                      raw_output_path, approval_required=False):
        seen["path"] = raw_output_path
        seen["description"] = description
        Path(raw_output_path).write_text(PASS_TEXT)
        return "t1"

    monkeypatch.setattr(pc, "dispatch_task", fake_dispatch)
    monkeypatch.setattr(pc, "poll_task", lambda *a, **k: {"status": "done"})
    monkeypatch.chdir(tmp_path)

    pc.run_role(
        server=None, workdir=Path("."), role="reviewer", title="t",
        prompt="p", model=_RESOLVED["review"], rundir=Path("./run"), cycle=1,
    )

    assert Path(seen["path"]).is_absolute()
    # and the instruction the agent actually reads carries that absolute path
    assert str(seen["path"]) in seen["description"]
