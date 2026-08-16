from reasona_dev.finding_adapter import (
    Disposition,
    RoleStatus,
    merge,
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
