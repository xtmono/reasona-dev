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

from reasona_dev.plan_compile import write_plan_yaml

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
    plan_text = Path(args.plan_file).read_text(encoding="utf-8")
    flags = _collect_flags(args, _ROLE_FLAGS)
    write_plan_yaml(
        plan_text,
        args.out,
        plan_name=args.plan_name or Path(args.plan_file).stem,
        description=args.description or f"Compiled from {args.plan_file}",
        dev_flag=flags.get("dev"),
        workdir=args.workdir,
        policy_flags=flags,
    )
    print(f"wrote {args.out}", file=sys.stderr)
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
    _add_role_flags(p_plan, _ROLE_FLAGS)
    p_plan.set_defaults(func=_cmd_compile_plan)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
