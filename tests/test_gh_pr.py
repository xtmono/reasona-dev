from pathlib import Path

import pytest

from reasona_dev import final_phase, gh_pr
from reasona_dev.plan_compile import PRUnit

UNIT = PRUnit(
    index="1", title="add subtract()", unit_type="feat",
    section="## PR 1: add subtract()\n\n- [ ] implement it\n",
)


# --- resolve_type_subject -----------------------------------------------------

def test_resolve_type_subject_uses_the_units_own_fields():
    assert gh_pr.resolve_type_subject(UNIT) == ("feat", "add subtract()")


def test_resolve_type_subject_defaults_to_feat_when_unset():
    unit = PRUnit(index="1", title="x")
    assert gh_pr.resolve_type_subject(unit) == ("feat", "x")


# --- build_pr_title (deterministic, sanitized) --------------------------------

def test_build_pr_title_basic():
    assert gh_pr.build_pr_title("feat", "add subtract()") == "feat: add subtract()"


def test_build_pr_title_strips_a_leading_issue_number():
    assert gh_pr.build_pr_title("feat", "#42 add subtract()") == "feat: add subtract()"


def test_build_pr_title_strips_a_trailing_period():
    assert gh_pr.build_pr_title("feat", "add subtract().") == "feat: add subtract()"


def test_build_pr_title_falls_back_to_feat_for_an_unrecognized_type():
    assert gh_pr.build_pr_title("bogus", "x").startswith("feat: ")


# --- build_pr_body -------------------------------------------------------------

def test_build_pr_body_has_the_required_sections_and_closing_ref():
    body = gh_pr.build_pr_body(issue_num=7, plan_name="myplan", unit=UNIT)
    assert "Closes #7" in body
    assert "## Changes" in body
    assert "## Why we need this" in body
    assert "## Test" in body


def test_build_pr_body_uses_the_summary_when_given():
    summary = {"changes": "reverted the error.rs edit", "why": "poc-scope violation", "test": "533 tests pass"}
    body = gh_pr.build_pr_body(issue_num=7, plan_name="myplan", unit=UNIT, summary=summary)
    assert "reverted the error.rs edit" in body
    assert "poc-scope violation" in body
    assert "533 tests pass" in body
    assert UNIT.section not in body


def test_build_pr_body_falls_back_to_plan_section_without_a_summary():
    body = gh_pr.build_pr_body(issue_num=7, plan_name="myplan", unit=UNIT)
    assert "implement it" in body  # from UNIT.section


# --- _parse_pr_summary / generate_pr_summary ------------------------------------

def test_parse_pr_summary_extracts_all_three_labeled_sections():
    text = "CHANGES: did the thing\nWHY: because reasons\nTEST: ran pytest, 12 passed"
    summary = gh_pr._parse_pr_summary(text)
    assert summary == {"changes": "did the thing", "why": "because reasons", "test": "ran pytest, 12 passed"}


def test_parse_pr_summary_tolerates_leading_prose_and_reordering():
    text = "Sure, here it is:\n\nWHY: because reasons\nTEST: ran pytest\nCHANGES: did the thing"
    summary = gh_pr._parse_pr_summary(text)
    assert summary == {"changes": "did the thing", "why": "because reasons", "test": "ran pytest"}


def test_parse_pr_summary_none_when_a_section_is_missing():
    assert gh_pr._parse_pr_summary("CHANGES: x\nWHY: y") is None


def test_parse_pr_summary_none_for_unrelated_text():
    assert gh_pr._parse_pr_summary("just some random output") is None


def test_generate_pr_summary_returns_none_on_dispatch_error(tmp_path):
    from reasona_dev.finding_adapter import ReviewResult, RoleStatus
    from reasona_dev.model_config import ResolvedModel
    from reasona_dev.pr_cycle import RoleRunResult

    def failing_run_role_fn(**kw):
        return RoleRunResult(
            role="backend", cycle=1, review_result=ReviewResult(role_status=RoleStatus.ERROR),
            raw_output_path=tmp_path / "missing.raw.txt", error_detail="no output",
        )

    summary = gh_pr.generate_pr_summary(
        workdir=tmp_path, unit=UNIT, plan_name="myplan",
        model=ResolvedModel("dev", "sonnet", "claude", "high", "default"),
        rundir=tmp_path / "run", run_role_fn=failing_run_role_fn,
    )
    assert summary is None


