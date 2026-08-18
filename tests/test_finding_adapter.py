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
