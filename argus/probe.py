"""Health probes for Hermes gateway."""

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .util import run_cmd


@dataclass
class ProbeResult:
    timestamp: str
    service_active: bool = False
    process_alive: bool = False
    process_pid: int | None = None
    log_fresh: bool = False
    log_age_seconds: int = -1
    polling_active: bool = False
    polling_age_seconds: int = -1
    last_poll_timestamp: str = ""
    new_errors: list[str] = field(default_factory=list)
    new_tracebacks: list[dict] = field(default_factory=list)
    memory_mb: float = 0.0
    level: str = "unknown"  # healthy, warning, degraded, critical

    def to_dict(self) -> dict:
        return {
            "ts": self.timestamp,
            "service_active": self.service_active,
            "process_alive": self.process_alive,
            "pid": self.process_pid,
            "log_fresh": self.log_fresh,
            "log_age_s": self.log_age_seconds,
            "polling_active": self.polling_active,
            "polling_age_s": self.polling_age_seconds,
            "last_poll_ts": self.last_poll_timestamp,
            "error_count": len(self.new_errors),
            "traceback_count": len(self.new_tracebacks),
            "memory_mb": self.memory_mb,
            "level": self.level,
        }


def check_service(service: str, user: bool = True) -> tuple[bool, str]:
    """Check if systemd service is active."""
    cmd = ["systemctl"]
    if user:
        cmd.append("--user")
    cmd += ["is-active", "--quiet", service]
    _, rc = run_cmd(cmd, timeout=10)
    active = rc == 0

    # Get memory usage if active
    if active:
        cmd2 = ["systemctl"]
        if user:
            cmd2.append("--user")
        cmd2 += ["show", service, "--property=MemoryCurrent"]
        out, _ = run_cmd(cmd2, timeout=10)
        return active, out.replace("MemoryCurrent=", "").strip()
    return active, "0"


def check_process() -> tuple[bool, int | None]:
    """Check if hermes gateway process is running."""
    out, rc = run_cmd(["pgrep", "-f", "hermes_cli.main gateway"], timeout=10)
    if rc == 0 and out:
        # Return first PID
        try:
            return True, int(out.split("\n")[0])
        except ValueError:
            return True, None
    return False, None


def check_log_freshness(log_path: str) -> tuple[bool, int]:
    """Check if log file has been written to recently."""
    p = Path(os.path.expanduser(log_path))
    if not p.exists():
        return False, -1
    age = int(time.time() - p.stat().st_mtime)
    return True, age


def check_polling(log_path: str) -> tuple[bool, int, str]:
    """Check for recent Telegram getUpdates in log. Returns (active, age_seconds, last_ts)."""
    p = Path(os.path.expanduser(log_path))
    if not p.exists():
        return False, -1, ""

    # Read last 500 lines efficiently
    try:
        out, _ = run_cmd(["tail", "-500", str(p)], timeout=10)
    except Exception:
        return False, -1, ""

    # Find last getUpdates line
    last_poll_ts = ""
    for line in reversed(out.split("\n")):
        if "getUpdates" in line:
            match = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
            if match:
                last_poll_ts = match.group(1)
            break

    if not last_poll_ts:
        return False, -1, ""

    # Parse timestamp
    try:
        from datetime import datetime
        poll_time = datetime.strptime(last_poll_ts, "%Y-%m-%d %H:%M:%S")
        age = int((datetime.now() - poll_time).total_seconds())
        return age < 300, age, last_poll_ts
    except Exception:
        return False, -1, last_poll_ts