def test_generate_pr_summary_parses_the_dispatched_roles_output(tmp_path):
    from reasona_dev.finding_adapter import ReviewResult, RoleStatus
    from reasona_dev.model_config import ResolvedModel
    from reasona_dev.pr_cycle import RoleRunResult

    def fake_run_role_fn(*, workdir, role, title, prompt, model, rundir, cycle, port, label=None, files=None):
        out = rundir / "pr_body-c1.raw.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("CHANGES: did x\nWHY: needed for y\nTEST: 5 tests pass", encoding="utf-8")
        return RoleRunResult(
            role=label or role, cycle=cycle,
            review_result=ReviewResult(role_status=RoleStatus.COMPLETE), raw_output_path=out,
        )

    summary = gh_pr.generate_pr_summary(
        workdir=tmp_path, unit=UNIT, plan_name="myplan",
        model=ResolvedModel("dev", "sonnet", "claude", "high", "default"),
        rundir=tmp_path / "run", run_role_fn=fake_run_role_fn,
    )
    assert summary == {"changes": "did x", "why": "needed for y", "test": "5 tests pass"}


# --- validate_pr_meta (P1-P7) --------------------------------------------------

def _valid_body(issue_num=1):
    return "Closes #1\n\n## Changes\nx\n\n## Why we need this\ny\n\n## Test\nz\n"


def test_validate_pr_meta_clean_title_and_body():
    assert gh_pr.validate_pr_meta(title="feat: add subtract()", body=_valid_body(), issue_num=1) == []


def test_validate_pr_meta_p1_hash_prefix():
    v = gh_pr.validate_pr_meta(title="#1 feat: add subtract()", body=_valid_body(), issue_num=1)
    assert "P1" in v


def test_validate_pr_meta_p2_not_conventional_commits():
    v = gh_pr.validate_pr_meta(title="add subtract()", body=_valid_body(), issue_num=1)
    assert "P2" in v


def test_validate_pr_meta_p3_trailing_period():
    v = gh_pr.validate_pr_meta(title="feat: add subtract().", body=_valid_body(), issue_num=1)
    assert "P3" in v


def test_validate_pr_meta_p4_missing_closes():
    body = "## Changes\nx\n\n## Why we need this\ny\n\n## Test\nz\n"
    v = gh_pr.validate_pr_meta(title="feat: x", body=body, issue_num=1)
    assert "P4" in v


def test_validate_pr_meta_p4_closes_the_wrong_issue():
    v = gh_pr.validate_pr_meta(title="feat: x", body=_valid_body(), issue_num=99)
    assert "P4" in v


def test_validate_pr_meta_p5_p6_p7_missing_sections():
    v = gh_pr.validate_pr_meta(title="feat: x", body="Closes #1\n", issue_num=1)
    assert {"P5", "P6", "P7"} <= set(v)


# --- create_issue / rename_branch_for_pr (mocked subprocess) ------------------

def test_create_issue_parses_the_issue_number(tmp_path, monkeypatch):
    def _fake_run(cmd, workdir, *, timeout=300):
        assert cmd[:3] == ["gh", "issue", "create"]
        return 0, "https://github.com/o/r/issues/42\n", ""

    monkeypatch.setattr(gh_pr._shell, "run", _fake_run)
    num, reason = gh_pr.create_issue(tmp_path, title="t", body="b")
    assert num == 42


def test_create_issue_reports_gh_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(gh_pr._shell, "run", lambda cmd, workdir, **kw: (1, "", "boom"))
    num, reason = gh_pr.create_issue(tmp_path, title="t", body="b")
    assert num is None and "boom" in reason


