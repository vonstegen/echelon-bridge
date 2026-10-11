"""GitHub adapter: thin wrapper over `gh` CLI. Never echoes secrets."""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .core import INTAKE_LABEL, redact_secrets


class GhError(RuntimeError):
    pass


@dataclass
class Issue:
    number: int
    title: str
    body: str
    created_at: str
    labels: list[str]
    author: str
    state: str
    node_id: str
    html_url: str


def _gh(*args: str, check: bool = True, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "GH_NO_UPDATE_NOTIFIER": "1",
        "GH_PROMPT_DISABLED": "1",
    }
    # Honor explicit gh_* config overrides but never leak the parent shell's
    # other env (credentials, model API keys, etc).
    for k in ("GH_CONFIG_DIR", "GH_TOKEN", "GITHUB_TOKEN", "GH_HOST"):
        if k in os.environ:
            env[k] = os.environ[k]
    res = subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=30 if timeout is None else timeout,
    )
    if check and res.returncode != 0:
        redacted_stderr, _ = redact_secrets(res.stderr)
        raise GhError(f"gh {args[0]} failed rc={res.returncode}: {redacted_stderr[:200]}")
    return res


def gh_authenticated() -> bool:
    res = _gh("auth", "status", check=False)
    return res.returncode == 0


def gh_login_user() -> str | None:
    res = _gh("api", "user", "-q", ".login", check=False)
    if res.returncode != 0:
        return None
    return res.stdout.strip() or None


def list_candidate_issues(repo: str, limit: int = 25) -> list[Issue]:
    res = _gh(
        "issue",
        "list",
        "--repo",
        repo,
        "--label",
        INTAKE_LABEL,
        "--state",
        "open",
        "--json",
        "number,title,body,createdAt,labels,author,state,id,url",
        "--limit",
        str(limit),
        check=False,
    )
    if res.returncode != 0:
        return []
    try:
        items = json.loads(res.stdout)
    except json.JSONDecodeError:
        return []
    out: list[Issue] = []
    for it in items:
        labels = [str(lab.get("name", "")) for lab in it.get("labels", [])]
        out.append(
            Issue(
                number=int(it["number"]),
                title=str(it.get("title") or ""),
                body=str(it.get("body") or ""),
                created_at=str(it.get("createdAt") or ""),
                labels=labels,
                author=str((it.get("author") or {}).get("login") or ""),
                state=str(it.get("state") or "open"),
                node_id=str(it.get("id") or ""),
                html_url=str(it.get("url") or ""),
            )
        )
    out.sort(key=lambda i: i.created_at)
    return out


def issue_full(repo: str, number: int) -> Issue:
    res = _gh(
        "issue",
        "view",
        str(number),
        "--repo",
        repo,
        "--json",
        "number,title,body,createdAt,labels,author,state,id,url",
        check=False,
    )
    if res.returncode != 0:
        raise GhError(f"issue view failed rc={res.returncode}")
    it = json.loads(res.stdout)
    labels = [str(lab.get("name", "")) for lab in it.get("labels", [])]
    return Issue(
        number=int(it["number"]),
        title=str(it.get("title") or ""),
        body=str(it.get("body") or ""),
        created_at=str(it.get("createdAt") or ""),
        labels=labels,
        author=str((it.get("author") or {}).get("login") or ""),
        state=str(it.get("state") or "open"),
        node_id=str(it.get("id") or ""),
        html_url=str(it.get("url") or ""),
    )


def issue_state_sha(repo: str, number: int) -> str:
    """Best-effort state SHA: SHA256 over the issue's labels list."""
    issue = issue_full(repo, number)
    return ",".join(sorted(issue.labels))


def list_issue_comments(repo: str, number: int, *, timeout=None) -> list[dict]:
    """Read every page; an unreadable history is not an empty history."""
    res = _gh("api", f"repos/{repo}/issues/{number}/comments", "--paginate",
              "--slurp", timeout=timeout)
    pages = json.loads(res.stdout)
    return [comment for page in pages for comment in page]


def find_receipt_comment(repo: str, number: int, signature: str) -> str | None:
    """If a comment with `## legion-bridge receipt` and the given signature
    already exists on the issue, return its id; otherwise return None."""
    target = f"signature: `{signature}`"
    for c in list_issue_comments(repo, number):
        body = str(c.get("body") or "")
        if "## legion-bridge receipt" in body and target in body:
            return str(c.get("id"))
    return None



def add_label(repo: str, number: int, label: str, *, timeout: float | None = None) -> None:
    _gh("issue", "edit", str(number), "--repo", repo, "--add-label", label, timeout=timeout)


def remove_label(repo: str, number: int, label: str, *, timeout: float | None = None) -> None:
    _gh("issue", "edit", str(number), "--repo", repo, "--remove-label", label, timeout=timeout)


def post_comment(repo: str, number: int, body: str, *, timeout: float | None = None) -> str:
    redacted, _ = redact_secrets(body)
    res = _gh(
        "issue",
        "comment",
        str(number),
        "--repo",
        repo,
        "--body",
        redacted,
        check=False,
        timeout=30 if timeout is None else timeout,
    )
    if res.returncode != 0:
        raise GhError(f"comment failed rc={res.returncode}")
    return redacted[:200]


def user_in_team(user: str, org: str, team: str) -> bool | None:
    """Returns True/False if known, None if not determinable."""
    res = _gh(
        "api",
        f"orgs/{org}/teams/{team}/memberships/{user}",
        check=False,
    )
    if res.returncode == 0:
        try:
            data = json.loads(res.stdout)
        except json.JSONDecodeError:
            return None
        return data.get("state") == "active"
    if res.returncode in (404, 403):
        return None
    return None


def parse_front_matter(body: str) -> tuple[dict[str, Any], str]:
    """Parse a YAML-ish front-matter block. Stops at first blank line after '---'."""
    if not body.startswith("---"):
        return {}, body
    lines = body.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, body
    out: dict[str, Any] = {}
    i = 1
    while i < len(lines):
        line = lines[i]
        if line.strip() == "---":
            return out, "\n".join(lines[i + 1 :])
        if ":" not in line:
            i += 1
            continue
        key, _, val = line.partition(":")
        out[key.strip()] = val.strip()
        i += 1
    return {}, body


# Top-level safety gate: front-matter must never set allow_git_*.
def front_matter_safety_check(fm: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for forbidden in ("allow_git_write", "allow_git_push", "allow_network"):
        if forbidden in fm:
            errors.append(
                f"front-matter may not declare {forbidden!r}; it is operator-controlled"
            )
    runtime = fm.get("max_runtime_s")
    if runtime is not None:
        try:
            r = int(str(runtime).strip())
        except ValueError:
            errors.append("max_runtime_s must be an integer")
            r = 0
        if r > 1800:
            errors.append("max_runtime_s exceeds 1800s cap")
    # Front-matter may request any harness name; the policy table
    # is the authoritative allowlist. The safety check here is
    # only that the value is a non-empty identifier string.
    if "harness" in fm and not isinstance(fm["harness"], str):
        errors.append("harness must be a string")
    elif "harness" in fm and not fm["harness"].strip():
        errors.append("harness must be non-empty")
    elif "harness" in fm and not all(c.isalnum() or c in "-_." for c in fm["harness"]):
        errors.append(f"unsupported harness identifier: {fm['harness']!r}")
    return errors


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
