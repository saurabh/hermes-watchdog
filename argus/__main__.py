"""argus — self-healing watchdog for Hermes Agent Gateway.

Usage:
    python -m argus              # Run one probe-evaluate-remediate cycle
    python -m argus --status     # Show current health + known issues
    python -m argus --issues     # List tracked issues and upstream status
    python -m argus --update     # Check for and apply hermes updates
    python -m argus --probe-only # Run probes without remediation
"""

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

import yaml

from . import incidents, probe, upstream
from .notify import has_escalation
from .remediate import remediate


LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def load_config(config_path: str | None = None) -> dict:
    """Load config from YAML file."""
    paths = [
        config_path,
        os.environ.get("ARGUS_CONFIG"),
        os.path.expanduser("~/.hermes/watchdog/config.yaml"),
        os.path.expanduser("~/.config/argus/config.yaml"),
        "config.yaml",
    ]
    for p in paths:
        if p and os.path.exists(p):
            with open(p) as f:
                return yaml.safe_load(f) or {}
    # Sensible defaults
    return {
        "hermes": {
            "home": "~/.hermes",
            "service": "hermes-gateway",
            "systemd_user": True,
            "logs": {
                "gateway": "~/.hermes/logs/gateway.log",
                "errors": "~/.hermes/logs/errors.log",
            },
        },
        "probe": {
            "polling_stale_seconds": 600,
            "log_stale_seconds": 300,
        },
        "remediation": {
            "cooldown_seconds": 300,
            "max_attempts": 3,
            "chain": ["systemctl_restart", "process_kill_restart", "escalate"],
        },
        "upstream": {
            "repo": "NousResearch/hermes-agent",
            "auto_issue_after": 3,
            "auto_pr": False,
        },
        "incidents": {
            "data_dir": "~/.hermes/watchdog",
            "retention_days": 90,
        },
    }


def get_data_dir(config: dict) -> str:
    return os.path.expanduser(
        config.get("incidents", {}).get("data_dir", "~/.hermes/watchdog")
    )


def run_cycle(config: dict) -> dict:
    """Run one full probe → evaluate → track → remediate → upstream cycle."""
    logger = logging.getLogger("argus")
    data_dir = get_data_dir(config)
    os.makedirs(data_dir, exist_ok=True)

    upstream_cfg = config.get("upstream", {})
    repo = upstream_cfg.get("repo", "NousResearch/hermes-agent")
    auto_issue_threshold = upstream_cfg.get("auto_issue_after", 3)
    hermes_home = config.get("hermes", {}).get("home", "~/.hermes")

    # 1. Probe
    result = probe.run_probes(config)
    probe_dict = result.to_dict()
    logger.info(
        "Probe: level=%s service=%s polling=%s errors=%d tracebacks=%d",
        result.level,
        result.service_active,
        result.polling_active,
        len(result.new_errors),
        len(result.new_tracebacks),
    )

    # 2. Log probe result
    incidents.append_health_log(data_dir, probe_dict)

    # 3. Track new errors
    for tb in result.new_tracebacks:
        issue = incidents.track_error(data_dir, tb)
        logger.info(
            "Tracked: %s (%s:%d:%s) — count=%d",
            issue["error_type"], issue["file"], issue["line"],
            issue["function"], issue["count"],
        )

    # 4. Remediate if needed
    remediation_result = remediate(
        config, probe_dict, result.new_tracebacks, data_dir
    )

    if remediation_result.get("action_taken"):
        logger.info(
            "Remediation: action=%s success=%s attempt=%d msg=%s",
            remediation_result["action_taken"],
            remediation_result["success"],
            remediation_result["attempt"],
            remediation_result["message"],
        )

    if remediation_result.get("update_applied"):
        logger.info("Hermes updated as part of remediation")

    # 5. Upstream search for new issues
    needs_search = incidents.get_issues_needing_upstream_search(data_dir)
    for issue in needs_search:
        logger.info("Searching upstream for: %s", issue["signature"])
        url = upstream.search_upstream(repo, issue)
        incidents.mark_upstream_searched(data_dir, issue["id"], url)
        if url:
            logger.info("Found upstream match: %s → %s", issue["id"], url)
        time.sleep(1)  # Rate limit

    # 6. Auto-file issues that hit threshold
    if auto_issue_threshold > 0:
        needs_filing = incidents.get_issues_needing_filing(data_dir, auto_issue_threshold)
        for issue in needs_filing:
            hermes_version = upstream.get_hermes_version(hermes_home)
            logger.info(
                "Filing upstream issue for %s (count=%d, threshold=%d)",
                issue["signature"], issue["count"], auto_issue_threshold,
            )
            url = upstream.file_issue(repo, issue, hermes_version)
            if url:
                incidents.mark_upstream_filed(data_dir, issue["id"], url)
                logger.info("Filed: %s → %s", issue["id"], url)
            time.sleep(2)  # Rate limit

    # 7. Check if known issues are fixed upstream (periodic, not every cycle)
    with incidents.locked_state(data_dir) as state:
        last_update_check = state.get("last_update_check", 0)
        needs_update_check = time.time() - last_update_check > 3600

    if needs_update_check:
        has_updates, behind, latest = upstream.check_for_updates(hermes_home)
        if has_updates:
            logger.info("Hermes is %d commits behind upstream (latest: %s)", behind, latest)

            # Read issues snapshot (don't hold lock during subprocess calls)
            snapshot = incidents._load_state(data_dir)
            for issue_id, issue in snapshot.get("known_issues", {}).items():
                if issue.get("resolved"):
                    continue
                if upstream.check_if_issue_fixed_upstream(hermes_home, issue):
                    logger.info(
                        "Issue %s (%s) appears fixed upstream — will apply on next remediation",
                        issue_id, issue["signature"],
                    )

        with incidents.locked_state(data_dir) as state:
            state["last_update_check"] = time.time()
            state["hermes_version"] = upstream.get_hermes_version(hermes_home)

        # Prune old incident reports during hourly check
        retention = config.get("incidents", {}).get("retention_days", 90)
        pruned = incidents.prune_old_incidents(data_dir, retention)
        if pruned:
            logger.info("Pruned %d old incident reports", pruned)

    return {
        "probe": probe_dict,
        "remediation": remediation_result,
        "errors_tracked": len(result.new_tracebacks),
    }


