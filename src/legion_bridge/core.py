"""LEGION bridge core: types, policy loader, audit, secrets, signing."""
from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import stat
import sys
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = "legion-bridge/v0.1"
INTAKE_LABEL = "legion-bridge:pending"
CLAIMED_LABEL = "legion-bridge:claimed"
RUNNING_LABEL = "legion-bridge:running"
TERMINAL_LABELS = {
    "legion-bridge:completed",
    "legion-bridge:failed",
    "legion-bridge:blocked",
    "legion-bridge:interrupted",
}

SECRET_PATTERNS = [
    re.compile(r"ghu_[A-Za-z0-9]{30,}"),
    re.compile(r"ghp_[A-Za-z0-9]{30,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{40,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"xoxb-[A-Za-z0-9-]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
]


def utcnow_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def utcnow_epoch() -> int:
    return int(_dt.datetime.now(_dt.timezone.utc).timestamp())


def redact_secrets(text: str) -> tuple[str, int]:
    redacted = text
    count = 0
    for pat in SECRET_PATTERNS:
        redacted, n = pat.subn("<redacted:secret>", redacted)
        count += n
    return redacted, count


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def sign(secret: bytes, payload: bytes | str) -> str:
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def verify(secret: bytes, payload: bytes | str, sig: str) -> bool:
    expected = sign(secret, payload)
    return hmac.compare_digest(expected, sig)


@dataclasses.dataclass
class WorkerIdentity:
    worker_id: str
    worker_type: str
    hostname: str

    @classmethod
    def load(cls, policy: "Policy") -> "WorkerIdentity":
        import socket
        return cls(
            worker_id=policy.worker_id,
            worker_type=policy.worker_type,
            hostname=socket.gethostname(),
        )


@dataclasses.dataclass
class Policy:
    worker_id: str
    worker_type: str
    allowed_repos: list[str]
    harness_policies: dict[str, "HarnessPolicy"]
    lease_ttl_s: int
    heartbeat_interval_s: int
    poll_interval_s: int
    poll_limit: int
    publish_retry_max: int
    allow_frontier: bool
    workdir_root: Path
    state_root: Path
    health_host: str
    health_port: int
    operator_team: str | None
    worker_unique: bool

    def harness(self, name: str) -> "HarnessPolicy | None":
        return self.harness_policies.get(name)


@dataclasses.dataclass
class HarnessPolicy:
    name: str
    binary: str
    argv_template: list[str]
    isolation: str
    env_passthrough: list[str]
    timeout_s: int
    max_cost_usd: float
    harness_kind: str


def _parse_value(v: str) -> Any:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
        return v[1:-1]
    if v in ("true", "True"):
        return True
    if v in ("false", "False"):
        return False
    try:
        if v.startswith("0x") or v.startswith("0X"):
            return int(v, 16)
        if "." in v or "e" in v or "E" in v:
            return float(v)
        return int(v)
    except ValueError:
        return v


def _parse_toml(text: str) -> dict[str, Any]:
    """Minimal TOML subset loader sufficient for policy files.

    Supports:
      - bare top-level keys (before any [section] header)
      - [section] and [a.b] tables
      - [[a.b]] arrays of tables
      - inline lists: ["a", "b"]
      - scalars: string, int, float, bool
    """
    out: dict[str, Any] = {}
    cur: Any = out
    in_section = False
    in_array_table = False
    last_array_table: list[Any] | None = None

    def as_dict(node: Any) -> dict[str, Any]:
        if isinstance(node, dict):
            return node
        if node is None:
            return {}
        raise ValueError(f"expected dict, got {type(node).__name__}")

    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if line.startswith("[[") and line.endswith("]]"):
            section = line[2:-2].strip()
            parts = section.split(".")
            container = as_dict(out)
            for p in parts[:-1]:
                container = as_dict(container.get(p)) if isinstance(container.get(p), dict) else {}
                if p not in out:
                    out[p] = {}
                container = out[p]
            leaf = parts[-1]
            existing = container.get(leaf)
            if not isinstance(existing, list):
                existing = []
                container[leaf] = existing
            new_item: dict[str, Any] = {}
            existing.append(new_item)
            cur = new_item
            in_section = True
            in_array_table = True
            last_array_table = existing
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            parts = section.split(".")
            container = out
            for p in parts:
                if p not in container or not isinstance(container[p], dict):
                    container[p] = {}
                container = container[p]
            cur = container
            in_section = True
            in_array_table = False
            last_array_table = None
            continue
        if "=" not in line:
            raise ValueError(f"policy: cannot parse line: {raw!r}")
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            if not inner:
                parsed_val: Any = []
            else:
                parsed_val = [_parse_value(x.strip()) for x in inner.split(",")]
        else:
            parsed_val = _parse_value(value)
        if in_section:
            target = as_dict(cur)
            target[key] = parsed_val
            if in_array_table and last_array_table is not None and cur is last_array_table[-1]:
                last_array_table[-1] = target
        else:
            out[key] = parsed_val

    return out


def load_policy(path: Path) -> Policy:
    text = path.read_text()
    raw = _parse_toml(text)
    worker = raw.get("worker", {}) if isinstance(raw.get("worker"), dict) else {}

    harnesses_raw = raw.get("harness", {})
    harness_policies: dict[str, HarnessPolicy] = {}
    if isinstance(harnesses_raw, dict):
        for name, hp in harnesses_raw.items():
            if isinstance(hp, list):
                if not hp:
                    continue
                hp = hp[0]
            if not isinstance(hp, dict):
                continue
            harness_policies[name] = HarnessPolicy(
                name=name,
                binary=str(hp.get("binary", name)),
                argv_template=list(hp.get("argv_template", [])),
                isolation=str(hp.get("isolation", "none")),
                env_passthrough=list(hp.get("env_passthrough", [])),
                timeout_s=int(hp.get("timeout_s", 600)),
                max_cost_usd=float(hp.get("max_cost_usd", 0.0)),
                harness_kind=str(hp.get("harness_kind", name)),
            )

    def first_list(*keys: str) -> list[str]:
        for k in keys:
            v = raw.get(k)
            if isinstance(v, list):
                return list(v)
            if isinstance(v, str):
                return [v]
        for section in raw.values():
            if isinstance(section, dict):
                for k in keys:
                    v = section.get(k)
                    if isinstance(v, list):
                        return list(v)
                    if isinstance(v, str):
                        return [v]
        return []

    def first_scalar(key: str, default: Any) -> Any:
        if key in raw and not isinstance(raw[key], (dict, list)):
            return raw[key]
        for section in raw.values():
            if isinstance(section, dict) and key in section and not isinstance(section[key], (dict, list)):
                return section[key]
        return default

    worker_id = str(first_scalar("worker_id", worker.get("id", "LEGION") if isinstance(worker, dict) else "LEGION"))
    worker_type = str(worker.get("type", "engineering") if isinstance(worker, dict) else "engineering")

    return Policy(
        worker_id=worker_id,
        worker_type=worker_type,
        allowed_repos=first_list("allowed_repos"),
        harness_policies=harness_policies,
        lease_ttl_s=int(first_scalar("lease_ttl_s", 600)),
        heartbeat_interval_s=int(first_scalar("heartbeat_interval_s", 60)),
        poll_interval_s=int(first_scalar("poll_interval_s", 30)),
        poll_limit=int(first_scalar("poll_limit", 25)),
        publish_retry_max=int(first_scalar("publish_retry_max", 3)),
        allow_frontier=bool(first_scalar("allow_frontier", False)),
        workdir_root=Path(os.path.expanduser(str(first_scalar("workdir_root", "~/.local/share/legion-bridge/work")))),
        state_root=Path(os.path.expanduser(str(first_scalar("state_root", "~/.local/share/legion-bridge/state")))),
        health_host=str(first_scalar("health_host", "127.0.0.1")),
        health_port=int(first_scalar("health_port", 0)),
        operator_team=first_scalar("operator_team", None),
        worker_unique=bool(first_scalar("worker_unique", True)),
    )


class SecretFile:
    """Mode-0600 key file, lazily created with a random key."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> bytes:
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            key = secrets.token_bytes(32)
            self.path.write_bytes(key)
            os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
            return key
        st = self.path.stat()
        if stat.S_IMODE(st.st_mode) & 0o077:
            raise PermissionError(
                f"secret file {self.path} is world/group accessible; refuse to use"
            )
        return self.path.read_bytes()


class AuditLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: str, **fields: Any) -> None:
        record: dict[str, Any] = {
            "ts": utcnow_iso(),
            "schema": SCHEMA_VERSION,
            "event": event,
            **fields,
        }
        redaction_count = 0
        for key, value in list(record.items()):
            if isinstance(value, str):
                safe, n = redact_secrets(value)
                record[key] = safe
                redaction_count += n
        record["redaction_count"] = redaction_count
        with self.path.open("a", encoding="utf-8") as f:
            f.write(canonical_json(record) + "\n")


class ClaimStore:
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS claims (
      repo TEXT NOT NULL,
      issue_number INTEGER NOT NULL,
      worker_id TEXT NOT NULL,
      claimed_at INTEGER NOT NULL,
      lease_expires_at INTEGER NOT NULL,
      heartbeat_at INTEGER NOT NULL,
      expected_state_sha TEXT NOT NULL,
      status TEXT NOT NULL,
      harness_requested TEXT,
      harness_resolved TEXT,
      session_id TEXT,
      PRIMARY KEY (repo, issue_number)
    );
    CREATE TABLE IF NOT EXISTS audit (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      ts TEXT NOT NULL,
      event TEXT NOT NULL,
      payload TEXT NOT NULL
    );
    """

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path))
        self._conn.executescript(self.SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def get(self, repo: str, issue_number: int) -> dict[str, Any] | None:
        cur = self._conn.execute(
            "SELECT * FROM claims WHERE repo=? AND issue_number=?",
            (repo, issue_number),
        )
        row = cur.fetchone()
        if not row:
            return None
        cols = [c[0] for c in cur.description]
        return dict(zip(cols, row))

    def upsert_claim(
        self,
        repo: str,
        issue_number: int,
        worker_id: str,
        expected_state_sha: str,
        lease_ttl_s: int,
        harness_requested: str | None = None,
    ) -> tuple[bool, str]:
        existing = self.get(repo, issue_number)
        now = utcnow_epoch()
        if existing and existing["status"] in ("claimed", "running"):
            lease_expired = existing["lease_expires_at"] <= now
            if not lease_expired:
                if existing["worker_id"] != worker_id:
                    return False, f"skipped-already-claimed-by:{existing['worker_id']}"
                return False, "skipped-already-claimed-self"
        self._conn.execute(
            """
            REPLACE INTO claims
            (repo, issue_number, worker_id, claimed_at, lease_expires_at,
             heartbeat_at, expected_state_sha, status, harness_requested,
             harness_resolved, session_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                repo,
                issue_number,
                worker_id,
                now,
                now + lease_ttl_s,
                now,
                expected_state_sha,
                "claimed",
                harness_requested,
                None,
                None,
            ),
        )
        self._conn.commit()
        return True, "claimed"

    def update_status(
        self,
        repo: str,
        issue_number: int,
        status: str,
        **fields: Any,
    ) -> None:
        sets = ["status=?", "heartbeat_at=?"]
        values: list[Any] = [status, utcnow_epoch()]
        for k, v in fields.items():
            sets.append(f"{k}=?")
            values.append(v)
        values.extend([repo, issue_number])
        self._conn.execute(
            f"UPDATE claims SET {', '.join(sets)} WHERE repo=? AND issue_number=?",
            values,
        )
        self._conn.commit()

    def list_active(self) -> Iterable[dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT * FROM claims WHERE status IN ('claimed','running')"
        )
        cols = [c[0] for c in cur.description]
        for row in cur.fetchall():
            yield dict(zip(cols, row))

    def log_audit(self, event: str, payload: dict[str, Any]) -> None:
        self._conn.execute(
            "INSERT INTO audit (ts, event, payload) VALUES (?, ?, ?)",
            (utcnow_iso(), event, canonical_json(payload)),
        )
        self._conn.commit()
