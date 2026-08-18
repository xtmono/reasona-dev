import pytest

from reasona_dev.finding_adapter import (
    Disposition,
    RoleStatus,
    Severity,
    merge,
    parse_kv_contract,
    parse_ocr_result,
    parse_text_contract,
)

SAMPLE = """\
MUST_FIX:
- [HIGH] src/session.rs:142 rotate_token
  || contract: the previous refresh token must be rejected after rotation
  || scenario: two requests refresh successfully with the same token
  || fix: include the previous-token state in the atomic update condition

ADVISORY:
- [MEDIUM] src/util.rs:88 parse_ttl
  || note: boundary handling is missing

VERDICT: FAIL
"""


def test_parses_must_fix_and_advisory():
    r = parse_text_contract(SAMPLE)
    assert r.gate() == "FIX_REQUIRED"
    assert len(r.must_fix) == 1
    assert len(r.advisory) == 1
    f = r.must_fix[0]
    assert f.path == "src/session.rs"
    assert f.line == 142
    assert f.symbol == "rotate_token"
    assert f.contract and f.scenario and f.fix
    assert f.is_evidence_complete()


def test_verdict_is_anchor_not_authority():
    text = SAMPLE.replace("VERDICT: FAIL", "VERDICT: PASS")  # deliberately wrong
    r = parse_text_contract(text)
    assert r.gate() == "FIX_REQUIRED"  # section membership wins
    assert r.contract_mismatch is True


def test_key_excludes_line_number():
    a = parse_text_contract(SAMPLE).must_fix[0]
    shifted = SAMPLE.replace("session.rs:142", "session.rs:200")
    b = parse_text_contract(shifted).must_fix[0]
    assert a.key() == b.key()


def test_incomplete_evidence_flagged_not_downgraded():
    text = """\
MUST_FIX:
- [CRITICAL] src/x.rs:1 foo
  || contract: something

VERDICT: FAIL
"""
    r = parse_text_contract(text)
    f = r.must_fix[0]
    assert f.contract_incomplete is True
    assert f.disposition is Disposition.MUST_FIX  # never silently downgraded


def test_pass_empty():
    r = parse_text_contract("VERDICT: PASS\n")
    assert r.gate() == "PASS"


def test_ocr_failed_becomes_inconclusive_not_finding():
    payload = {"status": "success", "comments": [], "failed": [{"path": "x.rs", "classification": "timeout"}]}
    r = parse_ocr_result(payload)
    assert r.role_status is RoleStatus.INCONCLUSIVE
    assert r.findings == []


def test_ocr_severity_maps_to_disposition():
    payload = {
        "status": "success",
        "comments": [
            {"path": "a.rs", "start_line": 1, "content": "x", "severity": "high"},
            {"path": "b.rs", "start_line": 2, "content": "y", "severity": "low"},
        ],
    }
    r = parse_ocr_result(payload)
    assert len(r.must_fix) == 1
    assert len(r.advisory) == 1


def test_merge_any_must_fix_blocks_all():
    passing = parse_text_contract("VERDICT: PASS\n")
    failing = parse_text_contract(SAMPLE)
    merged = merge(passing, failing)
    assert merged.gate() == "FIX_REQUIRED"


def test_merge_inconclusive_dominates():
    inc = parse_ocr_result({"status": "success", "comments": [], "failed": [{"path": "x", "classification": "t"}]})
    passing = parse_text_contract("VERDICT: PASS\n")
    merged = merge(inc, passing)
    assert merged.role_status is RoleStatus.INCONCLUSIVE


KV_SAMPLE = """\
=== ext-bugbot RESULT ===
VERDICT: FAIL
COUNT_BLOCKING=1
COUNT_NON_BLOCKING=1
COUNT_CRITICAL=0
COUNT_HIGH=1
COUNT_MEDIUM=1
COUNT_LOW=0
BLOCKING_JSON=[{"file": "src/session.rs", "line": 142, "severity": "high", "title": "token reuse", "description": "the previous refresh token is not rejected after rotation", "additional_locations": []}]
NON_BLOCKING_JSON=[{"file": "src/util.rs", "line": 88, "severity": "medium", "title": "boundary handling", "description": "TTL boundary is off by one", "additional_locations": []}]
=== END ===
"""


