"""Smoke test: simulate a claim, run a local harness, publish a receipt.

Exercises the harness router and execution supervisor without
touching GitHub. Records manifest, stdout, result, and a signed
receipt in a temporary workdir.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from legion_bridge.core import (  # noqa: E402
    AuditLog,
    ClaimStore,
    Policy,
    SecretFile,
    WorkerIdentity,
    canonical_json,
    load_policy,
    sign,
)


def _fresh_policy(tmp: Path) -> Policy:
    workdir = tmp / "work"
    statedir = tmp / "state"
    body = f"""
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
workdir_root = "{workdir}"
state_root = "{statedir}"

[[harness.local]]
binary = "echo"
argv_template = ["local-harness-stub", "ok"]
isolation = "none"
env_passthrough = ["PATH"]
timeout_s = 60
max_cost_usd = 0.0
harness_kind = "local"
"""
    p = tmp / "policy.toml"
    p.write_text(body)
    return load_policy(p)


class TestHarnessRouting(unittest.TestCase):
    def test_local_harness_runs(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            policy = _fresh_policy(tmp)
            identity = WorkerIdentity.load(policy)
            db = ClaimStore(policy.state_root / "claims.sqlite")
            ok, info = db.upsert_claim(
                "o/r", 42, identity.worker_id, "expected-state-sha", policy.lease_ttl_s, "local"
            )
            self.assertTrue(ok, info)
            hp = policy.harness("local")
            self.assertIsNotNone(hp)
            self.assertTrue(shutil.which(hp.binary), "echo must be on PATH")
            workdir = policy.workdir_root / "o__r__n42"
            workdir.mkdir(parents=True, exist_ok=True)
            manifest = {
                "schema": "legion-bridge/v0.1",
                "worker_id": identity.worker_id,
                "hostname": identity.hostname,
                "repo": "o/r",
                "issue": 42,
                "harness_requested": "local",
                "harness_resolved": hp.name,
                "binary": hp.binary,
                "argv_template": hp.argv_template,
                "isolation": hp.isolation,
                "max_runtime_s": hp.timeout_s,
                "started_at": "2026-10-11T00:00:00+00:00",
            }
            (workdir / "manifest.json").write_text(json.dumps(manifest, indent=2))
            db.update_status("o/r", 42, "running", harness_resolved=hp.name)
            proc = subprocess.run(
                [hp.binary, *hp.argv_template],
                cwd=str(workdir),
                capture_output=True,
                text=True,
                timeout=hp.timeout_s,
            )
            (workdir / "stdout.log").write_text(proc.stdout)
            (workdir / "stderr.log").write_text(proc.stderr)
            self.assertEqual(proc.returncode, 0)
            self.assertIn("ok", proc.stdout)
            result = {
                "schema": "legion-bridge/v0.1",
                "rc": proc.returncode,
                "timed_out": False,
                "finished_at": "2026-10-11T00:00:01+00:00",
            }
            (workdir / "result.json").write_text(json.dumps(result, indent=2))
            secret = SecretFile(tmp / "worker.key").read()
            receipt = {
                "schema": "legion-bridge/v0.1",
                "worker_id": identity.worker_id,
                "harness_resolved": hp.name,
                "harness_requested": "local",
                "binary": hp.binary,
            }
            receipt["signature"] = sign(secret, canonical_json(receipt).encode("utf-8"))
            (workdir / "receipt.json").write_text(json.dumps(receipt, indent=2))
            db.update_status("o/r", 42, "receipt_published")
            row = db.get("o/r", 42)
            self.assertEqual(row["status"], "receipt_published")
            self.assertEqual(row["harness_resolved"], "local")
            audit = AuditLog(policy.state_root / "audit.jsonl")
            audit.append("test_event", ok=True)
            log_text = (policy.state_root / "audit.jsonl").read_text()
            self.assertIn("test_event", log_text)
            db.close()


if __name__ == "__main__":
    unittest.main()