def show_status(config: dict) -> None:
    """Print current health status."""
    data_dir = get_data_dir(config)
    result = probe.run_probes(config)

    print(f"{'=' * 60}")
    print(f"  ARGUS — Hermes Gateway Health Status")
    print(f"{'=' * 60}")
    print(f"  Level:        {result.level.upper()}")
    print(f"  Service:      {'active' if result.service_active else 'DOWN'}")
    print(f"  Process:      PID {result.process_pid or 'none'}")
    print(f"  Memory:       {result.memory_mb} MB")
    print(f"  Log age:      {result.log_age_seconds}s")
    print(f"  Polling age:  {result.polling_age_seconds}s")
    print(f"  New errors:   {len(result.new_errors)}")
    print(f"  Tracebacks:   {len(result.new_tracebacks)}")
    print()

    # Show remediation state (read-only, no lock needed)
    state = incidents._load_state(data_dir)
    attempts = state.get("current_remediation_attempts", 0)
    print(f"  Remediation attempts: {attempts}")
    print(f"  Known issues:         {len(state.get('known_issues', {}))}")
    print(f"  Escalation:           {'ACTIVE — operator intervention needed' if has_escalation(data_dir) else 'none'}")
    print(f"  Hermes version:       {state.get('hermes_version', 'unknown')}")
    print()

    # Check for updates
    hermes_home = config.get("hermes", {}).get("home", "~/.hermes")
    has_updates, behind, latest = upstream.check_for_updates(hermes_home)
    if has_updates:
        print(f"  Updates:       {behind} commits behind")
        print(f"  Latest:        {latest}")
    else:
        print(f"  Updates:       up to date")
    print(f"{'=' * 60}")


def show_issues(config: dict) -> None:
    """Print tracked issues."""
    data_dir = get_data_dir(config)
    state = incidents._load_state(data_dir)  # read-only, no lock needed
    issues = state.get("known_issues", {})

    if not issues:
        print("No tracked issues.")
        return

    print(f"{'=' * 80}")
    print(f"  ARGUS — Tracked Issues ({len(issues)} total)")
    print(f"{'=' * 80}")

    for issue_id, issue in sorted(
        issues.items(), key=lambda x: x[1].get("count", 0), reverse=True
    ):
        status = "RESOLVED" if issue.get("resolved") else "ACTIVE"
        upstream_status = ""
        if issue.get("upstream_filed"):
            upstream_status = f" [FILED: {issue['upstream_filed']}]"
        elif issue.get("upstream_issue"):
            upstream_status = f" [UPSTREAM: {issue['upstream_issue']}]"
        elif issue.get("upstream_searched"):
            upstream_status = " [no upstream match]"

        print(f"\n  [{status}] {issue['error_type']}: {issue['error_message']}")
        print(f"    Location:   {issue['file']}:{issue['line']} ({issue['function']})")
        print(f"    Count:      {issue['count']}")
        print(f"    First seen: {issue['first_seen']}")
        print(f"    Last seen:  {issue['last_seen']}")
        print(f"    Signature:  {issue['signature']}")
        if upstream_status:
            print(f"    Upstream:  {upstream_status}")

    print(f"\n{'=' * 80}")


