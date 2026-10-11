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


def cmd_claim(args: argparse.Namespace) -> int:
    policy = load_policy(_policy_path(args))
    identity = WorkerIdentity.load(policy)
    audit = AuditLog(policy.state_root / "audit.jsonl")
    secret = SecretFile(Path(os.path.expanduser("~/.config/legion-bridge/worker.key"))).read()
    db = ClaimStore(policy.state_root / "claims.sqlite")
    repo = args.repo
    number = int(args.issue)
    if repo not in policy.allowed_repos:
        audit.append("claim_denied", reason="repo_not_allowed", repo=repo)
        print(json.dumps({"status": "denied", "reason": "repo_not_allowed"}))
        return 3
    issue = gh.issue_full(repo, number)
    if INTAKE_LABEL not in issue.labels:
        audit.append("claim_skipped", reason="no_intake_label", repo=repo, number=number)
        print(json.dumps({"status": "skipped", "reason": "no_intake_label"}))
        return 0
    expected_sha = gh.issue_state_sha(repo, number)
    ok, info = db.upsert_claim(
        repo=repo,
        issue_number=number,
        worker_id=identity.worker_id,
        expected_state_sha=expected_sha,
        lease_ttl_s=policy.lease_ttl_s,
        harness_requested=None,
    )
    if not ok:
        audit.append("claim_skipped", reason=info, repo=repo, number=number)
        print(json.dumps({"status": info}))
        return 0
    try:
        gh.remove_label(repo, number, INTAKE_LABEL)
    except Exception as e:  # noqa: BLE001
        audit.append("claim_label_remove_failed", error=str(e), repo=repo, number=number)
    try:
        gh.add_label(repo, number, CLAIMED_LABEL)
    except Exception as e:  # noqa: BLE001
        audit.append("claim_label_add_failed", error=str(e), repo=repo, number=number)
    audit.append("claim_ok", repo=repo, number=number, worker_id=identity.worker_id, expected_state_sha=expected_sha, signature=sign(secret, canonical_json({"repo": repo, "number": number, "worker_id": identity.worker_id, "expected_state_sha": expected_sha}).encode("utf-8")))
    print(json.dumps({"status": "claimed", "worker_id": identity.worker_id, "expected_state_sha": expected_sha}))
    db.close()
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    policy = load_policy(_policy_path(args))
    identity = WorkerIdentity.load(policy)
    audit = AuditLog(policy.state_root / "audit.jsonl")
    db = ClaimStore(policy.state_root / "claims.sqlite")
    repo = args.repo
    number = int(args.issue)
    if repo not in policy.allowed_repos:
        audit.append("run_denied", reason="repo_not_allowed", repo=repo)
        return 3
    claim = db.get(repo, number)
    if not claim or claim["worker_id"] != identity.worker_id:
        audit.append("run_denied", reason="not_claimed_by_worker", repo=repo, number=number)
        return 3
    issue = gh.issue_full(repo, number)
    fm, body = gh.parse_front_matter(issue.body)
    errs = gh.front_matter_safety_check(fm)
    if errs:
        audit.append("run_blocked", errors=errs, repo=repo, number=number)
        try:
            gh.add_label(repo, number, "legion-bridge:blocked")
            gh.post_comment(repo, number, f"legion-bridge blocked: {errs}")
        except Exception:  # noqa: BLE001
            pass
        return 4
    requested = str(fm.get("harness") or "omp")
    hp = policy.harness(requested)
    if hp is None:
        audit.append("run_blocked", reason="unknown_harness", harness=requested)
        return 5
    if not shutil.which(hp.binary):
        audit.append("run_blocked", reason="harness_not_installed", harness=requested, binary=hp.binary)
        try:
            gh.post_comment(repo, number, f"legion-bridge: harness {requested!r} ({hp.binary}) not installed on {identity.hostname}")
        except Exception:  # noqa: BLE001
            pass
        return 6
    workdir = policy.workdir_root / f"{repo.replace('/', '__')}__n{number}"
    workdir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema": SCHEMA_VERSION,
        "worker_id": identity.worker_id,
        "hostname": identity.hostname,
        "repo": repo,
        "issue": number,
        "harness_requested": requested,
        "harness_resolved": hp.name,
        "binary": hp.binary,
        "argv_template": hp.argv_template,
        "isolation": hp.isolation,
        "max_runtime_s": hp.timeout_s,
        "started_at": utcnow_iso(),
    }
    (workdir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    try:
        gh.add_label(repo, number, RUNNING_LABEL)
    except Exception:  # noqa: BLE001
        pass
    db.update_status(repo, number, "running", harness_resolved=hp.name)
    audit.append("run_start", repo=repo, number=number, harness=hp.name, binary=hp.binary, workdir=str(workdir))
    cmd = [hp.binary, *hp.argv_template]
    env = os.environ.copy()
    for var in hp.env_passthrough:
        if var in os.environ:
            env[var] = os.environ[var]
    isolation_cmd: list[str] = []
    if hp.isolation == "bwrap":
        bw = shutil.which("bwrap")
        if not bw:
            audit.append("run_blocked", reason="bwrap_not_installed")
            db.update_status(repo, number, "failed", harness_resolved=hp.name)
            return 7
        isolation_cmd = [
            bw,
            "--ro-bind", "/", "/",
            "--tmpfs", "/home",
            "--tmpfs", "/root",
            "--tmpfs", "/tmp",
            "--proc", "/proc",
            "--dev", "/dev",
            "--unshare-pid",
            "--die-with-parent",
        ]
    full_cmd = isolation_cmd + cmd
    stdout_path = workdir / "stdout.log"
    stderr_path = workdir / "stderr.log"
    try:
        with stdout_path.open("w") as out, stderr_path.open("w") as err:
            proc = subprocess.run(
                full_cmd,
                cwd=str(workdir),
                env=env,
                stdout=out,
                stderr=err,
                timeout=hp.timeout_s,
                check=False,
            )
        rc = proc.returncode
        timed_out = False
    except subprocess.TimeoutExpired:
        rc = -1
        timed_out = True
    result = {
        "schema": SCHEMA_VERSION,
        "rc": rc,
        "timed_out": timed_out,
        "finished_at": utcnow_iso(),
    }
    (workdir / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True))
    if timed_out or rc != 0:
        try:
            gh.remove_label(repo, number, "legion-bridge:interrupted")
            gh.add_label(repo, number, "legion-bridge:failed")
        except Exception:  # noqa: BLE001
            pass
        db.update_status(repo, number, "failed", harness_resolved=hp.name)
        audit.append("run_failed", repo=repo, number=number, rc=rc, timed_out=timed_out)
        return 8
    try:
        gh.remove_label(repo, number, "legion-bridge:interrupted")
        gh.add_label(repo, number, "legion-bridge:completed")
    except Exception:  # noqa: BLE001
        pass
    db.update_status(repo, number, "completed", harness_resolved=hp.name)
    audit.append("run_ok", repo=repo, number=number, harness=hp.name)
    db.close()
    return 0


