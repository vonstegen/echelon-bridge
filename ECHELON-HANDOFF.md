# ECHELON — GitHub Handoff (Checkpoint 11)

**Status:** PR OPEN, AWAITING OPERATOR REVIEW
**Date:** 2026-10-11

## 1. Repository and branch

- **Repository:** https://github.com/vonstegen/echelon-bridge
- **Branch:** feature/echelon-bridge-checkpoint-1-10
- **Base:** main
- **Pull request:** https://github.com/vonstegen/echelon-bridge/pull/1
- **Commit SHA:** 5e3d9ae1d450b999e89017b2b1314c498379f04f
- **PR state:** OPEN (not merged; auto-merge is disabled)

## 2. Checkpoint evidence

| Checkpoint | Document | Result |
| --- | --- | --- |
| C1 | ECHELON-SYSTEM-INVENTORY.md | PASS (read-only) |
| C2 | ECHELON-COMMUNICATION-DESIGN.md | DESIGN (DRAFT) |
| C3 | ECHELON-IDENTITY.md | CONFIGURED |
| C4 | ECHELON-HARNESS-INTEGRATION.md | PASS (19/19 unit tests) |
| C5 | ECHELON-WORK-ORDER-SAFETY.md | DESIGN |
| C6 | ECHELON-MODES.md | CONFIGURED (STANDBY default) |
| C7 | ECHELON-WORKSPACE.md | CONFIGURED |
| C8 | ECHELON-COMMUNICATION-TEST.md | PASS (10/10 acceptance) |
| C9 | ECHELON-FAILOVER-TEST.md | PARTIAL (upstream gap documented) |
| C10 | ECHELON-SERVICE-OPERATIONS.md | UNIT INSTALLED, NOT ENABLED |

## 3. Worker capabilities (published)

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

## 4. Available harnesses

| Harness | Version | Path | Detected |
| --- | --- | --- | --- |
| omp | 18.0.6 | /home/andre/.npm-global/bin/omp | yes |
| hermes | 0.15.1 | /home/andre/.local/bin/hermes | yes |
| local | n/a | /usr/bin/echo stub | yes |

## 5. Service status

- systemd --user unit: `/home/andre/.config/systemd/user/echelon-bridge.service`
- Loaded: yes
- Enabled: NO
- Active: NO
- Worker mode: STANDBY

## 6. Known limitations

1. Upstream `legion-bridge` ownership check has a `stale_foreign` gap
   (C9). One-line fix proposed; not applied here.
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

## 7. Operator actions to activate

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

## 8. Final acceptance

The full pipeline (per the work order) was demonstrated end-to-end
against a real GitHub test issue (#1 on
`vonstegen/echelon-bridge-test`) without depending on VIGIL or LEGION.
The signed receipt was posted as a comment with signature
`d8e7d6fa9dc5bfbb1108cdf6ba8f0645e91f38d00183416bb7cbde166572a304`.

ECHELON remains in STANDBY mode. The systemd unit is installed but
not enabled. Activation is gated on the upstream C9 fix and operator
approval.
