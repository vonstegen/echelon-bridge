# ECHELON — Local Development Workspace (Checkpoint 7)

**Status:** CONFIGURED
**Workspace root:** /home/andre/echelon-bridge
**Date:** 2026-10-11

## 1. Layout

```
/home/andre/echelon-bridge/
  .venv/                  Python venv (not committed)
  bin/echelon-mode        mode helper
  src/legion_bridge/      shared bridge package (from upstream)
  tests/                  bridge unit + fake-GitHub E2E tests
  etc/                    upstream unit file template
  pyproject.toml
  conftest.py
  ECHELON-*.md            checkpoint documents (this file and others)
  .gitignore
```

## 2. Git workflow

- One feature branch per checkpoint set:
  `feature/echelon-bridge-checkpoint-1-7` for C1-C7, etc.
- Commit messages reference checkpoint numbers, e.g.
  `C4: install legion-bridge in .venv; detect omp+hermes`.
- Operator pushes and opens a PR; the bridge never auto-merges.
- Tags: none at this stage. Operator may tag `v0.1.0-echelon` after C11.

## 3. Test execution

```
cd /home/andre/echelon-bridge
.venv/bin/python -m unittest discover -t . -s tests
```

19 unit + fake-GitHub E2E tests, all green.

## 4. Lint

No lint config in upstream; we do not add a divergent convention.
Operators may run `ruff` or `pyflakes` ad hoc; the bridge code is short
and idiomatic.

## 5. Logs

- `~/.local/share/echelon-bridge/audit.jsonl` — append-only JSONL.
- `~/.local/share/echelon-bridge/runs/<order_id>/stdout.log`,
  `stderr.log`, `manifest.json`, `result.json`, `receipt.json` — per
  work-order.

## 6. Never commit

The work order specifies these MUST NEVER be committed. ECHELON's
`.gitignore` is configured to ignore them at the bridge and config
level; the operator must additionally ensure they never enter
`tests/fixtures/`:

- API keys, SSH private keys, auth tokens
- Financial statements, bank account info, tax documents
- Production databases
- Real personal data

All test data MUST be synthetic. The C8 communication test uses an
empty initial.txt and a synthetic issue body; no real data.

## 7. Sign-off

Workspace exists at /home/andre/echelon-bridge. Venv installed.
Bridge source in place. Tests pass. .gitignore prevents accidental
secret commits. Per-order run dirs under
~/.local/share/echelon-bridge/runs/ are created on first claim.

Proceed to Checkpoint 8 (isolated communication test).
