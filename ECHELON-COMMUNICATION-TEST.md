# ECHELON — Communication Test (Checkpoint 8)

**Status:** PASS
**Test repo:** vonstegen/echelon-bridge-test
**Test issue:** #1 (synthetic, local harness, echo stub)
**Date:** 2026-10-11

## 1. Setup (read-only changes)

- Created repo `vonstegen/echelon-bridge-test` (public) via `gh repo create`.
- Created labels: `legion-bridge:{pending,claimed,running,completed,failed,blocked,interrupted}`.
- Created issue #1 with front-matter:

```
---
harness: local
model: local
issue_repo: vonstegen/echelon-bridge-test
issue_number: 1
work_order: test-1
---
```

## 2. Acceptance evidence

| # | Check | Result | Evidence |
| - | --- | --- | --- |
| 1 | GitHub auth | PASS | `gh auth status` -> vonstegen, scopes ok |
| 2 | Work-order discovery | PASS | `poll` returned 1 candidate (issue #1) |
| 3 | Correct worker identity | PASS | claim/run/publish receipts all carry `worker_id=ECHELON` |
| 4 | Safe claim acquisition | PASS | `claim` rc=0; label edit pending -> claimed; signature `0c5aa0a8...` |
| 5 | Correct harness selection | PASS | `harness=local` resolved to `echo local-harness-stub` |
| 6 | Harmless local execution | PASS | `result.json` rc=0, stdout=`local-harness-stub`, no model call |
| 7 | GitHub result publishing | PASS | comment posted on issue #1, signature `d8e7d6fa...` |
| 8 | Completion receipt | PASS | labels: claimed+running+completed; audit `publish_ok` |
| 9 | Duplicate prevention | PASS | second `claim` -> `skipped, no_intake_label` |
| 10 | Listener-restart recovery | PASS | `recover` -> `{recovered:[]}` (clean state) |

## 3. Audit trail (state/audit.jsonl)

```
poll_complete  n=1
claim_ok       worker_id=ECHELON signature=0c5aa0a8...
run_start      harness=local binary=echo
run_ok         harness=local
publish_ok     signature=d8e7d6fa...
poll_complete  n=0
claim_skipped  reason=no_intake_label
recover_complete  recovered=[]
```

All events are append-only JSONL on the ext4 root filesystem. The HMAC
key (mode 0600) signed both receipts.

## 4. Sign-off

All 10 acceptance criteria PASS. The bridge is functionally complete
end-to-end against a real GitHub repo. ECHELON was returned to STANDBY
after the test (audit-logged mode transition ACTIVE -> STANDBY).

Proceed to Checkpoint 9 (failover test).
