"""Ports `/gh-review`'s watcher (`~/repository/tas-dev-plugins/plugins/dev/
skills/gh-review/tools/watch.py`) -- near verbatim. The original is already
pure Python + `gh api graphql` subprocess calls with zero LLM involvement
(a deterministic classifier over GraphQL JSON), so it needed no redesign to
fit this project's own "no model in the judgment loop" rule -- only its
subprocess helper was swapped for `reasona_dev._shell.run()`. `watch.py`'s
own `main()` (argparse entry point AND the polling `while True` loop with
its interval/budget bookkeeping) was NOT ported here: `reasona_dev.gh_review`
calls `take_snapshot()`/`classify()` as a library and owns the polling loop
itself, using this project's own budget primitives
(`cycle_gate.FixBudget`'s `"gh_review"` stage) rather than a standalone
script's own `--max-wait`/`--interval` bookkeeping.

**Why this exists as a separate module from `pr_cycle`'s own bugbot/
compliance dispatch.** `pr_cycle.py`'s scan cycle runs bugbot/compliance as
LOCAL Bernstein dispatches, before a PR ever exists. The signals this module
watches -- `statusCheckRollup`, a `claude[bot]`-family PR comment, a
`github-actions[bot]`-family PR review -- are produced by the TARGET repo's
own GitHub Actions, running against the pushed commit on GitHub's
infrastructure, independent of and in addition to the local scan cycle.
Confirmed with the user directly: these are two genuinely separate checks,
not a re-run of the same one.

The three signals, and the decision tree over them, are unchanged from the
original (see `classify()`'s own docstring below for the exact tree and its
reasoning).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from reasona_dev import _shell

GH_TIMEOUT = 30

# --- Compliance signal pattern (TAS PR Rule Compliance Review) -------------
COMPLIANCE_BOT_LOGINS = frozenset({"claude", "claude-code", "claude-ai", "claude-review"})

COMPLIANCE_MARKER_RE = re.compile(
    r"\bTAS\b[^\n]{0,80}?\bCompliance\b[^\n]{0,40}?\bReview\b",
    re.IGNORECASE,
)

COMPLIANCE_RESULT_BLOCK_RE = re.compile(
    r"===\s*[\w-]+\s+RESULT\s*===\s*\n"
    r"(?P<body>[\s\S]*?)\n"
    r"===\s*END\s*===",
    re.IGNORECASE,
)
COMPLIANCE_RESULT_VERDICT_RE = re.compile(
    r"(?m)^\s*VERDICT\s*[:：]\s*(PASS|FAIL)\b",
    re.IGNORECASE,
)

COMPLIANCE_VERDICT_LINE_FAIL_RE = re.compile(
    r"(?:^|\n)\s*VERDICT\s*[:：]\s*FAIL\b", re.IGNORECASE
)
COMPLIANCE_VERDICT_LINE_PASS_RE = re.compile(
    r"(?:^|\n)\s*VERDICT\s*[:：]\s*PASS\b", re.IGNORECASE
)

COMPLIANCE_VERDICT_FAIL_RES = [
    re.compile(r"(?:^|\s)Verdict\s*[:：][^\n]*?\*\*\s*FAIL\s*\*\*", re.IGNORECASE),
    re.compile(r"\*\*\s*FAIL\s*\*\*", re.IGNORECASE),
]

COMPLIANCE_VERDICT_PASS_RES = [
    re.compile(r"(?:^|\s)Verdict\s*[:：][^\n]*?\*\*\s*PASS\s*\*\*", re.IGNORECASE),
    re.compile(r"\*\*\s*No\s+rule\s+violations?\s+found[\s.!]*\*\*", re.IGNORECASE),
    re.compile(r"\bNo\s+rule\s+violations?\s+found\b", re.IGNORECASE),
    re.compile(r"(?:^|\W)LGTM(?:\W|$)"),
]

# --- BugBot signal pattern (Claude BugBot Analysis) -------------------------
BUGBOT_BOT_LOGINS = frozenset({"github-actions", "claude", "claude-code", "claude-bugbot"})
BUGBOT_MARKER_RE = re.compile(r"BugBot Analysis", re.IGNORECASE)

BUGBOT_RESULT_BLOCK_RE = re.compile(
    r"===\s*bugbot\s+RESULT\s*===\s*\n"
    r"(?P<body>[\s\S]*?)\n"
    r"===\s*END\s*===",
    re.IGNORECASE,
)
BUGBOT_RESULT_VERDICT_RE = re.compile(
    r"(?m)^\s*VERDICT\s*[:：]\s*(CLEAN|FOUND)\b",
    re.IGNORECASE,
)

BUGBOT_CLEAN_RES = [
    re.compile(r"(?:^|\n)\s*##\s*🟢", re.IGNORECASE),
    re.compile(r"\bNo\s+(?:\w+\s+)?bugs?\s+found\b", re.IGNORECASE),
    re.compile(r"\bno\s+(?:\w+\s+)?(?:issues?|defects?|vulnerabilit(?:y|ies))\s+found\b", re.IGNORECASE),
    re.compile(r"(?mi)^\s*VERDICT\s*[:：]\s*(?:PASS|CLEAN)\s*$"),
]

BUGBOT_FOUND_RES = [
    re.compile(r"(?:^|\n)\s*##\s*🔴", re.IGNORECASE),
    re.compile(r"\b\d+\s+bugs?\s+found\b", re.IGNORECASE),
    re.compile(r"\b\d+\s+findings?\b", re.IGNORECASE),
    re.compile(r"\b\d+\s+(?:issues?|defects?)\s+found\b", re.IGNORECASE),
    re.compile(r"(?m)^\s*#{1,6}\s+Detailed\s+findings\b", re.IGNORECASE),
    re.compile(r"(?mi)^\s*VERDICT\s*[:：]\s*(?:FAIL|FOUND)\s*$"),
]


class FetchError(Exception):
    pass


def run_gh(args: list[str], work_dir: str | Path) -> tuple[int, str, str]:
    return _shell.run(["gh", *args], Path(work_dir), timeout=GH_TIMEOUT)


def normalize_bot(login: str) -> str:
    # REST returns "claude[bot]"; GraphQL Bot.login returns bare "claude".
    return re.sub(r"\[bot\]$", "", login or "")


def is_bot(author: dict | None) -> bool:
    """Detect bot via Actor union __typename. REST never reaches here."""
    return bool(author and author.get("__typename") == "Bot")


def split_repo(repo_str: str) -> tuple[str, str]:
    parts = (repo_str or "").split("/", 1)
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"repo must be owner/name, got: {repo_str!r}")
    return parts[0], parts[1]


# --- GraphQL queries ---------------------------------------------------

SNAPSHOT_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      state
      comments(first: 100) {
        nodes {
          databaseId
          createdAt
          body
          author { __typename, login }
        }
        pageInfo { hasNextPage, endCursor }
      }
      reviews(first: 100) {
        nodes {
          databaseId
          state
          submittedAt
          body
          author { __typename, login }
        }
        pageInfo { hasNextPage, endCursor }
      }
      commits(last: 1) {
        nodes {
          commit {
            oid
            statusCheckRollup {
              state
              contexts(first: 100) {
                nodes {
                  __typename
                  ... on CheckRun {
                    name
                    status
                    conclusion
                  }
                  ... on StatusContext {
                    context
                    state
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""


MORE_COMMENTS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $after: String!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      comments(first: 100, after: $after) {
        nodes {
          databaseId
          createdAt
          body
          author { __typename, login }
        }
        pageInfo { hasNextPage, endCursor }
      }
    }
  }
}
"""


