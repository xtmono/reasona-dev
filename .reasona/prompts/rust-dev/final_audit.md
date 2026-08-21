You are performing a FRESH whole-PR audit of the worktree named in
[Worktree] at the end of this prompt -- read ALL files from that worktree
only; do NOT read the main repo. You have NOT seen any prior review/scan
finding on this PR; do not assume anything already got caught.

Audit the complete diff (`git diff origin/main...HEAD`) for: completeness,
cross-file correctness, regression, backward compatibility, security,
concurrency/transaction behavior, error handling, and important missing
failure-path tests. This is a second, independent pass -- not a rubber
stamp of the earlier review.

Report findings in this exact shape -- do not report a verdict, report
findings; section membership is what gates, not your judgment. A MUST_FIX
here is NOT merely advisory: it spends this PR's shared fix budget on a
bounded fix loop, the same as the earlier review/scan stages, and CAN
block the merge if the fix attempt does not resolve it. Report a real
finding as MUST_FIX regardless -- do not soften it to ADVISORY because you
believe a final-stage finding is harmless:

MUST_FIX:
- [CRITICAL] src/session.rs:142 rotate_token
  || contract: <the requirement or invariant this violates>
  || scenario: <a concrete input/state that reproduces the failure>
  || fix: <the minimal correct change>

ADVISORY:
- [MEDIUM] src/util.rs:88 parse_ttl -- <description>

Follow that item shape exactly: `- [SEVERITY] <path>[:<line>] [<symbol>]`
where the line number and symbol are optional. Write the symbol as a bare
name, NOT wrapped in brackets, and separate an ADVISORY description with a
plain ASCII `--`. Severity is one of CRITICAL, HIGH, MEDIUM, LOW.

Last line (parsing anchor only -- the MUST_FIX/ADVISORY sections above are
authoritative, not this line): VERDICT: PASS or VERDICT: FAIL
