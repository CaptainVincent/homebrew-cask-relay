"""Maintain the tap checkout, without installing or publishing."""

import argparse
import json
import sys
from pathlib import Path

from .sync import sync, tokens, update_policy


def main():
    parser = argparse.ArgumentParser(prog="python3 -m cask_relay", description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    source = actions.add_parser("sync", help="Verify and refresh recipes from Homebrew")
    source.add_argument(
        "--dry-run", action="store_true", help="Inspect without writing"
    )
    source.add_argument("--tap", help="Registered owner/tap (adds livecheck)")
    source.add_argument(
        "--bump",
        action="store_true",
        help="Native write-only bump (downloads archives)",
    )
    actions.add_parser("list", help="Print one allowlisted token per line")
    actions.add_parser("policy", help="Classify worktree changes against HEAD")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    try:
        if args.action == "list":
            print("\n".join(tokens(root)))
        else:
            result = (
                sync(root, args.dry_run, args.tap, args.bump)
                if args.action == "sync"
                else update_policy(root)
            )
            print(json.dumps(result, indent=2))
    except Exception as error:  # noqa: BLE001 - Report sync errors at the CLI boundary.
        print(f"Maintenance stopped: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
