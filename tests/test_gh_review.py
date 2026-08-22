from pathlib import Path

from reasona_dev import gh_review, gh_review_watch as watch
from reasona_dev.cycle_gate import GH_REVIEW_MAX_CYCLE, FixBudget
from reasona_dev.finding_adapter import ReviewResult, RoleStatus
from reasona_dev.model_config import ResolvedModel
from reasona_dev.pr_cycle import RoleRunResult

_RESOLVED = {"dev": ResolvedModel("dev", "sonnet", "claude", "high", "d")}


def _snap(ci="passing", compliance="pass", bugbot="clean", comp_body="", bug_body=""):
    return {
        "ci": {"state": ci, "failing_checks": ["lint"] if ci == "failing" else []},
        "compliance": {"state": compliance, "body": comp_body, "comment_id": 1},
        "bugbot": {"state": bugbot, "body": bug_body, "review_id": 2},
    }


def _fn(*, workdir, role, title, prompt, model, rundir, cycle, label=None, port=None, files=None):
    return RoleRunResult(role=role, cycle=cycle,
                         review_result=ReviewResult(role_status=RoleStatus.COMPLETE),
                         raw_output_path=Path("/dev/null"))


def _run(tmp_path, monkeypatch, snapshots, *, push_ok=True, budget=None, **kw):
    """`snapshots` is an iterable of pre-built snapshot dicts, consumed one
    per `take_snapshot()` call -- the caller controls exactly what each
    poll sees, so the loop's own control flow is what's under test."""
    it = iter(snapshots)

    def _fake_snapshot(owner, name, pr, work_dir):
        try:
            return next(it)
        except StopIteration:
            raise AssertionError("watcher called more times than snapshots provided")

    monkeypatch.setattr(watch, "take_snapshot", _fake_snapshot)
    monkeypatch.setattr(watch, "classify", watch.classify)  # real classifier, fake data

    def _fake_shell_run(cmd, workdir, **kw2):
        if cmd[:2] == ["git", "push"]:
            return (0, "", "") if push_ok else (1, "", "rejected")
        if cmd[:2] == ["git", "rev-parse"]:
            return 0, "abc1234", ""
        return 0, "", ""  # gh api reply posts etc.

    monkeypatch.setattr(gh_review._shell, "run", _fake_shell_run)

    return gh_review.run_gh_review(
        workdir=tmp_path, pr_url="https://github.com/o/r/pull/7", pr_num=7, pr_title="t",
        resolved=_RESOLVED, rundir=tmp_path / "r", budget=budget if budget is not None else FixBudget(),
        poll_interval_seconds=0, run_role_fn=_fn, **kw,
    )


def test_parse_fixed_bullets_extracts_every_fixed_line():
    text = "report text\n\nFIXED: a\nnot a bullet\nFIXED:  b  \n"
    assert gh_review._parse_fixed_bullets(text) == ["a", "b"]


def test_parse_fixed_bullets_empty_when_none_present():
    assert gh_review._parse_fixed_bullets("just a report, no FIXED lines") == []


def test_owner_repo_from_pr_url_parses_the_standard_shape():
    assert gh_review.owner_repo_from_pr_url("https://github.com/o/r/pull/7") == ("o", "r")


def test_owner_repo_from_pr_url_returns_none_on_a_malformed_url():
    assert gh_review.owner_repo_from_pr_url("not a url") is None


def test_run_gh_review_passes_immediately_when_terminal(tmp_path, monkeypatch):
    result = _run(tmp_path, monkeypatch, [_snap()])
    assert result.passed
    assert result.watcher_calls == 1
    assert result.fix_commits == []


def test_run_gh_review_reports_a_bad_pr_url_without_polling(tmp_path, monkeypatch):
    result = gh_review.run_gh_review(
        workdir=tmp_path, pr_url="not-a-url", pr_num=1, pr_title="t", resolved=_RESOLVED,
        rundir=tmp_path / "r", budget=FixBudget(), run_role_fn=_fn,
    )
    assert not result.passed
    assert "owner/repo" in result.reason


def test_run_gh_review_dispatches_a_fix_on_ci_failure_then_passes(tmp_path, monkeypatch):
    result = _run(tmp_path, monkeypatch, [_snap(ci="failing"), _snap()])
    assert result.passed
    assert result.watcher_calls == 2
    assert len(result.fix_commits) == 1
    assert result.dispatches[0].role == "backend"


def test_port_reaches_the_fix_dispatch(tmp_path, monkeypatch):
    seen_ports = []

    def _recording_fn(*, workdir, role, title, prompt, model, rundir, cycle, label=None, port=None, files=None):
        seen_ports.append(port)
        return RoleRunResult(role=role, cycle=cycle,
                             review_result=ReviewResult(role_status=RoleStatus.COMPLETE),
                             raw_output_path=Path("/dev/null"))

    it = iter([_snap(ci="failing"), _snap()])

    def _fake_snapshot(owner, name, pr, work_dir):
        return next(it)

    monkeypatch.setattr(watch, "take_snapshot", _fake_snapshot)
    monkeypatch.setattr(gh_review._shell, "run", lambda cmd, workdir, **kw2: (0, "abc1234", ""))

    gh_review.run_gh_review(
        workdir=tmp_path, pr_url="https://github.com/o/r/pull/7", pr_num=7, pr_title="t",
        resolved=_RESOLVED, rundir=tmp_path / "r", budget=FixBudget(),
        poll_interval_seconds=0, run_role_fn=_recording_fn, port=19999,
    )
    assert seen_ports == [19999]


