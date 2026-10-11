"""High-level operations: claim, run, publish, recover, poll."""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import github as gh
from .core import (
    CLAIMED_LABEL,
    INTAKE_LABEL,
    RUNNING_LABEL,
    SecretFile,
    canonical_json,
    sha256_hex,
    sign,
    utcnow_epoch,
    utcnow_iso,
)


SECRET_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "COPILOT_GITHUB_TOKEN",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "PI_SMOL_MODEL",
    "PI_SLOW_MODEL",
    "PI_PLAN_MODEL",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "GOOGLE_API_KEY",
)


@dataclass
class OpResult:
    ok: bool
    status: str
    detail: dict[str, Any] = field(default_factory=dict)


def load(policy_path):
    from .core import AuditLog, ClaimStore, load_policy, WorkerIdentity
    p = Path(os.path.expanduser(str(policy_path)))
    policy = load_policy(p)
    identity = WorkerIdentity.load(policy)
    audit = AuditLog(policy.state_root / "audit.jsonl")
    db = ClaimStore(policy.state_root / "claims.sqlite")
    return policy, identity, audit, db, p


def op_poll(policy, audit):
    if not gh.gh_authenticated():
        audit.append("poll_skipped", reason="gh_not_authenticated")
        return []
    detected = []
    for repo in policy.allowed_repos:
        try:
            issues = gh.list_candidate_issues(repo, limit=policy.poll_limit)
        except Exception as e:  # noqa: BLE001
            audit.append("poll_error", repo=repo, error=str(e))
            continue
        for issue in issues:
            detected.append({
                "repo": repo,
                "number": issue.number,
                "title": issue.title,
                "labels": list(issue.labels),
                "created_at": issue.created_at,
                "author": issue.author,
            })
    audit.append("poll_complete", n=len(detected))
    return detected


def _operator_authorized(policy, identity, audit, author):
    if not policy.operator_team and not policy.operator_org:
        return True
    if not policy.operator_team or not policy.operator_org:
        return False
    if not author:
        audit.append("operator_check_no_author")
        return False
    decision = gh.user_in_team(author, policy.operator_org, policy.operator_team)
    if decision is None:
        audit.append("operator_check_unknown", author=author, team=policy.operator_team, org=policy.operator_org)
        return False
    if not decision:
        audit.append("operator_check_denied", author=author, team=policy.operator_team, org=policy.operator_org)
    return bool(decision)


def _frontier_authorized(policy, audit, hp):
    if hp.harness_kind in policy.frontier_kinds and not policy.allow_frontier:
        audit.append("frontier_blocked", harness=hp.name, kind=hp.harness_kind)
        return False
    return True


def _build_child_env(hp):
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
    }
    for var in hp.env_passthrough:
        if var in SECRET_ENV_VARS:
            continue
        if var in os.environ:
            env[var] = os.environ[var]
    return env