def test_kv_contract_parses_blocking_and_non_blocking():
    r = parse_kv_contract(KV_SAMPLE)
    assert r.role_status is RoleStatus.COMPLETE
    assert len(r.must_fix) == 1
    assert len(r.advisory) == 1
    mf = r.must_fix[0]
    assert mf.path == "src/session.rs"
    assert mf.line == 142
    assert mf.severity == Severity.HIGH
    assert mf.disposition is Disposition.MUST_FIX


def test_kv_contract_gate_is_fix_required():
    assert parse_kv_contract(KV_SAMPLE).gate() == "FIX_REQUIRED"


def test_kv_contract_must_fix_marked_contract_incomplete():
    # worker.md's KV wire shape has no contract/scenario/fix breakdown --
    # every MUST_FIX from it is inherently missing that evidence.
    mf = parse_kv_contract(KV_SAMPLE).must_fix[0]
    assert mf.contract_incomplete is True


def test_kv_contract_zero_findings_pass():
    text = (
        "=== ext-bugbot RESULT ===\n"
        "VERDICT: PASS\n"
        "COUNT_BLOCKING=0\n"
        "COUNT_NON_BLOCKING=0\n"
        "BLOCKING_JSON=[]\n"
        "NON_BLOCKING_JSON=[]\n"
        "=== END ===\n"
    )
    r = parse_kv_contract(text)
    assert r.gate() == "PASS"
    assert r.findings == []


def test_kv_contract_missing_block_is_error_not_pass():
    # worker.md -> *RESULT parsing*: "Missing block ... -> cycle FAIL" --
    # must never silently read as a clean pass.
    r = parse_kv_contract("some garbled or truncated output with no RESULT block\n")
    assert r.role_status is RoleStatus.ERROR
    assert r.gate() == "ERROR"


def test_kv_contract_verdict_mismatch_recorded_not_trusted():
    text = (
        "=== ext-bugbot RESULT ===\n"
        "VERDICT: PASS\n"  # disagrees with a non-empty BLOCKING_JSON below
        "BLOCKING_JSON=[{\"file\": \"a.rs\", \"line\": 1, \"severity\": \"critical\", "
        "\"title\": \"x\", \"description\": \"y\", \"additional_locations\": []}]\n"
        "NON_BLOCKING_JSON=[]\n"
        "=== END ===\n"
    )
    r = parse_kv_contract(text)
    assert r.contract_mismatch is True
    assert r.gate() == "FIX_REQUIRED"  # section membership wins, not VERDICT


def test_kv_contract_merges_with_text_contract_reviewer():
    # bugbot (kv) and review (text contract) findings merge through the same
    # deterministic merge() -- any MUST_FIX from either blocks.
    bugbot = parse_kv_contract(KV_SAMPLE)
    review = parse_text_contract("VERDICT: PASS\n")
    merged = merge(bugbot, review)
    assert merged.gate() == "FIX_REQUIRED"
    assert len(merged.must_fix) == 1


# --- contract detection (parse_role_output) ---------------------------------

_TEXT_OUT = """MUST_FIX:

ADVISORY:
- [LOW] src/a.py -- minor

VERDICT: PASS
"""

_KV_OUT = (
    "=== ext-bugbot RESULT ===\n"
    "VERDICT: PASS\nBLOCKING_JSON=[]\nNON_BLOCKING_JSON=[]\n=== END ===\n"
)


