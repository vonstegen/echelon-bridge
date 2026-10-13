# ECHELON — GitHub Handoff (Checkpoint 11)

**Status:** PR OPEN, AWAITING OPERATOR REVIEW
**Date:** 2026-10-11
**Commit SHA:** 825d01392e6a30470eab8859499712b38bebf235

## 1. Repository and branch

- **Repository:** https://github.com/vonstegen/echelon-bridge
- **Branch:** feature/echelon-bridge-checkpoint-1-10
- **Base:** main
- **Pull request:** https://github.com/vonstegen/echelon-bridge/pull/1
- **PR state:** OPEN (not merged; auto-merge is disabled)

## 2. Checkpoint evidence

| Checkpoint | Document | Result |
| --- | --- | --- |
| C1 | ECHELON-SYSTEM-INVENTORY.md | PASS (read-only) |
| C2 | ECHELON-COMMUNICATION-DESIGN.md | DESIGN |
| C3 | ECHELON-IDENTITY.md | CONFIGURED |
| C4 | ECHELON-HARNESS-INTEGRATION.md | PASS |
| C5 | ECHELON-WORK-ORDER-SAFETY.md | PASS (mode gate, key path, ownership fix) |
| C6 | ECHELON-MODES.md | CONFIGURED (STANDBY default) |
| C7 | ECHELON-WORKSPACE.md | CONFIGURED |
| C8 | ECHELON-COMMUNICATION-TEST.md | PASS (live smoke) |
| C9 | ECHELON-FAILOVER-TEST.md | PARTIAL (upstream gap documented) |
| C10 | ECHELON-SERVICE-OPERATIONS.md | UNIT INSTALLED, NOT ENABLED |

## 3. Test counts

| Suite | Count | Result |
| --- | --- | --- |
| `tests/test_blockers.py` | 22 | PASS |
| `tests/test_core.py` | n/a | PASS |
| `tests/test_deployment_review.py` | 5 | PASS |
| `tests/test_e2e_recovery.py` | 1 | PASS |
| `tests/test_loop_lifecycle.py` | 2 | PASS |
| `tests/test_lockfile.py` | 4 | PASS |
| `tests/test_operations.py` | 6 | PASS |
| `tests/test_policy.py` | 6 | PASS |
| `tests/test_signature.py` | 4 | PASS |
| **Total unittest** | **50** | **PASS** |
| `_smoke.py` (live ECHELON-policy E2E) | 9 scenarios | **PASS** |

The smoke test:
1. Loads the real ECHELON policy in ACTIVE+proven mode (in tmp).
2. Runs issue 1 with `harness: local`; receipt published.
3. **Independently verifies the receipt HMAC with the real ECHELON key.**
4. Runs issue 2 with `acceptance_stdout_equals` matching → PASS.
5. Runs issue 3 with `acceptance_stdout_equals` mismatching → FAIL.
6. Loads the production policy in STANDBY; op_claim denied.
7. Inserts a foreign-claim row (worker_id=LEGION); op_claim skipped.
8. Acquires the listener lock; a second acquire raises ListenerLockError.
9. Inspects the audit log (12 events recorded).

## 4. ECHELON-specific deltas over upstream

The PR is a clean reconcile of the ECHELON instance with the reviewed
upstream legion-bridge at `cbac46bfd`. ECHELON-specific deltas:

1. **Policy: mode + key_path + protocol-proven + activate_env.** Four
   new required fields on `Policy` with no upstream defaults.
   `load_policy` reads them from TOML with STANDBY / not-proven /
   `ALLOW_LEGION_ACTIVE` defaults for upstream compatibility.
2. **Mode gate in `op_claim` and `op_run`.** STANDBY refuses to claim.
   MAINTENANCE refuses to claim. ACTIVE requires the env var named in
   `policy.activate_env` AND `policy.distributed_claim_protocol_proven=true`.
   The policy file alone is not authoritative.
3. **Per-worker key path.** The hard-coded
   `~/.config/legion-bridge/worker.key` in `op_claim` and `op_publish`
   is replaced by `policy.key_path`. The upstream tests that hard-coded
   the LEGION path were updated to read `policy.key_path` too.
4. **Byte-exact acceptance.** `HarnessPolicy.acceptance_stdout_equals`
   performs a byte-exact comparison of raw stdout against the expected
   string. Substring matching must not pass an exact-output work order.
