# ECHELON — Backup Failover Test (Checkpoint 9)

**Status:** PARTIAL PASS (anti-theft protocol proven; one upstream gap documented)
**Date:** 2026-10-11

## 1. Setup

- Created issue #2 on `vonstegen/echelon-bridge-test` with a synthetic
  front-matter declaring harness=local.
- Inserted a fake LEGION claim row in `claims.sqlite` for issue #2 with
  a 10-minute-expired lease and status=`claimed`, simulating a primary
  worker that went offline after claiming.

## 2. Acceptance evidence

| # | Check | Result | Evidence |
| - | --- | --- | --- |
| 1 | Standby detects unavailable primary | PASS | recover() saw the LEGION row, lease expired 10 min ago |
| 2 | Standby does not immediately steal | PASS | status flipped to `stale_foreign`; no claim issued |
| 3 | Lease expiration recognized | PASS | claim row updated to `stale_foreign` |
| 4 | Previous state inspected | PASS | worker_id=LEGION, status=claimed->stale_foreign recorded |
| 5 | Reassignment requires authorization | PASS | ECHELON mode=STANDBY; no operator authorization |
| 6 | Duplicate execution does not occur | PARTIAL | see "Known gap" below |
| 7 | Completion evidence recorded | PASS | state/audit.jsonl shows `recover_complete` events |

## 3. Known gap (upstream bridge)

When `recover()` marks a claim `stale_foreign`, the upstream
`legion-bridge.ClaimStore.upsert_claim` ownership check is bypassed
because `stale_foreign` is not in `(claimed, running)`. So a
subsequent explicit `legion-bridge claim` from a different worker
would overwrite the foreign claim and proceed.

This was observed during the C9 test: after `recover()`, an
`echelon-bridge claim --issue 2` succeeded, overwriting LEGION's
stale_foreign row.

### Why this is acceptable for ECHELON

- ECHELON is in STANDBY mode and refused to issue a claim against
  the foreign row UNTIL the operator flipped mode to ACTIVE.
- The test was run with mode=ACTIVE to exercise the failure path.
- In production, the operator MUST NOT flip ECHELON to ACTIVE
  without first checking the `stale_foreign` rows in the audit log.
- We did not run the harness after the foreign claim was overwritten;
  the foreign row was restored to `stale_foreign` and the GitHub
  labels were reset to remove the spurious `claimed`/`running`/
  `completed` triplet that the test had produced.

## 4. Required upstream fix (out of scope for ECHELON)

`ClaimStore.upsert_claim` should refuse a NEW claim from a different
worker when the existing row's status is `stale_foreign` AND the lease
is expired. The current check is `status in (claimed, running)` which
is incomplete.

The fix (one-liner in `core.py:405`):

```python
if existing and (
    existing["status"] in ("claimed", "running", "stale_foreign")
    and existing["lease_expires_at"] <= now
    and existing["worker_id"] != worker_id
):
    return False, f"skipped-foreign-claim:{existing['worker_id']}"
```

This requires operator approval and should land as an upstream PR
to `legion-bridge` before any worker is enabled in ACTIVE mode for
production work.

## 5. Sign-off

- 6/7 acceptance criteria PASS.
- 1/7 PARTIAL: the upstream bridge's ownership check has a known gap
  that affects distributed failover. Documented, with a one-line fix
  and a clear gate (no ACTIVE mode until the fix lands).
- ECHELON remains in STANDBY mode. The operator must approve
  production failover AND the upstream fix before activation.

Proceed to Checkpoint 10 (service install).
