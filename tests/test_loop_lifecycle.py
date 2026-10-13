"""End-to-end test for the listener loop.

Replaces the GitHub adapter with an in-process fake. Drives the
loop with --once and verifies that:
  1. The issue is detected and processed.
  2. The harness actually runs.
  3. The receipt is signed and posted.
  4. A second --once run does not re-process the same issue.
  5. A stale claim is recovered.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import os
import unittest
os.environ.setdefault("ALLOW_LEGION_ACTIVE", "1")
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from legion_bridge import github as gh_mod  # noqa: E402
from legion_bridge import loop as lb_loop  # noqa: E402
from legion_bridge.core import (  # noqa: E402
    CLAIMED_LABEL,
    INTAKE_LABEL,
    SecretFile,
    canonical_json,
    verify,
    utcnow_epoch,
)


class FakeGitHub:
    def __init__(self) -> None:
        self.issues: dict[tuple[str, int], dict[str, Any]] = {}
        self.comments: dict[tuple[str, int], list[dict[str, Any]]] = {}

    def add_issue(self, repo: str, number: int, title: str, body: str, author: str = "ops") -> None:
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

    def set_labels(self, repo: str, number: int, labels: list[str]) -> None:
        self.issues[(repo, number)]["labels"] = list(labels)

    def labels(self, repo: str, number: int) -> list[str]:
        return list(self.issues[(repo, number)]["labels"])


def install_fake(test: "LoopTest", fake: FakeGitHub) -> None:
    def list_candidate_issues(repo: str, limit: int = 25) -> list[Any]:
        out: list[Any] = []
        for (r, n), issue in fake.issues.items():
            if r != repo:
                continue
            if INTAKE_LABEL not in issue["labels"]:
                continue
            out.append(gh_mod.Issue(
                number=issue["number"],
                title=issue["title"],
                body=issue["body"],
                created_at=issue["createdAt"],
                labels=issue["labels"],
                author=issue["author"]["login"],
                state=issue["state"],
                node_id=issue["node_id"],
                html_url=issue["url"],
            ))
        out.sort(key=lambda i: i.created_at)
        return out[:limit]

    def issue_full(repo: str, number: int) -> gh_mod.Issue:
        issue = fake.issues[(repo, number)]
        return gh_mod.Issue(
            number=issue["number"],
            title=issue["title"],
            body=issue["body"],
            created_at=issue["createdAt"],
            labels=issue["labels"],
            author=issue["author"]["login"],
            state=issue["state"],
            node_id=issue["node_id"],
            html_url=issue["url"],
        )

    def issue_state_sha(repo: str, number: int) -> str:
        return ",".join(sorted(fake.labels(repo, number)))

    def add_label(repo: str, number: int, label: str, *, timeout: Any = None) -> None:
        labels = fake.labels(repo, number)
        if label not in labels:
            labels.append(label)
        fake.set_labels(repo, number, labels)

    def remove_label(repo: str, number: int, label: str, *, timeout: Any = None) -> None:
        labels = [l for l in fake.labels(repo, number) if l != label]
        fake.set_labels(repo, number, labels)

    def post_comment(repo: str, number: int, body: str, *, timeout: Any = None) -> str:
        fake.comments.setdefault((repo, number), []).append({"body": body})
        return body[:200]

    def gh_authenticated() -> bool:
        return True

    test._patch("list_candidate_issues", list_candidate_issues)
    test._patch("issue_full", issue_full)
    test._patch("issue_state_sha", issue_state_sha)
    test._patch("add_label", add_label)
    test._patch("remove_label", remove_label)
    test._patch("post_comment", post_comment)
    test._patch("gh_authenticated", gh_authenticated)
    test._patch("list_issue_comments", lambda repo, number, **kw: list(fake.comments.get((repo, number), [])))


def make_policy(tmp: Path) -> Path:
    body = f"""
[worker]
id = "LEGION"
type = "engineering"