MORE_REVIEWS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $after: String!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviews(first: 100, after: $after) {
        nodes {
          databaseId
          state
          submittedAt
          body
          author { __typename, login }
        }
        pageInfo { hasNextPage, endCursor }
      }
    }
  }
}
"""


def _paginate(owner, name, pr, work_dir, query, initial_page, kind):
    """Walk a connection's pageInfo until exhausted (capped at 50 pages)."""
    nodes = list(initial_page.get("nodes") or [])
    page = initial_page.get("pageInfo") or {}
    fetched = 0
    while page.get("hasNextPage") and fetched < 50:
        cursor = page.get("endCursor")
        if not cursor:
            break
        rc, out, err = run_gh([
            "api", "graphql",
            "-f", f"query={query}",
            "-F", f"owner={owner}",
            "-f", f"name={name}",
            "-F", f"number={pr}",
            "-f", f"after={cursor}",
        ], work_dir)
        if rc != 0:
            raise FetchError(
                f"gh api graphql ({kind} page) failed: rc={rc} err={err.strip()[:200]}"
            )
        try:
            data = json.loads(out)
        except json.JSONDecodeError as e:
            raise FetchError(f"{kind}-page parse: {e}")
        if data.get("errors"):
            first = data["errors"][0] if data["errors"] else {}
            raise FetchError(
                f"graphql errors ({kind} page): {first.get('message', '?')[:200]}"
            )
        conn = (
            ((data.get("data") or {}).get("repository") or {})
            .get("pullRequest") or {}
        ).get(kind) or {}
        nodes.extend(conn.get("nodes") or [])
        page = conn.get("pageInfo") or {}
        fetched += 1
    return nodes, page