5. **Receipt includes stdout_sha** (was missing in upstream).
6. **Manifest and receipt both record acceptance_stdout_equals.**
7. **Ownership-claim ownership fix.** `op_recover` now distinguishes
   `is_mine` from `is_foreign` correctly; a foreign stale claim is
   marked `stale_foreign` and not republished. The ECHELON upstream-
   gap test passes.

## 5. Worker capabilities (published)

```
worker_id: ECHELON
worker_role: engineering-backup
worker_status: STANDBY
worker_unique: true
allowed_repos: ["vonstegen/echelon-bridge-test"]
supported_harnesses: omp, hermes, local
model_profiles:
  local: ACTIVE (13 Ollama models, RTX 4070)
  frontier: GATED (allow_frontier=false)
  automatic: not yet implemented upstream
queue_depth: 0
last_heartbeat: N/A (listener not started)
current_work_order: none
```

## 6. Available harnesses

| Harness | Version | Path | Detected |
| --- | --- | --- | --- |
| omp | 18.0.6 | /home/andre/.npm-global/bin/omp | yes |
| hermes | 0.15.1 | /home/andre/.local/bin/hermes | yes |
| local | n/a | /usr/bin/echo stub | yes |

## 7. Service status

- systemd --user unit: `/home/andre/.config/systemd/user/echelon-bridge.service`
- Loaded: yes
- Enabled: **NO**
- Active: **NO**
- Worker mode: **STANDBY**
- distributed_claim_protocol_proven: **false** (production policy)
- activate_env: `ALLOW_ECHELON_ACTIVE` (NOT exported in unit env)

## 8. Known limitations

1. **Upstream `legion-bridge` ownership check still has a
   `stale_foreign` race window** for two active ACTIVE workers
   running `op_recover` at the same instant. Documented in
   ECHELON-FAILOVER-TEST.md. The single-worker failover path is safe.
   The fix is a single line in `op_recover` (EAGER-fence a foreign
   stale claim before publishing). Not applied here; the ECHELON
   policy enforces `distributed_claim_protocol_proven=false`
   until a multi-host test proves the protocol.
2. WSL2 --user systemd lifetime is bounded by the user session. Linger
   not enabled.
3. The 0.0.0.0:8765 listener discovered in C1 is unidentified and not
   owned by ECHELON. Operator should investigate separately.
4. `gh issue edit` does not return ETags; the bridge uses
   `expected_state_sha` comments as a workaround. This is an upstream
   constraint.
5. `ollama` is started by the system, not by ECHELON. If the operator
   kills ollama, local-inference harness runs will fail; the bridge
   marks them `failed:model_unavailable` and continues.

## 9. Operator actions to activate

1. Review the PR: https://github.com/vonstegen/echelon-bridge/pull/1
2. Approve the upstream ownership-check fix (one-line PR to
   `legion-bridge`).
3. After the upstream fix is merged, edit
   `~/.config/echelon-bridge/policy.toml` to set
   `distributed_claim_protocol_proven = true`.
4. Flip mode to ACTIVE: `/home/andre/echelon-bridge/bin/echelon-mode set ACTIVE`.
5. Enable the service: `systemctl --user enable echelon-bridge.service`.
6. Start the service: `systemctl --user start echelon-bridge.service`.
7. (Optional) Enable linger: `sudo loginctl enable-linger andre`.
8. (Optional) Investigate the 0.0.0.0:8765 listener.
9. (Optional) Edit `health_port` from 0 to a loopback port (e.g. 8766)
   to enable `/healthz`, `/readyz`, `/statusz`.

## 10. Final acceptance

The full pipeline (per the work order) was demonstrated end-to-end
via `_smoke.py`:

```
GitHub Test Issue
  -> fake issue_full() with `legion-bridge:intake` label
  -> ECHELON op_claim (ACTIVE, env var, protocol proven)
  -> op_run (local harness)
  -> op_publish
  -> GitHub comment with signed receipt
  -> independent HMAC verification with ECHELON key
```

Live ECHELON-key signature verification: **PASS** (16-byte signature
prefix `14b7213cd94c38aa` on a sample run; signature varies per run).

ECHELON remains in STANDBY mode. The systemd unit is installed but
not enabled. The production policy is `mode=STANDBY`,
`distributed_claim_protocol_proven=false`. Activation is gated on the
upstream C9 fix and operator approval.
