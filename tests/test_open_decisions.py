from reasona_dev.open_decisions import entry_summary, undecided_entries

PLAN_NO_SECTION = """\
---
pr_units:
  - index: 1
    title: x
---

## PR 1: x
"""

PLAN_ALL_DECIDED = PLAN_NO_SECTION + """
## Open decisions (human)

- [key: rejection-delta] Does the category axis inherit the join axis's rejection contract?
  - Options: (i) accept the tightening (ii) WARN instead of a hard error
  - Default if unresolved: (i)
  - decided: (i) -- nothing breaks today
"""

PLAN_ONE_UNDECIDED = PLAN_NO_SECTION + """
## Open decisions (human)

- [key: rejection-delta] Does the category axis inherit the join axis's rejection contract?
  - Options: (i) accept the tightening (ii) WARN instead of a hard error
  - Default if unresolved: (i)
"""

PLAN_MIXED = PLAN_NO_SECTION + """
## Open decisions (human)

- [key: a] First question, already decided.
  - decided: yes
- [key: b] Second question, still open.
  - Default if unresolved: no
"""

PLAN_TABLE_FORM = PLAN_NO_SECTION + """
## Open decisions (human)

| decision | options |
| --- | --- |
| Does X inherit Y | accept / warn / drop |
"""


def test_no_section_at_all_has_nothing_undecided():
    assert undecided_entries(PLAN_NO_SECTION) == []


def test_a_fully_decided_section_has_nothing_undecided():
    assert undecided_entries(PLAN_ALL_DECIDED) == []


def test_an_entry_missing_decided_is_reported():
    undecided = undecided_entries(PLAN_ONE_UNDECIDED)
    assert len(undecided) == 1
    assert "rejection-delta" in undecided[0]


def test_only_the_undecided_entry_is_reported_not_the_decided_one():
    undecided = undecided_entries(PLAN_MIXED)
    assert len(undecided) == 1
    assert "Second question" in undecided[0]
    assert "First question" not in undecided[0]


def test_a_markdown_table_is_invisible_to_this_parser_same_as_reasona_plans():
    """Column-0 table rows are not entries -- reasona-plan's own
    check_plan._open_decisions() rejects this shape for the identical
    reason: a decision written as a table row is invisible to the parser
    and goes uncounted by both sides of the contract. This gate only sees
    genuine bullet entries, so a table-form section reports NOTHING
    undecided here (plan-ralf's own format gate is what rejects the table
    shape outright, before this gate ever runs)."""
    assert undecided_entries(PLAN_TABLE_FORM) == []


def test_entry_summary_strips_the_key_tag_and_bullet_marker():
    undecided = undecided_entries(PLAN_ONE_UNDECIDED)
    summary = entry_summary(undecided[0])
    assert summary.startswith("Does the category axis")
    assert "[key:" not in summary