def fetch_snapshot_raw(owner: str, name: str, pr: int, work_dir: str | Path) -> dict:
    """Single snapshot fetch + comments/reviews pagination. Returns the
    pullRequest dict with `comments.nodes`, `reviews.nodes` containing all
    pages concatenated."""
    rc, out, err = run_gh([
        "api", "graphql",
        "-f", f"query={SNAPSHOT_QUERY}",
        "-F", f"owner={owner}",
        "-f", f"name={name}",
        "-F", f"number={pr}",
    ], work_dir)
    if rc != 0:
        raise FetchError(f"gh api graphql failed: rc={rc} err={err.strip()[:200]}")
    try:
        data = json.loads(out)
    except json.JSONDecodeError as e:
        raise FetchError(f"snapshot parse: {e}")
    if data.get("errors"):
        first = data["errors"][0] if data["errors"] else {}
        raise FetchError(f"graphql errors: {first.get('message', '?')[:200]}")
    pr_data = ((data.get("data") or {}).get("repository") or {}).get("pullRequest")
    if pr_data is None:
        raise FetchError("pull request not found in graphql response")

    comments_nodes, _ = _paginate(
        owner, name, pr, work_dir,
        MORE_COMMENTS_QUERY, pr_data.get("comments") or {}, "comments",
    )
    pr_data["comments"] = {"nodes": comments_nodes}

    reviews_nodes, _ = _paginate(
        owner, name, pr, work_dir,
        MORE_REVIEWS_QUERY, pr_data.get("reviews") or {}, "reviews",
    )
    pr_data["reviews"] = {"nodes": reviews_nodes}

    return pr_data


# --- Signal parsers ----------------------------------------------------

CI_FAIL_CONCLUSIONS = {
    "FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE",
}


