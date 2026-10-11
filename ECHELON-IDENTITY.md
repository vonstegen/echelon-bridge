# ECHELON — Worker Identity (Checkpoint 3)

**Status:** CONFIGURED
**Worker ID:** ECHELON
**Worker role:** engineering-backup
**Worker status:** STANDBY
**Date:** 2026-10-11
**Operator:** vonstegen

## 1. Identity (published)

```
worker_id: ECHELON
worker_role: engineering-backup
worker_status: STANDBY
worker_unique: true
allowed_repos: ["vonstegen/echelon-bridge-test"]
supported_harnesses: detected at startup, never assumed
model_profiles: local, frontier (frontier gated by allow_frontier=false)
```

The bridge never advertises a capability it cannot prove at startup. The
`legion-bridge status` command is the authoritative source; the operator
publishes that output to GitHub as a worker registration issue.

## 2. Files created

- `~/.config/echelon-bridge/policy.toml` (mode 0644)
- `~/.config/echelon-bridge/worker.key` (mode 0600, 32-byte HMAC key)
- `~/.local/share/echelon-bridge/state/` (claims.sqlite will be created
  on first claim)
- `~/.local/share/echelon-bridge/work/`
- `~/.local/share/echelon-bridge/runs/`

No machine-wide service was enabled. No existing config was modified.

## 3. Dynamic harness detection

At every listener start, `legion-bridge status` probes:

- `command -v omp` -> omp 18.0.6 (path /home/andre/.npm-global/bin/omp)
- `command -v hermes` -> hermes 0.15.1 (path /home/andre/.local/bin/hermes)
- `command -v pi` -> pi 0.80.3 (path /home/andre/.npm-global/bin/pi)
- `command -v ollama` -> 0.30.8, listening 127.0.0.1:11434

Harnesses not on PATH are NOT advertised. The `local` harness is a built-in
echo stub for tests; it is always present.

## 4. Model profiles

| Profile | Source | Status on ECHELON |
| --- | --- | --- |
| local | ollama serve 127.0.0.1:11434 | ACTIVE (13 models) |
| frontier | omp 18 + provider env vars | GATED (allow_frontier=false) |
| automatic | policy-driven | not yet implemented in upstream |

## 5. What ECHELON publishes

Per the work order, the worker publishes:

- worker_id, worker_role, worker_status
- supported_harnesses (detected, not assumed)
- available model profiles (local; frontier gated)
- current execution status (idle in STANDBY)
- last heartbeat (N/A until listener starts; populated every 60s)
- current work-order identifier (none)

It NEVER publishes:

- API keys, tokens, HMAC keys, SSH keys
- filesystem paths under /home, /root, or anything sensitive
- env values from `operator.env`

## 6. Reproducing `legion-bridge status`

Once the package is installed in a venv:

```
~/.venvs/echelon-bridge/bin/python -m legion_bridge.cli status \
  --policy ~/.config/echelon-bridge/policy.toml
```

Expected output (post-install):

```
worker_id=ECHELON
role=engineering
mode=STANDBY
allowed_repos=[vonstegen/echelon-bridge-test]
lease_ttl_s=600 heartbeat_interval_s=60 poll_interval_s=30
allow_frontier=false worker_unique=true
harnesses=omp,hermes,local
models=local(qwen3.5:27b,gemma3:27b,...)
health=127.0.0.1:0 (disabled)
queue_depth=0
```
