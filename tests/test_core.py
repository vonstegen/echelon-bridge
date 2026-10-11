"""Tests for legion-bridge core, no network."""
from __future__ import annotations

import json
import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path

from legion_bridge.core import (
    AuditLog,
    ClaimStore,
    HarnessPolicy,
    Policy,
    SecretFile,
    _parse_toml,
    canonical_json,
    load_policy,
    redact_secrets,
    sign,
    utcnow_epoch,
    verify,
)


def _write_policy(tmp: Path, body: str) -> Path:
    p = tmp / "policy.toml"
    p.write_text(body)
    return p


class TestRedact(unittest.TestCase):
    def test_ghu_token_redacted(self) -> None:
        text = "Authorization: ghu_abcdefghijklmnopqrstuvwxyz0123456789"
        out, n = redact_secrets(text)
        self.assertEqual(n, 1)
        self.assertNotIn("ghu_", out)
        self.assertIn("<redacted:secret>", out)

    def test_no_secrets(self) -> None:
        text = "just a normal log line"
        out, n = redact_secrets(text)
        self.assertEqual(n, 0)
        self.assertEqual(out, text)


class TestToml(unittest.TestCase):
    def test_minimal_policy(self) -> None:
        body = """
[worker]
id = "LEGION"
type = "engineering"

allowed_repos = ["a/b", "c/d"]
lease_ttl_s = 600
heartbeat_interval_s = 60

[[harness.omp]]
binary = "omp"
argv_template = ["--version"]
isolation = "none"
env_passthrough = ["PATH"]
timeout_s = 600
max_cost_usd = 0.0
harness_kind = "omp"
"""
        parsed = _parse_toml(body)
        self.assertEqual(parsed["worker"]["id"], "LEGION")
        # Bare keys placed after a [section] live under that section.
        self.assertEqual(parsed["worker"]["allowed_repos"], ["a/b", "c/d"])
        self.assertEqual(parsed["harness"]["omp"][0]["binary"], "omp")
        self.assertEqual(parsed["harness"]["omp"][0]["argv_template"], ["--version"])


class TestSign(unittest.TestCase):
    def test_round_trip(self) -> None:
        secret = b"k" * 32
        payload = b"hello"
        sig = sign(secret, payload)
        self.assertTrue(verify(secret, payload, sig))
        self.assertFalse(verify(secret, payload + b"x", sig))


class TestSecretFile(unittest.TestCase):
    def test_create_and_read(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "worker.key"
            sf = SecretFile(p)
            k1 = sf.read()
            self.assertTrue(p.exists())
            mode = stat.S_IMODE(p.stat().st_mode)
            self.assertEqual(mode, 0o600)
            k2 = sf.read()
            self.assertEqual(k1, k2)

    def test_reject_world_readable(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "worker.key"
            p.write_bytes(b"x" * 32)
            os.chmod(p, 0o644)
            with self.assertRaises(PermissionError):
                SecretFile(p).read()


class TestClaimStore(unittest.TestCase):
    def test_first_claim_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            db = ClaimStore(Path(d) / "c.sqlite")
            ok, info = db.upsert_claim("o/r", 1, "W1", "sha", 60)
            self.assertTrue(ok)
            self.assertEqual(info, "claimed")

    def test_self_reclaim_blocked_within_lease(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            db = ClaimStore(Path(d) / "c.sqlite")
            db.upsert_claim("o/r", 1, "W1", "sha", 60)
            ok, info = db.upsert_claim("o/r", 1, "W1", "sha", 60)
            self.assertFalse(ok)
            self.assertIn("skipped-already-claimed", info)

    def test_foreign_worker_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            db = ClaimStore(Path(d) / "c.sqlite")
            db.upsert_claim("o/r", 1, "W1", "sha", 600)
            ok, info = db.upsert_claim("o/r", 1, "W2", "sha", 60)
            self.assertFalse(ok)
            self.assertIn("skipped-already-claimed-by:W1", info)

    def test_lease_expiry_allows_reclaim(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            db = ClaimStore(Path(d) / "c.sqlite")
            db.upsert_claim("o/r", 1, "W1", "sha", 1)
            # Re-write row with a long-past lease to simulate expiry.
            past = utcnow_epoch() - 100
            with sqlite3.connect(str(Path(d) / "c.sqlite")) as conn:
                conn.execute(
                    "UPDATE claims SET lease_expires_at=? WHERE repo='o/r' AND issue_number=1",
                    (past,),
                )
                conn.commit()
            ok, info = db.upsert_claim("o/r", 1, "W2", "sha2", 60)
            self.assertTrue(ok, info)
            self.assertEqual(info, "claimed")


class TestAudit(unittest.TestCase):
    def test_appends_redact(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            a = AuditLog(Path(d) / "audit.jsonl")
            a.append("test_event", token="ghu_abcdefghijklmnopqrstuvwxyz0123456789", note="ok")
            text = (Path(d) / "audit.jsonl").read_text()
            self.assertNotIn("ghu_", text)
            self.assertIn("<redacted:secret>", text)
            self.assertIn('"redaction_count":1', text)


class TestPolicyLoad(unittest.TestCase):
    def test_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            body = """
[worker]
id = "LEGION"
type = "engineering"

allowed_repos = ["o/r"]
lease_ttl_s = 600
heartbeat_interval_s = 60
poll_interval_s = 30
poll_limit = 25
publish_retry_max = 3
allow_frontier = false
worker_unique = true
health_host = "127.0.0.1"
health_port = 0
workdir_root = "/tmp/w"
state_root = "/tmp/s"

[[harness.omp]]
binary = "omp"
argv_template = ["--version"]
isolation = "none"
env_passthrough = ["PATH"]
timeout_s = 600
max_cost_usd = 0.0
harness_kind = "omp"
"""
            p = _write_policy(Path(d), body)
            pol = load_policy(p)
            self.assertEqual(pol.worker_id, "LEGION")
            self.assertEqual(pol.allowed_repos, ["o/r"])
            self.assertIn("omp", pol.harness_policies)


class TestFrontMatterSafety(unittest.TestCase):
    def test_allow_git_write_rejected(self) -> None:
        from legion_bridge.github import front_matter_safety_check
        errs = front_matter_safety_check({"allow_git_write": "true"})
        self.assertTrue(any("allow_git_write" in e for e in errs))

    def test_runtime_capped(self) -> None:
        from legion_bridge.github import front_matter_safety_check
        errs = front_matter_safety_check({"max_runtime_s": "9999"})
        self.assertTrue(any("max_runtime_s" in e for e in errs))

    def test_unknown_harness_rejected(self) -> None:
        from legion_bridge.github import front_matter_safety_check
        errs = front_matter_safety_check({"harness": "rogue"})
        self.assertTrue(any("unsupported harness" in e for e in errs))

    def test_clean_passes(self) -> None:
        from legion_bridge.github import front_matter_safety_check
        errs = front_matter_safety_check({"harness": "omp", "max_runtime_s": "60"})
        self.assertEqual(errs, [])


if __name__ == "__main__":
    unittest.main()