def cmd_publish(args: argparse.Namespace) -> int:
    policy = load_policy(_policy_path(args))
    identity = WorkerIdentity.load(policy)
    secret = SecretFile(Path(os.path.expanduser("~/.config/legion-bridge/worker.key"))).read()
    audit = AuditLog(policy.state_root / "audit.jsonl")
    db = ClaimStore(policy.state_root / "claims.sqlite")
    repo = args.repo
    number = int(args.issue)
    workdir = policy.workdir_root / f"{repo.replace('/', '__')}__n{number}"
    manifest_path = workdir / "manifest.json"
    result_path = workdir / "result.json"
    if not manifest_path.exists() or not result_path.exists():
        audit.append("publish_denied", reason="missing_manifest_or_result", repo=repo, number=number)
        return 3
    manifest = json.loads(manifest_path.read_text())
    result = json.loads(result_path.read_text())
    receipt = {
        "schema": SCHEMA_VERSION,
        "worker_id": identity.worker_id,
        "hostname": identity.hostname,
        "repo": repo,
        "issue": number,
        "harness_requested": manifest.get("harness_requested"),
        "harness_resolved": manifest.get("harness_resolved"),
        "binary": manifest.get("binary"),
        "started_at": manifest.get("started_at"),
        "finished_at": result.get("finished_at"),
        "rc": result.get("rc"),
        "timed_out": result.get("timed_out"),
    }
    receipt["signature"] = sign(secret, canonical_json(receipt).encode("utf-8"))
    (workdir / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True))
    body = (
        f"## legion-bridge receipt\n\n"
        f"- worker_id: `{receipt['worker_id']}`\n"
        f"- hostname: `{receipt['hostname']}`\n"
        f"- harness: `{receipt['harness_resolved']}` (requested `{receipt['harness_requested']}`)\n"
        f"- binary: `{receipt['binary']}`\n"
        f"- started_at: `{receipt['started_at']}`\n"
        f"- finished_at: `{receipt['finished_at']}`\n"
        f"- rc: `{receipt['rc']}`  timed_out: `{receipt['timed_out']}`\n"
        f"- signature: `{receipt['signature']}`\n"
    )
    last_err = None
    for attempt in range(policy.publish_retry_max):
        try:
            gh.post_comment(repo, number, body)
            last_err = None
            break
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
            time.sleep(1 + attempt * 2)
    if last_err is not None:
        db.update_status(repo, number, "publish_failed")
        audit.append("publish_failed", error=last_err, repo=repo, number=number)
        return 9
    db.update_status(repo, number, "receipt_published")
    audit.append("publish_ok", repo=repo, number=number, signature=receipt["signature"])
    db.close()
    return 0


def cmd_recover(args: argparse.Namespace) -> int:
    policy = load_policy(_policy_path(args))
    identity = WorkerIdentity.load(policy)
    audit = AuditLog(policy.state_root / "audit.jsonl")
    db = ClaimStore(policy.state_root / "claims.sqlite")
    now = utcnow_epoch()
    recovered = []
    for claim in db.list_active():
        if claim["lease_expires_at"] > now:
            continue
        repo, number = claim["repo"], claim["issue_number"]
        try:
            issue = gh.issue_full(repo, number)
        except Exception as e:  # noqa: BLE001
            audit.append("recover_unreachable", error=str(e), repo=repo, number=number)
            continue
        labels = issue.labels
        mine = CLAIMED_LABEL
        is_mine = (
            claim["worker_id"] == identity.worker_id
            and (
                mine in labels
                or mine not in labels  # lease-expired + claim row is mine → recover regardless
            )
        )
        if is_mine:
            db.update_status(repo, number, "recovered", session_id=claim.get("session_id"))
            try:
                gh.remove_label(repo, number, mine)
                gh.add_label(repo, number, INTAKE_LABEL)
                gh.add_label(repo, number, "legion-bridge:interrupted")
                gh.post_comment(repo, number, "legion-bridge: stale claim recovered; requeueing")
            except Exception:  # noqa: BLE001
                pass
            recovered.append({"repo": repo, "number": number, "reason": "stale_lease"})
        else:
            db.update_status(repo, number, "stale_foreign")
    audit.append("recover_complete", recovered=recovered)
    print(json.dumps({"recovered": recovered}, indent=2, sort_keys=True))
    db.close()
    return 0


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
