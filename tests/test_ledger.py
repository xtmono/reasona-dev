from reasona_dev import ledger

PLAN = "testplan"


def test_dev_not_dispatched_by_default(tmp_path):
    assert ledger.dev_already_dispatched(tmp_path, PLAN, "pr-1") is False


def test_mark_dev_dispatched_is_read_back(tmp_path):
    ledger.mark_dev_dispatched(tmp_path, PLAN, "pr-1")
    assert ledger.dev_already_dispatched(tmp_path, PLAN, "pr-1") is True


def test_dev_dispatched_flag_is_per_unit(tmp_path):
    """Cycle-0 is now dispatched per PR unit, into that unit's own
    worktree -- the flag must not leak across units in the same plan."""
    ledger.mark_dev_dispatched(tmp_path, PLAN, "pr-1")
    assert ledger.dev_already_dispatched(tmp_path, PLAN, "pr-1") is True
    assert ledger.dev_already_dispatched(tmp_path, PLAN, "pr-2") is False


def test_dev_dispatched_flag_is_plan_scoped(tmp_path):
    """Two plans that both exist under the same workdir must not share a
    cycle-0 flag -- see reasona_dev/ledger.py's own docstring on why every
    path here is namespaced by plan_name."""
    ledger.mark_dev_dispatched(tmp_path, "plan-a", "pr-1")
    assert ledger.dev_already_dispatched(tmp_path, "plan-a", "pr-1") is True
    assert ledger.dev_already_dispatched(tmp_path, "plan-b", "pr-1") is False


def test_dev_dispatched_flag_survives_alongside_terminal_status(tmp_path):
    """Both live in the same unit ledger.json -- marking one must not
    clobber the other."""
    ledger.mark_dev_dispatched(tmp_path, PLAN, "pr-1")
    ledger.mark_unit_terminal(tmp_path, PLAN, "pr-1", status="shipped", reason="merged")
    assert ledger.dev_already_dispatched(tmp_path, PLAN, "pr-1") is True
    assert ledger.unit_status(tmp_path, PLAN, "pr-1") == "shipped"


def test_unit_status_is_none_by_default(tmp_path):
    assert ledger.unit_status(tmp_path, PLAN, "pr-1") is None


def test_mark_unit_terminal_is_read_back(tmp_path):
    ledger.mark_unit_terminal(tmp_path, PLAN, "pr-1", status="shipped", reason="merged")
    assert ledger.unit_status(tmp_path, PLAN, "pr-1") == "shipped"


def test_units_are_tracked_independently(tmp_path):
    ledger.mark_unit_terminal(tmp_path, PLAN, "pr-1", status="shipped", reason="merged")
    ledger.mark_unit_terminal(tmp_path, PLAN, "pr-2", status="failed", reason="review did not converge")
    assert ledger.unit_status(tmp_path, PLAN, "pr-1") == "shipped"
    assert ledger.unit_status(tmp_path, PLAN, "pr-2") == "failed"


def test_units_are_tracked_independently_per_plan(tmp_path):
    """Two different plans naming a unit `pr-1` (the common, unavoidable
    case -- `plan_compile._stage_name()` is just `f"pr-{index}"`) must not
    share a ledger file."""
    ledger.mark_unit_terminal(tmp_path, "plan-a", "pr-1", status="shipped", reason="merged")
    ledger.mark_unit_terminal(tmp_path, "plan-b", "pr-1", status="failed", reason="broke")
    assert ledger.unit_status(tmp_path, "plan-a", "pr-1") == "shipped"
    assert ledger.unit_status(tmp_path, "plan-b", "pr-1") == "failed"


def test_progress_roundtrips(tmp_path):
    progress = {"phase": "review", "review_cycle": 2, "route": "BOUNDED"}
    ledger.save_progress(tmp_path, PLAN, "pr-1", progress)
    assert ledger.load_progress(tmp_path, PLAN, "pr-1") == progress


def test_progress_is_none_by_default(tmp_path):
    assert ledger.load_progress(tmp_path, PLAN, "pr-1") is None