def test_rename_branch_for_pr_builds_the_issue_slug(tmp_path, monkeypatch):
    calls = []

    def _fake_run(cmd, workdir, **kw):
        calls.append(cmd)
        return 0, "", ""

    monkeypatch.setattr(gh_pr._shell, "run", _fake_run)
    branch, reason = gh_pr.rename_branch_for_pr(tmp_path, issue_num=42, subject="Add Subtract()!")
    assert branch == "issue/42-add-subtract"
    assert calls == [["git", "branch", "-m", branch]]


# --- repair_pr ------------------------------------------------------------------

def test_repair_pr_succeeds_immediately_when_the_edit_is_clean(tmp_path, monkeypatch):
    monkeypatch.setattr(gh_pr._shell, "run", lambda cmd, workdir, **kw: (0, "", ""))
    ok, reason = gh_pr.repair_pr(
        tmp_path, pr_url="https://gh/pr/1", title="feat: x", body=_valid_body(), issue_num=1,
    )
    assert ok and "1 attempt" in reason


def test_repair_pr_gives_up_after_max_attempts(tmp_path, monkeypatch):
    monkeypatch.setattr(gh_pr._shell, "run", lambda cmd, workdir, **kw: (0, "", ""))
    ok, reason = gh_pr.repair_pr(
        tmp_path, pr_url="https://gh/pr/1", title="not-conventional", body="", issue_num=1, max_attempts=2,
    )
    assert not ok and "2 repair attempt(s)" in reason


def test_repair_pr_reports_a_gh_edit_failure_immediately(tmp_path, monkeypatch):
    monkeypatch.setattr(gh_pr._shell, "run", lambda cmd, workdir, **kw: (1, "", "not authenticated"))
    ok, reason = gh_pr.repair_pr(
        tmp_path, pr_url="https://gh/pr/1", title="feat: x", body=_valid_body(), issue_num=1,
    )
    assert not ok and "not authenticated" in reason


# --- find_duplicate_pr (DUP-WORKER guard) ----------------------------------------

def test_find_duplicate_pr_finds_an_exact_title_match(tmp_path, monkeypatch):
    def _fake_run(cmd, workdir, **kw):
        assert cmd[:3] == ["gh", "pr", "list"]
        return 0, '[{"number": 9, "title": "feat: add subtract()", "url": "https://gh/pr/9"}]', ""

    monkeypatch.setattr(gh_pr._shell, "run", _fake_run)
    num, url = gh_pr.find_duplicate_pr(tmp_path, title="feat: add subtract()")
    assert num == 9 and url == "https://gh/pr/9"


def test_find_duplicate_pr_ignores_a_non_matching_title(tmp_path, monkeypatch):
    monkeypatch.setattr(
        gh_pr._shell, "run",
        lambda cmd, workdir, **kw: (0, '[{"number": 9, "title": "fix: something else", "url": "https://gh/pr/9"}]', ""),
    )
    num, url = gh_pr.find_duplicate_pr(tmp_path, title="feat: add subtract()")
    assert num is None and url is None


def test_find_duplicate_pr_no_open_prs(tmp_path, monkeypatch):
    monkeypatch.setattr(gh_pr._shell, "run", lambda cmd, workdir, **kw: (0, "[]", ""))
    num, url = gh_pr.find_duplicate_pr(tmp_path, title="feat: add subtract()")
    assert num is None and url is None


def test_find_duplicate_pr_returns_none_on_gh_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(gh_pr._shell, "run", lambda cmd, workdir, **kw: (1, "", "not authenticated"))
    num, url = gh_pr.find_duplicate_pr(tmp_path, title="feat: add subtract()")
    assert num is None and url is None