def parse_ci(pr_data: dict) -> dict:
    """Return {state, failing_checks, pending_checks, head_sha}.

    `rollup.state` is authoritative -- GitHub aggregates ALL contexts, not
    just the first-100 window. Per-context iteration only enriches the
    failing/pending name lists.
    """
    commits = (pr_data.get("commits") or {}).get("nodes") or []
    if not commits:
        return {"state": "unknown", "failing_checks": [], "pending_checks": [], "head_sha": None}
    commit = commits[0].get("commit") or {}
    head_sha = commit.get("oid")
    rollup = commit.get("statusCheckRollup") or {}
    rollup_state = rollup.get("state")
    contexts = (rollup.get("contexts") or {}).get("nodes") or []

    failing = []
    pending = []
    for c in contexts:
        tn = c.get("__typename")
        if tn == "CheckRun":
            name = c.get("name") or "<unnamed>"
            status = c.get("status") or ""
            conclusion = c.get("conclusion") or ""
            if status != "COMPLETED":
                pending.append(name)
            elif conclusion in CI_FAIL_CONCLUSIONS:
                failing.append(name)
        elif tn == "StatusContext":
            name = c.get("context") or "<unnamed>"
            ctx_state = c.get("state") or ""
            if ctx_state in ("FAILURE", "ERROR"):
                failing.append(name)
            elif ctx_state in ("PENDING", "EXPECTED"):
                pending.append(name)

    if rollup_state == "SUCCESS":
        state = "passing"
    elif rollup_state in ("FAILURE", "ERROR"):
        state = "failing"
        if not failing:
            failing = [f"<rollup={rollup_state}>"]
    elif rollup_state in ("PENDING", "EXPECTED"):
        state = "pending"
    elif rollup_state is None and not contexts:
        # No CI configured on this repo/branch -> treat as passing so we
        # move on to evaluate the review-bot artefacts.
        state = "passing"
    else:
        state = "unknown"

    return {"state": state, "failing_checks": failing, "pending_checks": pending, "head_sha": head_sha}


def _compliance_verdict(body: str) -> str | None:
    """Return 'PASS' / 'FAIL' / None for a compliance comment body -- see
    module docstring for the 4-tier resolution order (RESULT block -> bare
    VERDICT line -> FAIL heuristics -> PASS heuristics)."""
    text = body or ""

    block = COMPLIANCE_RESULT_BLOCK_RE.search(text)
    if block:
        inner = block.group("body")
        m = COMPLIANCE_RESULT_VERDICT_RE.search(inner)
        if m:
            return m.group(1).upper()

    if COMPLIANCE_VERDICT_LINE_FAIL_RE.search(text):
        return "FAIL"
    if COMPLIANCE_VERDICT_LINE_PASS_RE.search(text):
        return "PASS"

    for pat in COMPLIANCE_VERDICT_FAIL_RES:
        if pat.search(text):
            return "FAIL"
    for pat in COMPLIANCE_VERDICT_PASS_RES:
        if pat.search(text):
            return "PASS"
    return None


def parse_compliance_review(issue_comments: list[dict]) -> dict:
    """Find the latest TAS PR Compliance Review comment. Matching order:
    bot-authored from the known login set -> login-only -> body-marker
    fallback. Returns {state, comment_id, body, created_at}."""
    bot_authored = []
    login_only = []
    body_only = []
    for c in issue_comments:
        author = c.get("author") or {}
        login = normalize_bot(author.get("login", "")).lower()
        body = c.get("body") or ""
        if not COMPLIANCE_MARKER_RE.search(body):
            continue
        if login in COMPLIANCE_BOT_LOGINS and is_bot(author):
            bot_authored.append(c)
        elif login in COMPLIANCE_BOT_LOGINS:
            login_only.append(c)
        else:
            body_only.append(c)

    matched = bot_authored or login_only or body_only
    if not matched:
        return {"state": "missing", "comment_id": None, "body": "", "created_at": None}

    matched.sort(key=lambda c: c.get("createdAt") or "")
    latest = matched[-1]
    body = latest.get("body") or ""
    verdict = _compliance_verdict(body)
    if verdict is None:
        state = "missing"
    elif verdict == "PASS":
        state = "pass"
    else:
        state = "fail"
    return {
        "state": state,
        "comment_id": latest.get("databaseId"),
        "body": body if state == "fail" else "",
        "created_at": latest.get("createdAt"),
    }


def _bugbot_classification(body: str) -> str | None:
    """Return 'clean' / 'found' / None -- see module docstring for the
    3-tier resolution order (RESULT block -> CLEAN heuristics -> FOUND
    heuristics)."""
    text = body or ""

    block = BUGBOT_RESULT_BLOCK_RE.search(text)
    if block:
        inner = block.group("body")
        m = BUGBOT_RESULT_VERDICT_RE.search(inner)
        if m:
            return m.group(1).lower()

    for pat in BUGBOT_CLEAN_RES:
        if pat.search(text):
            return "clean"
    for pat in BUGBOT_FOUND_RES:
        if pat.search(text):
            return "found"
    return None