def test_run_gh_review_dispatches_one_fix_covering_both_actionable_signals(tmp_path, monkeypatch):
    """A round where BOTH compliance and bugbot are actionable in the same
    snapshot must dispatch ONE fix and do ONE push, not two."""
    result = _run(
        tmp_path, monkeypatch,
        [_snap(compliance="fail", comp_body="fix this", bugbot="found", bug_body="and this"), _snap()],
    )
    assert result.passed
    assert len(result.fix_commits) == 1
    assert len(result.dispatches) == 1


def test_run_gh_review_posts_the_llm_written_fixed_bullets_in_the_bot_reply(tmp_path, monkeypatch):
    """The fixing dispatch's own `FIXED:` lines (`_FIXED_BULLETS_INSTRUCTION`)
    become the reply comment's bullets -- not a bare "fixed in <sha>" line
    with no description of what was actually fixed."""
    out = tmp_path / "raw.txt"
    out.write_text(
        "some markdown report\n\nFIXED: renamed the leaking helper\nFIXED: added a regression test\n",
        encoding="utf-8",
    )

    def _fn_with_bullets(*, workdir, role, title, prompt, model, rundir, cycle, label=None, port=None, files=None):
        assert "FIXED:" in prompt
        return RoleRunResult(role=role, cycle=cycle,
                             review_result=ReviewResult(role_status=RoleStatus.COMPLETE),
                             raw_output_path=out)

    posted = []

    def _fake_shell_run(cmd, workdir, **kw2):
        if cmd[:2] == ["git", "push"]:
            return 0, "", ""
        if cmd[:2] == ["git", "rev-parse"]:
            return 0, "abc1234", ""
        if cmd[:3] == ["gh", "api", "-X"]:
            posted.append(cmd[-1])
        return 0, "", ""

    it = iter([_snap(compliance="fail", comp_body="fix this"), _snap()])
    monkeypatch.setattr(watch, "take_snapshot", lambda owner, name, pr, work_dir: next(it))
    monkeypatch.setattr(gh_review._shell, "run", _fake_shell_run)

    result = gh_review.run_gh_review(
        workdir=tmp_path, pr_url="https://github.com/o/r/pull/7", pr_num=7, pr_title="t",
        resolved=_RESOLVED, rundir=tmp_path / "r", budget=FixBudget(),
        poll_interval_seconds=0, run_role_fn=_fn_with_bullets,
    )
    assert result.passed
    assert len(posted) == 1
    assert "renamed the leaking helper" in posted[0]
    assert "added a regression test" in posted[0]


def test_run_gh_review_budget_exhausted_reports_blocked(tmp_path, monkeypatch):
    snapshots = [_snap(ci="failing") for _ in range(GH_REVIEW_MAX_CYCLE + 1)]
    result = _run(tmp_path, monkeypatch, snapshots)
    assert not result.passed
    assert f"{GH_REVIEW_MAX_CYCLE} cycles" in result.reason


def test_run_gh_review_pr_not_open_blocks_immediately(tmp_path, monkeypatch):
    def _fake_snapshot(owner, name, pr, work_dir):
        raise watch.FetchError("pr not open: MERGED")

    monkeypatch.setattr(watch, "take_snapshot", _fake_snapshot)
    result = gh_review.run_gh_review(
        workdir=tmp_path, pr_url="https://github.com/o/r/pull/7", pr_num=7, pr_title="t",
        resolved=_RESOLVED, rundir=tmp_path / "r", budget=FixBudget(), run_role_fn=_fn,
    )
    assert not result.passed and "pr not open" in result.reason


def test_run_gh_review_a_transient_fetch_error_is_retried_not_terminal(tmp_path, monkeypatch):
    calls = {"n": 0}

    def _fake_snapshot(owner, name, pr, work_dir):
        calls["n"] += 1
        if calls["n"] == 1:
            raise watch.FetchError("gh api graphql failed: rc=1")
        return _snap()

    monkeypatch.setattr(watch, "take_snapshot", _fake_snapshot)

    def _fake_shell_run(cmd, workdir, **kw):
        return 0, "abc1234", ""

    monkeypatch.setattr(gh_review._shell, "run", _fake_shell_run)
    result = gh_review.run_gh_review(
        workdir=tmp_path, pr_url="https://github.com/o/r/pull/7", pr_num=7, pr_title="t",
        resolved=_RESOLVED, rundir=tmp_path / "r", budget=FixBudget(),
        poll_interval_seconds=0, run_role_fn=_fn,
    )
    assert result.passed
    assert calls["n"] == 2


def test_run_gh_review_timeout_reports_blocked(tmp_path, monkeypatch):
    def _fake_snapshot(owner, name, pr, work_dir):
        return _snap(ci="pending")

    monkeypatch.setattr(watch, "take_snapshot", _fake_snapshot)
    result = gh_review.run_gh_review(
        workdir=tmp_path, pr_url="https://github.com/o/r/pull/7", pr_num=7, pr_title="t",
        resolved=_RESOLVED, rundir=tmp_path / "r", budget=FixBudget(),
        max_wait_seconds=0, poll_interval_seconds=0, run_role_fn=_fn,
    )
    assert not result.passed and "timeout" in result.reason


def test_run_gh_review_a_failed_push_blocks_without_looping_forever(tmp_path, monkeypatch):
    result = _run(tmp_path, monkeypatch, [_snap(ci="failing")], push_ok=False)
    assert not result.passed
    assert "git push failed" in result.reason


def test_run_gh_review_spends_the_gh_review_budget_stage(tmp_path, monkeypatch):
    budget = FixBudget()
    result = _run(tmp_path, monkeypatch, [_snap(ci="failing"), _snap()], budget=budget)
    assert result.passed
    assert budget.review_cycles == 1  # charged to the review stage
    assert budget.total_used == 1
