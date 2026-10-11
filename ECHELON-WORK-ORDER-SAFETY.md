# ECHELON — Work-Order Safety (Checkpoint 5)

**Status:** DESIGN (built on the shared legion-bridge protocol)
**Date:** 2026-10-11

## 1. Authorized repositories

`allowed_repos` in `~/.config/echelon-bridge/policy.toml`:

```
allowed_repos = ["vonstegen/echelon-bridge-test"]
```

Adding a repo requires operator edit; front-matter `allow_git_*` is
rejected. Until the operator adds more, the bridge will only consider
issues on this one repo. No overlap with LEGION's test repo.

## 2. Authorized work orders

A work order is an issue on an allowed_repo that has the
`legion-bridge:pending` label. A label alone is NOT a lock (work order
text). The bridge treats the label as a hint, not authority.

## 3. Atomic claim acquisition

Claim state lives in two places that MUST agree:

1. SQLite row in `claims.sqlite` with primary key
   (issue_repo, issue_number, worker_id).
2. GitHub-side label edit `legion-bridge:pending -> legion-bridge:claimed`.

The bridge uses an `expected_state_sha` comment marker (SHA-256 of
sorted labels) as a compare-and-set token. If the issue changed since
the listener fetched it, the claim is rejected and the issue is
re-polled on the next cycle. This prevents the classic "two workers
grab the same issue" race.

Lease TTL = 2 * heartbeat_interval_s = 600 s. A claim without a fresh
heartbeat is considered abandoned and is requeueable.

## 4. Claim ownership verification

Before each transition, the bridge re-reads the issue and asserts:

- The `legion-bridge:claimed` label is present.
- The local `claims.worker_id` column matches the configured worker_id.
- The claim row's `status` is `claimed` or `running`.
- The lease has not expired.

If any of these fail, the worker does NOT execute. The audit log
records `claim_ownership_violation` and the worker falls through to
recover().

## 5. Duplicate-execution prevention

A second worker (LEGION, LUMEN, ...) that fetches the same issue sees
`legion-bridge:claimed` and the local SQLite row for the same
(repo, issue) is already in the canonical state. The poll code returns
`skipped-already-claimed-by:<worker_id>` and the issue is not re-listed.
`worker_unique=true` is the default in policy. Operators can opt in to
sharing a worker_id across machines, but then both ends must agree on
`expected_state_sha` AND the lease.

## 6. Idempotent completion reporting

`legion-bridge publish` computes a SHA-256 of the receipt body and
stores it on the local claim row. On retry, the SHA is compared; if
the receipt is already on GitHub, the publish is a no-op (rc=0). If the
receipt was already accepted by GitHub but the local ack was lost, the
publish re-verifies the readback and the claim is marked complete
without a second comment.

## 7. Anti-theft rule

ECHELON will NEVER steal a work order merely because the other worker
is temporarily offline. The rule is: a claim is reclaimable IFF

(a) `claims.worker_id` matches the local worker_id, AND
(b) the lease has expired, AND
(c) the runtime PID is dead, AND
(d) the operator has not explicitly held the claim.

If (a) holds but (b)-(c) do not (e.g. the other worker is alive but
slow), ECHELON does nothing. The issue is left for the rightful owner.

## 8. Single active worker

Until C9 (failover test) PASSES, ECHELON refuses to claim a work
order that is also `legion-bridge:claimed` by another worker. The
`distributed_claim_protocol_proven` flag in policy is FALSE by default.
The operator flips it only after reviewing the C9 evidence. This is
the explicit work-order rule: "if a shared claim mechanism does not
exist, implement and test one before enabling multiple active workers."

## 9. Mode gate

In STANDBY mode, the listener polls but does NOT claim. The poll
command emits a heartbeat comment with `mode=STANDBY` so the operator
can verify the bridge is alive. The claim command refuses with
`worker_mode_blocked` unless mode=ACTIVE.

## 10. Sign-off

The shared legion-bridge protocol already implements atomic claim
acquisition, ownership verification, lease expiration, heartbeat
renewal, duplicate-execution prevention, and idempotent publish.
ECHELON's additions: explicit STANDBY-mode gate, anti-theft rule for
the LEGION worker, single-active-worker guard until C9 passes.

No code changes were required for C5 beyond the policy file. The 19
unit tests in the shared package cover the safety protocol.

Proceed to Checkpoint 6.