allowed_repos = ["o/r"]
lease_ttl_s = 600
heartbeat_interval_s = 60
poll_interval_s = 1
poll_limit = 25
publish_retry_max = 3
allow_frontier = false
worker_unique = true
# === ECHELON: enable ACTIVE for the test (gated by env var) ===
mode = "ACTIVE"
distributed_claim_protocol_proven = true
key_path = "{tmp / 'worker.key'}"
health_host = "127.0.0.1"
health_port = 0
workdir_root = "{tmp / 'work'}"
state_root = "{tmp / 'state'}"

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
    return p


class LoopTest(unittest.TestCase):
    def setUp(self) -> None:
        self._patches: list[tuple[str, Any]] = []
        self.fake = FakeGitHub()

    def _patch(self, name: str, value: Any) -> None:
        self._patches.append((name, getattr(gh_mod, name, None)))
        setattr(gh_mod, name, value)

    def tearDown(self) -> None:
        for name, prev in reversed(self._patches):
            if prev is None:
                if hasattr(gh_mod, name):
                    delattr(gh_mod, name)
            else:
                setattr(gh_mod, name, prev)

    def test_loop_processes_one_issue(self) -> None:
        install_fake(self, self.fake)
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            policy_path = make_policy(tmp)
            self.fake.add_issue("o/r", 1, "noop", "---\nharness: local\n---\nnoop")
            rc = lb_loop.main(["--policy", str(policy_path), "--once"])
            self.assertEqual(rc, 0)
            self.assertIn(CLAIMED_LABEL, self.fake.labels("o/r", 1) + [""])
            self.assertIn("legion-bridge:completed", self.fake.labels("o/r", 1))
            self.assertNotIn(INTAKE_LABEL, self.fake.labels("o/r", 1))
            comments = self.fake.comments[("o/r", 1)]
            self.assertTrue(any("legion-bridge receipt" in c["body"] for c in comments))
            workdir = tmp / "work" / "o__r__n1"
            receipt = json.loads((workdir / "receipt.json").read_text())
            # === ECHELON: per-worker key path ===
            secret = SecretFile(tmp / "worker.key").read()
            payload = {k: v for k, v in receipt.items() if k != "signature"}
            self.assertTrue(verify(secret, canonical_json(payload).encode("utf-8"), receipt["signature"]))
            # Second run: no new processing
            before = len(self.fake.comments[("o/r", 1)])
            rc = lb_loop.main(["--policy", str(policy_path), "--once"])
            self.assertEqual(rc, 0)
            after = len(self.fake.comments[("o/r", 1)])
            self.assertEqual(after, before)

    def test_loop_recovers_stale_claim(self) -> None:
        install_fake(self, self.fake)
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            policy_path = make_policy(tmp)
            # Simulate a listener that crashed mid-run.
            db_path = tmp / "state" / "claims.sqlite"
            self.fake.add_issue("o/r", 2, "stale", "---\nharness: local\n---\nnoop")
            (tmp / "state").mkdir(parents=True, exist_ok=True)
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
                    "REPLACE INTO claims (repo, issue_number, worker_id, claimed_at, lease_expires_at, heartbeat_at, expected_state_sha, status, harness_requested, harness_resolved, session_id) "
                    "VALUES ('o/r', 2, 'LEGION', ?, ?, ?, 'sha', 'claimed', 'local', NULL, NULL)",
                    (utcnow_epoch() - 200, utcnow_epoch() - 100, utcnow_epoch() - 100),
                )
                conn.commit()
            # Run a single tick: the loop should recover the stale claim,
            # requeue the issue, and process it.
            rc = lb_loop.main(["--policy", str(policy_path), "--once"])
            self.assertEqual(rc, 0)
            # Issue must be completed with receipt.
            self.assertIn("legion-bridge:completed", self.fake.labels("o/r", 2))
            self.assertTrue(any("legion-bridge receipt" in c["body"] for c in self.fake.comments[("o/r", 2)]))


if __name__ == "__main__":
    unittest.main()