def test_list_merged_pr_titles_maps_title_to_number_and_url(tmp_path, monkeypatch):
    def _fake_run(cmd, workdir, **kw):
        assert cmd[:3] == ["gh", "pr", "list"]
        assert "--state" in cmd and "merged" in cmd
        return 0, (
            '[{"number": 9, "title": "feat: add subtract()", "url": "https://gh/pr/9"},'
            ' {"number": 11, "title": "fix: rounding error", "url": "https://gh/pr/11"}]'
        ), ""

    monkeypatch.setattr(gh_pr._shell, "run", _fake_run)
    titles = gh_pr.list_merged_pr_titles(tmp_path)
    assert titles == {
        "feat: add subtract()": (9, "https://gh/pr/9"),
        "fix: rounding error": (11, "https://gh/pr/11"),
    }


def test_list_merged_pr_titles_is_one_call_regardless_of_result_size(tmp_path, monkeypatch):
    calls = []

    def _fake_run(cmd, workdir, **kw):
        calls.append(cmd)
        return 0, "[]", ""

    monkeypatch.setattr(gh_pr._shell, "run", _fake_run)
    gh_pr.list_merged_pr_titles(tmp_path)
    assert len(calls) == 1


def test_list_merged_pr_titles_returns_empty_on_gh_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(gh_pr._shell, "run", lambda cmd, workdir, **kw: (1, "", "not authenticated"))
    assert gh_pr.list_merged_pr_titles(tmp_path) == {}


def test_list_merged_pr_titles_returns_empty_on_malformed_json(tmp_path, monkeypatch):
    monkeypatch.setattr(gh_pr._shell, "run", lambda cmd, workdir, **kw: (0, "not json", ""))
    assert gh_pr.list_merged_pr_titles(tmp_path) == {}


# --- run_gh_pr composition -------------------------------------------------------

def test_run_gh_pr_refuses_to_create_a_duplicate(tmp_path, monkeypatch):
    """worker.md's DUP-WORKER guard: a sibling already has this unit's
    exact title open -- do not create a second issue or PR."""
    def _fake_run(cmd, workdir, **kw):
        if cmd[:3] == ["gh", "pr", "list"]:
            return 0, '[{"number": 9, "title": "feat: add subtract()", "url": "https://gh/pr/9"}]', ""
        pytest.fail(f"must not go any further -- duplicate found (call: {cmd})")

    monkeypatch.setattr(gh_pr._shell, "run", _fake_run)
    result = gh_pr.run_gh_pr(workdir=tmp_path, stage_name="pr-1", unit=UNIT, plan_name=None)
    assert result.passed is False
    assert result.duplicate is True
    assert result.pr_num == 9
    assert result.pr_url == "https://gh/pr/9"
    assert "duplicate" in result.reason




def _stub_shell(monkeypatch, *, issue_out="https://github.com/o/r/issues/42\n"):
    def _fake_run(cmd, workdir, **kw):
        if cmd[:3] == ["gh", "pr", "list"]:
            return 0, "[]", ""
        if cmd[:3] == ["gh", "issue", "create"]:
            return 0, issue_out, ""
        if cmd[:3] == ["git", "branch", "-m"]:
            return 0, "", ""
        raise AssertionError(f"unexpected shell call: {cmd}")

    monkeypatch.setattr(gh_pr._shell, "run", _fake_run)


def test_run_gh_pr_creates_issue_renames_branch_and_creates_pr(tmp_path, monkeypatch):
    _stub_shell(monkeypatch)
    seen = {}

    def _fake_create_pr(workdir, *, title, body, head, base, known_pr_url):
        seen["title"] = title
        seen["head"] = head
        seen["base"] = base
        return "https://github.com/o/r/pull/7", "PR created"

    monkeypatch.setattr(final_phase, "create_pr", _fake_create_pr)

    result = gh_pr.run_gh_pr(workdir=tmp_path, stage_name="pr-1", unit=UNIT, plan_name=None)
    assert result.passed
    assert result.pr_url == "https://github.com/o/r/pull/7"
    assert result.pr_num == 7
    assert result.issue_num == 42
    assert seen["head"] == "issue/42-add-subtract"
    assert seen["base"] == "main"
    assert seen["title"] == "feat: add subtract()"


