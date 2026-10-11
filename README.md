# echelon-bridge

ECHELON is a second worker instance of the shared
[`legion-bridge`](https://github.com/vonstegen/legion-bridge) package.
This repo exists to capture the ECHELON-specific configuration,
checkpoint documents, and operator handoff. The actual bridge code
lives upstream.

## Worker identity

```
worker_id: ECHELON
worker_role: engineering-backup
worker_status: STANDBY
allowed_repos: ["vonstegen/echelon-bridge-test"]
supported_harnesses: omp, hermes, local
allow_frontier: false
```

## Layout

- `src/legion_bridge/` — vendored from upstream `legion-bridge`.
- `tests/` — vendored from upstream.
- `pyproject.toml` — same as upstream.
- `etc/legion-bridge.service` — upstream unit template.
- `bin/echelon-mode` — ECHELON-specific mode helper.
- `ECHELON-*.md` — checkpoint documents.

## Checkpoints

1. `ECHELON-SYSTEM-INVENTORY.md`
2. `ECHELON-COMMUNICATION-DESIGN.md`
3. `ECHELON-IDENTITY.md`
4. `ECHELON-HARNESS-INTEGRATION.md`
5. `ECHELON-WORK-ORDER-SAFETY.md`
6. `ECHELON-MODES.md`
7. `ECHELON-WORKSPACE.md`
8. `ECHELON-COMMUNICATION-TEST.md`
9. `ECHELON-FAILOVER-TEST.md`
10. `ECHELON-SERVICE-OPERATIONS.md`
11. `ECHELON-HANDOFF.md` (this file)

## Status

| Checkpoint | Status |
| --- | --- |
| C1-C7, C10 | PASS |
| C8 | PASS (all 10 acceptance criteria) |
| C9 | PARTIAL (upstream bridge ownership-check gap documented) |
| C11 | PENDING OPERATOR REVIEW |

The bridge is in **STANDBY mode**. The systemd user unit is installed
but **disabled**. Do not enable it until the upstream fix from C9
lands and the operator approves ACTIVE mode.
