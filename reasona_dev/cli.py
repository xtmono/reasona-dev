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
        [--verify MODEL] [--final-audit MODEL]

(`render-review`, which rendered a `bernstein review --pipeline` YAML, was
removed with `review_pipeline.py` -- see docs/ARCHITECTURE.md §3.5.4.
review/bugbot/compliance dispatch now goes through `reasona_dev.pr_cycle`.)

Every role flag mirrors dev-ralf's own flag names one-to-one
(dev-ralf-renewal-claude.md §3.7: `--dev`, `--review`, `--recheck`,
`--bugbot`, `--verify`, `--final-audit`) -- this CLI does not invent new
flag names.

`compile-plan` accepts every role's flag, not just `--dev`: `--dev` still
controls the plan step itself (the only thing this subcommand generates),
but `compile_to_bernstein_plan()` also syncs `<workdir>/bernstein.yaml`'s
`role_model_policy` as a side effect (`bernstein_config.py`), and that sync
needs the full flag > env var > project cfg > global cfg chain for
review/recheck/bugbot/verify/final_audit too -- omitting those flags here
used to mean the "flag" layer was silently unreachable for every role
except dev when syncing role_model_policy.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from reasona_dev.plan_compile import MAX_PR_UNITS, write_plan_yaml

_ROLE_FLAGS = ("dev", "review", "recheck", "bugbot", "verify", "final_audit")


def _add_role_flags(parser: argparse.ArgumentParser, roles: tuple[str, ...]) -> None:
    for role in roles:
        flag_name = "--final-audit" if role == "final_audit" else f"--{role}"
        parser.add_argument(
            flag_name,
            dest=role,
            default=None,
            metavar="MODEL",
            help=f"Force the {role} role's model (highest priority -- overrides env var and config file).",
        )


def _collect_flags(args: argparse.Namespace, roles: tuple[str, ...]) -> dict[str, str]:
    return {role: val for role in roles if (val := getattr(args, role, None))}


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
            workdir=args.workdir,
            policy_flags=flags,
            max_pr_units=args.max_pr_units,
        )
    except PlanError as exc:
        # A plan defect is the author's to fix, so it exits as a clean
        # diagnostic rather than a traceback.
        print(f"reasona-dev: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {args.out}", file=sys.stderr)
    return 0


def _cmd_structure_gate(args: argparse.Namespace) -> int:
    from reasona_dev import structure_gate

    return structure_gate.main([args.workdir or ".", args.base, args.head])


def _cmd_acceptance(args: argparse.Namespace) -> int:
    from reasona_dev import acceptance

    return acceptance.main([args.criteria_file, args.workdir or "."])


def _cmd_ship_gate(args: argparse.Namespace) -> int:
    from reasona_dev import ship_gate

    decision = ship_gate.evaluate(
        args.workdir or ".", args.stage,
        cycle_verdict=args.cycle_verdict, base=args.base, head=args.head,
        record=not args.no_record,
    )
    print(decision.render(), file=sys.stderr)
    return 0 if decision.passed else 1


def _cmd_cycles_report(args: argparse.Namespace) -> int:
    from reasona_dev import cycles_query

    print(cycles_query.render(args.workdir or ".", include_effective=args.effective))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="reasona-dev")
    sub = parser.add_subparsers(dest="command", required=True)

    p_plan = sub.add_parser("compile-plan", help="Compile a plan document into a Bernstein plan.yaml")
    p_plan.add_argument("plan_file", help="Path to the dev-ralf/reasona-plan-style plan document")
    p_plan.add_argument("-o", "--out", required=True, help="Output path for the compiled plan.yaml")
    p_plan.add_argument("--plan-name", default=None)
    p_plan.add_argument("--description", default=None)
    p_plan.add_argument("--workdir", default=None, help="Target repository root (default: cwd)")
    p_plan.add_argument(
        "--max-pr-units", type=int, default=MAX_PR_UNITS,
        help=(
            f"Refuse a plan with more than N PR units (default: {MAX_PR_UNITS}). "
            "0 disables the cap. A large plan is a batch inside which nothing "
            "learned in the first PR can reach the last one's specification."
        ),
    )
    _add_role_flags(p_plan, _ROLE_FLAGS)
    p_plan.set_defaults(func=_cmd_compile_plan)

    p_struct = sub.add_parser(
        "structure-gate",
        help="Deterministic structural checks (file size, duplication, dependency direction)",
    )
    p_struct.add_argument("--workdir", default=None, help="Target repository root (default: cwd)")
    p_struct.add_argument("--base", default="origin/main", help="Diff base for growth checks")
    p_struct.add_argument("--head", default="HEAD", help="Diff head for growth checks")
    p_struct.set_defaults(func=_cmd_structure_gate)

    p_accept = sub.add_parser(
        "acceptance",
        help="Run a PR unit's acceptance criteria (pre-merge gate)",
    )
    p_accept.add_argument(
        "criteria_file",
        help="Path to .reasona/acceptance-<stage>.json, written by compile-plan",
    )
    p_accept.add_argument("--workdir", default=None, help="Directory to run criteria in (default: cwd)")
    p_accept.set_defaults(func=_cmd_acceptance)

    p_ship = sub.add_parser(
        "ship-gate",
        help="The composed pre-merge verdict: review AND acceptance AND structure",
    )
    p_ship.add_argument("stage", help="Stage name, e.g. pr-1")
    p_ship.add_argument("--workdir", default=None, help="Target repository root (default: cwd)")
    p_ship.add_argument(
        "--cycle-verdict", default=None,
        help="The review/scan verdict from pr_cycle (PASS/PASS_WITH_NOTES/FAIL). Omitted = not asserted.",
    )
    p_ship.add_argument("--base", default="origin/main")
    p_ship.add_argument("--head", default="HEAD")
    p_ship.add_argument(
        "--no-record", action="store_true",
        help="Do not append to cycles.jsonl (dry run / re-evaluation)",
    )
    p_ship.set_defaults(func=_cmd_ship_gate)

    p_report = sub.add_parser(
        "cycles-report",
        help="Attribution, budget, and acceptance-coverage queries over cycles.jsonl",
    )
    p_report.add_argument("--workdir", default=None, help="Target repository root (default: cwd)")
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
