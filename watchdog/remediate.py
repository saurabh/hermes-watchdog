"""Remediation chain — escalating restart logic with cooldowns."""

import logging
import subprocess
import time

from . import incidents

logger = logging.getLogger("watchdog.remediate")


def _run(cmd: list[str], timeout: int = 30) -> tuple[str, int]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip(), r.returncode
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return str(e), -1


def systemctl_restart(service: str, user: bool = True) -> tuple[bool, str]:
    """Restart hermes via systemd."""
    cmd = ["systemctl"]
    if user:
        cmd.append("--user")
    cmd += ["restart", service]
    out, rc = _run(cmd)
    if rc == 0:
        # Wait for it to come up
        time.sleep(3)
        check_cmd = ["systemctl"]
        if user:
            check_cmd.append("--user")
        check_cmd += ["is-active", "--quiet", service]
        _, check_rc = _run(check_cmd)
        if check_rc == 0:
            return True, "Service restarted successfully"
        return False, "Service restarted but not active"
    return False, f"systemctl restart failed: {out}"


def process_kill_restart(service: str, user: bool = True) -> tuple[bool, str]:
    """Kill the hermes process and let systemd restart it."""
    # Kill the process
    out, rc = _run(["pkill", "-f", "hermes_cli.main gateway"])
    time.sleep(5)

    # Check if systemd restarted it
    check_cmd = ["systemctl"]
    if user:
        check_cmd.append("--user")
    check_cmd += ["is-active", "--quiet", service]
    _, check_rc = _run(check_cmd)

    if check_rc == 0:
        return True, "Process killed, systemd restarted it"

    # Try explicit start
    start_cmd = ["systemctl"]
    if user:
        start_cmd.append("--user")
    start_cmd += ["start", service]
    _run(start_cmd)
    time.sleep(3)

    _, check_rc2 = _run(check_cmd)
    if check_rc2 == 0:
        return True, "Process killed, manually restarted"
    return False, "Process killed but service failed to restart"


def remediate(
    config: dict,
    probe_result: dict,
    tracebacks: list[dict],
    data_dir: str,
) -> dict:
    """Run the remediation chain based on health level.

    Returns {
        "action_taken": str | None,
        "success": bool,
        "message": str,
        "attempt": int,
        "escalated": bool,
        "incident_file": str | None,
        "update_applied": bool,
    }
    """
    hermes = config.get("hermes", {})
    remediation = config.get("remediation", {})
    service = hermes.get("service", "hermes-gateway")
    user_svc = hermes.get("systemd_user", True)
    cooldown_secs = remediation.get("cooldown_seconds", 300)
    max_attempts = remediation.get("max_attempts", 3)
    hermes_home = hermes.get("home", "~/.hermes")

    level = probe_result.get("level", "healthy")

    result = {
        "action_taken": None,
        "success": False,
        "message": "no action needed",
        "attempt": 0,
        "escalated": False,
        "incident_file": None,
        "update_applied": False,
    }

    if level in ("healthy", "warning"):
        # Reset attempt counter when healthy
        if level == "healthy":
            incidents.reset_remediation_attempts(data_dir)
        return result

    # Check attempt count
    attempt = incidents.get_remediation_attempts(data_dir)
    if attempt >= max_attempts:
        result["escalated"] = True
        result["message"] = (
            f"Max remediation attempts ({max_attempts}) reached. "
            "Escalating to operator."
        )
        result["attempt"] = attempt
        logger.error(result["message"])
        return result

    # Determine action from chain
    chain = remediation.get("chain", [
        "systemctl_restart",
        "process_kill_restart",
        "escalate",
    ])
    action_idx = min(attempt, len(chain) - 1)
    action = chain[action_idx]

    if action == "escalate":
        result["escalated"] = True
        result["message"] = "Escalating to operator (chain exhausted)"
        result["attempt"] = attempt
        return result

    # Check cooldown
    remaining = incidents.get_cooldown(data_dir, action)
    if remaining > 0:
        result["message"] = f"Action '{action}' on cooldown ({remaining:.0f}s remaining)"
        logger.info(result["message"])
        return result

    # Write incident report BEFORE remediation
    incident_file = incidents.write_incident_report(
        data_dir, probe_result, tracebacks,
        remediation_action=action,
    )
    result["incident_file"] = incident_file

    # Check if upstream has a fix we should pull first
    from . import upstream
    has_updates, behind, latest = upstream.check_for_updates(hermes_home)

    if has_updates and tracebacks:
        # Check if any of our known issues are fixed upstream
        for tb in tracebacks:
            if upstream.check_if_issue_fixed_upstream(hermes_home, tb):
                logger.info(
                    "Upstream fix found for %s — applying update before restart",
                    tb["signature"],
                )
                success, msg = upstream.apply_update(
                    hermes_home, service, user_svc
                )
                if success:
                    result["update_applied"] = True
                    result["action_taken"] = f"update_and_restart (was {behind} commits behind)"
                    result["success"] = True
                    result["message"] = f"Applied update ({behind} commits) and restarted: {msg}"
                    incidents.set_cooldown(data_dir, action, cooldown_secs)
                    incidents.increment_remediation_attempts(data_dir)
                    result["attempt"] = attempt + 1
                    logger.info(result["message"])
                    return result

    # Execute remediation action
    logger.info("Executing remediation: %s (attempt %d/%d)", action, attempt + 1, max_attempts)

    if action == "systemctl_restart":
        success, msg = systemctl_restart(service, user_svc)
    elif action == "process_kill_restart":
        success, msg = process_kill_restart(service, user_svc)
    else:
        success, msg = False, f"Unknown action: {action}"

    result["action_taken"] = action
    result["success"] = success
    result["message"] = msg
    result["attempt"] = attempt + 1

    # Set cooldown and increment attempts
    incidents.set_cooldown(data_dir, action, cooldown_secs)
    incidents.increment_remediation_attempts(data_dir)

    if success:
        logger.info("Remediation succeeded: %s", msg)
    else:
        logger.error("Remediation failed: %s", msg)

    return result
