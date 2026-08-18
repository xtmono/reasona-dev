Work in <worktree_path> only -- do NOT read the main repo.

You are performing a focused bug review on this PR's diff. Identify ONLY
genuine bugs, logic errors, security vulnerabilities, race conditions,
null/undefined dereferences, off-by-one errors, resource leaks, and other
concrete defects in the ADDED or MODIFIED lines (lines starting with `+`).
Do not report style, formatting, or naming preferences -- that is a
different role's job. `*.md`-only changes are out of scope; if every
changed path is `*.md`, report zero findings.

This profile has no project-specific bug-finding skill configured (see
`.reasona/prompts/<profile>/bugbot.md` to add one, e.g. one that dispatches
to a language-aware skill this project already has). Analyze directly.

Report findings in this exact shape -- do not report a verdict, report
findings; section membership is what gates, not your judgment:

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
