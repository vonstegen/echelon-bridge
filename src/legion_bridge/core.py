"""LEGION bridge core: types, policy loader, audit, secrets, signing."""
from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import stat
import threading
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = "legion-bridge/v0.1"
INTAKE_LABEL = "legion-bridge:pending"
CLAIMED_LABEL = "legion-bridge:claimed"
RUNNING_LABEL = "legion-bridge:running"
COMPLETED_LABEL = "legion-bridge:completed"
FAILED_LABEL = "legion-bridge:failed"
BLOCKED_LABEL = "legion-bridge:blocked"
INTERRUPTED_LABEL = "legion-bridge:interrupted"
TERMINAL_LABELS = {COMPLETED_LABEL, FAILED_LABEL, BLOCKED_LABEL}

SECRET_PATTERNS = [
    "ghu_",
    "ghp_",
    "gho_",
    "ghs_",
    "ghr_",
    "sk-ant-",
    "sk-",
    "xoxb-",
    "xoxp-",
    "AKIA",
]


def utcnow_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def utcnow_epoch() -> int:
    return int(_dt.datetime.now(_dt.timezone.utc).timestamp())


def redact_secrets(text: str) -> tuple[str, int]:
    redacted = text
    count = 0
    for p in SECRET_PATTERNS:
        if p in redacted:
            redacted = redacted.replace(p, "<redacted:secret>")
            count += 1
    return redacted, count


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(data) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()

def sign(secret: bytes, payload) -> str:
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def verify(secret: bytes, payload, sig: str) -> bool:
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
    allowed_repos: list
    harness_policies: dict
    lease_ttl_s: int
    heartbeat_interval_s: int
    poll_interval_s: int
    poll_limit: int
    publish_retry_max: int
    publish_max_attempts: int
    allow_frontier: bool
    frontier_kinds: list
    workdir_root: Path
    state_root: Path
    health_host: str
    health_port: int
    operator_team: str | None
    operator_org: str | None
    worker_unique: bool
    host: str | None
    github_timeout_s: int
    # === ECHELON: worker-mode + key-path + distributed-claim gate ===
    mode: str
    key_path: Path
    distributed_claim_protocol_proven: bool
    activate_env: str
    def harness(self, name: str):
        return self.harness_policies.get(name)


@dataclasses.dataclass
class HarnessPolicy:
    name: str
    binary: str
    argv_template: list
    isolation: str
    env_passthrough: list
    timeout_s: int
    max_cost_usd: float
    harness_kind: str
    input_template: str | None
    acceptance_exit_code: int
    acceptance_stdout_contains: str | None
    # === ECHELON: exact-output acceptance (raw stdout must equal this
    # string after byte-exact comparison, NOT substring). Substring
    # matching must not pass an exact-output work order. ===
    acceptance_stdout_equals: str | None


def _parse_value(v: str):
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


def _parse_toml(text: str) -> dict:
    out = {}
    cur = out
    in_section = False
    in_array_table = False
    last_array_table = None

    def as_dict(node):
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
            new_item = {}
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
                parsed_val = []
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
    harness_policies = {}
    if isinstance(harnesses_raw, dict):
        for name, hp in harnesses_raw.items():
            if isinstance(hp, list):
                if not hp:
                    continue
                hp = hp[-1]
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
                input_template=(None if hp.get("input_template") is None else str(hp.get("input_template"))),
                acceptance_exit_code=int(hp.get("acceptance_exit_code", 0)),
                acceptance_stdout_contains=(None if hp.get("acceptance_stdout_contains") is None else str(hp.get("acceptance_stdout_contains"))),
                # === ECHELON: exact-output acceptance ===
                acceptance_stdout_equals=(None if hp.get("acceptance_stdout_equals") is None else str(hp.get("acceptance_stdout_equals"))),
            )

    def first_list(*keys):
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

    def first_scalar(key, default):
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
        publish_max_attempts=int(first_scalar("publish_max_attempts", 10)),
        allow_frontier=bool(first_scalar("allow_frontier", False)),
        frontier_kinds=first_list("frontier_kinds") or ["frontier", "openai", "anthropic", "copilot"],
        workdir_root=Path(os.path.expanduser(str(first_scalar("workdir_root", "~/.local/share/legion-bridge/work")))),
        state_root=Path(os.path.expanduser(str(first_scalar("state_root", "~/.local/share/legion-bridge/state")))),
        health_host=str(first_scalar("health_host", "127.0.0.1")),
        health_port=int(first_scalar("health_port", 0)),
        operator_team=first_scalar("operator_team", None),
        operator_org=first_scalar("operator_org", None),
        worker_unique=bool(first_scalar("worker_unique", True)),
        host=(None if first_scalar("host", None) is None else str(first_scalar("host", None))),
        github_timeout_s=int(first_scalar("github_timeout_s", 30)),
        # === ECHELON: mode + key path + distributed-claim gate ===
        mode=str(first_scalar("mode", "STANDBY")).upper(),
        key_path=Path(os.path.expanduser(str(first_scalar("key_path", "~/.config/legion-bridge/worker.key")))),
        distributed_claim_protocol_proven=bool(first_scalar("distributed_claim_protocol_proven", False)),
        activate_env=str(first_scalar("activate_env", "ALLOW_LEGION_ACTIVE")),
    )


