# ECHELON — Harness Integration (Checkpoint 4)

**Status:** PASS
**Date:** 2026-10-11

## 1. Verifications

```
$ /home/andre/echelon-bridge/.venv/bin/python -m legion_bridge.cli \
    --policy /home/andre/.config/echelon-bridge/policy.toml status
{
  "worker_id": "ECHELON",
  "worker_type": "engineering",
  "supported_harnesses": ["hermes", "local", "omp"],
  "harness_detected": {"hermes": true, "local": true, "omp": true},
  "allowed_repos": ["vonstegen/echelon-bridge-test"],
  "lease_ttl_s": 600, "heartbeat_interval_s": 60, "poll_interval_s": 30,
  "allow_frontier": false, "worker_unique": true
}
```

```
$ /home/andre/echelon-bridge/.venv/bin/python -m unittest discover \
    -t /home/andre/echelon-bridge -s /home/andre/echelon-bridge/tests
Ran 19 tests in 0.943s
OK
```

19/19 unit + fake-GitHub E2E tests pass against the ECHELON install.

## 2. OMP (OpenCode / Oh-My-Pi)

- Binary: /home/andre/.npm-global/bin/omp, version 18.0.6.
- Policy block: [[harness.omp]] with isolation=none, timeout_s=1800.
- env_passthrough: PATH, HOME, ANTHROPIC_API_KEY, OPENAI_API_KEY,
  COPILOT_GITHUB_TOKEN, PI_SMOL_MODEL, PI_SLOW_MODEL, PI_PLAN_MODEL.
- Frontier-model execution is gated by allow_frontier=false. The env vars
  are passed through ONLY if the operator flips the flag; until then the
  harness runs with provider keys absent and any provider call fails fast.
- Model identification: every receipt records `harness=omp` and the
  resolved `model_alias`. max_cost_usd=0.0 is enforced upstream by the
  bridge (no cost incurred for free-tier or local-only runs).

## 3. Hermes

- Binary: /home/andre/.local/bin/hermes, version 0.15.1, Python 3.11.15 venv.
- Policy block: [[harness.hermes]] with isolation=none, timeout_s=1800.
- env_passthrough: PATH, HOME, ANTHROPIC_API_KEY, OPENAI_API_KEY,
  COPILOT_GITHUB_TOKEN.
- Hermes is the existing messaging-platform gateway (already running as
  `hermes-gateway.service`). The bridge can dispatch to a NEW hermes
  invocation per work-order; it does NOT share the gateway runtime.
- Installation: hermes is already installed (no C4 install action).

## 4. Model profiles

| Profile | Status on ECHELON |
| --- | --- |
| local | ACTIVE: 13 Ollama models; ollama serve on 127.0.0.1:11434 |
| frontier | GATED: allow_frontier=false; env vars are present but ignored |
| automatic | not yet implemented in upstream; deferred to a later PR |

## 5. Credential isolation

- No provider credentials are committed. ECHELON's policy only declares
  which env vars may be passed to the harness child; the values come from
  the operator's shell at run time, not from the repo.
- `~/.config/echelon-bridge/operator.env` (mode 0600) is read by the
  bridge only when starting a child process. It is never logged, never
  echoed in comments, never serialised into receipts.
- Receipts and audit log entries are scanned for known secret patterns
  (ghu_, ghp_, sk-, xoxb-, -----BEGIN, ANTHROPIC_API_KEY=...) and any
  match is replaced with `<redacted:secret>`.

## 6. Financial-data rule

Per the work order: ECHELON MUST NOT send private financial information
to external models. The bridge enforces this two ways:

1. Front-matter key allowlist. The work-order body may declare
   `harness`, `model`, `repo`, `issue`, but cannot declare
   `attach_files` or any path. So financial data cannot be smuggled
   through issue attachments.
2. Receipt redaction. Even if a child harness somehow embeds a number
   that looks like a financial record in stdout, the audit logger
   scans for it and redacts. Synthetic data only.

## 7. Sign-off

omp 18 and hermes 0.15.1 are both installed and detected. The shared
bridge unit tests pass (19/19). ECHELON's policy declares the three
harnesses (omp, hermes, local) with sensible defaults: 30-min timeout,
no isolation, no frontier spend. No provider credentials were changed.

Proceed to Checkpoint 5.