def op_claim(policy, identity, audit, db, repo, number):
    # === ECHELON: hard mode gate enforced by execution, not just by
    # a stored policy field. STANDBY refuses to claim; MAINTENANCE
    # refuses to claim. ACTIVE requires (a) the env var named in
    # policy.activate_env AND (b) distributed_claim_protocol_proven
    # in policy. The mode helper is a convenience for the operator;
    # it is not authoritative. ===
    mode = policy.mode
    if mode == "MAINTENANCE":
        audit.append("claim_denied", reason="mode_maintenance", repo=repo, number=number)
        return OpResult(False, "denied", {"reason": "mode_maintenance"})
    if mode == "STANDBY":
        audit.append("claim_denied", reason="mode_standby", repo=repo, number=number)
        return OpResult(False, "denied", {"reason": "mode_standby"})
    if mode == "ACTIVE":
        if not os.environ.get(policy.activate_env):
            audit.append("claim_denied", reason="activate_env_missing", env=policy.activate_env, repo=repo, number=number)
            return OpResult(False, "denied", {"reason": "activate_env_missing", "env": policy.activate_env})
        if not policy.distributed_claim_protocol_proven:
            audit.append("claim_denied", reason="distributed_claim_protocol_not_proven", repo=repo, number=number)
            return OpResult(False, "denied", {"reason": "distributed_claim_protocol_not_proven"})
    if repo not in policy.allowed_repos:
        audit.append("claim_denied", reason="repo_not_allowed", repo=repo)
        return OpResult(False, "denied", {"reason": "repo_not_allowed"})
    if db.has_completion_token(repo, number) or db.has_execution(repo, number):
        audit.append("claim_denied", reason="already_completed", repo=repo, number=number)
        return OpResult(False, "denied", {"reason": "already_completed"})
    try:
        issue = gh.issue_full(repo, number)
    except Exception as e:  # noqa: BLE001
        audit.append("claim_error", error=str(e), repo=repo, number=number)
        return OpResult(False, "error", {"reason": str(e)})
    if issue.state.lower() != "open":
        return OpResult(False, "denied", {"reason": "issue_closed"})
    if INTAKE_LABEL not in issue.labels:
        audit.append("claim_skipped", reason="no_intake_label", repo=repo, number=number)
        return OpResult(False, "skipped", {"reason": "no_intake_label"})
    if not _operator_authorized(policy, identity, audit, issue.author):
        db.update_status(repo, number, "denied_operator")
        return OpResult(False, "denied", {"reason": "operator_not_authorized", "author": issue.author})
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
        return OpResult(False, info, {"reason": info})
    try:
        gh.remove_label(repo, number, INTAKE_LABEL, timeout=policy.github_timeout_s)
    except Exception as e:  # noqa: BLE001
        db.delete_claim(repo, number)
        audit.append("claim_label_remove_failed", error=str(e), repo=repo, number=number, rolled_back=True)
        return OpResult(False, "error", {"reason": "label_remove_failed", "error": str(e)})
    try:
        gh.add_label(repo, number, CLAIMED_LABEL, timeout=policy.github_timeout_s)
    except Exception as e:  # noqa: BLE001
        try:
            gh.add_label(repo, number, INTAKE_LABEL, timeout=policy.github_timeout_s)
        except Exception:  # noqa: BLE001
            pass
        db.delete_claim(repo, number)
        audit.append("claim_label_add_failed", error=str(e), repo=repo, number=number, rolled_back=True)
        return OpResult(False, "error", {"reason": "label_add_failed", "error": str(e)})
    # === ECHELON: per-worker key path, not a hard-coded LEGION path. ===
    secret = SecretFile(policy.key_path).read()
    sig = sign(secret, canonical_json({"repo": repo, "number": number, "worker_id": identity.worker_id, "expected_state_sha": expected_sha}).encode("utf-8"))
    audit.append("claim_ok", repo=repo, number=number, worker_id=identity.worker_id, expected_state_sha=expected_sha, signature=sig)
    return OpResult(True, "claimed", {"expected_state_sha": expected_sha})


def _stop_process_group(proc, grace_s=5.0):
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=grace_s)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        pass


def _run_harness(policy, hp, full_cmd, env, workdir, timeout_s, db, repo, number):
    stop_heartbeat = threading.Event()

    def _heartbeat():
        while not stop_heartbeat.wait(policy.heartbeat_interval_s):
            try:
                db.update_status(
                    repo, number, "running",
                    harness_resolved=hp.name,
                    lease_expires_at=int(time.time()) + policy.lease_ttl_s + policy.heartbeat_interval_s,
                )
            except Exception:  # noqa: BLE001
                pass

    hb = threading.Thread(target=_heartbeat, daemon=True)
    hb.start()
    stdout_path = workdir / "stdout.log"
    stderr_path = workdir / "stderr.log"
    proc = None
    rc = -1
    timed_out = False
    try:
        with stdout_path.open("wb") as out, stderr_path.open("wb") as err:
            proc = subprocess.Popen(
                full_cmd,
                cwd=str(workdir),
                env=env,
                stdout=out,
                stderr=err,
                start_new_session=True,
            )
            try:
                rc = proc.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                if proc is not None:
                    _stop_process_group(proc)
                rc = -1
    except FileNotFoundError:
        stop_heartbeat.set()
        hb.join(timeout=2.0)
        raise
    finally:
        stop_heartbeat.set()
        hb.join(timeout=2.0)
    return rc, timed_out


