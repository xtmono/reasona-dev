from reasona_dev import cycles_log, cycles_query
from reasona_dev.finding_adapter import Disposition, Finding, ReviewResult, RoleStatus, Severity


def _f(path, symbol, contract):
    return Finding(
        disposition=Disposition.MUST_FIX, severity=Severity.HIGH, path=path,
        line=1, symbol=symbol, contract=contract, scenario="s", fix="x",
    )


def _log(workdir, unit, role, *findings, stage="review", cycle=1):
    cycles_log.record_dispatch(
        workdir=workdir, stage_name=unit, stage=stage, cycle=cycle,
        role=role, model="m", adapter="a",
        result=ReviewResult(role_status=RoleStatus.COMPLETE, findings=list(findings)),
    )


def test_empty_log_renders_without_crashing(tmp_path):
    assert "no records yet" in cycles_query.render(tmp_path)


def test_unique_findings_are_credited_to_the_only_role_that_found_them(tmp_path):
    _log(tmp_path, "pr-1", "reviewer", _f("a.rs", "x", "c1"))
    _log(tmp_path, "pr-1", "bugbot", _f("b.rs", "y", "c2"), stage="scan")

    by_role = {a.role: a for a in cycles_query.attribution(tmp_path)}
    assert by_role["reviewer"].unique == 1
    assert by_role["bugbot"].unique == 1
    assert by_role["reviewer"].duplicate == 0


def test_a_finding_two_roles_report_counts_as_duplicate_for_both(tmp_path):
    shared = _f("a.rs", "x", "same contract")
    _log(tmp_path, "pr-1", "reviewer", shared)
    _log(tmp_path, "pr-1", "bugbot", _f("a.rs", "x", "same contract"), stage="scan")

    by_role = {a.role: a for a in cycles_query.attribution(tmp_path)}
    assert by_role["reviewer"].duplicate == 1
    assert by_role["bugbot"].duplicate == 1
    assert by_role["reviewer"].unique == 0
    assert by_role["bugbot"].unique == 0
    # first-catch goes to whoever was dispatched first
    assert by_role["reviewer"].first_catch == 1
    assert by_role["bugbot"].first_catch == 0


def test_a_role_with_only_duplicates_is_the_drop_candidate(tmp_path):
    """The table has to support this conclusion directly, without
    interpretation -- that is its whole purpose."""
    for i in range(3):
        f = _f(f"f{i}.rs", "x", f"c{i}")
        _log(tmp_path, f"pr-{i}", "reviewer", f)
        _log(tmp_path, f"pr-{i}", "compliance", _f(f"f{i}.rs", "x", f"c{i}"), stage="scan")
    _log(tmp_path, "pr-9", "bugbot", _f("z.rs", "z", "only bugbot"), stage="scan")

    by_role = {a.role: a for a in cycles_query.attribution(tmp_path)}
    assert by_role["compliance"].unique == 0 and by_role["compliance"].duplicate == 3
    assert by_role["bugbot"].unique == 1


def test_budget_reports_cycles_used_and_terminal_reason(tmp_path):
    _log(tmp_path, "pr-1", "reviewer", _f("a.rs", "x", "c"), cycle=1)
    _log(tmp_path, "pr-1", "reviewer", _f("a.rs", "x", "c"), cycle=2)
    _log(tmp_path, "pr-1", "reviewer", _f("a.rs", "x", "c"), cycle=3)
    cycles_log.record_decision(
        workdir=tmp_path, stage_name="pr-1", stage="review", cycle=3,
        action="fail", reason="not converging",
    )
    cycles_log.record_decision(
        workdir=tmp_path, stage_name="pr-1", stage="review", cycle=2,
        action="spawn_fix_escalated", reason="recurring", escalated_model="opus",
    )

    b = cycles_query.budget(tmp_path)
    assert b.units == 1
    assert b.review_cycles["pr-1"] == 3
    assert b.escalations == 1
    assert b.terminal_reasons["not converging"] == 1


def test_acceptance_coverage_counts_declaring_units(tmp_path):
    _log(tmp_path, "pr-1", "reviewer")
    _log(tmp_path, "pr-2", "reviewer")
    cycles_log.record_acceptance(
        workdir=tmp_path, stage_name="pr-1", declared=True,
        results=[{"id": "AC-1", "passed": True}],
    )
    cycles_log.record_acceptance(workdir=tmp_path, stage_name="pr-2", declared=False, results=[])

    cov = cycles_query.acceptance_coverage(tmp_path)
    assert cov.units_total == 2
    assert cov.units_declaring == 1
    assert cov.units_passing == 1
    assert cov.coverage_pct == 50.0


def test_gate_vs_acceptance_four_way_split(tmp_path):
    # pr-1: review found something, acceptance passed -> gate_only
    _log(tmp_path, "pr-1", "reviewer", _f("a.rs", "x", "c"))
    cycles_log.record_acceptance(
        workdir=tmp_path, stage_name="pr-1", declared=True, results=[{"id": "A", "passed": True}]
    )
    # pr-2: review clean, acceptance failed -> acceptance_only
    _log(tmp_path, "pr-2", "reviewer")
    cycles_log.record_acceptance(
        workdir=tmp_path, stage_name="pr-2", declared=True, results=[{"id": "A", "passed": False}]
    )
    # pr-3: both clean -> neither
    _log(tmp_path, "pr-3", "reviewer")
    cycles_log.record_acceptance(
        workdir=tmp_path, stage_name="pr-3", declared=True, results=[{"id": "A", "passed": True}]
    )

    split = cycles_query.gate_vs_acceptance(tmp_path)
    assert split == {"gate_only": 1, "acceptance_only": 1, "both": 0, "neither": 1}


def test_units_without_declared_criteria_are_excluded_from_the_split(tmp_path):
    """A unit with no criteria cannot testify about what criteria would have
    caught; counting it would understate their value using plan coverage."""
    _log(tmp_path, "pr-1", "reviewer", _f("a.rs", "x", "c"))
    cycles_log.record_acceptance(workdir=tmp_path, stage_name="pr-1", declared=False, results=[])
    assert sum(cycles_query.gate_vs_acceptance(tmp_path).values()) == 0


def test_render_includes_every_section(tmp_path):
    _log(tmp_path, "pr-1", "reviewer", _f("a.rs", "x", "c"))
    cycles_log.record_acceptance(
        workdir=tmp_path, stage_name="pr-1", declared=True, results=[{"id": "A", "passed": True}]
    )
    out = cycles_query.render(tmp_path)
    assert "role attribution (exact)" in out
    assert "budget:" in out
    assert "acceptance coverage:" in out
    assert "gate vs acceptance" in out
    # the approximate proxy stays out unless explicitly asked for
    assert "APPROXIMATE" not in out


def test_effective_proxy_is_opt_in_and_labelled(tmp_path):
    _log(tmp_path, "pr-1", "reviewer", _f("a.rs", "x", "c"))
    out = cycles_query.render(tmp_path, include_effective=True)
    assert "APPROXIMATE" in out
    assert "base-rate caveat" in out
