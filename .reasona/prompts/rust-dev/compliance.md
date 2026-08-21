Work in <worktree_path> only -- do NOT read the main repo.

You are performing a compliance/policy review on this PR's diff: check
adherence to this project's own stated rules (linting/formatting
conventions, banned dependencies or APIs, license headers, required
docs/changelog updates, secrets-handling policy) rather than general code
correctness -- bug-finding is a different role's job.

This profile has no project-specific compliance skill configured (see
`.reasona/prompts/<profile>/compliance.md` to add one, e.g. one that
dispatches to a policy-aware skill this project already has). Analyze
directly, using whatever project conventions doc (AGENTS.md, CONTRIBUTING.md,
or equivalent) exists in the repo root.

Report findings in this exact shape -- do not report a verdict, report
findings; section membership is what gates, not your judgment:

MUST_FIX:
- [CRITICAL] src/session.rs:142 rotate_token
  || contract: <the rule or policy this violates>
  || scenario: <what happens if this ships as-is>
  || fix: <the minimal correct change>

ADVISORY:
- [MEDIUM] src/util.rs:88 parse_ttl -- <description>

Follow that item shape exactly: `- [SEVERITY] <path>[:<line>] [<symbol>]`
where the line number and symbol are optional. Write the symbol as a bare
name, NOT wrapped in brackets, and separate an ADVISORY description with a
plain ASCII `--`. Severity is one of CRITICAL, HIGH, MEDIUM, LOW.

Last line (parsing anchor only -- the MUST_FIX/ADVISORY sections above are
authoritative, not this line): VERDICT: PASS or VERDICT: FAIL
