from reasona_dev.squash import build, classify, guard


def test_clean_title_passes():
    msg = build("feat", "add JWT rotation", ["body line one"])
    assert msg.title == "feat: add JWT rotation"
    assert classify(guard(msg)) == "PASS"


def test_issue_number_prefix_is_title_violation():
    msg = build("feat", "add JWT rotation", [])
    msg.title = "#42 " + msg.title  # simulate a bad title reaching guard directly
    v = guard(msg)
    assert classify(v) == "FAIL"
    assert any(c.startswith("T") for c in v)


def test_co_authored_by_in_body_is_title_only():
    msg = build("fix", "correct off-by-one", ["Co-Authored-By: someone"])
    v = guard(msg)
    assert classify(v) == "TITLE_ONLY"


def test_github_closing_ref_suffix_not_added_by_builder():
    msg = build("chore", "cleanup", [])
    assert "#" not in msg.title
