"""End-to-end recovery test with a fake GitHub server.

We replace the `legion_bridge.github` module's `gh` calls with a
local in-process issue store. The bridge code paths are exercised
exactly as they would be in production, except `gh` is stubbed.

Demonstrates:
  1. Detection of an issue with the intake label.
  2. Claim: claim row + label transition.
  3. Harness selection from front matter.
  4. Harness actually starts (echo).
  5. Local audit + result captured.
  6. Receipt published as a comment.
  7. Repeated poll does not re-execute.
  8. Listener restart: stale-claim recovery works.
  9. Receipt is independently verifiable with the worker key.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from legion_bridge import cli as lb_cli  # noqa: E402
from legion_bridge import github as gh_mod  # noqa: E402
from legion_bridge.core import (  # noqa: E402
    CLAIMED_LABEL,
    INTAKE_LABEL,
    RUNNING_LABEL,
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

    def state_sha(self, repo: str, number: int) -> str:
        return ",".join(sorted(self.labels(repo, number)))


def install_fake_github(test: "E2E", fake: FakeGitHub) -> None:
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
        return fake.state_sha(repo, number)

    def add_label(repo: str, number: int, label: str) -> None:
        labels = fake.labels(repo, number)
        if label not in labels:
            labels.append(label)
        fake.set_labels(repo, number, labels)

    def remove_label(repo: str, number: int, label: str) -> None:
        labels = [l for l in fake.labels(repo, number) if l != label]
        fake.set_labels(repo, number, labels)

    def post_comment(repo: str, number: int, body: str) -> str:
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


def make_policy(tmp: Path) -> Path:
    body = f"""
[worker]
id = "LEGION"
type = "engineering"

allowed_repos = ["vonstegen/legion-bridge-test"]
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


class E2E(unittest.TestCase):
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

    def test_full_lifecycle(self) -> None:
        install_fake_github(self, self.fake)
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            policy_path = make_policy(tmp)
            # 1. Detection
            self.fake.add_issue(
                "vonstegen/legion-bridge-test",
                1,
                "noop-test",
                "---\nharness: local\n---\nnoop",
            )
            argv = ["--policy", str(policy_path), "poll"]
            self.assertEqual(lb_cli.main(argv), 0)
            audit_path = tmp / "state" / "audit.jsonl"
            log = audit_path.read_text()
            self.assertIn('"event":"poll_complete"', log)

            # 2. Claim
            self.assertEqual(
                lb_cli.main(["--policy", str(policy_path), "claim",
                             "--repo", "vonstegen/legion-bridge-test",
                             "--issue", "1"]),
                0,
            )
            self.assertNotIn(INTAKE_LABEL, self.fake.labels("vonstegen/legion-bridge-test", 1))
            self.assertIn(CLAIMED_LABEL, self.fake.labels("vonstegen/legion-bridge-test", 1))

            # 3. Run: harness routing actually invokes echo.
            self.assertEqual(
                lb_cli.main(["--policy", str(policy_path), "run",
                             "--repo", "vonstegen/legion-bridge-test",
                             "--issue", "1"]),
                0,
            )
            self.assertIn(RUNNING_LABEL, self.fake.labels("vonstegen/legion-bridge-test", 1))
            self.assertIn("legion-bridge:completed", self.fake.labels("vonstegen/legion-bridge-test", 1))

            # 4. Publish signed receipt.
            self.assertEqual(
                lb_cli.main(["--policy", str(policy_path), "publish",
                             "--repo", "vonstegen/legion-bridge-test",
                             "--issue", "1"]),
                0,
            )
            comments = self.fake.comments[("vonstegen/legion-bridge-test", 1)]
            receipt_comments = [c for c in comments if "legion-bridge receipt" in c["body"]]
            self.assertEqual(len(receipt_comments), 1)

            # 5. Repeated poll does not re-execute.
            self.assertEqual(lb_cli.main(argv), 0)
            log2 = audit_path.read_text()
            second_segment = log2.split('"event":"poll_complete"')[-1]
            self.assertNotIn('"event":"claim_ok"', second_segment)

            # 6. Stale-claim recovery: simulate a crashed listener with an active claim.
            self.fake.add_issue(
                "vonstegen/legion-bridge-test",
                2,
                "stale-test",
                "---\nharness: local\n---\nnoop",
            )
            self.fake.set_labels("vonstegen/legion-bridge-test", 2, [CLAIMED_LABEL])
            db_path = tmp / "state" / "claims.sqlite"
            with sqlite3.connect(str(db_path)) as conn:
                conn.execute(
                    "REPLACE INTO claims (repo, issue_number, worker_id, claimed_at, lease_expires_at, heartbeat_at, expected_state_sha, status, harness_requested, harness_resolved, session_id) "
                    "VALUES ('vonstegen/legion-bridge-test', 2, 'LEGION', ?, ?, ?, 'sha', 'claimed', 'local', NULL, NULL)",
                    (utcnow_epoch() - 200, utcnow_epoch() - 100, utcnow_epoch() - 100),
                )
                conn.commit()
            self.assertEqual(
                lb_cli.main(["--policy", str(policy_path), "recover"]),
                0,
            )
            self.assertIn(INTAKE_LABEL, self.fake.labels("vonstegen/legion-bridge-test", 2))
            self.assertIn("legion-bridge:interrupted", self.fake.labels("vonstegen/legion-bridge-test", 2))

            workdir = tmp / "work" / "vonstegen__legion-bridge-test__n1"
            receipt = json.loads((workdir / "receipt.json").read_text())
            secret = SecretFile(Path.home() / ".config" / "legion-bridge" / "worker.key").read()
            payload = {k: v for k, v in receipt.items() if k != "signature"}
            self.assertTrue(verify(secret, canonical_json(payload).encode("utf-8"), receipt["signature"]))
            self.assertEqual(receipt["harness_resolved"], "local")
            self.assertEqual(receipt["harness_requested"], "local")
            self.assertEqual(receipt["binary"], "echo")
            self.assertEqual(receipt["worker_id"], "LEGION")


if __name__ == "__main__":
    unittest.main()
