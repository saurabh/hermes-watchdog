"""Incident tracking — deduplicate errors, track occurrences, write reports."""

import fcntl
import hashlib
import json
import os
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


STATE_FILE = "state.json"
HEALTH_LOG = "health.jsonl"
MAX_HEALTH_LOG_BYTES = 10_000_000  # 10 MB
_LOCK_FILE = ".state.lock"

def _default_state() -> dict:
    """Fresh default state. Always returns a new dict with new nested dicts."""
    return {"known_issues": {}, "cooldowns": {}, "last_update_check": 0, "hermes_version": ""}


def _load_state(data_dir: str) -> dict:
    """Load watchdog state. Safe for read-only use without lock; writers must hold lock."""
    p = Path(data_dir) / STATE_FILE
    if p.exists():
        try:
            return json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return _default_state()


def _save_state(data_dir: str, state: dict) -> None:
    """Save watchdog state atomically. Caller must hold lock."""
    d = Path(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    target = d / STATE_FILE

    fd, tmp_path = tempfile.mkstemp(dir=str(d), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as tmp_f:
            json.dump(state, tmp_f, indent=2)
        os.replace(tmp_path, str(target))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


@contextmanager
def locked_state(data_dir: str):
    """Load state under exclusive lock, yield it, save on exit.

    Usage:
        with locked_state(data_dir) as state:
            state["key"] = "value"
        # state is saved atomically on exit
    """
    d = Path(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    lock_path = d / _LOCK_FILE

    with open(lock_path, "w") as lock_f:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        try:
            state = _load_state(data_dir)
            yield state
            _save_state(data_dir, state)
        finally:
            fcntl.flock(lock_f, fcntl.LOCK_UN)


def append_health_log(data_dir: str, probe_result: dict) -> None:
    """Append probe result to health JSONL log, rotating if too large."""
    p = Path(data_dir) / HEALTH_LOG
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists() and p.stat().st_size > MAX_HEALTH_LOG_BYTES:
        lines = p.read_text().splitlines()
        p.write_text("\n".join(lines[len(lines) // 2 :]) + "\n")
    with open(p, "a") as f:
        f.write(json.dumps(probe_result) + "\n")


def _signature_to_id(signature: str) -> str:
    """Convert error signature to short ID for filenames."""
    return hashlib.sha256(signature.encode()).hexdigest()[:12]


def track_error(data_dir: str, traceback_info: dict) -> dict:
    """Track an error occurrence. Returns the issue record with updated count.

    Issue record:
    {
        "id": str,
        "signature": str,
        "error_type": str,
        "error_message": str,
        "file": str,
        "line": int,
        "function": str,
        "first_seen": str,
        "last_seen": str,
        "count": int,
        "upstream_searched": bool,
        "upstream_issue": str | None,   # URL of matching issue/PR
        "upstream_filed": str | None,   # URL of issue we created
        "resolved": bool,
        "sample_traceback": str,
    }
    """
    with locked_state(data_dir) as state:
        issues = state.setdefault("known_issues", {})

        sig = traceback_info["signature"]
        issue_id = _signature_to_id(sig)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        if issue_id in issues:
            issue = issues[issue_id]
            issue["last_seen"] = now
            issue["count"] += 1
            if traceback_info.get("traceback") and len(traceback_info["traceback"]) > len(
                issue.get("sample_traceback", "")
            ):
                issue["sample_traceback"] = traceback_info["traceback"]
        else:
            issue = {
                "id": issue_id,
                "signature": sig,
                "error_type": traceback_info["error_type"],
                "error_message": traceback_info["error_message"],
                "file": traceback_info.get("file", ""),
                "line": traceback_info.get("line", 0),
                "function": traceback_info.get("function", ""),
                "first_seen": now,
                "last_seen": now,
                "count": 1,
                "upstream_searched": False,
                "upstream_issue": None,
                "upstream_filed": None,
                "resolved": False,
                "sample_traceback": traceback_info.get("traceback", ""),
            }
            issues[issue_id] = issue

    return issue


def write_incident_report(
    data_dir: str,
    probe_result: dict,
    tracebacks: list[dict],
    remediation_action: str | None = None,
    remediation_result: str | None = None,
) -> str:
    """Write a markdown incident report. Returns the file path."""
    incidents_dir = Path(data_dir) / "incidents"
    incidents_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y-%m-%d-%H%M%S")

    # Create slug from primary error
    slug = "unknown"
    if tracebacks:
        tb = tracebacks[0]
        slug = f"{tb['error_type'].lower()}-{tb['function']}"
    elif not probe_result.get("service_active"):
        slug = "service-down"
    elif not probe_result.get("polling_active"):
        slug = "polling-stale"

    # Sanitize slug
    slug = slug.replace("/", "-").replace(" ", "-")[:50]
    filename = f"{ts}-{slug}.md"
    filepath = incidents_dir / filename

    # Build report
    lines = [
        f"# Incident: {slug}",
        "",
        f"- **Detected:** {now.strftime('%Y-%m-%d %H:%M:%S UTC')}",
        f"- **Severity:** {probe_result.get('level', 'unknown')}",
        f"- **Service Active:** {probe_result.get('service_active', '?')}",
        f"- **Process PID:** {probe_result.get('pid', '?')}",
        f"- **Polling Age:** {probe_result.get('polling_age_s', '?')}s",
        f"- **Memory:** {probe_result.get('memory_mb', '?')} MB",
        "",
        "## Evidence",
        "",
    ]

    if tracebacks:
        for i, tb in enumerate(tracebacks):
            lines.append(f"### Error {i+1}: {tb['error_type']}: {tb['error_message']}")
            lines.append(f"- **File:** `{tb['file']}:{tb['line']}` in `{tb['function']}`")
            lines.append(f"- **Signature:** `{tb['signature']}`")
            lines.append("")
            lines.append("```")
            lines.append(tb.get("traceback", "no traceback captured"))
            lines.append("```")
            lines.append("")
    else:
        lines.append("No tracebacks captured. Probe data above describes the issue.")
        lines.append("")

    lines.append("## Remediation")
    lines.append("")
    lines.append("| Step | Action | Result | Timestamp |")
    lines.append("|------|--------|--------|-----------|")
    if remediation_action:
        result_str = remediation_result or "pending"
        lines.append(
            f"| 1 | {remediation_action} | {result_str} | "
            f"{now.strftime('%H:%M:%S')} |"
        )
    else:
        lines.append("| - | No action taken | - | - |")

    lines.append("")
    lines.append("## Status")
    lines.append("")
    lines.append("unresolved")
    lines.append("")

    filepath.write_text("\n".join(lines))
    return str(filepath)


def get_issues_needing_upstream_search(data_dir: str) -> list[dict]:
    """Get issues that haven't been searched upstream yet."""
    state = _load_state(data_dir)
    return [
        issue
        for issue in state.get("known_issues", {}).values()
        if not issue.get("upstream_searched") and not issue.get("resolved")
    ]


def get_issues_needing_filing(data_dir: str, threshold: int = 3) -> list[dict]:
    """Get issues that hit the occurrence threshold and have no upstream match."""
    state = _load_state(data_dir)
    return [
        issue
        for issue in state.get("known_issues", {}).values()
        if (
            issue.get("count", 0) >= threshold
            and not issue.get("upstream_issue")
            and not issue.get("upstream_filed")
            and not issue.get("resolved")
        )
    ]


def mark_upstream_searched(data_dir: str, issue_id: str, upstream_url: str | None) -> None:
    """Mark an issue as searched upstream, optionally with matching URL."""
    with locked_state(data_dir) as state:
        issue = state.get("known_issues", {}).get(issue_id)
        if issue:
            issue["upstream_searched"] = True
            issue["upstream_issue"] = upstream_url


def mark_upstream_filed(data_dir: str, issue_id: str, filed_url: str) -> None:
    """Record the URL of an issue we filed upstream."""
    with locked_state(data_dir) as state:
        issue = state.get("known_issues", {}).get(issue_id)
        if issue:
            issue["upstream_filed"] = filed_url


def mark_resolved(data_dir: str, issue_id: str) -> None:
    """Mark an issue as resolved (e.g., after upstream fix merged)."""
    with locked_state(data_dir) as state:
        issue = state.get("known_issues", {}).get(issue_id)
        if issue:
            issue["resolved"] = True


def get_cooldown(data_dir: str, action: str) -> float:
    """Get remaining cooldown seconds for a remediation action."""
    state = _load_state(data_dir)
    cooldowns = state.get("cooldowns", {})
    if action not in cooldowns:
        return 0
    expires = cooldowns[action]
    remaining = expires - time.time()
    return max(0, remaining)


def set_cooldown(data_dir: str, action: str, duration_seconds: int) -> None:
    """Set a cooldown for a remediation action."""
    with locked_state(data_dir) as state:
        cooldowns = state.setdefault("cooldowns", {})
        cooldowns[action] = time.time() + duration_seconds


def get_remediation_attempts(data_dir: str) -> int:
    """Get count of remediation attempts in the current incident window."""
    state = _load_state(data_dir)
    return state.get("current_remediation_attempts", 0)


def increment_remediation_attempts(data_dir: str) -> int:
    """Increment and return remediation attempt count."""
    with locked_state(data_dir) as state:
        count = state.get("current_remediation_attempts", 0) + 1
        state["current_remediation_attempts"] = count
    return count


def reset_remediation_attempts(data_dir: str) -> None:
    """Reset remediation attempt counter (called when health returns to normal)."""
    with locked_state(data_dir) as state:
        state["current_remediation_attempts"] = 0


def prune_old_incidents(data_dir: str, retention_days: int = 90) -> int:
    """Remove resolved incident reports older than retention_days. Returns count removed."""
    incidents_dir = Path(data_dir) / "incidents"
    if not incidents_dir.exists():
        return 0

    cutoff = time.time() - (retention_days * 86400)
    removed = 0
    for f in incidents_dir.glob("*.md"):
        if f.stat().st_mtime < cutoff:
            f.unlink()
            removed += 1
    return removed
