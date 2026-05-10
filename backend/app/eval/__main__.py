"""CLI entrypoint: `python -m app.eval [--dry-run]`."""

from __future__ import annotations

import argparse
import asyncio
import sys

from .run import run_eval


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="app.eval", description="Run the eval harness.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List cases and scorers without touching DB or LLM. Exit 0.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    return asyncio.run(run_eval(dry_run=args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
