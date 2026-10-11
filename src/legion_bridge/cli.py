"""LEGION bridge CLI."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

from . import github as gh
from .core import (
    CLAIMED_LABEL,
    INTAKE_LABEL,
    RUNNING_LABEL,
    SCHEMA_VERSION,
    TERMINAL_LABELS,
    AuditLog,
    ClaimStore,
    HarnessPolicy,
    Policy,
    SecretFile,
    WorkerIdentity,
    canonical_json,
    redact_secrets,
    sha256_hex,
    sign,
    utcnow_epoch,
    utcnow_iso,
    load_policy,
)


def _policy_path(args: argparse.Namespace) -> Path:
    return Path(os.path.expanduser(args.policy))


def cmd_status(args: argparse.Namespace) -> int:
    policy = load_policy(_policy_path(args))
    identity = WorkerIdentity.load(policy)
    report: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "worker_id": identity.worker_id,
        "worker_type": identity.worker_type,
        "hostname": identity.hostname,
        "allowed_repos": policy.allowed_repos,
        "supported_harnesses": sorted(policy.harness_policies.keys()),
        "harness_detected": {
            name: bool(shutil.which(hp.binary)) for name, hp in policy.harness_policies.items()
        },
        "allow_frontier": policy.allow_frontier,
        "lease_ttl_s": policy.lease_ttl_s,
        "heartbeat_interval_s": policy.heartbeat_interval_s,
        "poll_interval_s": policy.poll_interval_s,
        "worker_unique": policy.worker_unique,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def cmd_poll_once(args: argparse.Namespace) -> int:
    policy = load_policy(_policy_path(args))
    audit = AuditLog(policy.state_root / "audit.jsonl")
    detected: list[dict[str, Any]] = []
    if not gh.gh_authenticated():
        audit.append("poll_skipped", reason="gh_not_authenticated")
        print(json.dumps({"skipped": "gh_not_authenticated"}))
        return 2
    for repo in policy.allowed_repos:
        try:
            issues = gh.list_candidate_issues(repo, limit=policy.poll_limit)
        except Exception as e:  # noqa: BLE001
            audit.append("poll_error", repo=repo, error=str(e))
            continue
        for issue in issues:
            detected.append(
                {
                    "repo": repo,
                    "number": issue.number,
                    "title": issue.title,
                    "labels": issue.labels,
                    "created_at": issue.created_at,
                    "author": issue.author,
                }
            )
    audit.append("poll_complete", n=len(detected))
    print(json.dumps({"candidates": detected}, indent=2, sort_keys=True))
    return 0


def _operation(args, name):
    from . import operations as ops
    from .lockfile import acquire, ListenerLockError
    policy, identity, audit, db, _ = ops.load(args.policy)
    try:
        if policy.host and policy.host != socket.gethostname():
            return 5
        try:
            lock = acquire(policy.state_root, identity.worker_id)
        except ListenerLockError:
            return 6
        try:
            if name == "recover":
                result = ops.op_recover(policy, identity, audit, db)
                print(json.dumps({"recovered": result}))
                return 0
            result = getattr(ops, "op_" + name)(policy, identity, audit, db, args.repo, int(args.issue))
            print(json.dumps({"status": result.status, **result.detail}))
            return 0 if result.ok else 3
        finally:
            lock.release()
    finally:
        db.close()


def cmd_claim(args):
    return _operation(args, "claim")


def cmd_run(args):
    return _operation(args, "run")


def cmd_publish(args):
    return _operation(args, "publish")


def cmd_recover(args):
    return _operation(args, "recover")


def cmd_health(args: argparse.Namespace) -> int:
    policy = load_policy(_policy_path(args))
    report: dict[str, Any] = {"ok": True, "worker_id": policy.worker_id}
    print(json.dumps(report))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="legion-bridge")
    parser.add_argument("--policy", default="~/.config/legion-bridge/policy.toml")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("status", help="Print worker status")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("poll", help="Poll once for candidate issues")
    p.set_defaults(func=cmd_poll_once)

    p = sub.add_parser("claim", help="Claim a specific issue")
    p.add_argument("--repo", required=True)
    p.add_argument("--issue", required=True)
    p.set_defaults(func=cmd_claim)

    p = sub.add_parser("run", help="Execute a claimed issue")
    p.add_argument("--repo", required=True)
    p.add_argument("--issue", required=True)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("publish", help="Publish a signed receipt")
    p.add_argument("--repo", required=True)
    p.add_argument("--issue", required=True)
    p.set_defaults(func=cmd_publish)

    p = sub.add_parser("recover", help="Sweep stale claims")
    p.set_defaults(func=cmd_recover)

    p = sub.add_parser("health", help="Local health probe")
    p.set_defaults(func=cmd_health)

    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
