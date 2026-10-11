# ECHELON — Communication Architecture (Checkpoint 2)

**Status:** DESIGN (DRAFT, pending operator approval)
**Worker ID:** ECHELON
**Companion repo:** legion-bridge (shared, reusable)
**Local source path:** /home/andre/echelon-bridge
**Config path:** ~/.config/echelon-bridge/
**State path:** ~/.local/share/echelon-bridge/ (on ext4 root, not on /mnt/c)

## 0. Decision: reuse legion-bridge

The work order calls for a "shared communication bridge with machine-specific
configuration." `vonstegen/legion-bridge` is exactly that. ECHELON is a second
worker instance: same Python package, different worker_id, different config,
different signing key, different systemd unit.

This document describes how each of the 12 functional requirements from the
work order is satisfied and what ECHELON-specific additions are required.

## 1. Pipeline overview

A single in-process listener (started by `legion-bridge loop`) drives the
state machine:

    pending -> claimed -> running -> completed | failed | blocked | interrupted

Each transition is: (a) recorded in the local SQLite claim DB, (b) mirrored
to GitHub via `gh issue edit` (labels) and a signed comment, (c) appended
to the append-only audit log. The harness actually started is the one
recorded in the receipt; GitHub labels are not authority.

## 2. Requirement coverage

| # | Requirement | Implementation |
| - | --- | --- |
| 1 | GitHub issue polling | `legion-bridge poll` runs `gh issue list --label legion-bridge:pending --state open` against each allowed repo. Paging past `--limit poll_limit` to avoid starvation. |
| 2 | Work-order discovery | Same as poll; a candidate is an open issue whose body has a front-matter block naming the requested harness and model. |
| 3 | Worker registration | `legion-bridge status` returns worker_id, allowed_repos, detected_harnesses, mode. Operator publishes this as a GitHub issue or wiki entry. |
| 4 | Harness selection | Server-side policy table `[[harness.X]]` maps `requested_harness` -> `{binary, argv_template, isolation, timeout_s, env_passthrough, max_cost_usd}`. Detection via `command -v`; missing harness -> BLOCKED comment. |
| 5 | Model selection | `legion-bridge models` resolves `requested_model_alias` against the chosen harness's known models. Frontier providers run only with `ALLOW_FRONTIER=1` in policy (default off). |
| 6 | Work-order claiming | Atomic claim: SQLite row in `claims` with PK (issue_repo, issue_number); label edit `pending -> claimed` on GitHub. Lease TTL = 2 * heartbeat_interval_s. |
| 7 | Execution supervision | `legion-bridge run` spawns the harness with `bwrap` (or `none` for trusted), captures stdout/stderr/result.json, enforces `max_runtime_s` with a watchdog. |
| 8 | Heartbeats | Background thread updates `heartbeat_at`; optionally posts a `legion-bridge:heartbeat` comment at `heartbeat_comment_interval_s`. |
| 9 | Timeouts | Per-harness `timeout_s` (default 1800). Watchdog SIGKILLs on expiry, posts `failed: timeout` comment, flips claim to `interrupted`. |
| 10 | Failure recovery | `legion-bridge recover` on startup: for each `claimed`/`running` with expired lease, decide recoverable (PID dead) vs stale_foreign (requeue). |
| 11 | Completion receipts | `legion-bridge publish` posts a signed JSON receipt, transitions label `running -> completed|failed|blocked`, verifies readback by SHA-256 of posted body. |
| 12 | GitHub reporting | All transitions become comments and label changes on the issue. Audit log has the redacted URL and a SHA-256 of every API call body. |

## 3. Known problems and mitigations

| Problem | Mitigation |
| --- | --- |
| Stale claims | Lease TTL = 2 * heartbeat_interval_s. recover() re-evaluates on every poll. |
| Duplicate execution | (a) SQLite PK (repo, issue). (b) GitHub label `legion-bridge:claimed` is witness. (c) worker_unique=true default. |
| Unresponsive listener | systemd Restart=on-failure + WatchdogSec=300. |
| Hung harness | bwrap --die-with-parent + watchdog SIGKILL + failed:timeout comment. |
| Model-provider failure | 3x retry-with-backoff in child; persistent -> failed:provider_error. |
| Lost lease / missing label | recover() reclaims if worker_id matches and lease expired. |
| interrupted label carried forward | recover() strips it when requeuing. |
| gh CLI no ETag on issue edit | expected_state_sha comment marker; CAS at claim level. |
| WSL2 /mnt/c fsync | State DB on ext4 root (/) only. |

## 4. ECHELON-specific additions to legion-bridge

- New `worker_id = ECHELON`; new HMAC key in `~/.config/echelon-bridge/worker.key`.
- New allowed_repos list. Default: `["vonstegen/echelon-bridge-test"]`
  (to be created in C8). No overlap with LEGION's test repo unless the
  operator co-grants one.
- New state dir: `~/.local/share/echelon-bridge/`.
- New audit log: `~/.local/share/echelon-bridge/audit.jsonl`.
- New systemd unit: `echelon-bridge.service` (companion to `legion-bridge.service`).
- Mode flag: `WORKER_MODE=STANDBY` by default; bridge will not accept
  new work until operator sets it to `ACTIVE`.
- `health_port` defaults to 0 (disabled) on ECHELON.

## 5. Threat model (assumptions)

- GitHub credentials can be revoked at any time. Bridge never bypasses
  GitHub; it always re-checks repo membership and labels on every poll.
- The GitHub API can be slow or unavailable. Polling backs off; audit
  records every poll_error.
- Issue authors may be hostile. The bridge never grants capability from
  the issue body; front-matter cannot set allow_git_* or unbounded runtime.
- A single hostile comment cannot redirect the bridge to an arbitrary
  harness, model, or remote. Front-matter keys are an allowlist; the
  harness actually started is recorded in the receipt.
- The operator trusts the host kernel (Ubuntu 24.04 + WSL2) but not
  necessarily the OMP/Hermes binaries; isolation is `bwrap` when the
  harness policy demands it.

## 6. Configuration files (ECHELON)

- `~/.config/echelon-bridge/policy.toml` — repos, harnesses, models,
  timeouts, lease TTL, ALLOW_FRONTIER.
- `~/.config/echelon-bridge/worker.key` — HMAC key, mode 0600, generated
  on first run.
- `~/.local/share/echelon-bridge/state/claims.sqlite` — claim DB.
- `~/.local/share/echelon-bridge/audit.jsonl` — append-only audit log.
- `~/.local/share/echelon-bridge/runs/<order_id>/` — per-order artifacts
  (manifest.json, stdout.log, stderr.log, result.json, receipt.json).

## 7. Decisions deferred

- Cross-machine failover: OFF until C9 (failover test) proves the
  claim ownership + lease + expected_state_sha protocol.
- Frontier providers: OFF until operator sets `ALLOW_FRONTIER=1`.
- Public network listener: OFF; only loopback health endpoint.
- Sharing the legion-bridge package as a system-wide pip install: not
  required; venv in /home/andre/echelon-bridge/.venv is sufficient.

## 8. Sign-off

This design is the basis for Checkpoint 3 (identity), Checkpoint 4
(harness integration), Checkpoint 5 (work-order safety), Checkpoint 6
(backup modes), Checkpoint 8 (isolated comm test), Checkpoint 9 (failover
test), and Checkpoint 10 (service install). No machine changes are
required by this document; it is read-only.

Pending operator approval to proceed to Checkpoint 3.
