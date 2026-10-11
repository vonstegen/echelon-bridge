# ECHELON — Backup Worker Modes (Checkpoint 6)

**Status:** CONFIGURED (default = STANDBY)
**Date:** 2026-10-11

## 1. Three modes

| Mode | Polls | Claims | Publishes | Failover eligible |
| --- | --- | --- | --- | --- |
| ACTIVE | yes | yes | yes | no (own work takes priority) |
| STANDBY | yes (heartbeat) | NO | reads only | yes |
| MAINTENANCE | NO | NO | NO | no (excluded) |

## 2. Default = STANDBY

`~/.config/echelon-bridge/policy.toml`:

```
[worker]
mode = "STANDBY"
```

The bridge will NOT claim a work order until the operator flips the
mode to ACTIVE. This is the explicit work-order rule: "The worker must
not begin production work until explicitly activated."

## 3. Mode transitions

Only the operator may transition modes. Transitions are recorded in
the audit log. There is no auto-flip from STANDBY to ACTIVE.

| From | To | Trigger |
| --- | --- | --- |
| MAINTENANCE | STANDBY | operator edit + `legion-bridge recover` |
| STANDBY | ACTIVE | operator edit; C8/C9 must have PASSED |
| ACTIVE | STANDBY | operator edit; in-flight claim continues, no new claim |
| STANDBY | MAINTENANCE | operator edit; used for upgrades |

## 4. Failover flow (STANDBY -> ACTIVE for a single reclaim)

Per the work order:

1. Detect missing heartbeat (poll returns 0 fresh comments; last
   heartbeat comment on the issue is older than 2 * heartbeat_interval_s).
2. Verify the worker lease has expired (claims.lease_expires_at < now).
3. Verify the previous job's execution state (claims.status in
   (claimed, running) AND the runtime PID is dead per /proc).
4. Check for committed outputs or partial results (read
   ~/.local/share/echelon-bridge/runs/<order_id>/ for result.json,
   manifest.json, stdout.log, stderr.log).
5. REQUEST APPROVAL before reassignment. ECHELON posts a comment
   `reclaim-request: <reason>` and waits for operator confirmation;
   it does NOT proceed on its own.
6. Acquire a new authorized claim (CAS with the issue's
   `expected_state_sha`).
7. Resume safely OR restart from a checkpoint:
   - If result.json is present and well-formed: complete with the
     existing receipt (skip re-execution).
   - Otherwise: re-run from the front-matter work-order.

## 5. Anti-takeover rule

The work order says: "Do not implement automatic takeover of unfinished
jobs until the distributed claim protocol is proven."

Therefore: until C9 (failover test) has PASSED, ECHELON's reclaim
request is ALWAYS posted as a comment and the actual claim edit is
gated by `distributed_claim_protocol_proven=true` in policy. The
default is FALSE. The operator flips it only after reviewing C9
evidence.

## 6. Mode helper script

A small helper exposes the mode without needing to edit the TOML by
hand:

```
/home/andre/echelon-bridge/bin/echelon-mode show
/home/andre/echelon-bridge/bin/echelon-mode set STANDBY|ACTIVE|MAINTENANCE
```

The helper edits policy.toml in place, validates the value, and
appends a `mode_transition` entry to the audit log. (Implementation
lands in C10 alongside the systemd unit.)

## 7. Sign-off

Mode = STANDBY is the default. The bridge is configured to fail closed
(no claim) until the operator activates it. The failover flow is
specified, but auto-takeover is gated on C9 evidence.

Proceed to Checkpoint 7.