class SecretFile:
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

    def append(self, event: str, **fields) -> None:
        record = {
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
    CREATE TABLE IF NOT EXISTS executions (
      repo TEXT NOT NULL, issue_number INTEGER NOT NULL,
      started_at INTEGER NOT NULL, PRIMARY KEY(repo, issue_number)
    );
    CREATE TABLE IF NOT EXISTS audit (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      ts TEXT NOT NULL,
      event TEXT NOT NULL,
      payload TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS completions (
      repo TEXT NOT NULL,
      issue_number INTEGER NOT NULL,
      token TEXT NOT NULL,
      receipt_signature TEXT,
      worker_id TEXT NOT NULL,
      published_at INTEGER NOT NULL,
      PRIMARY KEY (repo, issue_number)
    );
    CREATE TABLE IF NOT EXISTS publish_retry (
      repo TEXT NOT NULL,
      issue_number INTEGER NOT NULL,
      attempts INTEGER NOT NULL DEFAULT 0,
      last_error TEXT,
      PRIMARY KEY (repo, issue_number)
    );
    """

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.executescript(self.SCHEMA)
        self._conn.commit()

    def has_execution(self, repo, number):
        with self._lock:
            return self._conn.execute("SELECT 1 FROM executions WHERE repo=? AND issue_number=?", (repo, number)).fetchone() is not None

    def begin_execution(self, repo, number):
        with self._lock:
            cur = self._conn.execute("INSERT OR IGNORE INTO executions VALUES (?, ?, ?)", (repo, number, utcnow_epoch()))
            self._conn.commit()
            return cur.rowcount == 1

    def list_unpublished(self):
        with self._lock:
            cur = self._conn.execute("SELECT * FROM claims WHERE status IN ('completed','failed','publish_failed')")
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def get(self, repo: str, issue_number: int):
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM claims WHERE repo=? AND issue_number=?",
                (repo, issue_number),
            )
            row = cur.fetchone()
            if not row:
                return None
            cols = [c[0] for c in cur.description]
            return dict(zip(cols, row))

    def upsert_claim(self, repo, issue_number, worker_id, expected_state_sha, lease_ttl_s, harness_requested=None):
        with self._lock:
            existing = self.get(repo, issue_number)
            now = utcnow_epoch()
            if existing:
                # === ECHELON: hard ownership guard. Refuse any claim from
                # a different worker when the existing row is `claimed`,
                # `running`, `stale_foreign`, or any other live state.
                # This closes the reported `stale_foreign` gap: after
                # recover() marks a foreign row stale_foreign, no other
                # worker may REPLACE it. The local worker may only
                # reclaim a row that (a) was originally its own AND
                # (b) has no execution fence. ===
                if existing["worker_id"] != worker_id:
                    return False, f"skipped-foreign-claim:{existing['worker_id']}"
                if existing["status"] in ("claimed", "running") and existing["lease_expires_at"] > now:
                    return False, "skipped-already-claimed-self"
            self._conn.execute(
                """
                REPLACE INTO claims
                (repo, issue_number, worker_id, claimed_at, lease_expires_at,
                 heartbeat_at, expected_state_sha, status, harness_requested,
                 harness_resolved, session_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (repo, issue_number, worker_id, now, now + lease_ttl_s, now,
                 expected_state_sha, "claimed", harness_requested, None, None),
            )
            self._conn.commit()
            return True, "claimed"

    def update_status(self, repo, issue_number, status, **fields):
        with self._lock:
            sets = ["status=?", "heartbeat_at=?"]
            values = [status, utcnow_epoch()]
            for k, v in fields.items():
                sets.append(f"{k}=?")
                values.append(v)
            values.extend([repo, issue_number])
            self._conn.execute(
                f"UPDATE claims SET {', '.join(sets)} WHERE repo=? AND issue_number=?",
                values,
            )
            self._conn.commit()

    def list_active(self) -> Iterable:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM claims WHERE status IN ('claimed','running')"
            )
            cols = [c[0] for c in cur.description]
            for row in cur.fetchall():
                yield dict(zip(cols, row))

    def delete_claim(self, repo, issue_number):
        with self._lock:
            self._conn.execute(
                "DELETE FROM claims WHERE repo=? AND issue_number=?",
                (repo, issue_number),
            )
            self._conn.commit()

    def list_publish_failed(self) -> list:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM claims WHERE status='publish_failed' ORDER BY heartbeat_at"
            )
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def has_completion_token(self, repo, issue_number) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "SELECT 1 FROM completions WHERE repo=? AND issue_number=?",
                (repo, issue_number),
            )
            return cur.fetchone() is not None

    def mark_completed(self, repo, issue_number, token, receipt_signature, worker_id):
        with self._lock:
            self._conn.execute(
                "REPLACE INTO completions (repo, issue_number, token, receipt_signature, worker_id, published_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (repo, issue_number, token, receipt_signature, worker_id, utcnow_epoch()),
            )
            self._conn.commit()

    def record_publish_attempt(self, repo, issue_number, error) -> int:
        with self._lock:
            self._conn.execute(
                "REPLACE INTO publish_retry (repo, issue_number, attempts, last_error) "
                "VALUES (?, ?, COALESCE((SELECT attempts FROM publish_retry WHERE repo=? AND issue_number=?), 0) + 1, ?)",
                (repo, issue_number, repo, issue_number, error),
            )
            self._conn.commit()
            cur = self._conn.execute(
                "SELECT attempts FROM publish_retry WHERE repo=? AND issue_number=?",
                (repo, issue_number),
            )
            row = cur.fetchone()
            return int(row[0]) if row else 0

    def clear_publish_retry(self, repo, issue_number):
        with self._lock:
            self._conn.execute(
                "DELETE FROM publish_retry WHERE repo=? AND issue_number=?",
                (repo, issue_number),
            )
            self._conn.commit()

    def log_audit(self, event, payload):
        with self._lock:
            self._conn.execute(
                "INSERT INTO audit (ts, event, payload) VALUES (?, ?, ?)",
                (utcnow_iso(), event, canonical_json(payload)),
            )
            self._conn.commit()