def op_run(policy, identity, audit, db, repo, number):
    # === ECHELON: mode gate (see op_claim). op_run is also gated so a
    # STANDBY worker that somehow has a claim (e.g. a stale row from a
    # different mode) cannot run the harness. ===
    if policy.mode in ("STANDBY", "MAINTENANCE"):
        audit.append("run_denied", reason=f"mode_{policy.mode.lower()}", repo=repo, number=number)
        return OpResult(False, "denied", {"reason": f"mode_{policy.mode.lower()}"})
    if policy.mode == "ACTIVE" and not os.environ.get(policy.activate_env):
        audit.append("run_denied", reason="activate_env_missing", env=policy.activate_env, repo=repo, number=number)
        return OpResult(False, "denied", {"reason": "activate_env_missing", "env": policy.activate_env})
    if repo not in policy.allowed_repos:
        audit.append("run_denied", reason="repo_not_allowed", repo=repo)
        return OpResult(False, "denied", {"reason": "repo_not_allowed"})
    claim = db.get(repo, number)
    if not claim or claim["worker_id"] != identity.worker_id:
        audit.append("run_denied", reason="not_claimed_by_worker", repo=repo, number=number)
        return OpResult(False, "denied", {"reason": "not_claimed_by_worker"})
    if db.has_completion_token(repo, number) or db.has_execution(repo, number):
        audit.append("run_skipped", reason="already_completed", repo=repo, number=number)
        return OpResult(False, "skipped", {"reason": "already_completed"})
    try:
        issue = gh.issue_full(repo, number)
    except Exception as e:  # noqa: BLE001
        audit.append("run_error", error=str(e), repo=repo, number=number)
        return OpResult(False, "error", {"reason": str(e)})
    if not _operator_authorized(policy, identity, audit, issue.author):
        return OpResult(False, "denied", {"reason": "operator_not_authorized"})
    fm, body = gh.parse_front_matter(issue.body)
    errs = gh.front_matter_safety_check(fm)
    if errs:
        audit.append("run_blocked", errors=errs, repo=repo, number=number)
        try:
            gh.add_label(repo, number, "legion-bridge:blocked", timeout=policy.github_timeout_s)
            gh.post_comment(repo, number, f"legion-bridge blocked: {errs}", timeout=policy.github_timeout_s)
        except Exception:  # noqa: BLE001
            pass
        db.update_status(repo, number, "blocked")
        return OpResult(False, "blocked", {"errors": errs})
    requested = str(fm.get("harness") or "omp")
    hp = policy.harness(requested)
    if hp is None:
        audit.append("run_blocked", reason="unknown_harness", harness=requested)
        return OpResult(False, "blocked", {"reason": f"unknown_harness:{requested}"})
    if not shutil.which(hp.binary):
        audit.append("run_blocked", reason="harness_not_installed", harness=requested, binary=hp.binary)
        try:
            gh.post_comment(
                repo, number,
                f"legion-bridge: harness {requested!r} ({hp.binary}) not installed on {identity.hostname}",
                timeout=policy.github_timeout_s,
            )
        except Exception:  # noqa: BLE001
            pass
        db.update_status(repo, number, "failed", harness_resolved=hp.name)
        return OpResult(False, "blocked", {"reason": "harness_not_installed", "harness": requested, "binary": hp.binary})
    if not _frontier_authorized(policy, audit, hp):
        try:
            gh.post_comment(
                repo, number,
                f"legion-bridge: harness {requested!r} requires frontier, but allow_frontier is false",
                timeout=policy.github_timeout_s,
            )
        except Exception:  # noqa: BLE001
            pass
        db.update_status(repo, number, "denied_frontier")
        return OpResult(False, "denied", {"reason": "frontier_disabled", "harness": requested})
    input_text = None
    if hp.input_template:
        try:
            input_text = hp.input_template.format(body=body, front_matter=json.dumps(fm, sort_keys=True))
        except KeyError as e:
            audit.append("run_blocked", reason="input_template_key_error", key=str(e))
            return OpResult(False, "blocked", {"reason": f"input_template_key:{e}"})
        except Exception as e:  # noqa: BLE001
            audit.append("run_blocked", reason="input_template_error", error=str(e))
            return OpResult(False, "blocked", {"reason": f"input_template_error:{e}"})
    argv = list(hp.argv_template)
    if input_text is not None:
        argv = [input_text if a == "{prompt}" else a for a in argv]
    workdir = policy.workdir_root / f"{repo.replace('/', '__')}__n{number}"
    workdir.mkdir(parents=True, exist_ok=True)
    front_matter_sha = sha256_hex(canonical_json(fm).encode("utf-8"))
    body_sha = sha256_hex(body.encode("utf-8"))
    input_sha = sha256_hex(input_text.encode("utf-8")) if input_text is not None else None
    manifest = {
        "schema": "legion-bridge/v0.1",
        "worker_id": identity.worker_id,
        "hostname": identity.hostname,
        "repo": repo,
        "issue": number,
        "harness_requested": requested,
        "harness_resolved": hp.name,
        "binary": hp.binary,
        "argv_template": argv,
        "isolation": hp.isolation,
        "max_runtime_s": hp.timeout_s,
        "started_at": utcnow_iso(),
        "front_matter_sha": front_matter_sha,
        "body_sha": body_sha,
        "input_sha": input_sha,
        "acceptance_exit_code": hp.acceptance_exit_code,
        "acceptance_stdout_contains": hp.acceptance_stdout_contains,
        "acceptance_stdout_equals": hp.acceptance_stdout_equals,
    }
    (workdir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    try:
        gh.add_label(repo, number, RUNNING_LABEL, timeout=policy.github_timeout_s)
    except Exception:  # noqa: BLE001
        pass
    db.update_status(repo, number, "running", harness_resolved=hp.name)
    audit.append("run_start", repo=repo, number=number, harness=hp.name, binary=hp.binary, workdir=str(workdir), input_sha=input_sha, body_sha=body_sha)
    cmd = [hp.binary, *argv]
    env = _build_child_env(hp)
    isolation_cmd = []
    if hp.isolation == "bwrap":
        bw = shutil.which("bwrap")
        if not bw:
            audit.append("run_blocked", reason="bwrap_not_installed")
            db.update_status(repo, number, "failed", harness_resolved=hp.name)
            return OpResult(False, "blocked", {"reason": "bwrap_not_installed"})
        workdir_arg = str(workdir.resolve())
        isolation_cmd = [
            bw,
            "--ro-bind", "/", "/",
            "--tmpfs", "/home",
            "--tmpfs", "/root",
            "--tmpfs", "/tmp",
            "--bind", workdir_arg, workdir_arg,
            "--proc", "/proc",
            "--dev", "/dev",
            "--unshare-pid",
            "--die-with-parent",
        ]
    full_cmd = isolation_cmd + cmd
    if not db.begin_execution(repo, number):
        return OpResult(False, "blocked", {"reason": "execution_already_started"})
    try:
        rc, timed_out = _run_harness(policy, hp, full_cmd, env, workdir, hp.timeout_s, db, repo, number)
    except FileNotFoundError as e:
        audit.append("run_failed", repo=repo, number=number, error=str(e))
        db.update_status(repo, number, "failed", harness_resolved=hp.name)
        return OpResult(False, "failed", {"error": str(e)})
    stdout_path = workdir / "stdout.log"
    stderr_path = workdir / "stderr.log"
    stdout_text = stdout_path.read_text(errors="replace") if stdout_path.exists() else ""
    stderr_text = stderr_path.read_text(errors="replace") if stderr_path.exists() else ""
    stdout_sha = sha256_hex(stdout_text.encode("utf-8"))
    stderr_sha = sha256_hex(stderr_text.encode("utf-8"))
    if timed_out or rc != 0:
        acceptance_passed = False
    else:
        if hp.acceptance_stdout_equals is not None:
            # === ECHELON: byte-exact comparison of the raw stdout.
            # Substring matching must not pass an exact-output work order. ===
            acceptance_passed = (rc == hp.acceptance_exit_code) and (stdout_text == hp.acceptance_stdout_equals)
        elif hp.acceptance_stdout_contains is None:
            acceptance_passed = (rc == hp.acceptance_exit_code)
        else:
            acceptance_passed = (rc == hp.acceptance_exit_code) and (hp.acceptance_stdout_contains in stdout_text)
    output_sha = stdout_sha if acceptance_passed else None
    result = {
        "schema": "legion-bridge/v0.1",
        "rc": rc,
        "timed_out": timed_out,
        "finished_at": utcnow_iso(),
        "stdout_sha": stdout_sha,
        "stderr_sha": stderr_sha,
        "stdout_preview": stdout_text[:256],
        "stderr_preview": stderr_text[:256],
        "acceptance_passed": acceptance_passed,
        "output_sha": output_sha,
    }
    (workdir / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True))
    if not acceptance_passed:
        try:
            gh.remove_label(repo, number, "legion-bridge:interrupted", timeout=policy.github_timeout_s)
            gh.add_label(repo, number, "legion-bridge:failed", timeout=policy.github_timeout_s)
        except Exception:  # noqa: BLE001
            pass
        db.update_status(repo, number, "failed", harness_resolved=hp.name)
        audit.append("run_failed", repo=repo, number=number, rc=rc, timed_out=timed_out, acceptance_passed=acceptance_passed)
        return OpResult(False, "failed", {"rc": rc, "timed_out": timed_out, "acceptance_passed": acceptance_passed})
    try:
        gh.remove_label(repo, number, "legion-bridge:interrupted", timeout=policy.github_timeout_s)
        gh.add_label(repo, number, "legion-bridge:completed", timeout=policy.github_timeout_s)
    except Exception:  # noqa: BLE001
        pass
    db.update_status(repo, number, "completed", harness_resolved=hp.name)
    audit.append("run_ok", repo=repo, number=number, harness=hp.name, input_sha=input_sha, output_sha=output_sha, acceptance_passed=acceptance_passed)
    return OpResult(True, "completed", {"harness": hp.name, "rc": rc, "workdir": str(workdir), "acceptance_passed": acceptance_passed})