def test_text_contract_is_detected_and_not_read_as_kv():
    """Live regression: the packaged bugbot/compliance prompts ask for the
    text contract, the role->parser table sent them to the KV parser, which
    correctly reported "missing block" as ERROR -- aborting the whole scan
    on output that was perfectly well formed."""
    from reasona_dev.finding_adapter import parse_role_output

    r = parse_role_output(_TEXT_OUT)
    assert r.role_status is RoleStatus.COMPLETE
    assert r.gate() == "PASS_WITH_NOTES"
    assert len(r.advisory) == 1


def test_kv_contract_is_still_detected():
    from reasona_dev.finding_adapter import parse_role_output

    r = parse_role_output(_KV_OUT)
    assert r.role_status is RoleStatus.COMPLETE
    assert r.gate() == "PASS"


def test_a_malformed_kv_block_still_fails_as_kv_not_reread_as_prose():
    """Detection must not turn a broken KV block into a silent text parse --
    a `=== RESULT ===` header with no JSON arrays is a parse failure, and
    worker.md says a missing block is a cycle FAIL, never 'zero findings'."""
    from reasona_dev.finding_adapter import parse_role_output

    broken = "=== ext-bugbot RESULT ===\nVERDICT: PASS\n=== END ===\n"
    assert parse_role_output(broken).role_status is RoleStatus.ERROR


def test_detection_keys_off_literal_markers_only():
    from reasona_dev.finding_adapter import parse_role_output

    # prose that merely mentions the marker word is still text
    prosey = "MUST_FIX:\n\nADVISORY:\n- [LOW] a.py -- mentions BLOCKING_JSON in passing\n\nVERDICT: PASS\n"
    assert parse_role_output(prosey).role_status is RoleStatus.COMPLETE


# --- renderings observed in real reviewer output -----------------------------

def test_a_bracketed_symbol_is_not_dropped():
    """LIVE REGRESSION, and the worst failure this parser can have: a reviewer
    correctly reported a missing function as CRITICAL, wrote the symbol as
    `[delete]` because the prompt's own notation spells the optional field
    `[symbol]`, the line failed to match, and the cycle recorded
    `gate=PASS mf=0` -- a false PASS on a review that had found real
    missing code. Only the acceptance gate caught it."""
    out = (
        "MUST_FIX:\n"
        "- [CRITICAL] src/store.py [delete]\n"
        "  || contract: delete() must exist\n"
        "  || scenario: AttributeError on import\n"
        "  || fix: add it\n"
        "\nVERDICT: FAIL\n"
    )
    r = parse_text_contract(out)
    assert r.gate() == "FIX_REQUIRED"
    assert len(r.must_fix) == 1
    assert r.must_fix[0].symbol == "delete"      # brackets stripped, not kept
    assert r.must_fix[0].path == "src/store.py"


def test_an_em_dash_separates_an_advisory_description():
    """Same live output, second dropped finding: the model used `—` where the
    prompt shows `--`."""
    for dash in ("--", "—", "–"):
        out = f"MUST_FIX:\n\nADVISORY:\n- [LOW] src/store.py {dash} no dedicated test file\n\nVERDICT: PASS\n"
        r = parse_text_contract(out)
        assert len(r.advisory) == 1, dash
        assert r.advisory[0].note == "no dedicated test file"


def test_bare_symbol_and_ascii_dash_still_parse():
    """The documented shape must keep working -- the tolerances are additive."""
    out = (
        "MUST_FIX:\n- [HIGH] src/a.rs:10 rotate_token\n"
        "  || contract: c\n  || scenario: s\n  || fix: f\n"
        "\nADVISORY:\n- [MEDIUM] src/b.rs:88 parse_ttl -- boundary handling\n\nVERDICT: FAIL\n"
    )
    r = parse_text_contract(out)
    assert r.must_fix[0].symbol == "rotate_token"
    assert r.must_fix[0].line == 10
    assert r.advisory[0].note == "boundary handling"


def test_a_bracketed_symbol_with_an_em_dash_note_parses():
    """Both tolerances at once, which is how they actually arrived."""
    out = "MUST_FIX:\n\nADVISORY:\n- [LOW] src/a.py [helper] — could be simpler\n\nVERDICT: PASS\n"
    r = parse_text_contract(out)
    assert r.advisory[0].symbol == "helper"
    assert r.advisory[0].note == "could be simpler"


