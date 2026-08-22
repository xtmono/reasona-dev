import json

from reasona_dev import gh_review_watch as watch


# --- parse_ci -------------------------------------------------------------------

def _commit(rollup_state, contexts=None):
    return {
        "commits": {
            "nodes": [{
                "commit": {
                    "oid": "abc123",
                    "statusCheckRollup": {
                        "state": rollup_state,
                        "contexts": {"nodes": contexts or []},
                    },
                },
            }],
        },
    }


def test_parse_ci_success_is_passing():
    assert watch.parse_ci(_commit("SUCCESS"))["state"] == "passing"


def test_parse_ci_failure_is_failing_with_names():
    contexts = [{"__typename": "CheckRun", "name": "lint", "status": "COMPLETED", "conclusion": "FAILURE"}]
    result = watch.parse_ci(_commit("FAILURE", contexts))
    assert result["state"] == "failing"
    assert result["failing_checks"] == ["lint"]


def test_parse_ci_failure_with_no_contexts_falls_back_to_rollup_label():
    result = watch.parse_ci(_commit("FAILURE"))
    assert result["state"] == "failing"
    assert result["failing_checks"] == ["<rollup=FAILURE>"]


def test_parse_ci_pending():
    assert watch.parse_ci(_commit("PENDING"))["state"] == "pending"


def test_parse_ci_no_commits_is_unknown():
    assert watch.parse_ci({"commits": {"nodes": []}})["state"] == "unknown"


def test_parse_ci_no_ci_configured_is_treated_as_passing():
    pr_data = {
        "commits": {"nodes": [{"commit": {"oid": "x", "statusCheckRollup": {"state": None, "contexts": {"nodes": []}}}}]},
    }
    assert watch.parse_ci(pr_data)["state"] == "passing"


def test_parse_ci_pending_check_run_is_pending_not_failing():
    contexts = [{"__typename": "CheckRun", "name": "slow-job", "status": "IN_PROGRESS", "conclusion": ""}]
    result = watch.parse_ci(_commit("PENDING", contexts))
    assert result["pending_checks"] == ["slow-job"]
    assert result["failing_checks"] == []


# --- parse_compliance_review -----------------------------------------------------

def _comment(body, login="claude", typename="Bot", created="2026-01-01T00:00:00Z"):
    return {"databaseId": 1, "createdAt": created, "body": body, "author": {"__typename": typename, "login": login}}


def test_compliance_missing_when_no_marker_matches():
    assert watch.parse_compliance_review([])["state"] == "missing"


def test_compliance_result_block_pass():
    body = "prose\n=== compliance RESULT ===\nVERDICT: PASS\n=== END ===\nTAS PR Compliance Review"
    result = watch.parse_compliance_review([_comment(body)])
    assert result["state"] == "pass"


def test_compliance_result_block_fail():
    body = "TAS PR Compliance Review\n=== compliance RESULT ===\nVERDICT: FAIL\n=== END ==="
    assert watch.parse_compliance_review([_comment(body)])["state"] == "fail"


def test_compliance_bare_verdict_line_fail_outranks_bold_pass_elsewhere():
    """A per-criterion table cell like `**PASS**` earlier in the body must
    not flip a bare trailing `VERDICT: FAIL` line."""
    body = "TAS PR Compliance Review\n| commits | **PASS** |\nVERDICT: FAIL\n"
    assert watch.parse_compliance_review([_comment(body)])["state"] == "fail"


def test_compliance_heuristic_pass_lgtm():
    body = "TAS PR Compliance Review\nLGTM"
    assert watch.parse_compliance_review([_comment(body)])["state"] == "pass"


def test_compliance_heuristic_fail_bold():
    body = "TAS PR Compliance Review\nVerdict: **FAIL** -- 2 blocking"
    assert watch.parse_compliance_review([_comment(body)])["state"] == "fail"


def test_compliance_marker_present_but_no_verdict_is_missing():
    body = "TAS PR Compliance Review\nstill working on it..."
    assert watch.parse_compliance_review([_comment(body)])["state"] == "missing"


def test_compliance_uses_the_latest_comment_by_created_at():
    old = _comment("TAS PR Compliance Review\nVERDICT: FAIL", created="2026-01-01T00:00:00Z")
    new = _comment("TAS PR Compliance Review\nVERDICT: PASS", created="2026-01-02T00:00:00Z")
    assert watch.parse_compliance_review([old, new])["state"] == "pass"


def test_compliance_in_progress_placeholder_does_not_erase_earlier_real_fail():
    """Real incident (PR #1264, 2026-08-22): a re-review round posts a
    `... round N in progress` placeholder -- matching the marker, but with
    no VERDICT of its own -- before its own result exists. The earlier
    round's real FAIL must still be reported, not overwritten by
    `state: "missing"`."""
    fail = _comment("TAS PR Compliance Review\nVERDICT: FAIL", created="2026-08-22T02:08:01Z")
    in_progress = _comment(
        "TAS PR Compliance Review -- round 2 in progress\n- [x] Round-cap check",
        created="2026-08-22T02:45:25Z",
    )
    result = watch.parse_compliance_review([fail, in_progress])
    assert result["state"] == "fail"
    assert result["round_in_progress"] is True


def test_compliance_no_round_in_progress_when_latest_has_the_verdict():
    only = _comment("TAS PR Compliance Review\nVERDICT: PASS")
    result = watch.parse_compliance_review([only])
    assert result["state"] == "pass"
    assert result["round_in_progress"] is False


def test_compliance_missing_state_also_reports_round_in_progress_false():
    result = watch.parse_compliance_review([])
    assert result["round_in_progress"] is False


