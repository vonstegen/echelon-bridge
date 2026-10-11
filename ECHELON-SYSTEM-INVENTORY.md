# ECHELON — System Inventory (Checkpoint 1)

**Status:** PASS (read-only discovery)
**Worker ID (proposed):** ECHELON
**Host:** AvSDellXPS
**Date:** 2026-10-11
**Operator:** vonstegen (`gh auth status` OK)
**Network:** 172.20.138.37/20 (eth0); WSL2 host; no tailscale

## 1. Hardware

- Hostname: AvSDellXPS
- OS: Ubuntu 24.04.4 LTS (Noble Numbat)
- Kernel: 6.6.87.2-microsoft-standard-WSL2 (WSL2)
- Arch: x86_64
- CPU: Intel Core Ultra 9 185H, 22 logical CPUs (11 cores x 2 threads)
- RAM: 31 GiB total, ~28 GiB available
- Swap: 8.0 GiB
- GPU: NVIDIA GeForce RTX 4070 Laptop, 8188 MiB VRAM, driver 581.32, CUDA 13.0
- Storage: 1007 GiB root FS, 708 GiB available (26% used)

WSL2 host — no launchd; only systemd --user is available.

## 2. Toolchain

- ssh: OpenSSH 9.6p1
- git: 2.43.0
- gh: 2.45.0, authed as `vonstegen` (scopes: gist, project, read:org, repo, workflow)
- python3: 3.12.3 (system; plus conda/miniforge and per-project venvs)
- node: v22.22.2; npm 10.9.7
- bun: 1.3.14
- docker: 29.8.0 (user in `docker` group; daemon reachable)
- tailscale: NOT installed (not required for GitHub bridge; flagged for future Mesh)
- systemctl --user: running; user bus 208 units, 4 active (codex-tunnel, dbus, gpg-agent, hermes-gateway)

## 3. Harness / inference runtimes

- omp: /home/andre/.npm-global/bin/omp (18.0.6)
- pi: /home/andre/.npm-global/bin/pi (0.80.3)
- hermes: /home/andre/.local/bin/hermes (0.15.1; Python 3.11.15 venv)
- ollama: /usr/local/bin/ollama (0.30.8, listening 127.0.0.1:11434)

### Ollama local models (13)

qwen3.5:27b-direct (17 GB), gemma4:26b-a4b-it-q4_K_M (17 GB), qwen3.5:27b (17 GB),
qwen3:14b-128k (9.3 GB), hermes3:8b (4.7 GB), gemma3:4b (3.3 GB), qwen3:14b (9.3 GB),
qwen3:8b (5.2 GB), gemma3:27b (17 GB), llama3.1:8b (4.9 GB), qwen2.5-coder:14b (9.0 GB),
nomic-embed-text (274 MB), llama3:latest (4.7 GB).

Largest local model: gemma4:26b-a4b-it-q4_K_M and qwen3.5:27b at 17 GB; both fit
comfortably on the 8 GB RTX 4070 with CPU offload.

## 4. Existing services (systemd --user)

Active: codex-tunnel.service (SSH tunnel localhost:6333 -> codex), dbus.service,
gpg-agent.service, hermes-gateway.service (Hermes Agent Gateway - messaging).

Existing unit file dir: /home/andre/.config/systemd/user (custom units:
codex-tunnel, hermes-gateway, openclaw-gateway, openclaw-node, wiki-auto-sync).

## 5. Listening ports (before bridge)

- 0.0.0.0:22 sshd
- 127.0.0.1:11434 ollama
- 127.0.0.1:6333 ssh-tunneled Qdrant
- 0.0.0.0:8765 unknown (probe candidate)
- systemd-resolved 53 on 127.0.0.53 / 127.0.0.54 / 10.255.255.254

Bridge health endpoint will be loopback only.

## 6. Git / GitHub credentials

- gh auth: `vonstegen` on github.com, scopes gist, project, read:org, repo, workflow.
- Git protocol: https.
- Existing git remotes for operator repos include vonstegen/hot-rod-rig-vigil and
  vonstegen/vigil.