def extract_new_errors(log_path: str, last_position_file: str) -> tuple[list[str], list[dict]]:
    """Extract new ERROR lines and tracebacks since last check.

    Returns (error_lines, tracebacks) where each traceback is:
    {
        "timestamp": str,
        "error_type": str,
        "error_message": str,
        "traceback": str,
        "signature": str,  # unique identifier for deduplication
        "file": str,       # source file where error occurred
        "line": int,        # line number
        "function": str,   # function name
    }
    """
    p = Path(os.path.expanduser(log_path))
    pos_p = Path(os.path.expanduser(last_position_file))

    if not p.exists():
        return [], []

    # Read last position
    last_pos = 0
    if pos_p.exists():
        try:
            last_pos = int(pos_p.read_text().strip())
        except (ValueError, OSError):
            last_pos = 0

    # Read new content (bounded to 5MB to prevent OOM on huge logs)
    MAX_READ_BYTES = 5 * 1024 * 1024
    try:
        file_size = p.stat().st_size
        if file_size < last_pos:
            # Log was rotated
            last_pos = 0

        bytes_to_read = file_size - last_pos
        if bytes_to_read > MAX_READ_BYTES:
            # Skip ahead — only read the last MAX_READ_BYTES
            last_pos = file_size - MAX_READ_BYTES

        with open(p, "rb") as f:
            f.seek(last_pos)
            raw = f.read(MAX_READ_BYTES)
            new_pos = f.tell()
        new_content = raw.decode("utf-8", errors="replace")

        # Save position
        pos_p.parent.mkdir(parents=True, exist_ok=True)
        pos_p.write_text(str(new_pos))
    except (OSError, IOError):
        return [], []

    if not new_content:
        return [], []

    # Extract ERROR lines
    error_lines = []
    for line in new_content.split("\n"):
        if " ERROR " in line:
            error_lines.append(line.strip())

    # Extract tracebacks
    tracebacks = []
    tb_pattern = re.compile(
        r"Traceback \(most recent call last\):\n((?:  .*\n)*)"
        r"(\w+(?:\.\w+)*): (.+)",
        re.MULTILINE,
    )

    for match in tb_pattern.finditer(new_content):
        tb_text = match.group(0)
        error_type = match.group(2)
        error_message = match.group(3).strip()

        # Extract last file/line/function from traceback
        file_match = re.findall(
            r'File "([^"]+)", line (\d+), in (\w+)', match.group(1)
        )
        src_file, src_line, src_func = "", 0, ""
        if file_match:
            src_file, src_line, src_func = file_match[-1]
            src_line = int(src_line)

        # Make path relative for signatures
        rel_file = src_file
        if "hermes-agent/" in rel_file:
            rel_file = rel_file.split("hermes-agent/", 1)[1]

        # Create signature for deduplication
        signature = f"{error_type}:{rel_file}:{src_line}:{src_func}"

        # Find timestamp from preceding ERROR line
        ts = ""
        tb_start = match.start()
        preceding = new_content[:tb_start].rsplit("\n", 2)
        for pline in reversed(preceding):
            ts_match = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", pline)
            if ts_match:
                ts = ts_match.group(1)
                break

        tracebacks.append({
            "timestamp": ts,
            "error_type": error_type,
            "error_message": error_message,
            "traceback": tb_text,
            "signature": signature,
            "file": rel_file,
            "line": src_line,
            "function": src_func,
        })

    return error_lines, tracebacks


def run_probes(config: dict) -> ProbeResult:
    """Run all health probes and return results."""
    from datetime import datetime, timezone

    result = ProbeResult(
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )

    hermes = config.get("hermes", {})
    probe_cfg = config.get("probe", {})
    data_dir = os.path.expanduser(
        config.get("incidents", {}).get("data_dir", "~/.hermes/watchdog")
    )

    service = hermes.get("service", "hermes-gateway")
    user_svc = hermes.get("systemd_user", True)
    gateway_log = hermes.get("logs", {}).get("gateway", "~/.hermes/logs/gateway.log")
    errors_log = hermes.get("logs", {}).get("errors", "~/.hermes/logs/errors.log")
    stale_poll = probe_cfg.get("polling_stale_seconds", 600)
    stale_log = probe_cfg.get("log_stale_seconds", 300)

    # Service check
    result.service_active, mem_str = check_service(service, user_svc)
    try:
        mem_bytes = int(mem_str)
        result.memory_mb = round(mem_bytes / 1024 / 1024, 1)
    except (ValueError, TypeError):
        pass

    # Process check
    result.process_alive, result.process_pid = check_process()

    # Log freshness
    _, result.log_age_seconds = check_log_freshness(gateway_log)
    result.log_fresh = 0 <= result.log_age_seconds < stale_log

    # Polling check
    result.polling_active, result.polling_age_seconds, result.last_poll_timestamp = (
        check_polling(gateway_log)
    )

    # Error extraction
    pos_file = os.path.join(data_dir, ".error_log_position")
    result.new_errors, result.new_tracebacks = extract_new_errors(errors_log, pos_file)

    # Also check gateway log for errors
    gw_pos_file = os.path.join(data_dir, ".gateway_log_position")
    gw_errors, gw_tbs = extract_new_errors(gateway_log, gw_pos_file)
    result.new_errors.extend(gw_errors)
    result.new_tracebacks.extend(gw_tbs)

    # Deduplicate tracebacks by signature
    seen = set()
    deduped = []
    for tb in result.new_tracebacks:
        if tb["signature"] not in seen:
            seen.add(tb["signature"])
            deduped.append(tb)
    result.new_tracebacks = deduped

    # Evaluate health level
    if not result.service_active or not result.process_alive:
        result.level = "critical"
    elif not result.polling_active and result.polling_age_seconds > stale_poll:
        if result.log_fresh:
            # Polling is stale but logs are fresh — gateway is busy working,
            # not dead. Downgrade to degraded instead of restarting it.
            result.level = "degraded"
        else:
            result.level = "critical"
    elif not result.log_fresh:
        result.level = "degraded"
    elif len(result.new_tracebacks) > 0:
        result.level = "warning"
    else:
        result.level = "healthy"

    return result
