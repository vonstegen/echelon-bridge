"""Live ECHELON-bridge smoke test.

Proves that the real ECHELON policy + key + ops all work end-to-end.
Uses a tmpdir copy of the policy, the real ECHELON key, and an
in-memory fake GitHub. Does not touch the production workdir or
any real GitHub resource.

Exit non-zero on any failure.
"""
import os
import sys
import json
import shutil
import tempfile
import dataclasses
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, '/home/andre/echelon-bridge/src')

from legion_bridge import github as gh_mod
from legion_bridge.core import (
    load_policy,
    WorkerIdentity,
    SecretFile,
    AuditLog,
    ClaimStore,
    canonical_json,
    verify,
    INTAKE_LABEL,
)
from legion_bridge import operations as ops
from unittest.mock import MagicMock


def step(title):
    print(f"\n--- {title} ---")


def main():
    src = Path('/home/andre/.config/echelon-bridge/policy.toml')
    if not src.exists():
        print(f"FAIL: policy not found at {src}")
        return 1

    tmp = Path(tempfile.mkdtemp(prefix='echelon-smoke-'))
    print(f"smoke dir: {tmp}")

    # 1. Stage policy: ACTIVE + proven, per-test workdir, real key.
    dst = tmp / 'policy.toml'
    text = src.read_text()
    text = text.replace('mode = "STANDBY"', 'mode = "ACTIVE"', 1)
    text = text.replace('distributed_claim_protocol_proven = false',
                        'distributed_claim_protocol_proven = true', 1)
    text = text.replace('workdir_root = "~/.local/share/echelon-bridge/work"',
                        f'workdir_root = "{tmp / "work"}"', 1)
    text = text.replace('state_root = "~/.local/share/echelon-bridge/state"',
                        f'state_root = "{tmp / "state"}"', 1)
    text = text.replace('key_path = "~/.config/echelon-bridge/worker.key"',
                        f'key_path = "{tmp / "worker.key"}"', 1)
    dst.write_text(text)
    shutil.copy('/home/andre/.config/echelon-bridge/worker.key',
                tmp / 'worker.key')
    os.chmod(tmp / 'worker.key', 0o600)

    os.environ['ALLOW_ECHELON_ACTIVE'] = '1'

    p = load_policy(dst)
    assert p.mode == 'ACTIVE', p.mode
    assert p.distributed_claim_protocol_proven is True
    assert p.worker_id == 'ECHELON', p.worker_id
    assert p.host == 'AvSDellXPS', p.host
    assert str(p.key_path) == str(tmp / 'worker.key'), p.key_path
    assert p.workdir_root == tmp / 'work', p.workdir_root
    step("policy loaded")
    print(f"  mode={p.mode} proven={p.distributed_claim_protocol_proven}")
    print(f"  worker_id={p.worker_id} host={p.host}")
    print(f"  harnesses={list(p.harness_policies.keys())}")

    ident = WorkerIdentity.load(p)
    assert ident.worker_id == 'ECHELON'
    assert ident.hostname == 'AvSDellXPS'

    (tmp / 'state').mkdir(parents=True, exist_ok=True)
    audit = AuditLog(tmp / 'state' / 'audit.jsonl')
    db = ClaimStore(tmp / 'state' / 'claims.sqlite')

    # 2. Issue 1: standard local run.
    step("issue 1: local run")

    def fake_issue_full(repo, number, **kw):
        m = MagicMock()
        m.labels = [INTAKE_LABEL]
        m.author = 'vonstegen'
        m.state = 'open'
        m.body = '---\nharness: local\n---\nnoop'
        return m

    with patch.object(gh_mod, 'issue_full', side_effect=fake_issue_full), \
         patch.object(gh_mod, 'issue_state_sha', return_value='a'*64), \
         patch.object(gh_mod, 'add_label', return_value=None), \
         patch.object(gh_mod, 'remove_label', return_value=None), \
         patch.object(gh_mod, 'post_comment', return_value=None), \
         patch.object(gh_mod, 'user_in_team', return_value=True), \
         patch.object(gh_mod, 'find_receipt_comment', return_value=None):
        res = ops.op_process_one(p, ident, audit, db,
                                  'vonstegen/echelon-bridge-test', 1)

    print(f"  op_process_one: ok={res.ok} status={res.status}")
    assert res.ok, f"expected ok=True, got {res.status} {res.detail}"

    workdir = p.workdir_root / 'vonstegen__echelon-bridge-test__n1'
    receipt = json.loads((workdir / 'receipt.json').read_text())
    print(f"  receipt.worker_id   = {receipt['worker_id']}")
    print(f"  receipt.hostname    = {receipt['hostname']}")
    print(f"  receipt.harness     = {receipt['harness_resolved']}")
    print(f"  receipt.acceptance  = {receipt['acceptance_passed']}")
    assert receipt['worker_id'] == 'ECHELON'
    assert receipt['hostname'] == 'AvSDellXPS'
    assert receipt['harness_resolved'] == 'local'

    # 3. INDEPENDENT signature verification with the ECHELON key.
    step("independent signature verification")
    secret = SecretFile(p.key_path).read()
    payload = {k: v for k, v in receipt.items() if k != 'signature'}
    if not verify(secret, canonical_json(payload).encode('utf-8'),
                  receipt['signature']):
        print("  FAIL: ECHELON-key signature verification failed")
        return 1
    print("  signature OK")
    print(f"  signature[0:16] = {receipt['signature'][:16]}...")

    # 4. Issue 2: exact-output acceptance (ECHELON-only field).
    step("issue 2: exact-output acceptance (pass)")

    local_h = p.harness_policies['local']
    new_local = dataclasses.replace(
        local_h,
        argv_template=['ECHO-EXACT'],
        acceptance_stdout_equals='ECHO-EXACT\n',
    )
    p2 = dataclasses.replace(
        p,
        harness_policies={**p.harness_policies, 'local': new_local},
    )
    def fake_issue_full_2(repo, number, **kw):
        m = MagicMock()
        m.labels = [INTAKE_LABEL]
        m.author = 'vonstegen'
        m.state = 'open'
        m.body = '---\nharness: local\n---\nECHO-EXACT'
        return m

    with patch.object(gh_mod, 'issue_full', side_effect=fake_issue_full_2), \
         patch.object(gh_mod, 'issue_state_sha', return_value='b'*64), \
         patch.object(gh_mod, 'add_label', return_value=None), \
         patch.object(gh_mod, 'remove_label', return_value=None), \
         patch.object(gh_mod, 'post_comment', return_value=None), \
         patch.object(gh_mod, 'user_in_team', return_value=True), \
         patch.object(gh_mod, 'find_receipt_comment', return_value=None):
        res2 = ops.op_process_one(p2, ident, audit, db,
                                   'vonstegen/echelon-bridge-test', 2)
    print(f"  op_process_one: ok={res2.ok}")
    assert res2.ok
    workdir2 = p2.workdir_root / 'vonstegen__echelon-bridge-test__n2'
    receipt2 = json.loads((workdir2 / 'receipt.json').read_text())
    assert receipt2['acceptance_passed'] is True, receipt2
    assert receipt2['acceptance_stdout_equals'] == 'ECHO-EXACT\n', receipt2
    print(f"  receipt2 acceptance_passed = {receipt2['acceptance_passed']}")
    print(f"  receipt2 stdout_sha = {receipt2['stdout_sha'][:16]}...")
    # 5. Issue 3: exact-output acceptance (FAIL: substring must not pass).
    step("issue 3: exact-output acceptance (must fail)")

    bad_local = dataclasses.replace(
        local_h,
        argv_template=['ECHO-EXACT'],
        acceptance_stdout_equals='TAMPERED\n',
    )
    p3 = dataclasses.replace(
        p,
        harness_policies={**p.harness_policies, 'local': bad_local},
    )
    with patch.object(gh_mod, 'issue_full', side_effect=fake_issue_full_2), \
         patch.object(gh_mod, 'issue_state_sha', return_value='c'*64), \
         patch.object(gh_mod, 'add_label', return_value=None), \
         patch.object(gh_mod, 'remove_label', return_value=None), \
         patch.object(gh_mod, 'post_comment', return_value=None), \
         patch.object(gh_mod, 'user_in_team', return_value=True), \
         patch.object(gh_mod, 'find_receipt_comment', return_value=None):
        res3 = ops.op_process_one(p3, ident, audit, db,
                                   'vonstegen/echelon-bridge-test', 3)
    print(f"  op_process_one: ok={res3.ok} status={res3.status}")
    # The run failed at the harness level (stdout != acceptance_stdout_equals)
    # reflects the failure. The completed label is NOT applied; the
    # failed label is.
    workdir3 = p3.workdir_root / 'vonstegen__echelon-bridge-test__n3'
    result3 = json.loads((workdir3 / 'result.json').read_text())
    receipt3 = json.loads((workdir3 / 'receipt.json').read_text())
    print(f"  result3 acceptance_passed  = {result3['acceptance_passed']}")
    print(f"  receipt3 acceptance_passed = {receipt3['acceptance_passed']}")
    assert result3['acceptance_passed'] is False
    assert receipt3['acceptance_passed'] is False
    # The output_sha on the receipt is None when acceptance fails
    # (the upstream definition of "output" requires acceptance).
    assert receipt3['output_sha'] is None
    # check the result.json for the acceptance_passed flag.
    workdir3 = p3.workdir_root / 'vonstegen__echelon-bridge-test__n3'
    result3 = json.loads((workdir3 / 'result.json').read_text())
    print(f"  result3 acceptance_passed = {result3['acceptance_passed']}")
    assert result3['acceptance_passed'] is False, result3
    # 6. STANDBY mode must deny.
    step("STANDBY mode must deny")
    del os.environ['ALLOW_ECHELON_ACTIVE']
    p_std = load_policy(src)  # the unmodified source
    assert p_std.mode == 'STANDBY'
    audit2 = AuditLog(tmp / 'state' / 'audit2.jsonl')
    db2 = ClaimStore(tmp / 'state' / 'claims2.sqlite')
    res_s = ops.op_claim(p_std, ident, audit2, db2,
                          'vonstegen/echelon-bridge-test', 99)
    print(f"  op_claim(STANDBY): ok={res_s.ok} reason={res_s.detail.get('reason')}")
    assert res_s.ok is False
    assert res_s.detail.get('reason') == 'mode_standby'
    os.environ['ALLOW_ECHELON_ACTIVE'] = '1'

    # 7. Foreign-claim ownership (a different worker_id holds the row).
    step("foreign-claim ownership")
    p4 = load_policy(dst)
    audit3 = AuditLog(tmp / 'state' / 'audit3.jsonl')
    db3 = ClaimStore(tmp / 'state' / 'claims3.sqlite')
    db3.upsert_claim('vonstegen/echelon-bridge-test', 50,
                     worker_id='LEGION',
                     expected_state_sha='x'*64, lease_ttl_s=600)
    def fake_issue_full_any(repo, number, **kw):
        m = MagicMock()
        m.labels = [INTAKE_LABEL]
        m.author = 'vonstegen'
        m.state = 'open'
        m.body = '---\nharness: local\n---\nnoop'
        return m
    with patch.object(gh_mod, 'issue_full', side_effect=fake_issue_full_any), \
         patch.object(gh_mod, 'issue_state_sha', return_value='x'*64), \
         patch.object(gh_mod, 'add_label', return_value=None), \
         patch.object(gh_mod, 'remove_label', return_value=None), \
         patch.object(gh_mod, 'post_comment', return_value=None), \
         patch.object(gh_mod, 'user_in_team', return_value=True), \
         patch.object(gh_mod, 'find_receipt_comment', return_value=None):
        res_f = ops.op_claim(p4, ident, audit3, db3,
                              'vonstegen/echelon-bridge-test', 50)
    print(f"  op_claim(foreign): ok={res_f.ok} status={res_f.status} reason={res_f.detail.get('reason')}")
    assert res_f.ok is False
    assert res_f.status.startswith('skipped'), res_f.status

    # 8. Concurrent-second-listener rejection.
    step("concurrent listener rejection")
    from legion_bridge.lockfile import acquire
    p5 = load_policy(dst)
    p5.state_root.mkdir(parents=True, exist_ok=True)
    held = acquire(str(p5.state_root), 'ECHELON')
    try:
        from legion_bridge.lockfile import ListenerLockError
        try:
            held2 = acquire(str(p5.state_root), 'ECHELON-2')
            held2.release()
            print("  FAIL: second acquire succeeded unexpectedly")
            return 1
        except ListenerLockError as e:
            print(f"  second acquire correctly rejected: {e}")
    finally:
        held.release()

    # 9. audit log line count.
    step("audit log")
    lines = (tmp / 'state' / 'audit.jsonl').read_text().splitlines()
    print(f"  events: {len(lines)}")
    for line in lines:
        ev = json.loads(line)
        print(f"    {ev.get('event')} {ev.get('reason','')}")

    print("\n*** LIVE ECHELON-BRIDGE SMOKE: PASS ***")
    return 0


if __name__ == '__main__':
    sys.exit(main())
