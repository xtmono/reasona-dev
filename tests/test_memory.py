from reasona_dev import cycles_log, memory
from reasona_dev.finding_adapter import Disposition, Finding, ReviewResult, RoleStatus, Severity


def _log(workdir, unit, *findings, role="reviewer"):
    cycles_log.record_dispatch(
        workdir=workdir, stage_name=unit, stage="review", cycle=1,
        role=role, model="opus", adapter="claude",
        result=ReviewResult(role_status=RoleStatus.COMPLETE, findings=list(findings)),
    )


def _f(path, symbol="foo", contract="missing negative test", disposition=Disposition.MUST_FIX):
    return Finding(
        disposition=disposition, severity=Severity.HIGH, path=path, line=1,
        symbol=symbol, contract=contract, scenario="s", fix="x",
    )


def test_single_occurrence_is_not_a_memory(tmp_path):
    _log(tmp_path, "pr-1", _f("crates/flow/src/a.rs"))
    assert memory.derive(tmp_path) == []


def test_pattern_across_two_units_becomes_a_memory(tmp_path):
    _log(tmp_path, "pr-1", _f("crates/flow/src/a.rs"))
    _log(tmp_path, "pr-2", _f("crates/flow/src/a.rs"))
    names = [m.name for m in memory.derive(tmp_path)]
    assert any(n.startswith("recurring-location-") for n in names)
    assert any(n.startswith("recurring-contract-") for n in names)


def test_repeats_within_one_unit_do_not_count(tmp_path):
    """Recurrence means across PR units -- three cycles of the same unit is
    one PR failing to converge, which RecurrenceTracker already handles."""
    for cycle in (1, 2, 3):
        cycles_log.record_dispatch(
            workdir=tmp_path, stage_name="pr-1", stage="review", cycle=cycle,
            role="reviewer", model="opus", adapter="claude",
            result=ReviewResult(role_status=RoleStatus.COMPLETE, findings=[_f("crates/flow/src/a.rs")]),
        )
    assert memory.derive(tmp_path) == []


def test_advisory_findings_are_not_memories(tmp_path):
    _log(tmp_path, "pr-1", _f("crates/flow/src/a.rs", disposition=Disposition.ADVISORY))
    _log(tmp_path, "pr-2", _f("crates/flow/src/a.rs", disposition=Disposition.ADVISORY))
    assert memory.derive(tmp_path) == []


def test_contract_grouping_is_scoped_to_a_directory(tmp_path):
    _log(tmp_path, "pr-1", _f("crates/flow/src/a.rs", symbol="a"))
    _log(tmp_path, "pr-2", _f("crates/flow/src/b.rs", symbol="b"))
    mems = [m for m in memory.derive(tmp_path) if m.name.startswith("recurring-contract-")]
    assert len(mems) == 1
    assert mems[0].scope_files == ["crates/flow/src"]
    # the two locations differ, so only the contract clusters -- no
    # location memory is invented
    assert not [m for m in memory.derive(tmp_path) if m.name.startswith("recurring-location-")]


def test_paraphrase_is_not_clustered(tmp_path):
    """Under-clustering is deliberate: a wrong grouping misdirects the next
    reviewer, which costs more than missing a pattern."""
    _log(tmp_path, "pr-1", _f("crates/flow/src/a.rs", symbol="a", contract="missing negative test"))
    _log(tmp_path, "pr-2", _f("crates/flow/src/b.rs", symbol="b", contract="the negative case is untested"))
    assert memory.derive(tmp_path) == []


def test_window_makes_decay_automatic(tmp_path):
    _log(tmp_path, "pr-1", _f("crates/flow/src/a.rs"))
    _log(tmp_path, "pr-2", _f("crates/flow/src/a.rs"))
    assert memory.derive(tmp_path, window_units=10)

    for i in range(3, 8):
        _log(tmp_path, f"pr-{i}", _f("crates/other/src/z.rs", symbol="z", contract="different thing"))

    # only the last 3 units are considered -- the old pattern falls out
    assert not [
        m for m in memory.derive(tmp_path, window_units=3)
        if "crates-flow" in m.name or "flow" in "".join(m.scope_files)
    ]


def test_regenerate_removes_files_that_no_longer_apply(tmp_path):
    _log(tmp_path, "pr-1", _f("crates/flow/src/a.rs"))
    _log(tmp_path, "pr-2", _f("crates/flow/src/a.rs"))
    memory.regenerate(tmp_path)
    assert list(memory.memory_dir(tmp_path).glob("*.md"))

    stale = memory.memory_dir(tmp_path) / "recurring-location-hand-written.md"
    stale.write_text("---\nname: x\ndescription: y\n---\n\nbody\n")
    memory.regenerate(tmp_path)
    assert not stale.exists()  # generation owns this directory


def test_roundtrip_through_disk_preserves_scope_and_observations(tmp_path):
    _log(tmp_path, "pr-1", _f("crates/flow/src/a.rs"))
    _log(tmp_path, "pr-2", _f("crates/flow/src/a.rs"))
    memory.regenerate(tmp_path)

    loaded = memory.load_all(tmp_path)
    assert loaded
    location = [m for m in loaded if m.name.startswith("recurring-location-")][0]
    assert location.scope_files == ["crates/flow/src"]
    assert location.observed == ["pr-1", "pr-2"]


def test_selection_is_file_scoped(tmp_path):
    _log(tmp_path, "pr-1", _f("crates/flow/src/a.rs"))
    _log(tmp_path, "pr-2", _f("crates/flow/src/a.rs"))
    memory.regenerate(tmp_path)

    assert memory.select(tmp_path, ["crates/flow/src/other.rs"])
    assert memory.select(tmp_path, ["crates/unrelated/src/x.rs"]) == []
    assert memory.select(tmp_path, []) == []


def test_selection_is_capped(tmp_path):
    for i in range(1, 9):
        _log(tmp_path, f"pr-{i}", _f(f"crates/flow/src/f{i}.rs", symbol=f"s{i}", contract=f"c{i}"))
        _log(tmp_path, f"pr-{i}b", _f(f"crates/flow/src/f{i}.rs", symbol=f"s{i}", contract=f"c{i}"))
    memory.regenerate(tmp_path, window_units=100)

    picked = memory.select(tmp_path, [f"crates/flow/src/f{i}.rs" for i in range(1, 9)], limit=3)
    assert len(picked) == 3


def test_prompt_block_is_empty_when_nothing_selected():
    assert memory.render_for_prompt([]) == ""


def test_prompt_block_is_framed_as_evidence_not_instruction(tmp_path):
    _log(tmp_path, "pr-1", _f("crates/flow/src/a.rs"))
    _log(tmp_path, "pr-2", _f("crates/flow/src/a.rs"))
    memory.regenerate(tmp_path)
    block = memory.render_for_prompt(memory.select(tmp_path, ["crates/flow/src/a.rs"]))
    assert "NOT a checklist" in block
    assert "PRIOR OBSERVATIONS" in block
