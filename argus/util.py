"""Shared utilities for argus watchdog."""

import subprocess


def run_cmd(cmd: list[str], timeout: int = 30, cwd: str | None = None) -> tuple[str, int]:
    """Run a command, return (stdout, exit_code).

    On timeout or missing binary, returns (error_message, -1).
    """
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        return r.stdout.strip(), r.returncode
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return str(e), -1
