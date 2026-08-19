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


def test_co_authored_by_in_body_is_stripped_by_builder():
    # build() actively drops trailer lines -- dev-ralf squash_build.py
    # clean_body step 5. A well-formed message never reaches guard() with a
    # trailer still in it.
    msg = build("fix", "correct off-by-one", ["Co-Authored-By: someone"])
    assert "Co-Authored-By" not in msg.body
    assert classify(guard(msg)) == "PASS"


def test_co_authored_by_reaching_guard_directly_is_title_only():
    # Simulates a trailer that reaches guard() without going through build()
    # (e.g. hand-edited after construction) -- same pattern as
    # test_issue_number_prefix_is_title_violation above.
    msg = build("fix", "correct off-by-one", [])
    msg.body = "Co-Authored-By: someone"
    v = guard(msg)
    assert classify(v) == "TITLE_ONLY"
    assert any(c.startswith("B2") for c in v)


def test_signed_off_by_is_also_forbidden():
    # dev-ralf B2_RE covers seven trailer forms; the old body check here only
    # matched two (co-authored-by, generated-by).
    msg = build("fix", "correct off-by-one", [])
    msg.body = "Signed-off-by: someone"
    v = guard(msg)
    assert any(c.startswith("B2") for c in v)


def test_closing_ref_in_body_is_stripped_by_builder():
    msg = build("fix", "correct off-by-one", ["fixes bug, closes #42"])
    assert "#42" not in msg.body
    assert classify(guard(msg)) == "PASS"


def test_long_body_line_is_wrapped_not_dropped():
    long_text = " ".join(f"word{i}" for i in range(40))  # well over 100 codepoints
    msg = build("feat", "add JWT rotation", [long_text])
    assert all(len(line) <= 100 for line in msg.body.splitlines())
    # nothing is silently dropped -- every word survives somewhere in the body
    for i in range(40):
        assert f"word{i}" in msg.body
    assert classify(guard(msg)) == "PASS"


def test_github_closing_ref_suffix_not_added_by_builder():
    msg = build("chore", "cleanup", [])
    assert "#" not in msg.title
