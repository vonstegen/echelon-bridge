"""Tests for legion-bridge core, no network."""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from legion_bridge.core import (  # noqa: E402
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
        out, n = redact_secrets("ghu_aaaa")
        self.assertEqual(n, 1)
        self.assertNotIn("ghu_", out)
        self.assertIn("<redacted:secret>", out)

    def test_no_secrets(self) -> None:
        text = "hello world"
        out, n = redact_secrets(text)
        self.assertEqual(n, 0)
        self.assertEqual(out, text)


class TestToml(unittest.TestCase):
    def test_minimal_policy(self) -> None:
        text = "[harness]\n[[harness.omp]]\nbinary = \"omp\"\nargv_template = [\"--version\"]\n"
        parsed = _parse_toml(text)
        self.assertIn("harness", parsed)
        self.assertEqual(parsed["harness"]["omp"][0]["argv_template"], ["--version"])


class TestSign(unittest.TestCase):
    def test_round_trip(self) -> None:
        secret = b"k" * 32
        payload = b"hello"
        sig = sign(secret, payload)
        self.assertTrue(len(sig) == 64)
        # Tampered payload must not verify.
        self.assertFalse(verify(secret, payload + b"x", sig))


class TestSecretFile(unittest.TestCase):
    def test_create_and_read(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "k"
            sf = SecretFile(p)
            k1 = sf.read()
            self.assertEqual(len(k1), 32)
            k2 = sf.read()
            self.assertEqual(k1, k2)
            # World-readable must be refused.
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
            self.assertIn("skipped-foreign-claim:W1", info)

    def test_lease_expiry_allows_self_reclaim(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            db = ClaimStore(Path(d) / "c.sqlite")
            db.upsert_claim("o/r", 1, "W1", "sha", 1)
            past = utcnow_epoch() - 100
            with sqlite3.connect(str(Path(d) / "c.sqlite")) as conn:
                conn.execute(
                    "UPDATE claims SET lease_expires_at=? WHERE repo='o/r' AND issue_number=1",
                    (past,),
                )
                conn.commit()
            # The original worker may reclaim its own expired lease.
            ok, info = db.upsert_claim("o/r", 1, "W1", "sha2", 60)
            self.assertTrue(ok, info)
            self.assertEqual(info, "claimed")

    def test_foreign_worker_cannot_steal_expired_lease(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            db = ClaimStore(Path(d) / "c.sqlite")
            db.upsert_claim("o/r", 1, "W1", "sha", 1)
            past = utcnow_epoch() - 100
            with sqlite3.connect(str(Path(d) / "c.sqlite")) as conn:
                conn.execute(
                    "UPDATE claims SET lease_expires_at=? WHERE repo='o/r' AND issue_number=1",
                    (past,),
                )
                conn.commit()
            ok, info = db.upsert_claim("o/r", 1, "W2", "sha2", 60)
            self.assertFalse(ok)
            self.assertIn("skipped-foreign-claim:W1", info)

    def test_stale_foreign_row_blocks_any_reclaim(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            db = ClaimStore(Path(d) / "c.sqlite")
            db.upsert_claim("o/r", 1, "W1", "sha", 1)
            past = utcnow_epoch() - 100
            with sqlite3.connect(str(Path(d) / "c.sqlite")) as conn:
                conn.execute(
                    "UPDATE claims SET lease_expires_at=?, status='stale_foreign' WHERE repo='o/r' AND issue_number=1",
                    (past,),
                )
                conn.commit()
            # Even after a long-expired lease with status=stale_foreign,
            # no other worker may claim. The issue is left for the
            # rightful owner or an operator decision.
            ok, info = db.upsert_claim("o/r", 1, "W2", "sha2", 60)
            self.assertFalse(ok)
            self.assertIn("skipped-foreign-claim:W1", info)


from legion_bridge.core import AuditLog  # noqa: E402


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
            body = (
                "[worker]\nid = \"LEGION\"\ntype = \"engineering\"\n"
                "allowed_repos = [\"o/r\"]\nlease_ttl_s = 600\n"
                "heartbeat_interval_s = 60\npoll_interval_s = 30\n"
                "poll_limit = 25\npublish_retry_max = 3\n"
                "allow_frontier = false\nworker_unique = true\n"
                "health_host = \"127.0.0.1\"\nhealth_port = 0\n"
                "workdir_root = \"/tmp/w\"\nstate_root = \"/tmp/s\"\n"
                "[[harness.omp]]\nbinary = \"omp\"\n"
                "argv_template = [\"--version\"]\n"
                "isolation = \"none\"\nenv_passthrough = [\"PATH\"]\n"
                "timeout_s = 600\nmax_cost_usd = 0.0\nharness_kind = \"omp\"\n"
            )
            p = _write_policy(Path(d), body)
            pol = load_policy(p)
            self.assertEqual(pol.worker_id, "LEGION")
            self.assertEqual(pol.allowed_repos, ["o/r"])
            self.assertEqual(pol.mode, "STANDBY")
            self.assertFalse(pol.distributed_claim_protocol_proven)
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
        errs = front_matter_safety_check({"harness": "rogue!"})
        self.assertTrue(any("unsupported harness identifier" in e for e in errs))

    def test_clean_passes(self) -> None:
        from legion_bridge.github import front_matter_safety_check
        errs = front_matter_safety_check({"harness": "omp", "max_runtime_s": "60"})
        self.assertEqual(errs, [])


if __name__ == "__main__":
    unittest.main()
