"""Autonomous listener loop.

Polls the configured repositories, claims any candidate work
order, runs it through the configured harness, and posts a
signed receipt. Honors SIGTERM/SIGINT for graceful shutdown.

Single-host enforcement:
  - Acquires an exclusive fcntl lock on `state_root/listener.lock`.
  - Refuses to start if `policy.host` is set and does not match
    the local hostname.
  - Calls `op_recover` at startup and on every tick.
"""
from __future__ import annotations

import argparse
import os
import signal
import socket
import sys
import time
from pathlib import Path

from . import github as gh
from . import operations as ops
from .lockfile import ListenerLockError, acquire as acquire_lock


def _tick(policy: Any, identity: Any, audit: Any, db: Any, *, stop_flag: dict | None = None) -> int:
    """One pass: recover stale claims, then poll + process candidates.

    Returns the number of (claim attempts). Honors stop_flag between
    candidates so SIGTERM halts the current tick after the in-flight
    candidate (not in the middle of one).
    """
    try:
        ops.op_recover(policy, identity, audit, db)
    except Exception as e:  # noqa: BLE001
        audit.append("recover_error", error=str(e))
    candidates = ops.op_poll(policy, audit)
    if not candidates:
        return 0
    attempts = 0
    for c in candidates:
        if stop_flag is not None and stop_flag.get("flag"):
            break
        if not c["labels"] or "legion-bridge:pending" not in c["labels"]:
            continue
        attempts += 1
        try:
            res = ops.op_process_one(policy, identity, audit, db, c["repo"], int(c["number"]))
            audit.append(
                "loop_processed",
                repo=c["repo"],
                number=int(c["number"]),
                ok=res.ok,
                status=res.status,
            )
        except Exception as e:  # noqa: BLE001
            audit.append("loop_unhandled", repo=c["repo"], number=int(c["number"]), error=str(e))
    return attempts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="legion-bridge loop")
    parser.add_argument("--policy", default="~/.config/legion-bridge/policy.toml")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single tick (recover+poll+process) and exit. Useful for tests and cron.",
    )
    parser.add_argument(
        "--max-ticks",
        type=int,
        default=0,
        help="Stop after this many ticks. 0 = unlimited.",
    )
    parser.add_argument(
        "--no-lock",
        action="store_true",
        help="Skip the listener.lock acquisition. Tests and operators only.",
    )
    args = parser.parse_args(argv)

    policy, identity, audit, db, _policy_path = ops.load(args.policy)

    # Single-host enforcement: refuse to run if policy.host is set and
    # does not match this machine's hostname.
    if policy.host:
        local = socket.gethostname()
        if local != policy.host:
            audit.append("loop_start_failed", reason="host_mismatch", expected=policy.host, actual=local)
            print(
                f"legion-bridge loop: policy.host={policy.host!r} does not match hostname={local!r}; aborting",
                file=sys.stderr,
            )
            return 5

    if not gh.gh_authenticated():
        audit.append("loop_start_failed", reason="gh_not_authenticated")
        print("legion-bridge loop: gh not authenticated; aborting", file=sys.stderr)
        return 2

    # Acquire the single-host lock unless explicitly bypassed.
    lock = None
    if not args.no_lock:
        try:
            lock = acquire_lock(policy.state_root, identity.worker_id)
        except ListenerLockError as e:
            audit.append("loop_start_failed", reason="listener_locked", error=str(e))
            print(f"legion-bridge loop: {e}", file=sys.stderr)
            return 6

    audit.append("loop_start", poll_interval_s=policy.poll_interval_s, hostname=socket.gethostname(), worker_id=identity.worker_id)
    stop = {"flag": False, "ticks": 0}

    def _handle(signum: int, _frame: object) -> None:
        stop["flag"] = True
        audit.append("loop_signal", signum=signum)

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)

    try:
        if args.once:
            stop["ticks"] = 1
            _tick(policy, identity, audit, db, stop_flag=stop)
        else:
            while not stop["flag"]:
                if args.max_ticks and stop["ticks"] >= args.max_ticks:
                    break
                _tick(policy, identity, audit, db, stop_flag=stop)
                stop["ticks"] += 1
                if stop["flag"]:
                    break
                for _ in range(max(1, policy.poll_interval_s)):
                    if stop["flag"]:
                        break
                    time.sleep(1)
    finally:
        audit.append("loop_stop", ticks=stop["ticks"])
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass
        if lock is not None:
            lock.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
