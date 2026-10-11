# ECHELON — Service Operations (Checkpoint 10)

**Status:** UNIT INSTALLED, NOT ENABLED
**Date:** 2026-10-11

## 1. Unit file

- Path: /home/andre/.config/systemd/user/echelon-bridge.service
- Loaded: yes (after `systemctl --user daemon-reload`)
- Enabled: NO
- Active: NO (state `inactive (dead)`)
- Single-instance: yes (systemd units are inherently single-instance per
  --user bus; combined with the in-process bridge lock, two concurrent
  invocations are impossible).

## 2. Unit body

```
[Unit]
Description=ECHELON bridge listener (independent work-order bridge)
Documentation=file:///home/andre/echelon-bridge/README.md
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=60

[Service]
Type=simple
WorkingDirectory=/home/andre/echelon-bridge
Environment="PYTHONUNBUFFERED=1"
Environment="HOME=/home/andre"
ExecStart=/home/andre/echelon-bridge/.venv/bin/python -m legion_bridge.cli loop --policy /home/andre/.config/echelon-bridge/policy.toml
Restart=on-failure
RestartSec=10
StartLimitBurst=5
TimeoutStopSec=15
StandardOutput=journal
StandardError=journal
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/home/andre/.local/share/echelon-bridge
ReadOnlyPaths=/home/andre/.config/echelon-bridge

[Install]
WantedBy=default.target
```

## 3. Operations

| Action | Command |
| --- | --- |
| Reload unit | `systemctl --user daemon-reload` |
| Show status | `systemctl --user status echelon-bridge.service` |
| View logs | `journalctl --user -u echelon-bridge.service -n 200` |
| Enable (DO NOT RUN YET) | `systemctl --user enable echelon-bridge.service` |
| Start (DO NOT RUN YET) | `systemctl --user start echelon-bridge.service` |
| Stop | `systemctl --user stop echelon-bridge.service` |
| Mode (operator-controlled) | `/home/andre/echelon-bridge/bin/echelon-mode show | set STANDBY|ACTIVE|MAINTENANCE` |

## 4. Safety properties

- **Automatic restart on failure**: `Restart=on-failure`, `RestartSec=10`.
- **Bounded restart storm**: `StartLimitBurst=5` in
  `StartLimitIntervalSec=60`. After 5 restarts in 60 s, systemd
  leaves the unit in `failed` state and stops trying.
- **Graceful shutdown**: `TimeoutStopSec=15`. The Python loop honours
  SIGTERM (sets the stop flag and exits within 1s).
- **Resource limits**: implicit via `PrivateTmp` and `ProtectSystem=strict`.
  No `MemoryMax` set; if the operator wants a ceiling, edit the unit
  and add `MemoryMax=4G` (or whatever the policy allows).
- **Filesystem isolation**: the bridge's state and audit dirs are
  writable; everything else in $HOME is read-only. The policy.toml
  is read-only.

## 5. Known limitations

- WSL2 --user systemd lifetime is bounded by the WSL user session.
  If the host reboots, the unit must be re-enabled. Use the operator
  login to re-enable; or move to `loginctl enable-linger andre` if
  the operator wants ECHELON to survive the WSL VM shutdown.
- The 0.0.0.0:8765 listener discovered in C1 is unidentified. It does
  not conflict with the bridge (which only binds loopback 127.0.0.1),
  but the operator should investigate it separately.
- Health endpoint is disabled (`health_port=0`). Enable by editing
  policy.toml and restarting, but only after C8/C9 PASS.

## 6. Sign-off

Unit installed, loaded, NOT enabled, NOT started. The bridge is in
STANDBY mode. Per the work order, do NOT enable or start the service
until the upstream ownership-check fix (C9) lands and the operator
approves ACTIVE mode.

Proceed to Checkpoint 11 (GitHub handoff).
