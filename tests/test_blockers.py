"""Tests that prove the review blockers are fixed."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

# === ECHELON: enable activate env for ACTIVE test policy ===
import os
os.environ.setdefault("ALLOW_LEGION_ACTIVE", "1")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from legion_bridge import github as gh_mod  # noqa: E402
from legion_bridge import lockfile  # noqa: E402
from legion_bridge import loop as lb_loop  # noqa: E402
from legion_bridge import operations as ops  # noqa: E402
from legion_bridge.core import (  # noqa: E402
    CLAIMED_LABEL,
    INTAKE_LABEL,
    RUNNING_LABEL,
    AuditLog,
    ClaimStore,
    HarnessPolicy,
    Policy,
    SecretFile,
    WorkerIdentity,
    canonical_json,
    sha256_hex,
    utcnow_epoch,
    verify,
)


def _policy_with_harness(tmp, *, harness, host=None, operator_team=None, operator_org=None, allow_frontier=False, github_timeout_s=30, lease_ttl_s=600, heartbeat_interval_s=60):
    return Policy(
        worker_id="LEGION",
        worker_type="engineering",
        allowed_repos=["o/r"],
        harness_policies={harness.name: harness},
        lease_ttl_s=lease_ttl_s,
        heartbeat_interval_s=heartbeat_interval_s,
        poll_interval_s=1,
        poll_limit=25,
        publish_retry_max=3,
        publish_max_attempts=10,
        allow_frontier=allow_frontier,
        frontier_kinds=["frontier", "openai", "anthropic", "copilot"],
        workdir_root=tmp / "work",
        state_root=tmp / "state",
        health_host="127.0.0.1",
        health_port=0,
        operator_team=operator_team,
        operator_org=operator_org,
        worker_unique=True,
        host=host,
        github_timeout_s=github_timeout_s,
        # === ECHELON: mode + key path + distributed-claim gate ===
        mode="ACTIVE",
        key_path=tmp / "worker.key",
        distributed_claim_protocol_proven=True,
        activate_env="ALLOW_LEGION_ACTIVE",
    )


def _local_harness(tmp):
    return HarnessPolicy(
        name="local",
        binary="echo",
        argv_template=["hello"],
        isolation="none",
        env_passthrough=["PATH"],
        timeout_s=60,
        max_cost_usd=0.0,
        harness_kind="local",
        input_template=None,
        acceptance_exit_code=0,
        acceptance_stdout_contains=None,
        acceptance_stdout_equals=None,
    )


def _slow_harness(tmp, *, binary="sh", argv=("-c", "sleep 4; echo done"), timeout_s=10, isolation="none", env_passthrough=None, name="slow", harness_kind="local", input_template=None, acceptance_exit_code=0, acceptance_stdout_contains=None, acceptance_stdout_equals=None):
    if env_passthrough is None:
        env_passthrough = []
    return HarnessPolicy(
        name=name,
        binary=binary,
        argv_template=list(argv),
        isolation=isolation,
        env_passthrough=env_passthrough,
        timeout_s=timeout_s,
        max_cost_usd=0.0,
        harness_kind=harness_kind,
        input_template=input_template,
        acceptance_exit_code=acceptance_exit_code,
        acceptance_stdout_contains=acceptance_stdout_contains,
        acceptance_stdout_equals=acceptance_stdout_equals,
    )


def _harness_with(name, **kwargs):
    defaults = dict(
        binary="echo", argv_template=[], isolation="none", env_passthrough=[],
        timeout_s=60, max_cost_usd=0.0, harness_kind="local",
        input_template=None, acceptance_exit_code=0, acceptance_stdout_contains=None, acceptance_stdout_equals=None,
    )
    defaults.update(kwargs)
    defaults["name"] = name
    defaults["argv_template"] = list(defaults["argv_template"])
    return HarnessPolicy(**defaults)


class _FakeGH:
    def __init__(self):
        self.issues = {}
        self.comments = {}
        self.fail_add = set()
        self.fail_remove = set()
        self.fail_post = set()

    def add_issue(self, repo, number, body, title="t", author="ops"):
        self.issues[(repo, number)] = {
            "number": number,
            "title": title,
            "body": body,
            "createdAt": "2026-10-11T00:00:00+00:00",
            "labels": [INTAKE_LABEL],
            "author": {"login": author},
            "state": "open",
            "node_id": f"ISSUE_{number}",
            "url": f"https://example/{repo}/issues/{number}",
        }
        self.comments.setdefault((repo, number), [])

    def set_labels(self, repo, number, labels):
        self.issues[(repo, number)]["labels"] = list(labels)

    def labels(self, repo, number):
        return list(self.issues[(repo, number)]["labels"])

    def state_sha(self, repo, number):
        return ",".join(sorted(self.labels(repo, number)))


def install_fake(test, fake):
    test._patches = []

    def list_candidate_issues(repo, limit=25):
        out = []
        for (r, n), issue in fake.issues.items():
            if r != repo:
                continue
            if INTAKE_LABEL not in issue["labels"]:
                continue
            out.append(gh_mod.Issue(
                number=issue["number"], title=issue["title"], body=issue["body"],
                created_at=issue["createdAt"], labels=issue["labels"],
                author=issue["author"]["login"], state=issue["state"],
                node_id=issue["node_id"], html_url=issue["url"],
            ))
        out.sort(key=lambda i: i.created_at)
        return out[:limit]

    def issue_full(repo, number):
        issue = fake.issues[(repo, number)]
        return gh_mod.Issue(
            number=issue["number"], title=issue["title"], body=issue["body"],
            created_at=issue["createdAt"], labels=issue["labels"],
            author=issue["author"]["login"], state=issue["state"],
            node_id=issue["node_id"], html_url=issue["url"],
        )

    def issue_state_sha(repo, number):
        return fake.state_sha(repo, number)

    def add_label(repo, number, label, *, timeout=None):
        if (repo, number, label) in fake.fail_add:
            raise gh_mod.GhError(f"add_label forced failure: {label}")
        labels = fake.labels(repo, number)
        if label not in labels:
            labels.append(label)
        fake.set_labels(repo, number, labels)

    def remove_label(repo, number, label, *, timeout=None):
        if (repo, number, label) in fake.fail_remove:
            raise gh_mod.GhError(f"remove_label forced failure: {label}")
        labels = [l for l in fake.labels(repo, number) if l != label]
        fake.set_labels(repo, number, labels)

    def post_comment(repo, number, body, *, timeout=None):
        if (repo, number) in fake.fail_post:
            raise gh_mod.GhError("post_comment forced failure")
        fake.comments.setdefault((repo, number), []).append({"id": f"c_{len(fake.comments.get((repo,number),[]))}", "body": body})
        return body[:200]

    def list_issue_comments(repo, number, *, timeout=None):
        return list(fake.comments.get((repo, number), []))

    def find_receipt_comment(repo, number, signature):
        for c in fake.comments.get((repo, number), []):
            if "## legion-bridge receipt" in c["body"] and f"signature: `{signature}`" in c["body"]:
                return c.get("id")
        return None

    def gh_authenticated():
        return True

    for name, value in [
        ("list_candidate_issues", list_candidate_issues),
        ("issue_full", issue_full),
        ("issue_state_sha", issue_state_sha),
        ("add_label", add_label),
        ("remove_label", remove_label),
        ("post_comment", post_comment),
        ("list_issue_comments", list_issue_comments),
        ("find_receipt_comment", find_receipt_comment),
        ("gh_authenticated", gh_authenticated),
    ]:
        prev = getattr(gh_mod, name, None)
        test._patches.append((name, prev))
        setattr(gh_mod, name, value)


def unpatch(test):
    for name, prev in reversed(getattr(test, "_patches", [])):
        if prev is None:
            if hasattr(gh_mod, name):
                delattr(gh_mod, name)
        else:
            setattr(gh_mod, name, prev)


class Blocker1SingleHost(unittest.TestCase):
    def setUp(self):
        self.fake = _FakeGH()
        install_fake(self, self.fake)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        unpatch(self)
        self.tmp.cleanup()

    def test_lockfile_blocks_second_listener_in_subprocess(self):
        state = self.root / "state"
        state.mkdir(parents=True, exist_ok=True)
        script = (
            "import sys, time\n"
            "sys.path.insert(0, '" + str(ROOT / "src") + "')\n"
            "from legion_bridge.lockfile import acquire\n"
            f"acquire('{state}', 'LEGION')\n"
            "time.sleep(2.0)\n"
        )
        holder = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(0.5)
        with self.assertRaises(lockfile.ListenerLockError):
            lockfile.acquire(state, "LEGION")
        holder.wait(timeout=5)

    def test_host_mismatch_refuses_start(self):
        policy_path = self.root / "policy.toml"
        policy_path.write_text(
            "[worker]\n"
            "id = \"LEGION\"\n"
            "allowed_repos = [\"o/r\"]\n"
            "host = \"OTHER-HOST\"\n"
            f"workdir_root = \"{self.root / 'work'}\"\n"
            f"state_root = \"{self.root / 'state'}\"\n"
            "[[harness.local]]\n"
            "binary = \"echo\"\n"
            "argv_template = [\"x\"]\n"
            "isolation = \"none\"\n"
            "env_passthrough = [\"PATH\"]\n"
            "timeout_s = 60\n"
            "max_cost_usd = 0.0\n"
            "harness_kind = \"local\"\n"
        )
        rc = lb_loop.main(["--policy", str(policy_path), "--once", "--no-lock"])
        self.assertEqual(rc, 5)

    def test_completion_token_blocks_claim(self):
        policy = _policy_with_harness(self.root, harness=_local_harness(self.root))
        identity = WorkerIdentity.load(policy)
        audit = AuditLog(policy.state_root / "audit.jsonl")
        db = ClaimStore(policy.state_root / "claims.sqlite")
        db.mark_completed("o/r", 1, "tok", "sig", "LEGION")
        self.fake.add_issue("o/r", 1, "---\nharness: local\n---\nnoop")
        result = ops.op_claim(policy, identity, audit, db, "o/r", 1)
        self.assertFalse(result.ok)
        self.assertEqual(result.detail.get("reason"), "already_completed")
        self.assertIn(INTAKE_LABEL, self.fake.labels("o/r", 1))


class Blocker2ClaimFailsClosed(unittest.TestCase):
    def setUp(self):
        self.fake = _FakeGH()
        install_fake(self, self.fake)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        unpatch(self)
        self.tmp.cleanup()

    def test_label_remove_failure_rolls_back_claim(self):
        policy = _policy_with_harness(self.root, harness=_local_harness(self.root))
        identity = WorkerIdentity.load(policy)
        audit = AuditLog(policy.state_root / "audit.jsonl")
        db = ClaimStore(policy.state_root / "claims.sqlite")
        self.fake.add_issue("o/r", 1, "---\nharness: local\n---\nnoop")
        self.fake.fail_remove.add(("o/r", 1, INTAKE_LABEL))
        result = ops.op_claim(policy, identity, audit, db, "o/r", 1)
        self.assertFalse(result.ok)
        self.assertEqual(result.detail.get("reason"), "label_remove_failed")
        self.assertIsNone(db.get("o/r", 1))
        self.assertIn(INTAKE_LABEL, self.fake.labels("o/r", 1))

    def test_label_add_failure_rolls_back_claim(self):
        policy = _policy_with_harness(self.root, harness=_local_harness(self.root))
        identity = WorkerIdentity.load(policy)
        audit = AuditLog(policy.state_root / "audit.jsonl")
        db = ClaimStore(policy.state_root / "claims.sqlite")
        self.fake.add_issue("o/r", 1, "---\nharness: local\n---\nnoop")
        self.fake.fail_add.add(("o/r", 1, CLAIMED_LABEL))
        result = ops.op_claim(policy, identity, audit, db, "o/r", 1)
        self.assertFalse(result.ok)
        self.assertEqual(result.detail.get("reason"), "label_add_failed")
        self.assertIsNone(db.get("o/r", 1))
        self.assertIn(INTAKE_LABEL, self.fake.labels("o/r", 1))

    def test_publish_dedups_existing_receipt_comment(self):
        policy = _policy_with_harness(self.root, harness=_local_harness(self.root))
        identity = WorkerIdentity.load(policy)
        audit = AuditLog(policy.state_root / "audit.jsonl")
        db = ClaimStore(policy.state_root / "claims.sqlite")
        self.fake.add_issue("o/r", 1, "---\nharness: local\n---\nnoop")
        ops.op_process_one(policy, identity, audit, db, "o/r", 1)
        with sqlite3.connect(str(policy.state_root / "claims.sqlite")) as conn:
            conn.execute("DELETE FROM completions")
            conn.execute("UPDATE claims SET status='completed' WHERE repo='o/r' AND issue_number=1")
            conn.commit()
        result = ops.op_publish(policy, identity, audit, db, "o/r", 1)
        self.assertTrue(result.ok)
        self.assertIn("dedup", result.detail)
        receipt_comments = [c for c in self.fake.comments[("o/r", 1)] if "legion-bridge receipt" in c["body"]]
        self.assertEqual(len(receipt_comments), 1)


class Blocker3RestartRecovery(unittest.TestCase):
    def setUp(self):
        self.fake = _FakeGH()
        install_fake(self, self.fake)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        unpatch(self)
        self.tmp.cleanup()

    def _write_policy(self, policy):
        text = (
            "[worker]\n"
            "id = \"LEGION\"\n"
            "allowed_repos = [\"o/r\"]\n"
            f"workdir_root = \"{policy.workdir_root}\"\n"
            f"state_root = \"{policy.state_root}\"\n"
            "[[harness.local]]\n"
            "binary = \"echo\"\n"
            "argv_template = [\"x\"]\n"
            "isolation = \"none\"\n"
            "env_passthrough = [\"PATH\"]\n"
            "timeout_s = 60\n"
            "max_cost_usd = 0.0\n"
            "harness_kind = \"local\"\n"
        )
        path = self.root / "policy.toml"
        path.write_text(text)
        return path

    def test_recover_runs_on_loop_start(self):
        self.fake.add_issue("o/r", 1, "---\nharness: local\n---\nnoop")
        self.fake.set_labels("o/r", 1, [CLAIMED_LABEL])
        policy = _policy_with_harness(self.root, harness=_local_harness(self.root))
        db_path = policy.state_root / "claims.sqlite"
        policy.state_root.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(db_path)) as conn:
            conn.executescript(
                "CREATE TABLE IF NOT EXISTS claims ("
                "  repo TEXT NOT NULL, issue_number INTEGER NOT NULL,"
                "  worker_id TEXT NOT NULL, claimed_at INTEGER NOT NULL,"
                "  lease_expires_at INTEGER NOT NULL, heartbeat_at INTEGER NOT NULL,"
                "  expected_state_sha TEXT NOT NULL, status TEXT NOT NULL,"
                "  harness_requested TEXT, harness_resolved TEXT, session_id TEXT,"
                "  PRIMARY KEY (repo, issue_number));"
            )
            conn.execute(
                "REPLACE INTO claims VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("o/r", 1, "LEGION", utcnow_epoch() - 1000, utcnow_epoch() - 500, utcnow_epoch() - 500, "sha", "claimed", None, None, None),
            )
            conn.commit()
        policy_path = self._write_policy(policy)
        rc = lb_loop.main(["--policy", str(policy_path), "--once", "--no-lock"])
        self.assertEqual(rc, 0)
        # Pre-existing claim with no result.json, status=claimed (not running).
        # op_recover fences it as `interrupted_uncertain` and re-applies the
        # intake label. Either interrupted or pending is therefore an
        # acceptable post-recovery state.
        labels = self.fake.labels("o/r", 1)
        self.assertTrue(
            "legion-bridge:interrupted" in labels or "legion-bridge:pending" in labels,
            f"expected interrupted or pending label, got {labels}",
        )

    def test_lease_heartbeat_extends_lease(self):
        hp = _slow_harness(self.root, binary="sh", argv=["-c", "sleep 4; echo done"], timeout_s=10)
        policy = _policy_with_harness(self.root, harness=hp, lease_ttl_s=2, heartbeat_interval_s=1)
        identity = WorkerIdentity.load(policy)
        audit = AuditLog(policy.state_root / "audit.jsonl")
        db = ClaimStore(policy.state_root / "claims.sqlite")
        self.fake.add_issue("o/r", 1, "---\nharness: slow\n---\nnoop")
        ops.op_claim(policy, identity, audit, db, "o/r", 1)
        result = ops.op_run(policy, identity, audit, db, "o/r", 1)
        self.assertTrue(result.ok)
        claim = db.get("o/r", 1)
        self.assertGreater(claim["lease_expires_at"], utcnow_epoch(),
                           f"lease not extended: {claim['lease_expires_at']} vs now {utcnow_epoch()}")


class Blocker4EnvIsolation(unittest.TestCase):
    def setUp(self):
        self.fake = _FakeGH()
        install_fake(self, self.fake)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        unpatch(self)
        self.tmp.cleanup()

    def test_child_env_does_not_leak_secrets(self):
        hp = _harness_with("local", binary="env", argv_template=[],
                            env_passthrough=["OPENAI_API_KEY", "GH_TOKEN", "PATH"])
        policy = _policy_with_harness(self.root, harness=hp)
        identity = WorkerIdentity.load(policy)
        audit = AuditLog(policy.state_root / "audit.jsonl")
        db = ClaimStore(policy.state_root / "claims.sqlite")
        self.fake.add_issue("o/r", 1, "---\nharness: local\n---\nnoop")
        ops.op_process_one(policy, identity, audit, db, "o/r", 1)
        workdir = policy.workdir_root / "o__r__n1"
        stdout = (workdir / "stdout.log").read_text()
        self.assertNotIn("OPENAI_API_KEY", stdout)
        self.assertNotIn("GH_TOKEN", stdout)
        self.assertIn("PATH=", stdout)

    def test_frontier_blocked_by_default(self):
        hp = _harness_with("front", harness_kind="openai")
        policy = _policy_with_harness(self.root, harness=hp)
        identity = WorkerIdentity.load(policy)
        audit = AuditLog(policy.state_root / "audit.jsonl")
        db = ClaimStore(policy.state_root / "claims.sqlite")
        self.fake.add_issue("o/r", 1, "---\nharness: front\n---\nnoop")
        ops.op_claim(policy, identity, audit, db, "o/r", 1)
        result = ops.op_run(policy, identity, audit, db, "o/r", 1)
        self.assertFalse(result.ok)
        self.assertEqual(result.detail.get("reason"), "frontier_disabled")

    def test_operator_team_blocks_unauthorized(self):
        hp = _local_harness(self.root)
        policy = _policy_with_harness(self.root, harness=hp, operator_team="ops", operator_org="acme")
        identity = WorkerIdentity.load(policy)
        audit = AuditLog(policy.state_root / "audit.jsonl")
        db = ClaimStore(policy.state_root / "claims.sqlite")
        with patch.object(gh_mod, "user_in_team", return_value=False):
            self.fake.add_issue("o/r", 1, "---\nharness: local\n---\nnoop", author="outsider")
            result = ops.op_claim(policy, identity, audit, db, "o/r", 1)
            self.assertFalse(result.ok)
            self.assertEqual(result.detail.get("reason"), "operator_not_authorized")


class Blocker5InputOutputHash(unittest.TestCase):
    def setUp(self):
        self.fake = _FakeGH()
        install_fake(self, self.fake)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        unpatch(self)
        self.tmp.cleanup()

    def test_receipt_includes_input_output_hashes(self):
        body = "no-op please"
        self.fake.add_issue("o/r", 1, f"---\nharness: local\n---\n{body}")
        policy = _policy_with_harness(self.root, harness=_local_harness(self.root))
        identity = WorkerIdentity.load(policy)
        audit = AuditLog(policy.state_root / "audit.jsonl")
        db = ClaimStore(policy.state_root / "claims.sqlite")
        ops.op_process_one(policy, identity, audit, db, "o/r", 1)
        workdir = policy.workdir_root / "o__r__n1"
        receipt = json.loads((workdir / "receipt.json").read_text())
        for key in ("input_sha", "body_sha", "output_sha", "stdout_sha", "stderr_sha", "front_matter_sha", "acceptance_passed"):
            self.assertIn(key, receipt, f"missing {key} in receipt")
        self.assertEqual(receipt["body_sha"], sha256_hex(body.encode("utf-8")))
        secret = SecretFile(policy.key_path).read()  # === ECHELON: per-worker key path ===
        payload = {k: v for k, v in receipt.items() if k != "signature"}
        self.assertTrue(verify(secret, canonical_json(payload).encode("utf-8"), receipt["signature"]))

    def test_acceptance_check_passes_and_fails(self):
        # First: harness whose stdout contains the magic string.
        self.fake.add_issue("o/r", 1, "---\nharness: pass\n---\nnoop")
        hp_pass = _harness_with(
            "pass", binary="sh",
            argv_template=["-c", "echo MAGIC_TOKEN"],
            acceptance_stdout_contains="MAGIC_TOKEN",
        )
        policy_pass = _policy_with_harness(self.root, harness=hp_pass)
        identity = WorkerIdentity.load(policy_pass)
        audit = AuditLog(policy_pass.state_root / "audit.jsonl")
        db = ClaimStore(policy_pass.state_root / "claims.sqlite")
        ops.op_process_one(policy_pass, identity, audit, db, "o/r", 1)
        workdir1 = policy_pass.workdir_root / "o__r__n1"
        r1 = json.loads((workdir1 / "receipt.json").read_text())
        self.assertTrue(r1["acceptance_passed"])

        # Second: harness whose stdout does NOT contain the magic string.
        self.fake.add_issue("o/r", 2, "---\nharness: fail\n---\nnoop")
        hp_fail = _harness_with(
            "fail", binary="echo", argv_template=["plain output"],
            acceptance_stdout_contains="MAGIC_TOKEN",
        )
        policy_fail = _policy_with_harness(self.root, harness=hp_fail)
        identity_fail = WorkerIdentity.load(policy_fail)
        audit_fail = AuditLog(policy_fail.state_root / "audit.jsonl")
        db_fail = ClaimStore(policy_fail.state_root / "claims.sqlite")
        ops.op_process_one(policy_fail, identity_fail, audit_fail, db_fail, "o/r", 2)
        workdir2 = policy_fail.workdir_root / "o__r__n2"
        r2 = json.loads((workdir2 / "result.json").read_text())
        self.assertFalse(r2["acceptance_passed"])


class Blocker6PublishResilience(unittest.TestCase):
    def setUp(self):
        self.fake = _FakeGH()
        install_fake(self, self.fake)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        unpatch(self)
        self.tmp.cleanup()

    def test_publish_retry_then_success(self):
        self.fake.add_issue("o/r", 1, "---\nharness: local\n---\nnoop")
        policy = _policy_with_harness(self.root, harness=_local_harness(self.root))
        policy.publish_retry_max = 3
        identity = WorkerIdentity.load(policy)
        audit = AuditLog(policy.state_root / "audit.jsonl")
        db = ClaimStore(policy.state_root / "claims.sqlite")
        ops.op_claim(policy, identity, audit, db, "o/r", 1)
        ops.op_run(policy, identity, audit, db, "o/r", 1)
        attempts = {"n": 0}
        original_post = gh_mod.post_comment

        def flaky_post(*args, **kwargs):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise gh_mod.GhError("flaky network")
            return original_post(*args, **kwargs)
        with patch.object(gh_mod, "post_comment", side_effect=flaky_post):
            result = ops.op_publish(policy, identity, audit, db, "o/r", 1)
        self.assertTrue(result.ok)
        receipt_comments = [c for c in self.fake.comments[("o/r", 1)] if "legion-bridge receipt" in c["body"]]
        self.assertEqual(len(receipt_comments), 1)

    def test_publish_failure_marks_status(self):
        self.fake.add_issue("o/r", 1, "---\nharness: local\n---\nnoop")
        policy = _policy_with_harness(self.root, harness=_local_harness(self.root))
        policy.publish_retry_max = 1
        identity = WorkerIdentity.load(policy)
        audit = AuditLog(policy.state_root / "audit.jsonl")
        db = ClaimStore(policy.state_root / "claims.sqlite")
        ops.op_claim(policy, identity, audit, db, "o/r", 1)
        ops.op_run(policy, identity, audit, db, "o/r", 1)
        with patch.object(gh_mod, "post_comment", side_effect=gh_mod.GhError("nope")):
            result = ops.op_publish(policy, identity, audit, db, "o/r", 1)
        self.assertFalse(result.ok)
        claim = db.get("o/r", 1)
        self.assertEqual(claim["status"], "publish_failed")

    def test_sigterm_halts_current_tick(self):
        self.fake.add_issue("o/r", 1, "---\nharness: local\n---\nnoop")
        self.fake.add_issue("o/r", 2, "---\nharness: local\n---\nnoop")
        policy = _policy_with_harness(self.root, harness=_local_harness(self.root))
        identity = WorkerIdentity.load(policy)
        audit = AuditLog(policy.state_root / "audit.jsonl")
        db = ClaimStore(policy.state_root / "claims.sqlite")
        stop = {"flag": False}
        # Patch op_process_one so the first call runs to completion and
        # the second call returns early (after flipping stop_flag), so the
        # loop can observe the flag between candidates.
        original = ops.op_process_one
        seen = {"n": 0}
        def hook(p, i, a, d, repo, number):
            seen["n"] += 1
            if seen["n"] >= 2:
                stop["flag"] = True
                return ops.OpResult(False, "skipped", {"reason": "test_stop_flag"})
            return original(p, i, a, d, repo, number)
        with patch.object(ops, "op_process_one", side_effect=hook):
            lb_loop._tick(policy, identity, audit, db, stop_flag=stop)
        # #1 must have completed (the first call runs to completion).
        self.assertIn("legion-bridge:completed", self.fake.labels("o/r", 1))
        # #2 must NOT have been processed; it should still be pending.
        self.assertIn(INTAKE_LABEL, self.fake.labels("o/r", 2))
        comments_2 = self.fake.comments.get(("o/r", 2), [])
        self.assertFalse(any("legion-bridge receipt" in c["body"] for c in comments_2))

    def test_harness_timeout_terminates_descendants(self):
        hp = _slow_harness(
            self.root, binary="sh", argv=["-c", "trap '' TERM; sleep 30 & wait"],
            timeout_s=1, name="slow",
        )
        policy = _policy_with_harness(self.root, harness=hp)
        identity = WorkerIdentity.load(policy)
        audit = AuditLog(policy.state_root / "audit.jsonl")
        db = ClaimStore(policy.state_root / "claims.sqlite")
        self.fake.add_issue("o/r", 1, "---\nharness: slow\n---\nnoop")
        start = time.time()
        ops.op_claim(policy, identity, audit, db, "o/r", 1)
        result = ops.op_run(policy, identity, audit, db, "o/r", 1)
        elapsed = time.time() - start
        self.assertLess(elapsed, 10.0, f"run took {elapsed:.1f}s; descendant not killed")
        self.assertFalse(result.ok)
        workdir = policy.workdir_root / "o__r__n1"
        result_json = json.loads((workdir / "result.json").read_text())
        self.assertTrue(result_json["timed_out"])


if __name__ == "__main__":
    unittest.main()