def op_publish(policy, identity, audit, db, repo, number):
    if repo not in policy.allowed_repos:
        return OpResult(False, "denied", {"reason": "repo_not_allowed"})
    workdir = policy.workdir_root / f"{repo.replace('/', '__')}__n{number}"
    manifest_path = workdir / "manifest.json"
    result_path = workdir / "result.json"
    if not manifest_path.exists() or not result_path.exists():
        audit.append("publish_denied", reason="missing_manifest_or_result", repo=repo, number=number)
        return OpResult(False, "denied", {"reason": "missing_manifest_or_result"})
    claim = db.get(repo, number)
    if claim and claim["status"] == "publish_failed_permanent":
        return OpResult(False, "publish_failed", {"reason": "retry_limit_reached"})
    manifest = json.loads(manifest_path.read_text())
    result = json.loads(result_path.read_text())
    if db.has_completion_token(repo, number):
        audit.append("publish_skip", reason="already_completed", repo=repo, number=number)
    # === ECHELON: per-worker key path. ===
    secret = SecretFile(policy.key_path).read()
    receipt = {
        "schema": "legion-bridge/v0.1",
        "worker_id": identity.worker_id,
        "hostname": identity.hostname,
        "repo": repo,
        "issue": number,
        "harness_requested": manifest.get("harness_requested"),
        "harness_resolved": manifest.get("harness_resolved"),
        "binary": manifest.get("binary"),
        "argv": manifest.get("argv_template"),
        "started_at": manifest.get("started_at"),
        "finished_at": result.get("finished_at"),
        "rc": result.get("rc"),
        "timed_out": result.get("timed_out"),
        "input_sha": manifest.get("input_sha"),
        "body_sha": manifest.get("body_sha"),
        "front_matter_sha": manifest.get("front_matter_sha"),
        "output_sha": result.get("output_sha"),
        "stdout_sha": result.get("stdout_sha"),
        "acceptance_stdout_contains": manifest.get("acceptance_stdout_contains"),
        "acceptance_stdout_equals": manifest.get("acceptance_stdout_equals"),
        "stderr_sha": result.get("stderr_sha"),
        "stdout_preview": result.get("stdout_preview"),
        "acceptance_passed": result.get("acceptance_passed"),
        "acceptance_exit_code": manifest.get("acceptance_exit_code"),
        "acceptance_stdout_contains": manifest.get("acceptance_stdout_contains"),
    }
    payload_for_sig = {k: v for k, v in receipt.items() if k != "signature"}
    sig = sign(secret, canonical_json(payload_for_sig).encode("utf-8"))
    receipt["signature"] = sig
    token = sha256_hex(canonical_json({"repo": repo, "issue": number, "worker_id": identity.worker_id, "signature": sig}).encode("utf-8"))
    (workdir / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True))
    try:
        existing = gh.find_receipt_comment(repo, number, sig)
    except Exception as e:  # noqa: BLE001
        attempts = db.record_publish_attempt(repo, number, str(e))
        db.update_status(repo, number, "publish_failed_permanent" if attempts >= policy.publish_max_attempts else "publish_failed")
        return OpResult(False, "publish_failed", {"reason": "receipt_history_unavailable"})
    if existing is not None:
        db.mark_completed(repo, number, token, sig, identity.worker_id)
        db.update_status(repo, number, "receipt_published")
        db.clear_publish_retry(repo, number)
        audit.append("publish_dedup", repo=repo, number=number, comment_id=existing)
        return OpResult(True, "receipt_published", {"signature": sig, "dedup": existing})
    body = (
        f"## legion-bridge receipt\n\n"
        f"- worker_id: `{receipt['worker_id']}`\n"
        f"- hostname: `{receipt['hostname']}`\n"
        f"- harness: `{receipt['harness_resolved']}` (requested `{receipt['harness_requested']}`)\n"
        f"- binary: `{receipt['binary']}`\n"
        f"- rc: `{receipt['rc']}`  timed_out: `{receipt['timed_out']}`  acceptance: `{receipt['acceptance_passed']}`\n"
        f"- input_sha: `{receipt['input_sha']}`\n"
        f"- body_sha: `{receipt['body_sha']}`\n"
        f"- output_sha: `{receipt['output_sha']}`\n"
        f"- started_at: `{receipt['started_at']}`\n"
        f"- finished_at: `{receipt['finished_at']}`\n"
        f"- stdout_preview: `{receipt['stdout_preview']}`\n"
        f"- signature: `{receipt['signature']}`\n"
    )
    last_err = None
    for attempt in range(policy.publish_retry_max):
        try:
            # Reconcile before every retry, including a POST whose response was lost.
            if gh.find_receipt_comment(repo, number, sig) is None:
                gh.post_comment(repo, number, body, timeout=policy.github_timeout_s)
            last_err = None
            break
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
            time.sleep(1 + attempt * 2)
    if last_err is not None:
        attempts = db.record_publish_attempt(repo, number, last_err)
        if attempts >= policy.publish_max_attempts:
            db.update_status(repo, number, "publish_failed_permanent")
            audit.append("publish_failed_permanent", error=last_err, repo=repo, number=number, attempts=attempts)
        else:
            db.update_status(repo, number, "publish_failed")
            audit.append("publish_failed", error=last_err, repo=repo, number=number, attempts=attempts)
        return OpResult(False, "publish_failed", {"error": last_err, "attempts": attempts})
    db.mark_completed(repo, number, token, sig, identity.worker_id)
    db.update_status(repo, number, "receipt_published")
    db.clear_publish_retry(repo, number)
    audit.append("publish_ok", repo=repo, number=number, signature=receipt["signature"], token=token)
    return OpResult(True, "receipt_published", {"signature": sig, "token": token})


