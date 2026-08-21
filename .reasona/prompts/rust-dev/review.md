You are reviewing the PR unit named in [Current PR unit]/[Worktree] at the
end of this prompt -- read ALL files from that worktree only; do NOT read
the main repo.

Evaluate PR unit completion:
1) project rules (AGENTS.md / agent-guide.md, or this project's equivalent
   contributor conventions doc)
2) docs/*.md updates
3) test coverage
4) **COMPLETENESS** -- enumerate EVERY checklist item (`- [ ]`) and every
   file/symbol named in the plan's `## PR <N>:` section, then confirm each
   is actually implemented in THIS worktree's diff. Any listed item / file
   / symbol that is missing, stubbed, or only partially done is a MUST_FIX.
   List each unimplemented item explicitly.
5) **SECURITY-SENSITIVE INFO** -- grep the diff (code AND docs/comments, not
   just new prose) for a concrete IP address, hostname, username, password,
   or API key/token. A "measured example" / curl transcript comment is
   exactly where these leak in from local testing -- do NOT wave it through
   as documentation. MUST_FIX; the fix is a `.env`-style alias or a
   placeholder, not deleting the example's meaning. Cite `file:line` and the
   concrete value found.

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

(This prompt's actual [Worktree] / [Current PR unit] values are appended
below, after everything above.)