## 7. Reference repos discovered

- vonstegen/legion-bridge (Python package, scaffold+tests) — the canonical shared
  bridge; LEGION is the first worker. ECHELON will be a second worker instance
  using the same `legion-bridge` package with machine-specific config.
- vonstegen/legion-bridge-test (disposable test repo for LEGION) — for ECHELON we
  will need a parallel `echelon-bridge-test` repo (already exists? verify in C2).
- vonstegen/vigil-mcp-work-orders-test, vonstegen/chat-session-recorder-organizer,
  vonstegen/personal-ai-financial-advisor — listed in the work order; not directly
  related to the bridge; the latter is a downstream consumer, not a control surface.
- Local VIGIL orchestrator: /home/andre/hot-rod-rig-vigil (Council orchestrator,
  NOT a work-order bridge). Do not modify.

## 8. Workspace conventions found

- Operator uses $HOME/echelon, $HOME/echelon-bridge (new), $HOME/hot-rod-rig-vigil,
  $HOME/.hermes, $HOME/dev/{echelon-win,git-projects/vigil}.
- Project venvs in place: /home/andre/echelon/venv, /home/andre/hot-rod-rig-vigil/.venv,
  /home/andre/.hermes/hermes-agent/venv.

## 9. Capability matrix

| Capability | Status | Notes |
| --- | --- | --- |
| Local inference | YES | Ollama + 13 models; RTX 4070 GPU + CUDA 13 |
| Frontier-model inference | YES (gated) | omp 18 supports provider profiles; ALLOW_FRONTIER off by default until operator approves |
| Software development | YES | python3, node, bun, git, gh, docker, 700 GB free disk |
| Independent code review | YES | omp + pi harnesses present; can run `pi` for review sessions |
| Persistent background services | YES | systemd --user (208 units, 4 running); pattern already used by `hermes-gateway` |

## 10. Risks and limitations

- WSL2 host: --user systemd unit lifetime is bounded by the user session. Use
  `Restart=always` and `RestartSec=5` for the bridge listener.
- WSL2 mounts (e.g. /mnt/c) have poor fsync behavior. The bridge state DB
  at ~/.local/share/echelon-bridge/claims.sqlite MUST live on the ext4 root
  (/) fs to avoid WAL corruption.

- Tailscale absent: no Mesh. Bridge reaches GitHub via HTTPS 443 outbound only.
  No inbound listener port is required; health endpoint is loopback-only.
- gh token scope: `repo` (no `admin:org`). Sufficient; we never modify org-level
  settings.
- The unknown 0.0.0.0:8765 listener must be identified before installing a new
  service that could collide. (Resolve in C10.)

## 11. Worker ID decision

Candidates: ECHELON or LUMEN.

Evidence: ECHELON already appears as a prior project namespace
(/home/andre/echelon — Unsloth training harness; /home/andre/dev/echelon-win —
local inference + claude_code_queue). LUMEN has no prior footprint on this
host.

Choice: **ECHELON**. The new bridge config lives in /home/andre/echelon-bridge
and ~/.config/echelon-bridge/, which do NOT collide with the existing
/home/andre/echelon training project. The operator already uses ECHELON as
a project name, so adopting it as a worker identity keeps mental model
consistent. LUMEN remains available for a future third machine.

## 12. Conclusion

ECHELON is a viable backup engineering worker:

- All required toolchain and harnesses are present.
- GitHub auth is valid and the right operator identity is in place.
- Local inference is available via Ollama + 13 models on a CUDA 13 RTX 4070.
- Frontier models are reachable via omp 18; provider credentials are gated
  by ALLOW_FRONTIER (default off) and never logged.
- systemd --user is the right service host (no launchd on Linux).
- A reusable shared bridge package (legion-bridge) already exists; ECHELON
  will be a second instance of it with machine-specific config.

No machine changes were made during discovery. Proceed to Checkpoint 2.
