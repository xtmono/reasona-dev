"""reasona-dev CLI -- the actual place a flag like `--dev opus` is typed.

Before this module existed, `resolve()`/`resolve_all()`'s `flag`/`flags`
parameters had no real caller: nothing in the codebase threaded an actual
command-line argument into them (`plan_compile.compile_to_bernstein_plan()`
didn't even accept a raw flag string for `dev` until this change). The
priority chain documented everywhere (`flag > env var > project cfg >
global cfg > fallback > default`) was correct in design but the top of it
was unreachable from an actual shell invocation.

Subcommands:

    reasona-dev compile-plan <plan.md> -o plan.yaml [--workdir DIR]
        [--dev MODEL] [--review MODEL] [--recheck MODEL] [--bugbot MODEL]
        [--compliance MODEL] [--final-audit MODEL]

(`render-review`, which rendered a `bernstein review --pipeline` YAML, was
removed with `review_pipeline.py` -- see docs/ARCHITECTURE.md §3.5.4.
review/bugbot/compliance dispatch now goes through `reasona_dev.pr_cycle`.)

Every role flag mirrors dev-ralf's own flag names one-to-one
(dev-ralf-renewal-claude.md §3.7: `--dev`, `--review`, `--recheck`,
`--bugbot`, `--compliance`, `--final-audit`) -- this CLI does not invent
new flag names.

`compile-plan` accepts every role's flag, not just `--dev`: `--dev` still
controls the plan step itself (the only thing this subcommand generates),
but `compile_to_bernstein_plan()` also syncs `<workdir>/bernstein.yaml`'s
`role_model_policy` as a side effect (`bernstein_config.py`), and that sync
needs the full flag > env var > project cfg > global cfg chain for
review/recheck/bugbot/compliance/final_audit too -- omitting those flags here
used to mean the "flag" layer was silently unreachable for every role
except dev when syncing role_model_policy.

`run-plan` drives every PR unit through its own dedicated git worktree,
dev-0 -> review -> scan -> ship_gate, in dependency order
(`reasona_dev.orchestrate`; see its own module docstring on why cycle-0 is
dispatched per unit, into that unit's worktree, rather than for the whole
plan up front). It stops there by default -- no PR, no merge. `--ship` opts
IN to the final stage (gh-pr, gh-review, squash-message guard; stops at an
open PR); `--merge` opts IN further to squash-merging it. Both default to
off: opening a real PR and squash-merging it are outward-facing, hard to
undo actions, not something to run unattended by default. `--skip-dev`
opts out of cycle-0 dispatch for every unit, for the rare case a unit's
worktree/cycle-0 was already set up by hand.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from reasona_dev.plan_compile import write_plan_yaml

_ROLE_FLAGS = ("dev", "review", "recheck", "bugbot", "compliance", "final_audit")


def _add_role_flags(parser: argparse.ArgumentParser, roles: tuple[str, ...]) -> None:
    for role in roles:
        flag_name = "--final-audit" if role == "final_audit" else f"--{role}"
        if role == "review":
            # Repeatable -- dev-ralf's own convention: `--review` is the
            # only role flag it allows more than once, to run independent
            # reviewers in parallel and merge their findings (worker.md's
            # "parallel reviewers"; see reasona_dev.finding_adapter.merge).
            parser.add_argument(
                flag_name,
                dest=role,
                action="append",
                default=None,
                metavar="MODEL",
                help="Force the review role's model (repeatable for multiple independent "
                     "reviewers; highest priority -- overrides env var and config file).",
            )
            continue
        parser.add_argument(
            flag_name,
            dest=role,
            default=None,
            metavar="MODEL",
            help=f"Force the {role} role's model (highest priority -- overrides env var and config file).",
        )


def _collect_flags(args: argparse.Namespace, roles: tuple[str, ...]) -> dict[str, str]:
    """Single value per role -- `review`'s repeatable flag collapses to its
    FIRST value here, for callers (`compile-plan`'s role_model_policy sync)
    that only need one representative reviewer. `run-plan` reads the full
    list separately via `_collect_review_flags`.
    """
    out: dict[str, str] = {}
    for role in roles:
        val = getattr(args, role, None)
        if not val:
            continue
        out[role] = val[0] if role == "review" else val
    return out


def _collect_review_flags(args: argparse.Namespace) -> list[str]:
    return list(getattr(args, "review", None) or [])


def _workdir(args: argparse.Namespace) -> Path:
    """The target repo, ALWAYS absolute.

    Resolved once here rather than in each subcommand, because the two used
    to disagree: `compile-plan` passed `None` through to `plan_compile`,
    which defaulted to `Path.cwd()` (absolute), while every other subcommand
    substituted the literal `"."` (relative). A relative workdir is not
    merely untidy -- it propagates into the path handed to an agent, and an
    agent runs inside a per-task git worktree where a relative path resolves
    against THAT tree, not the project root the driver reads from. Observed
    live: the agent wrote its report into its own worktree, spent its
    remaining turns hunting for the file the driver was asking about, and
    died on `error_max_turns` while the driver recorded the role as ERROR.

    `run_role` also resolves its own rundir, so that specific path is
    defended twice on purpose. Absorbing it at the entry point is what keeps
    a NEW caller from having to rediscover the rule.
    """
    return Path(args.workdir or ".").resolve()


def _cmd_compile_plan(args: argparse.Namespace) -> int:
    from reasona_dev.plan_compile import PlanError

    plan_text = Path(args.plan_file).read_text(encoding="utf-8")
    flags = _collect_flags(args, _ROLE_FLAGS)
    try:
        write_plan_yaml(
            plan_text,
            args.out,
            plan_name=args.plan_name or Path(args.plan_file).stem,
            description=args.description or f"Compiled from {args.plan_file}",
            dev_flag=flags.get("dev"),
            workdir=_workdir(args),
            policy_flags=flags,
        )
    except PlanError as exc:
        # A plan defect is the author's to fix, so it exits as a clean
        # diagnostic rather than a traceback.
        print(f"reasona-dev: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {args.out}", file=sys.stderr)
    return 0


def _cmd_acceptance(args: argparse.Namespace) -> int:
    from reasona_dev import acceptance

    return acceptance.main([args.criteria_file, str(_workdir(args))])


def _cmd_prompts(args: argparse.Namespace) -> int:
    from reasona_dev.prompt_profile import available_profiles

    workdir = _workdir(args)
    found = available_profiles(workdir)
    if not found:
        print(
            f"no prompt profiles found.\nsearched: {workdir}/.reasona/prompts/ "
            "then ~/.reasona/prompts/\n"
            "copy this repo's .reasona/prompts/generic/ into either location to start.",
            file=sys.stderr,
        )
        return 1
    for name, roles in found.items():
        print(f"{name}: {', '.join(roles)}")
    return 0


def _cmd_run_plan(args: argparse.Namespace) -> int:
    from reasona_dev import ledger, orchestrate
    from reasona_dev.model_config import resolve_all
    from reasona_dev.plan_compile import PlanError

    workdir = _workdir(args)
    plan_text = Path(args.plan_file).read_text(encoding="utf-8")
    flags = _collect_flags(args, _ROLE_FLAGS)
    review_flags = _collect_review_flags(args)
    resolved = resolve_all(workdir=workdir, flags=flags, review_flags=review_flags)
    plan_name = Path(args.plan_file).stem

    if args.restart:
        try:
            units = orchestrate.resolve_plan_units(plan_text, workdir)
        except PlanError as exc:
            print(f"reasona-dev: {exc}", file=sys.stderr)
            return 1
        ledger.clear(workdir, plan_name, [u.stage_name for u in units])

    try:
        result = orchestrate.run_plan(
            workdir=workdir,
            plan_name=plan_name,
            plan_text=plan_text,
            resolved=resolved,
            rundir=Path(args.rundir).resolve() if args.rundir else None,
            port=args.port,
            job=args.job,
            base=args.base,
            head=args.head,
            ship=args.ship or args.merge,
            merge=args.merge,
            from_pr=args.from_pr,
            resume=not args.restart,
            skip_dev=args.skip_dev,
            dev_flag=flags.get("dev"),
            policy_flags=flags,
            gh_review_max_wait_seconds=args.gh_review_max_wait,
        )
    except PlanError as exc:
        print(f"reasona-dev: {exc}", file=sys.stderr)
        return 1
    print(result.render(), file=sys.stderr)
    return 0 if result.passed else 1


def _cmd_ship_gate(args: argparse.Namespace) -> int:
    from reasona_dev import ship_gate

    decision = ship_gate.evaluate(
        _workdir(args), args.stage,
        cycle_verdict=args.cycle_verdict, record=not args.no_record,
    )
    print(decision.render(), file=sys.stderr)
    return 0 if decision.passed else 1


def _cmd_cycles_report(args: argparse.Namespace) -> int:
    from reasona_dev import cycles_query

    print(cycles_query.render(_workdir(args), include_effective=args.effective))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="reasona-dev")
    sub = parser.add_subparsers(dest="command", required=True)

    p_plan = sub.add_parser("compile-plan", help="Compile a plan document into a Bernstein plan.yaml")
    p_plan.add_argument("plan_file", help="Path to the dev-ralf/reasona-plan-style plan document")
    p_plan.add_argument("-o", "--out", required=True, help="Output path for the compiled plan.yaml")
    p_plan.add_argument("--plan-name", default=None)
    p_plan.add_argument("--description", default=None)
    p_plan.add_argument("--workdir", default=None, help="Target repository root, resolved to an absolute path (default: cwd)")
    _add_role_flags(p_plan, _ROLE_FLAGS)
    p_plan.set_defaults(func=_cmd_compile_plan)

    p_accept = sub.add_parser(
        "acceptance",
        help="Run a PR unit's acceptance criteria (pre-merge gate)",
    )
    p_accept.add_argument(
        "criteria_file",
        help="Path to .reasona/acceptance-<stage>.json, written by compile-plan",
    )
    p_accept.add_argument("--workdir", default=None, help="Directory to run criteria in, resolved to an absolute path (default: cwd)")
    p_accept.set_defaults(func=_cmd_acceptance)

    p_prompts = sub.add_parser(
        "prompts",
        help="List prompt profiles visible from here (project layer then global layer)",
    )
    p_prompts.add_argument("--workdir", default=None, help="Target repository root, resolved to an absolute path (default: cwd)")
    p_prompts.set_defaults(func=_cmd_prompts)

    p_run = sub.add_parser(
        "run-plan",
        help="Run every PR unit through review -> scan -> ship, in dependency order",
    )
    p_run.add_argument("plan_file", help="Path to the plan document (manifest form)")
    p_run.add_argument("--workdir", default=None, help="Target repository root, resolved to an absolute path (default: cwd)")
    p_run.add_argument("--rundir", default=None, help="Where role outputs and the ledger land (default: <workdir>/.reasona/log/<plan>)")
    p_run.add_argument("--port", type=int, default=8052, help="Port each `bernstein run` dispatch binds (reused sequentially, or the first of `--job` consecutive ports when running concurrently)")
    p_run.add_argument(
        "--job", type=int, default=1,
        help="Max PR units to run at once (default: 1, sequential -- unchanged from before this flag existed). "
             "job>1 runs independent units concurrently, each on its own port (--port through --port+job-1).",
    )
    p_run.add_argument("--base", default="origin/main")
    p_run.add_argument("--head", default="HEAD")
    p_run.add_argument(
        "--gh-review-max-wait", type=int, default=900, dest="gh_review_max_wait",
        metavar="SECONDS",
        help=(
            "Wall-clock budget for gh-review's CI/bot-watch loop per unit "
            "(--ship only). Matches /gh-review's own --max-wait default "
            "(900s). This is wait time for GitHub's own workflows to "
            "finish, not a dev-fix attempt count -- see "
            "cycle_gate.MAX_GH_REVIEW_CYCLES for that bound."
        ),
    )
    p_run.add_argument(
        "--skip-dev", action="store_true",
        help=(
            "Force-skip compiling and dispatching cycle-0, regardless of the ledger. "
            "Rarely needed -- a plan whose cycle-0 already ran skips it automatically "
            "on the next run-plan call (see the ledger note below); this is for the "
            "case the ledger doesn't know about, e.g. cycle-0 was run by hand."
        ),
    )
    p_run.add_argument(
        "--restart", action="store_true",
        help=(
            "Ignore and clear this plan's ledger (.reasona/log/<plan>/<stage>/ledger.json) "
            "and run every unit fresh, including re-dispatching cycle-0 into a fresh "
            "worktree. Use when the plan itself changed since the last run, not for a "
            "plain retry after an interruption -- a plain re-run of the same command "
            "already resumes automatically."
        ),
    )
    p_run.add_argument(
        "--ship", action="store_true",
        help=(
            "Run the final stage after a passing review: sync-main, conditional final "
            "audit, ship_gate, gh-pr (issue + PR, structural validation and repair), "
            "gh-review (CI/bot watch and auto-fix). Stops at an open PR unless --merge "
            "is also given. Off by default -- opening a real PR is not something to run "
            "unattended by default."
        ),
    )
    p_run.add_argument(
        "--merge", action="store_true",
        help=(
            "Squash-merge the PR once it passes the up-to-date gate. Implies "
            "--ship. Off by default: a squash-merge rewrites a real default "
            "branch and is not something to discover after the fact."
        ),
    )
    p_run.add_argument(
        "--from-pr", default=None, metavar="INDEX",
        help=(
            "Manual override of the automatic ledger-based resume: start at the PR "
            "unit with this index regardless of what the ledger says, dropping every "
            "unit ordered before it from this run entirely. Only needed when the "
            "ledger is unavailable or wrong -- a plain re-run already resumes at the "
            "first not-yet-shipped unit on its own."
        ),
    )
    _add_role_flags(p_run, _ROLE_FLAGS)
    p_run.set_defaults(func=_cmd_run_plan)

    p_ship = sub.add_parser(
        "ship-gate",
        help="The composed pre-merge verdict: review AND acceptance AND structure",
    )
    p_ship.add_argument("stage", help="Stage name, e.g. pr-1")
    p_ship.add_argument("--workdir", default=None, help="Target repository root, resolved to an absolute path (default: cwd)")
    p_ship.add_argument(
        "--cycle-verdict", default=None,
        help="The review/scan verdict from pr_cycle (PASS/PASS_WITH_NOTES/FAIL). Omitted = not asserted.",
    )
    p_ship.add_argument(
        "--no-record", action="store_true",
        help="Do not append to cycles.jsonl (dry run / re-evaluation)",
    )
    p_ship.set_defaults(func=_cmd_ship_gate)

    p_report = sub.add_parser(
        "cycles-report",
        help="Attribution, budget, and acceptance-coverage queries over cycles.jsonl",
    )
    p_report.add_argument("--workdir", default=None, help="Target repository root, resolved to an absolute path (default: cwd)")
    p_report.add_argument(
        "--effective", action="store_true",
        help="Also report the APPROXIMATE 'file touched again within 7d' proxy (base-rate caveat applies)",
    )
    p_report.set_defaults(func=_cmd_cycles_report)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