def do_update(config: dict) -> None:
    """Check for and apply hermes updates."""
    hermes_home = config.get("hermes", {}).get("home", "~/.hermes")
    hermes = config.get("hermes", {})
    service = hermes.get("service", "hermes-gateway")
    user_svc = hermes.get("systemd_user", True)

    has_updates, behind, latest = upstream.check_for_updates(hermes_home)
    if not has_updates:
        print("Hermes is up to date.")
        return

    print(f"Hermes is {behind} commits behind upstream.")
    print(f"Latest: {latest}")
    print()

    # Check if any known issues are fixed
    data_dir = get_data_dir(config)
    state = incidents._load_state(data_dir)  # read-only snapshot
    fixes_found = []
    for issue_id, issue in state.get("known_issues", {}).items():
        if issue.get("resolved"):
            continue
        if upstream.check_if_issue_fixed_upstream(hermes_home, issue):
            fixes_found.append(issue)

    if fixes_found:
        print(f"Found upstream fixes for {len(fixes_found)} known issue(s):")
        for issue in fixes_found:
            print(f"  - {issue['error_type']}: {issue['error_message']}")
        print()

    print("Applying update...")
    success, msg = upstream.apply_update(hermes_home, service, user_svc)
    if success:
        print(f"Update applied: {msg}")
        # Mark fixed issues as resolved
        for issue in fixes_found:
            incidents.mark_resolved(data_dir, issue["id"])
            print(f"  Resolved: {issue['signature']}")
    else:
        print(f"Update failed: {msg}")


def main():
    parser = argparse.ArgumentParser(
        description="argus — self-healing watchdog for Hermes Agent Gateway"
    )
    parser.add_argument(
        "--config", "-c", help="Path to config.yaml"
    )
    parser.add_argument(
        "--status", "-s", action="store_true", help="Show current health status"
    )
    parser.add_argument(
        "--issues", "-i", action="store_true", help="List tracked issues"
    )
    parser.add_argument(
        "--update", "-u", action="store_true", help="Check for and apply hermes updates"
    )
    parser.add_argument(
        "--probe-only", action="store_true", help="Run probes without remediation"
    )
    parser.add_argument(
        "--json", action="store_true", help="Output in JSON format"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Verbose logging"
    )

    args = parser.parse_args()

    # Setup logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(format=LOG_FORMAT, level=level)
    logger = logging.getLogger("argus")

    config = load_config(args.config)

    # Log to file in configured data_dir
    data_dir = get_data_dir(config)
    os.makedirs(data_dir, exist_ok=True)
    fh = logging.FileHandler(os.path.join(data_dir, "argus.log"))
    fh.setFormatter(logging.Formatter(LOG_FORMAT))
    fh.setLevel(logging.INFO)
    logging.getLogger().addHandler(fh)

    if args.status:
        show_status(config)
        return

    if args.issues:
        show_issues(config)
        return

    if args.update:
        do_update(config)
        return

    if args.probe_only:
        result = probe.run_probes(config)
        if args.json:
            print(json.dumps(result.to_dict(), indent=2))
        else:
            print(f"Level: {result.level}")
            print(f"Service: {'active' if result.service_active else 'DOWN'}")
            print(f"Polling: {result.polling_age_seconds}s ago")
            print(f"Errors: {len(result.new_errors)}")
        return

    # Full cycle
    cycle_result = run_cycle(config)

    if args.json:
        print(json.dumps(cycle_result, indent=2))
    else:
        p = cycle_result["probe"]
        r = cycle_result["remediation"]
        logger.info(
            "Cycle complete: level=%s action=%s success=%s errors_tracked=%d",
            p["level"],
            r.get("action_taken", "none"),
            r.get("success", "-"),
            cycle_result["errors_tracked"],
        )


if __name__ == "__main__":
    main()