def test_marking_a_unit_terminal_clears_its_progress(tmp_path):
    """A shipped/failed unit has nothing left to resume -- a stale
    in-progress checkpoint left behind would outlive its meaning."""
    ledger.save_progress(tmp_path, PLAN, "pr-1", {"phase": "review", "review_cycle": 1})
    ledger.mark_unit_terminal(tmp_path, PLAN, "pr-1", status="shipped", reason="merged")
    assert ledger.load_progress(tmp_path, PLAN, "pr-1") is None
    # the terminal status itself must survive that same write
    assert ledger.unit_status(tmp_path, PLAN, "pr-1") == "shipped"


def test_clear_progress_leaves_the_terminal_status_alone(tmp_path):
    ledger.mark_unit_terminal(tmp_path, PLAN, "pr-1", status="shipped", reason="merged")
    ledger.save_progress(tmp_path, PLAN, "pr-1", {"phase": "review", "review_cycle": 1})
    ledger.clear_progress(tmp_path, PLAN, "pr-1")
    assert ledger.load_progress(tmp_path, PLAN, "pr-1") is None
    assert ledger.unit_status(tmp_path, PLAN, "pr-1") == "shipped"


def test_pr_url_hint_roundtrips(tmp_path):
    assert ledger.known_pr_url(tmp_path, PLAN, "pr-1") is None
    ledger.mark_pr_created(tmp_path, PLAN, "pr-1", "https://github.com/x/y/pull/1")
    assert ledger.known_pr_url(tmp_path, PLAN, "pr-1") == "https://github.com/x/y/pull/1"


def test_clear_wipes_dev_flag_and_every_unit_ledger(tmp_path):
    ledger.mark_dev_dispatched(tmp_path, PLAN, "pr-1")
    ledger.mark_dev_dispatched(tmp_path, PLAN, "pr-2")
    ledger.mark_unit_terminal(tmp_path, PLAN, "pr-1", status="shipped", reason="merged")
    ledger.mark_unit_terminal(tmp_path, PLAN, "pr-2", status="shipped", reason="merged")

    ledger.clear(tmp_path, PLAN, ["pr-1", "pr-2"])

    assert ledger.dev_already_dispatched(tmp_path, PLAN, "pr-1") is False
    assert ledger.dev_already_dispatched(tmp_path, PLAN, "pr-2") is False
    assert ledger.unit_status(tmp_path, PLAN, "pr-1") is None
    assert ledger.unit_status(tmp_path, PLAN, "pr-2") is None


def test_clear_does_not_touch_a_different_plan(tmp_path):
    ledger.mark_dev_dispatched(tmp_path, "plan-a", "pr-1")
    ledger.mark_unit_terminal(tmp_path, "plan-a", "pr-1", status="shipped", reason="merged")

    ledger.clear(tmp_path, PLAN, ["pr-1"])  # a different (unrelated) plan

    assert ledger.dev_already_dispatched(tmp_path, "plan-a", "pr-1") is True
    assert ledger.unit_status(tmp_path, "plan-a", "pr-1") == "shipped"


def test_clear_on_a_never_written_ledger_does_not_raise(tmp_path):
    ledger.clear(tmp_path, PLAN, ["pr-1", "pr-2"])  # nothing to clear -- must not raise


def test_a_corrupt_ledger_file_is_treated_as_absent(tmp_path):
    path = ledger.unit_dir(tmp_path, PLAN, "pr-1") / "ledger.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not valid json")
    assert ledger.dev_already_dispatched(tmp_path, PLAN, "pr-1") is False
    assert ledger.unit_status(tmp_path, PLAN, "pr-1") is None


def test_issue_number_hint_roundtrips(tmp_path):
    assert ledger.known_issue_number(tmp_path, PLAN, "pr-1") is None
    ledger.mark_issue_created(tmp_path, PLAN, "pr-1", 42)
    assert ledger.known_issue_number(tmp_path, PLAN, "pr-1") == 42


def test_unit_dir_is_namespaced_by_plan_then_stage(tmp_path):
    assert ledger.unit_dir(tmp_path, "my-plan", "pr-3") == tmp_path / ".reasona" / "log" / "dev" / "my-plan" / "pr-3"
