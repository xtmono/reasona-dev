"""Plan-level teardown reporting -- item 6 of the dev-ralf source-level
parity re-check (docs/ARCHITECTURE.md §3.14.7). Ports dev-ralf's
`completeness.py` (names a plan promised that the repo never got) and
`scope_report.py` (source files a unit touched but never declared). Both
report and never block.
"""

from reasona_dev import plan_report
from reasona_dev.final_phase import MERGED, TailResult
from reasona_dev.orchestrate import UnitOutcome
from reasona_dev.plan_compile import PRUnit


def _unit(index="1", section="", files=None):
    return PRUnit(index=index, title=f"unit {index}", section=section, files=files or [])


# --- completeness ---------------------------------------------------------

def test_a_promised_name_that_exists_is_not_reported(tmp_path):
    (tmp_path / "src.py").write_text("def resolve_flow_part():\n    pass\n")
    units = [_unit(section="Add `resolve_flow_part` to the resolver.")]
    r = plan_report.completeness(tmp_path, units)
    assert r.clean
    assert r.checked == 1


def test_a_promised_name_the_repo_never_got_is_reported(tmp_path):
    (tmp_path / "src.py").write_text("def something_else():\n    pass\n")
    units = [_unit(section="Add `resolve_flow_part` to the resolver.")]
    r = plan_report.completeness(tmp_path, units)
    assert not r.clean
    assert r.absent["1"] == ["resolve_flow_part"]


def test_a_promised_file_path_counts_as_present_when_the_path_exists(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "thing.py").write_text("x = 1\n")
    units = [_unit(section="Create `pkg/thing.py`.")]
    assert plan_report.completeness(tmp_path, units).clean


def test_a_name_present_only_in_another_units_output_still_counts(tmp_path):
    """Plan-level, not per-PR: a plan legitimately names something a LATER
    unit builds, which is exactly why dev-ralf abandoned the per-PR variant
    (19.7% flagged, 0 real findings)."""
    (tmp_path / "src.py").write_text("def built_by_unit_two():\n    pass\n")
    units = [
        _unit(index="1", section="Unit 2 will add `built_by_unit_two`."),
        _unit(index="2", section="Add `built_by_unit_two`."),
    ]
    assert plan_report.completeness(tmp_path, units).clean


def test_plan_documents_are_excluded_from_the_corpus(tmp_path):
    """A name appearing ONLY in the plan that promised it is the absence
    being looked for -- the plan file must not be its own evidence."""
    plans = tmp_path / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "p.md").write_text("Add `never_implemented_thing`.\n")
    units = [_unit(section="Add `never_implemented_thing`.")]
    r = plan_report.completeness(tmp_path, units)
    assert r.absent["1"] == ["never_implemented_thing"]


def test_the_plan_file_itself_is_excluded_even_outside_docs_plans(tmp_path):
    """reasona-dev takes an arbitrary `--plan` path, so the `docs/plans/`
    prefix dev-ralf hardcodes is not enough: a plan kept anywhere else was
    its own evidence and made this report silently always clean. Caught by
    running the reporter against a real repo, not by the fixture above
    (which happened to use the conventional directory)."""
    plan = tmp_path / "plan.md"
    plan.write_text("Add `never_built_thing`.\n")
    (tmp_path / "src.py").write_text("x = 1\n")
    units = [_unit(section="Add `never_built_thing`.")]

    assert plan_report.completeness(tmp_path, units).clean  # without the exclusion: wrong
    r = plan_report.completeness(tmp_path, units, plan_path=plan)
    assert r.absent["1"] == ["never_built_thing"]


def test_prose_and_short_tokens_are_not_treated_as_promised_names(tmp_path):
    (tmp_path / "src.py").write_text("x = 1\n")
    units = [_unit(section="Use `the parser` and `a` and `ok` carefully.")]
    r = plan_report.completeness(tmp_path, units)
    assert r.checked == 0
    assert r.clean


# --- scope divergence -----------------------------------------------------

def _outcome(stage_name, declared, changed):
    return UnitOutcome(
        stage_name=stage_name, profile="rust-dev", status="shipped", reason="ok",
        unit=_unit(index=stage_name, files=declared),
        tail=TailResult(stage_name=stage_name, status=MERGED, reason="ok", changed_files=changed),
    )


def test_a_unit_that_stayed_inside_its_declared_files_is_clean():
    r = plan_report.scope_divergence([_outcome("pr-1", ["src/a.py"], ["src/a.py"])])
    assert r.clean
    assert r.measured_units == 1


def test_a_unit_that_touched_an_undeclared_source_file_is_reported():
    r = plan_report.scope_divergence([_outcome("pr-1", ["src/a.py"], ["src/a.py", "src/b.py"])])
    assert not r.clean
    assert r.undeclared["pr-1"] == ["src/b.py"]
    assert r.undeclared_units == 1


def test_an_undeclared_NON_source_file_is_not_reported():
    """dev-ralf's scope report is about source scope; a README or config
    touched alongside is not the signal."""
    r = plan_report.scope_divergence([_outcome("pr-1", ["src/a.py"], ["src/a.py", "README.md"])])
    assert r.clean


def test_a_unit_with_no_recorded_changed_files_is_counted_out_not_clean():
    """A unit that never reached the merge step has nothing to compare --
    counting it as clean would overstate the coverage of this report."""
    out = UnitOutcome(stage_name="pr-1", profile="rust-dev", status="failed", reason="x",
                      unit=_unit(files=["src/a.py"]))
    r = plan_report.scope_divergence([out])
    assert r.measured_units == 0
    assert r.clean


def test_a_unit_that_declared_nothing_reports_every_source_file_it_touched():
    r = plan_report.scope_divergence([_outcome("pr-1", [], ["src/a.py"])])
    assert r.undeclared["pr-1"] == ["src/a.py"]


# --- render / build -------------------------------------------------------

def test_render_names_both_axes_when_clean(tmp_path):
    (tmp_path / "src.py").write_text("def foo_bar():\n    pass\n")
    out = plan_report.render(
        plan_report.completeness(tmp_path, [_unit(section="Add `foo_bar`.")]),
        plan_report.scope_divergence([_outcome("pr-1", ["src/a.py"], ["src/a.py"])]),
    )
    assert "all present" in out
    assert "none touched an undeclared source file" in out


def test_render_lists_the_offenders_when_not_clean(tmp_path):
    (tmp_path / "src.py").write_text("x = 1\n")
    out = plan_report.render(
        plan_report.completeness(tmp_path, [_unit(section="Add `missing_symbol`.")]),
        plan_report.scope_divergence([_outcome("pr-1", ["src/a.py"], ["src/a.py", "src/b.py"])]),
    )
    assert "missing_symbol" in out
    assert "src/b.py" in out


def test_build_never_raises_even_on_a_broken_input(tmp_path):
    """This runs after every unit has already merged -- a reporting bug
    must not turn a finished run into a traceback."""
    out = plan_report.build(tmp_path, [object()], [object()])
    assert isinstance(out, str)
    assert out  # either a real report or a "skipped (...)" line, never an exception