def parse_bugbot_analysis(reviews: list[dict], issue_comments: list[dict]) -> dict:
    """Find the latest Claude BugBot Analysis artefact across both PR-level
    reviews and issue comments (publishing surface varies by installation).
    Returns {state: clean|found|missing, review_id, body, submitted_at}."""
    bot_authored = []
    login_only = []
    body_only = []

    def _bucket(item, body, login, author, timestamp):
        cand = {"databaseId": item.get("databaseId"), "body": body, "submittedAt": timestamp}
        if login in BUGBOT_BOT_LOGINS and is_bot(author):
            bot_authored.append(cand)
        elif login in BUGBOT_BOT_LOGINS:
            login_only.append(cand)
        else:
            body_only.append(cand)

    for r in reviews:
        if (r.get("state") or "") == "DISMISSED":
            continue
        author = r.get("author") or {}
        login = normalize_bot(author.get("login", "")).lower()
        body = r.get("body") or ""
        if not BUGBOT_MARKER_RE.search(body):
            continue
        _bucket(r, body, login, author, r.get("submittedAt"))

    for c in issue_comments:
        author = c.get("author") or {}
        login = normalize_bot(author.get("login", "")).lower()
        body = c.get("body") or ""
        if not BUGBOT_MARKER_RE.search(body):
            continue
        _bucket(c, body, login, author, c.get("createdAt"))

    matched = bot_authored or login_only or body_only
    if not matched:
        return {"state": "missing", "review_id": None, "body": "", "submitted_at": None}

    matched.sort(key=lambda x: x.get("submittedAt") or "")
    latest = matched[-1]
    body = latest["body"]
    kind = _bugbot_classification(body)
    state = kind if kind is not None else "missing"
    return {
        "state": state,
        "review_id": latest.get("databaseId"),
        "body": body if state == "found" else "",
        "submitted_at": latest.get("submittedAt"),
    }


# --- decision + snapshot wiring -----------------------------------------

def take_snapshot(owner: str, name: str, pr: int, work_dir: str | Path) -> dict:
    pr_data = fetch_snapshot_raw(owner, name, pr, work_dir)
    if pr_data.get("state") != "OPEN":
        raise FetchError(f"pr not open: {pr_data.get('state')}")
    ci = parse_ci(pr_data)
    issue_comments = (pr_data.get("comments") or {}).get("nodes") or []
    reviews = (pr_data.get("reviews") or {}).get("nodes") or []
    compliance = parse_compliance_review(issue_comments)
    bugbot = parse_bugbot_analysis(reviews, issue_comments)
    return {"head_sha": ci.get("head_sha"), "ci": ci, "compliance": compliance, "bugbot": bugbot}


def classify(snap: dict) -> str:
    """Decision tree over the three signals (verbatim from the original
    watcher):

      - ci.state == failing             -> actionable
      - ci.state != passing             -> continue   (still waiting on CI)
      - compliance.state == fail        -> actionable
      - bugbot.state    == found        -> actionable
      - compliance.state == missing  OR
        bugbot.state    == missing      -> continue   (CI green but bot
                                                        artefact not yet
                                                        visible; rare race)
      - else                            -> terminal

    CI is the prerequisite signal because the workflows that produce the
    compliance comment and the BugBot review are themselves part of CI --
    once CI is SUCCESS, both workflows ran to completion on the current
    head SHA, so the latest visible artefacts are the up-to-date verdicts.
    """
    ci_state = snap["ci"]["state"]
    comp_state = snap["compliance"]["state"]
    bug_state = snap["bugbot"]["state"]

    if ci_state == "failing":
        return "actionable"
    if ci_state != "passing":
        return "continue"

    if comp_state == "fail":
        return "actionable"
    if bug_state == "found":
        return "actionable"
    if comp_state == "missing" or bug_state == "missing":
        return "continue"
    return "terminal"