def test_run_gh_pr_refuses_to_create_a_pr_when_full_ci_fails(tmp_path, monkeypatch):
    """B-5: worker.md §4 -- a full CI failure blocks PR creation outright,
    never opening a PR whose accumulated commits do not build/test clean."""
    _stub_shell(monkeypatch)
    (tmp_path / ".reasona").mkdir()
    (tmp_path / ".reasona" / "reasona.yaml").write_text("ci:\n  full: whatever\n")
    monkeypatch.setattr(gh_pr.ci_gate, "run_full", lambda workdir, command, **kw: (False, "build failed"))

    create_pr_calls = []
    monkeypatch.setattr(final_phase, "create_pr", lambda *a, **kw: create_pr_calls.append(1) or (None, "should not be called"))

    result = gh_pr.run_gh_pr(workdir=tmp_path, stage_name="pr-1", unit=UNIT, plan_name=None)
    assert result.passed is False
    assert "full CI failed" in result.reason
    assert create_pr_calls == []


def test_run_gh_pr_reuses_a_known_issue_on_resume(tmp_path, monkeypatch):
    from reasona_dev import ledger

    ledger.mark_issue_created(tmp_path, "plan", "pr-1", 99)

    def _fake_run(cmd, workdir, **kw):
        if cmd[:3] == ["gh", "pr", "list"]:
            return 0, "[]", ""
        if cmd[:3] == ["gh", "issue", "create"]:
            pytest.fail("must not create a second issue -- one is already known")
        if cmd[:3] == ["git", "branch", "-m"]:
            return 0, "", ""
        raise AssertionError(f"unexpected shell call: {cmd}")

    monkeypatch.setattr(gh_pr._shell, "run", _fake_run)
    monkeypatch.setattr(
        final_phase, "create_pr",
        lambda workdir, **kw: ("https://github.com/o/r/pull/7", "PR created"),
    )

    result = gh_pr.run_gh_pr(workdir=tmp_path, stage_name="pr-1", unit=UNIT, plan_name="plan")
    assert result.issue_num == 99


def test_run_gh_pr_blocks_when_issue_creation_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(gh_pr._shell, "run", lambda cmd, workdir, **kw: (1, "", "gh: not authenticated"))
    result = gh_pr.run_gh_pr(workdir=tmp_path, stage_name="pr-1", unit=UNIT, plan_name=None)
    assert not result.passed
    assert "not authenticated" in result.reason


def test_run_gh_pr_repairs_a_violation_and_still_passes(tmp_path, monkeypatch):
    """The builder is deterministic and should not normally violate P1-P7,
    but if it ever does, repair_pr() should push a corrected version rather
    than failing the unit outright."""
    _stub_shell(monkeypatch)
    edits = []

    def _fake_create_pr(workdir, *, title, body, head, base, known_pr_url):
        return "https://github.com/o/r/pull/7", "PR created"

    def _fake_validate(*, title, body, issue_num):
        # First call (post-create) reports a violation; the repair_pr()
        # internal re-check (second call) reports clean.
        edits.append(1)
        return ["P4"] if len(edits) == 1 else []

    monkeypatch.setattr(final_phase, "create_pr", _fake_create_pr)
    monkeypatch.setattr(gh_pr, "validate_pr_meta", _fake_validate)

    def _fake_run_with_edit(cmd, workdir, **kw):
        if cmd[:3] == ["gh", "pr", "list"]:
            return 0, "[]", ""
        if cmd[:3] == ["gh", "issue", "create"]:
            return 0, "https://github.com/o/r/issues/42\n", ""
        if cmd[:3] == ["git", "branch", "-m"]:
            return 0, "", ""
        if cmd[:3] == ["gh", "pr", "edit"]:
            return 0, "", ""
        raise AssertionError(f"unexpected shell call: {cmd}")

    monkeypatch.setattr(gh_pr._shell, "run", _fake_run_with_edit)

    result = gh_pr.run_gh_pr(workdir=tmp_path, stage_name="pr-1", unit=UNIT, plan_name=None)
    assert result.passed