def op_process_one(policy, identity, audit, db, repo, number):
    cr = op_claim(policy, identity, audit, db, repo, number)
    if not cr.ok:
        return cr
    rr = op_run(policy, identity, audit, db, repo, number)
    if not rr.ok and rr.status == "blocked":
        return rr
    pr = op_publish(policy, identity, audit, db, repo, number)
    return pr


def op_recover(policy, identity, audit, db):
    recovered = []
    now_s = int(time.time())
    for claim in db.list_unpublished():
        repo, number = claim["repo"], claim["issue_number"]
        if repo in policy.allowed_repos and claim["worker_id"] == identity.worker_id:
            workdir = policy.workdir_root / f"{repo.replace('/', '__')}__n{number}"
            if (workdir / "result.json").exists():
                op_publish(policy, identity, audit, db, repo, number)
    for claim in db.list_active():
        if claim["lease_expires_at"] > now_s:
            continue
        repo, number = claim["repo"], claim["issue_number"]
        if repo not in policy.allowed_repos:
            audit.append("recover_denied", repo=repo, number=number, reason="repo_not_allowed")
            continue
        if claim["worker_id"] == identity.worker_id and (db.has_execution(repo, number) or claim["status"] == "running"):
            workdir = policy.workdir_root / f"{repo.replace('/', '__')}__n{number}"
            if (workdir / "result.json").exists():
                op_publish(policy, identity, audit, db, repo, number)
            else:
                db.update_status(repo, number, "interrupted_uncertain")
                # Persist a fence even for pre-upgrade running claims.
                db.begin_execution(repo, number)
                audit.append("recover_uncertain", repo=repo, number=number)
            continue
        try:
            issue = gh.issue_full(repo, number)
        except Exception as e:  # noqa: BLE001
            audit.append("recover_unreachable", error=str(e), repo=repo, number=number)
            continue
        is_mine = claim["worker_id"] == identity.worker_id
        if is_mine:
            db.update_status(repo, number, "recovered")
            try:
                gh.remove_label(repo, number, CLAIMED_LABEL, timeout=policy.github_timeout_s)
                gh.remove_label(repo, number, RUNNING_LABEL, timeout=policy.github_timeout_s)
                gh.add_label(repo, number, INTAKE_LABEL, timeout=policy.github_timeout_s)
                gh.add_label(repo, number, "legion-bridge:interrupted", timeout=policy.github_timeout_s)
                gh.post_comment(repo, number, "legion-bridge: stale claim recovered; requeueing", timeout=policy.github_timeout_s)
            except Exception:  # noqa: BLE001
                pass
            recovered.append({"repo": repo, "number": number, "reason": "stale_lease"})
        else:
            db.update_status(repo, number, "stale_foreign")
    audit.append("recover_complete", recovered=recovered)
    return recovered