def test_compliance_bot_authored_wins_over_body_only_match():
    """A non-bot account whose body happens to contain the marker text
    must not outrank an actual bot-authored artefact."""
    fake = _comment("TAS PR Compliance Review\nVERDICT: FAIL", login="random-user", typename="User")
    real = _comment("TAS PR Compliance Review\nVERDICT: PASS", login="claude", typename="Bot")
    result = watch.parse_compliance_review([fake, real])
    assert result["state"] == "pass"


# --- parse_bugbot_analysis --------------------------------------------------------

def _review(body, login="github-actions", typename="Bot", submitted="2026-01-01T00:00:00Z", state="COMMENTED"):
    return {
        "databaseId": 2, "state": state, "submittedAt": submitted, "body": body,
        "author": {"__typename": typename, "login": login},
    }


def test_bugbot_missing_when_no_marker():
    assert watch.parse_bugbot_analysis([], [])["state"] == "missing"


def test_bugbot_clean_header():
    result = watch.parse_bugbot_analysis([_review("## 🟢 [Claude] BugBot Analysis\nNo bugs found.")], [])
    assert result["state"] == "clean"


def test_bugbot_found_header():
    result = watch.parse_bugbot_analysis([_review("## 🔴 BugBot Analysis\n### Detailed findings\n1. issue")], [])
    assert result["state"] == "found"


def test_bugbot_clean_phrase_not_flipped_by_the_word_findings():
    """'Notable observations / findings' in a clean body must not trigger
    FOUND via a bare 'findings' token."""
    body = "BugBot Analysis\nNo bugs found. Notable observations / findings: none."
    assert watch.parse_bugbot_analysis([_review(body)], [])["state"] == "clean"


def test_bugbot_quantitative_found_phrase():
    assert watch.parse_bugbot_analysis([_review("BugBot Analysis\n3 bugs found")], [])["state"] == "found"


def test_bugbot_dismissed_review_is_ignored():
    dismissed = _review("## 🔴 BugBot Analysis\n### Detailed findings\n1. x", state="DISMISSED")
    assert watch.parse_bugbot_analysis([dismissed], [])["state"] == "missing"


def test_bugbot_also_matches_issue_comments_not_just_reviews():
    comment = _comment("## 🟢 [Claude] BugBot Analysis\nNo bugs found.", login="claude", typename="Bot")
    assert watch.parse_bugbot_analysis([], [comment])["state"] == "clean"


def test_bugbot_submitted_at_is_populated():
    result = watch.parse_bugbot_analysis([_review("## 🟢 [Claude] BugBot Analysis\nNo bugs found.")], [])
    assert result["submitted_at"] == "2026-01-01T00:00:00Z"


# --- classify() decision tree -----------------------------------------------------

def _snap(ci="passing", compliance="pass", bugbot="clean"):
    return {
        "ci": {"state": ci}, "compliance": {"state": compliance}, "bugbot": {"state": bugbot},
    }


def test_classify_terminal_when_all_three_pass():
    assert watch.classify(_snap()) == "terminal"


def test_classify_ci_failing_is_actionable_regardless_of_bots():
    assert watch.classify(_snap(ci="failing", compliance="missing", bugbot="missing")) == "actionable"


def test_classify_ci_pending_is_continue_even_if_bots_would_be_actionable():
    """CI must gate the bot artefacts -- a stale artefact from a prior head
    SHA could otherwise mis-classify while CI is still in flight."""
    assert watch.classify(_snap(ci="pending", compliance="fail", bugbot="found")) == "continue"


def test_classify_compliance_fail_is_actionable():
    assert watch.classify(_snap(compliance="fail")) == "actionable"


def test_classify_bugbot_found_is_actionable():
    assert watch.classify(_snap(bugbot="found")) == "actionable"


def test_classify_missing_bot_artefact_is_continue():
    assert watch.classify(_snap(compliance="missing")) == "continue"
    assert watch.classify(_snap(bugbot="missing")) == "continue"


# --- take_snapshot / fetch_snapshot_raw (mocked gh) -------------------------------

def test_take_snapshot_raises_on_a_closed_pr(tmp_path, monkeypatch):
    def _fake_run(args, work_dir):
        payload = {"data": {"repository": {"pullRequest": {
            "state": "MERGED", "comments": {"nodes": [], "pageInfo": {"hasNextPage": False}},
            "reviews": {"nodes": [], "pageInfo": {"hasNextPage": False}},
            "commits": {"nodes": []},
        }}}}
        return 0, json.dumps(payload), ""

    monkeypatch.setattr(watch, "run_gh", _fake_run)
    try:
        watch.take_snapshot("o", "r", 1, tmp_path)
        assert False, "expected FetchError"
    except watch.FetchError as exc:
        assert "pr not open" in str(exc)


def test_take_snapshot_terminal_end_to_end(tmp_path, monkeypatch):
    def _fake_run(args, work_dir):
        payload = {"data": {"repository": {"pullRequest": {
            "state": "OPEN",
            "comments": {"nodes": [_comment("TAS PR Compliance Review\nVERDICT: PASS")], "pageInfo": {"hasNextPage": False}},
            "reviews": {"nodes": [_review("## 🟢 [Claude] BugBot Analysis\nNo bugs found.")], "pageInfo": {"hasNextPage": False}},
            **_commit("SUCCESS"),
        }}}}
        return 0, json.dumps(payload), ""

    monkeypatch.setattr(watch, "run_gh", _fake_run)
    snap = watch.take_snapshot("o", "r", 1, tmp_path)
    assert watch.classify(snap) == "terminal"


def test_split_repo_rejects_a_malformed_string():
    import pytest
    with pytest.raises(ValueError):
        watch.split_repo("not-a-repo-string")
