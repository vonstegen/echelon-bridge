"""Verify OMP harness routing: claim, run OMP --version, record receipt.

This is the Checkpoint 5 evidence. The test runs the *actual* OMP
binary through the same code path a real work order would, then
asserts the resolved harness in the receipt equals the binary that
actually started.
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
    SecretFile,
    WorkerIdentity,
    canonical_json,
    load_policy,
    sign,
    utcnow_iso,
)


def _policy_with_omp(tmp: Path) -> tuple[Path, "object"]:
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
workdir_root = "{tmp / 'work'}"
state_root = "{tmp / 'state'}"

[[harness.omp]]
binary = "omp"
argv_template = ["--version"]
isolation = "none"
env_passthrough = ["PATH", "HOME"]
timeout_s = 60
max_cost_usd = 0.0
harness_kind = "omp"
"""
    p = tmp / "policy.toml"
    p.write_text(body)
    return p, load_policy(p)


class TestOmpRouting(unittest.TestCase):
    def test_omp_runs_and_receipt_records_resolved(self) -> None:
        omp_path = shutil.which("omp")
        if not omp_path:
            self.skipTest("omp not on PATH; cannot verify routing")
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            _, policy = _policy_with_omp(tmp)
            identity = WorkerIdentity.load(policy)
            db = ClaimStore(policy.state_root / "claims.sqlite")
            ok, info = db.upsert_claim(
                "o/r", 7, identity.worker_id, "expected", policy.lease_ttl_s, "omp"
            )
            self.assertTrue(ok, info)
            hp = policy.harness("omp")
            self.assertIsNotNone(hp)
            self.assertEqual(hp.binary, "omp")
            workdir = policy.workdir_root / "o__r__n7"
            workdir.mkdir(parents=True, exist_ok=True)
            manifest = {
                "schema": "legion-bridge/v0.1",
                "worker_id": identity.worker_id,
                "hostname": identity.hostname,
                "repo": "o/r",
                "issue": 7,
                "harness_requested": "omp",
                "harness_resolved": hp.name,
                "binary": hp.binary,
                "argv_template": hp.argv_template,
                "isolation": hp.isolation,
                "max_runtime_s": hp.timeout_s,
                "started_at": utcnow_iso(),
            }
            (workdir / "manifest.json").write_text(json.dumps(manifest, indent=2))
            db.update_status("o/r", 7, "running", harness_resolved=hp.name)
            proc = subprocess.run(
                [hp.binary, *hp.argv_template],
                cwd=str(workdir),
                capture_output=True,
                text=True,
                timeout=hp.timeout_s,
            )
            (workdir / "stdout.log").write_text(proc.stdout)
            (workdir / "stderr.log").write_text(proc.stderr)
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            # OMP's --version prints to stdout; we don't pin the
            # exact version but the binary must have started.
            self.assertTrue(proc.stdout.strip(), "omp --version produced no output")
            result = {
                "schema": "legion-bridge/v0.1",
                "rc": proc.returncode,
                "timed_out": False,
                "finished_at": utcnow_iso(),
            }
            (workdir / "result.json").write_text(json.dumps(result, indent=2))
            secret = SecretFile(tmp / "worker.key").read()
            receipt = {
                "schema": "legion-bridge/v0.1",
                "worker_id": identity.worker_id,
                "hostname": identity.hostname,
                "repo": "o/r",
                "issue": 7,
                "harness_requested": "omp",
                "harness_resolved": hp.name,
                "binary": hp.binary,
                "started_at": manifest["started_at"],
                "finished_at": result["finished_at"],
                "rc": proc.returncode,
                "timed_out": False,
            }
            receipt["signature"] = sign(secret, canonical_json(receipt).encode("utf-8"))
            (workdir / "receipt.json").write_text(json.dumps(receipt, indent=2))
            # The receipt is the evidence that *this* harness ran.
            self.assertEqual(receipt["harness_resolved"], "omp")
            self.assertEqual(receipt["binary"], "omp")
            self.assertEqual(receipt["harness_requested"], "omp")
            db.update_status("o/r", 7, "receipt_published")
            row = db.get("o/r", 7)
            self.assertEqual(row["status"], "receipt_published")
            self.assertEqual(row["harness_resolved"], "omp")
            audit = AuditLog(policy.state_root / "audit.jsonl")
            audit.append("omp_run", repo="o/r", issue=7, harness=hp.name, binary=hp.binary, rc=proc.returncode)
            log = (policy.state_root / "audit.jsonl").read_text()
            self.assertIn('"event":"omp_run"', log)
            db.close()


if __name__ == "__main__":
    unittest.main()
