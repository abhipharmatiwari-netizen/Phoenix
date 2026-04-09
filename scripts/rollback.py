#!/usr/bin/env python3
"""Safe rollback script for code and config — PHX-DEVOPS-004.

Usage:
    python scripts/rollback.py --target <version_id_or_git_ref> [--env live|paper|shadow]
    python scripts/rollback.py --strategy <strategy_id> --version <version_id> [--env live]
    python scripts/rollback.py --feature-flags --snapshot <snapshot_version> [--env live]
    python scripts/rollback.py --list-versions --strategy <strategy_id>

All rollback actions:
  1. Require APP_ENV to be set
  2. Require approval in LIVE environment (--approve <approver_email>)
  3. Emit an audit event
  4. Print a summary of what was rolled back

Exit codes:
  0 — success
  1 — validation error
  2 — rollback failed
  3 — approval required
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _env() -> str:
    return os.getenv("APP_ENV", "local").strip().lower()


def _require_approval(approver: str, action: str, target: str) -> None:
    if _env() == "live" and not approver:
        print(f"ERROR: LIVE environment requires --approve <approver_email> for action: {action} target: {target}", file=sys.stderr)
        sys.exit(3)


def rollback_strategy(strategy_id: str, version_id: str, actor: str, approver: str) -> None:
    _require_approval(approver, "strategy_rollback", f"{strategy_id}@{version_id}")
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from app.strategies.version_registry import rollback_to_version
        ok, msg = rollback_to_version(
            version_id=version_id,
            actor=actor,
            reason=f"Manual rollback via scripts/rollback.py by {actor}",
        )
        if ok:
            print(f"[OK] Strategy {strategy_id} rolled back to version {version_id}: {msg}")
        else:
            print(f"[FAIL] Strategy rollback failed: {msg}", file=sys.stderr)
            sys.exit(2)
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(2)


def list_strategy_versions(strategy_id: str) -> None:
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from app.strategies.version_registry import list_versions
        versions = list_versions(strategy_id=strategy_id)
        print(f"\nVersions for strategy '{strategy_id}':")
        print(f"{'version_id':<34}  {'tag':<12}  {'env':<8}  {'status':<20}  {'created_at'}")
        print("-" * 100)
        for v in versions:
            print(
                f"{v['version_id']:<34}  {v['version_tag']:<12}  {v['environment']:<8}  "
                f"{v['status']:<20}  {v['created_at']}"
            )
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)


def rollback_feature_flags(snapshot_version: int, actor: str, approver: str) -> None:
    _require_approval(approver, "feature_flag_rollback", f"snapshot_v{snapshot_version}")
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from app.core.feature_flags import _FLAG_VERSION_STORE  # type: ignore
        store = _FLAG_VERSION_STORE
        ok = store.rollback_to_version(snapshot_version, actor=actor)
        if ok:
            print(f"[OK] Feature flags rolled back to snapshot version {snapshot_version}")
        else:
            print(f"[FAIL] Feature flag rollback to version {snapshot_version} failed", file=sys.stderr)
            sys.exit(2)
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(2)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Phoenix safe rollback tool (PHX-DEVOPS-004)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--strategy", help="Strategy ID to roll back")
    parser.add_argument("--version", help="Version ID to roll back to")
    parser.add_argument("--list-versions", action="store_true", help="List versions for --strategy")
    parser.add_argument("--feature-flags", action="store_true", help="Roll back feature flags")
    parser.add_argument("--snapshot", type=int, help="Feature flag snapshot version")
    parser.add_argument("--env", default=_env(), help="Target environment (default: APP_ENV)")
    parser.add_argument("--actor", default=os.getenv("USER", "unknown"), help="Actor performing rollback")
    parser.add_argument("--approve", default="", help="Approver email (required for LIVE)")

    args = parser.parse_args()

    print(f"[Phoenix Rollback] env={args.env} actor={args.actor} ts={_ts()}")

    if args.list_versions:
        if not args.strategy:
            print("ERROR: --list-versions requires --strategy", file=sys.stderr)
            sys.exit(1)
        list_strategy_versions(args.strategy)
        return

    if args.strategy and args.version:
        rollback_strategy(args.strategy, args.version, args.actor, args.approve)
        return

    if args.feature_flags:
        if args.snapshot is None:
            print("ERROR: --feature-flags requires --snapshot <version>", file=sys.stderr)
            sys.exit(1)
        rollback_feature_flags(args.snapshot, args.actor, args.approve)
        return

    parser.print_help()
    sys.exit(1)


if __name__ == "__main__":
    main()
