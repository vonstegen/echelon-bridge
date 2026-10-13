"""Regression checks for the controlled deployment review."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from tests.test_blockers import _FakeGH, install_fake, unpatch, _policy_with_harness, _local_harness
from legion_bridge import operations as ops, github as gh
from legion_bridge.core import AuditLog, ClaimStore, WorkerIdentity, INTAKE_LABEL

# === ECHELON: enable activate env so ACTIVE test policy can run ===
import os
os.environ.setdefault("ALLOW_LEGION_ACTIVE", "1")

class DeploymentReview(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fake = _FakeGH()
        install_fake(self, self.fake)
        self.policy = _policy_with_harness(self.root, harness=_local_harness(self.root))
        self.identity = WorkerIdentity.load(self.policy)
        self.audit = AuditLog(self.policy.state_root / 'audit.jsonl')
        self.db = ClaimStore(self.policy.state_root / 'claims.sqlite')
        self.fake.add_issue('o/r', 1, '---\nharness: local\n---\nnoop')
    def tearDown(self):
        self.db.close()
        unpatch(self)
        self.tmp.cleanup()
    def call(self, fn, repo='o/r'):
        return fn(self.policy, self.identity, self.audit, self.db, repo, 1)
    def stale(self):
        self.db.update_status('o/r', 1, 'running', lease_expires_at=0)
    def test_unknown_team_membership_denied(self):
        self.policy.operator_team='ops'; self.policy.operator_org='acme'
        with patch.object(gh, 'user_in_team', return_value=None):
            self.assertFalse(self.call(ops.op_claim).ok)
    def test_partial_team_policy_denied(self):
        self.policy.operator_team='ops'
        self.assertFalse(self.call(ops.op_claim).ok)
    def test_recovery_never_reads_disallowed_repo(self):
        self.db.upsert_claim('forbidden/repo', 1, 'LEGION', 'sha', -1)
        with patch.object(gh, 'issue_full', side_effect=AssertionError('forbidden read')) as read:
            ops.op_recover(self.policy, self.identity, self.audit, self.db)
            read.assert_not_called()
        self.assertFalse(self.call(ops.op_publish, 'forbidden/repo').ok)
    def test_execution_fence_blocks_replay_before_publication(self):
        self.call(ops.op_claim); self.call(ops.op_run)
        self.fake.set_labels('o/r',1,[INTAKE_LABEL])
        self.assertFalse(self.call(ops.op_claim).ok)
        with patch.object(ops, '_run_harness', side_effect=AssertionError('replayed')):
            self.assertFalse(self.call(ops.op_run).ok)
    def test_restart_publishes_result_without_rerun(self):
        self.call(ops.op_claim); self.call(ops.op_run); self.stale()
        with patch.object(ops, '_run_harness', side_effect=AssertionError('replayed')):
            ops.op_recover(self.policy,self.identity,self.audit,self.db)
        self.assertTrue(self.db.has_completion_token('o/r',1))
        self.assertEqual(len(self.fake.comments[('o/r',1)]),1)
    def test_uncertain_running_claim_is_quarantined(self):
        self.call(ops.op_claim); self.stale()
        ops.op_recover(self.policy,self.identity,self.audit,self.db)
        self.assertEqual(self.db.get('o/r',1)['status'],'interrupted_uncertain')
        self.fake.set_labels('o/r',1,[INTAKE_LABEL])
        self.assertFalse(self.call(ops.op_claim).ok)
    def test_failed_history_read_never_posts(self):
        self.call(ops.op_claim); self.call(ops.op_run)
        with patch.object(gh,'find_receipt_comment',side_effect=gh.GhError('offline')):
            self.assertFalse(self.call(ops.op_publish).ok)
        self.assertEqual(self.fake.comments[('o/r',1)],[])
    def test_lost_post_response_is_reconciled(self):
        self.call(ops.op_claim); self.call(ops.op_run)
        post=gh.post_comment
        def lost(*a,**kw):
            post(*a,**kw)
            raise gh.GhError('response lost')
        with patch.object(gh,'post_comment',side_effect=lost), patch.object(ops.time,'sleep'):
            self.assertTrue(self.call(ops.op_publish).ok)
        self.assertEqual(len(self.fake.comments[('o/r',1)]),1)
    def test_publish_failed_retried_by_recovery(self):
        self.call(ops.op_claim); self.call(ops.op_run)
        self.policy.publish_retry_max=1
        with patch.object(gh,'post_comment',side_effect=gh.GhError('offline')), patch.object(ops.time,'sleep'):
            self.assertFalse(self.call(ops.op_publish).ok)
        ops.op_recover(self.policy,self.identity,self.audit,self.db)
        self.assertTrue(self.db.has_completion_token('o/r',1))

class GitHubAdapter(unittest.TestCase):
    def test_real_comment_adapter_flattens_pages(self):
        from types import SimpleNamespace
        with patch.object(gh,'_gh',return_value=SimpleNamespace(stdout=json.dumps([[{'id':1}],[{'id':2}]]))):
            self.assertEqual(gh.list_issue_comments('o/r',1),[{'id':1},{'id':2}])
    def test_active_team_membership_field(self):
        from types import SimpleNamespace
        with patch.object(gh,'_gh',return_value=SimpleNamespace(returncode=0,stdout='{"state":"active"}')):
            self.assertTrue(gh.user_in_team('u','o','t'))
