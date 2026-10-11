"""Listener loop. Not enabled until operator approval."""
from __future__ import annotations

import argparse
import signal
import sys
import time
from pathlib import Path

from . import github as gh
from .cli import _policy_path
from .core import AuditLog, load_policy


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="legion-bridge loop")
    parser.add_argument("--policy", default="~/.config/legion-bridge/policy.toml")
    args = parser.parse_args(argv)
    policy = load_policy(_policy_path(args))
    audit = AuditLog(policy.state_root / "audit.jsonl")
    if not gh.gh_authenticated():
        audit.append("loop_start_failed", reason="gh_not_authenticated")
        print("legion-bridge loop: gh not authenticated; aborting", file=sys.stderr)
        return 2
    audit.append("loop_start", poll_interval_s=policy.poll_interval_s)
    stop = {"flag": False}

    def _handle(signum: int, _frame: object) -> None:
        stop["flag"] = True
        audit.append("loop_signal", signum=signum)

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    while not stop["flag"]:
        # Real loop would poll → claim → run → publish; left to the
        # operator to wire up after enabling the service.
        time.sleep(policy.poll_interval_s)
    audit.append("loop_stop")
    return 0


if __name__ == "__main__":
    sys.exit(main())