def test_prompts_no_longer_teach_the_bracketed_notation():
    """The parser now tolerates it, but the prompt should not be the thing
    producing it -- a meta-notation a model can reproduce literally is a
    defect in the prompt, not just in the parser."""
    from pathlib import Path

    for md in sorted((Path(__file__).resolve().parent.parent / ".reasona" / "prompts" / "generic").glob("*.md")):
        text = md.read_text()
        if "MUST_FIX:" not in text:
            continue
        assert "path[:line] [symbol]" not in text, f"{md.name} still shows the ambiguous notation"
        assert "NOT wrapped in brackets" in text, f"{md.name} does not state the symbol rule"


# --- never drop silently -----------------------------------------------------

def test_a_markdown_section_heading_is_recognized():
    """LIVE REGRESSION: a reviewer wrote `## MUST_FIX`. The strict
    `MUST_FIX:` form did not match, so no disposition was ever set and EVERY
    item under it was dropped -- the role reported PASS with two findings
    sitting in its own output."""
    out = "## MUST_FIX\n\n- [CRITICAL] src/a.py\n\n## ADVISORY\n\n- [LOW] src/b.py\n\nVERDICT: FAIL\n"
    r = parse_text_contract(out)
    assert r.gate() == "FIX_REQUIRED"
    assert len(r.must_fix) == 1 and len(r.advisory) == 1


@pytest.mark.parametrize("line,expected_path", [
    ("- [HIGH] src/store.py:9-10 [keys function]", "src/store.py:9-10"),
    ("- [HIGH] src/store.py (no test file)", "src/store.py"),
    ("- [CRITICAL] src/a.py :: something odd", "src/a.py"),
])
def test_an_unparseable_item_still_becomes_a_finding(line, expected_path):
    """Widening the strict shape one rendering at a time does not converge --
    one live run produced a bracketed symbol WITH a space, a parenthetical, a
    line RANGE and an em dash. For a gate whose failure mode is a false PASS,
    "I could not parse this" must never render as "there was nothing here"."""
    r = parse_text_contract(f"MUST_FIX:\n{line}\n\nVERDICT: FAIL\n")
    assert r.gate() == "FIX_REQUIRED"
    assert len(r.must_fix) == 1
    f = r.must_fix[0]
    assert f.path == expected_path
    assert f.contract_incomplete is True   # evidence was not extractable
    assert f.raw == line                   # nothing thrown away


def test_section_membership_still_decides_disposition_for_loose_items():
    """An unparsed line in ADVISORY must NOT block; one in MUST_FIX must."""
    out = "MUST_FIX:\n\nADVISORY:\n- [LOW] src/a.py (some prose)\n\nVERDICT: PASS\n"
    r = parse_text_contract(out)
    assert r.gate() == "PASS_WITH_NOTES"
    assert len(r.must_fix) == 0 and len(r.advisory) == 1


def test_a_well_formed_item_still_takes_the_structured_path():
    """The catch-all is a fallback, not a replacement -- path/line/symbol and
    the evidence fields must still be extracted when they are there."""
    out = (
        "MUST_FIX:\n- [HIGH] src/a.rs:10 rotate_token\n"
        "  || contract: c\n  || scenario: s\n  || fix: f\n\nVERDICT: FAIL\n"
    )
    f = parse_text_contract(out).must_fix[0]
    assert (f.path, f.line, f.symbol) == ("src/a.rs", 10, "rotate_token")
    assert f.contract_incomplete is False


def test_a_severity_line_outside_any_section_is_not_invented_into_a_finding():
    """The catch-all keys off section membership; prose elsewhere in the
    report must not become findings."""
    out = "Some preamble mentioning - [HIGH] nothing in particular\n\nVERDICT: PASS\n"
    assert parse_text_contract(out).gate() == "PASS"
