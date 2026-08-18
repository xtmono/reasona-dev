You are RE-CHECKING a fix on PR <N> in <worktree_path> -- read ALL files from
this path only; do NOT read the main repo.

This is a BOUNDED recheck, not a fresh review. The fix that just landed
touched only files that were already named in the findings being confirmed,
so a new omission hunt is out of scope here and would only re-litigate
ground the previous full review already covered.

Do exactly two things:

1) **CONFIRM** -- for each finding listed below, determine whether the stated
   `contract` now holds. Re-run the `scenario` mentally against the current
   code. A finding whose contract still fails is a MUST_FIX again; report it
   with the SAME path and symbol so it is recognized as the same finding, and
   state in `contract:` what specifically still fails.

2) **REGRESSION** -- restricted to the files the fix touched, check whether
   the fix introduced a new defect: broken invariants in adjacent code paths,
   a now-unreachable branch, a changed signature whose other call sites were
   not updated, or an error path that no longer propagates. Anything found
   here is a new MUST_FIX.

Do NOT report findings outside those two categories. In particular, do not
raise style, naming, documentation, or coverage observations about code the
fix did not touch -- if it was acceptable before this fix, it is out of scope
now.

Report findings in this exact shape -- do not report a verdict, report
findings; section membership is what gates, not your judgment:

MUST_FIX:
- [CRITICAL|HIGH] path[:line] [symbol]
  || contract: <the requirement or invariant this violates>
  || scenario: <a concrete input/state that reproduces the failure>
  || fix: <the minimal correct change>

ADVISORY:
- [MEDIUM|LOW] path[:line] [symbol] -- <description>

If every listed finding is resolved and no regression is present, emit no
MUST_FIX items at all.

Last line (parsing anchor only -- the MUST_FIX/ADVISORY sections above are
authoritative, not this line): VERDICT: PASS or VERDICT: FAIL
[Worktree]: <worktree_path>  [Plan file]: <path>  [Current PR unit]: PR <N> -- <title>
