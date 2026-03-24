"""GitHub upstream integration — search for existing issues/PRs, file new ones."""

import json
import logging
import subprocess

logger = logging.getLogger("watchdog.upstream")


def _gh(args: list[str], timeout: int = 30) -> tuple[str, int]:
    """Run a gh CLI command."""
    cmd = ["gh"] + args
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip(), r.returncode
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.warning("gh command failed: %s", e)
        return "", -1


def search_upstream(repo: str, issue: dict) -> str | None:
    """Search GitHub for existing issues/PRs matching an error.

    Returns the URL of the best match, or None.
    """
    error_type = issue.get("error_type", "")
    error_message = issue.get("error_message", "")
    file_path = issue.get("file", "")

    # Try specific search first: error type + file
    queries = []
    if file_path and error_type:
        queries.append(f"{error_type} {file_path}")
    if error_message:
        # Use first 60 chars of error message
        queries.append(error_message[:60])
    if error_type:
        queries.append(error_type)

    for query in queries:
        # Search issues
        out, rc = _gh([
            "search", "issues",
            "--repo", repo,
            query,
            "--limit", "5",
            "--json", "url,title,state",
        ])
        if rc == 0 and out:
            try:
                results = json.loads(out)
                if results:
                    # Prefer open issues/PRs
                    for r in results:
                        if r.get("state") == "OPEN":
                            logger.info("Found upstream match: %s", r["url"])
                            return r["url"]
                    # Fall back to any match
                    logger.info("Found upstream match (closed): %s", results[0]["url"])
                    return results[0]["url"]
            except json.JSONDecodeError:
                pass

        # Search PRs
        out, rc = _gh([
            "search", "prs",
            "--repo", repo,
            query,
            "--limit", "5",
            "--json", "url,title,state",
        ])
        if rc == 0 and out:
            try:
                results = json.loads(out)
                if results:
                    for r in results:
                        if r.get("state") == "OPEN":
                            logger.info("Found upstream PR: %s", r["url"])
                            return r["url"]
                    return results[0]["url"]
            except json.JSONDecodeError:
                pass

    return None


def file_issue(repo: str, issue: dict, hermes_version: str = "") -> str | None:
    """Create a GitHub issue for a recurring error. Returns the issue URL or None."""

    title = f"bug: {issue['error_type']} in {issue['file']}:{issue['line']} ({issue['function']})"
    if len(title) > 100:
        title = f"bug: {issue['error_type']} in {issue['file']}:{issue['function']}"

    body = f"""## Bug Report (auto-filed by argus-watchdog)

**Error:** `{issue['error_type']}: {issue['error_message']}`
**Location:** `{issue['file']}:{issue['line']}` in `{issue['function']}`
**Occurrences:** {issue['count']}
**First seen:** {issue['first_seen']}
**Last seen:** {issue['last_seen']}
**Hermes version:** {hermes_version or 'unknown'}

## Stack Trace

```
{issue.get('sample_traceback', 'no traceback captured')}
```

## Context

This issue was automatically detected by [argus-watchdog](https://github.com/anthropics/argus-watchdog), a self-healing companion for Hermes Agent Gateway. It has occurred {issue['count']} times and no existing upstream issue or PR was found matching this error pattern.

**Error signature:** `{issue['signature']}`

## Environment

- **Platform:** Gateway mode (Telegram)
- **Hermes version:** {hermes_version or 'unknown'}
- **Python:** 3.11

---
*Filed automatically after {issue['count']} occurrences. If this is a duplicate, please link the original issue.*
"""

    out, rc = _gh([
        "issue", "create",
        "--repo", repo,
        "--title", title,
        "--body", body,
    ])

    if rc == 0 and out:
        # gh issue create outputs the URL
        url = out.strip()
        logger.info("Filed upstream issue: %s", url)
        return url

    logger.error("Failed to file upstream issue (rc=%d): %s", rc, out)
    return None


def get_hermes_version(hermes_home: str) -> str:
    """Get current hermes-agent git commit hash."""
    import os
    agent_dir = os.path.join(os.path.expanduser(hermes_home), "hermes-agent")
    out, rc = _gh(["--git-dir", "unused"])  # dummy, we use git directly
    try:
        r = subprocess.run(
            ["git", "log", "--oneline", "-1"],
            capture_output=True, text=True, timeout=10,
            cwd=agent_dir,
        )
        return r.stdout.strip() if r.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def check_for_updates(hermes_home: str) -> tuple[bool, int, str]:
    """Check if hermes-agent has upstream updates available.

    Returns (has_updates, commits_behind, latest_commit_summary).
    """
    import os
    agent_dir = os.path.join(os.path.expanduser(hermes_home), "hermes-agent")

    try:
        # Fetch latest
        subprocess.run(
            ["git", "fetch", "origin", "--quiet"],
            capture_output=True, timeout=30,
            cwd=agent_dir,
        )

        # Count commits behind
        r = subprocess.run(
            ["git", "rev-list", "HEAD..origin/main", "--count"],
            capture_output=True, text=True, timeout=10,
            cwd=agent_dir,
        )
        behind = int(r.stdout.strip()) if r.returncode == 0 else 0

        # Get latest commit message
        r2 = subprocess.run(
            ["git", "log", "--oneline", "origin/main", "-1"],
            capture_output=True, text=True, timeout=10,
            cwd=agent_dir,
        )
        latest = r2.stdout.strip() if r2.returncode == 0 else ""

        return behind > 0, behind, latest
    except Exception as e:
        logger.warning("Failed to check for updates: %s", e)
        return False, 0, ""


def check_if_issue_fixed_upstream(hermes_home: str, issue: dict) -> bool:
    """Check if a known issue has been fixed in upstream commits we haven't pulled yet."""
    import os
    agent_dir = os.path.join(os.path.expanduser(hermes_home), "hermes-agent")

    # Search unpulled commits for references to the error
    search_terms = [
        issue.get("error_type", ""),
        issue.get("file", "").split("/")[-1] if issue.get("file") else "",
        issue.get("function", ""),
    ]

    for term in search_terms:
        if not term:
            continue
        try:
            r = subprocess.run(
                ["git", "log", "HEAD..origin/main", "--oneline", f"--grep={term}"],
                capture_output=True, text=True, timeout=10,
                cwd=agent_dir,
            )
            if r.returncode == 0 and r.stdout.strip():
                logger.info(
                    "Issue %s may be fixed upstream: %s",
                    issue["id"], r.stdout.strip().split("\n")[0],
                )
                return True
        except Exception:
            pass

    return False


def apply_update(hermes_home: str, service: str, systemd_user: bool = True) -> tuple[bool, str]:
    """Pull latest hermes-agent and restart the service.

    Returns (success, message).
    """
    import os
    agent_dir = os.path.join(os.path.expanduser(hermes_home), "hermes-agent")

    steps = []

    # 1. Stash local changes
    try:
        r = subprocess.run(
            ["git", "stash"],
            capture_output=True, text=True, timeout=15,
            cwd=agent_dir,
        )
        steps.append(f"git stash: {r.stdout.strip()}")
    except Exception as e:
        return False, f"git stash failed: {e}"

    # 2. Pull latest
    try:
        r = subprocess.run(
            ["git", "pull", "origin", "main"],
            capture_output=True, text=True, timeout=60,
            cwd=agent_dir,
        )
        if r.returncode != 0:
            # Try to restore
            subprocess.run(["git", "stash", "pop"], cwd=agent_dir, capture_output=True)
            return False, f"git pull failed: {r.stderr}"
        steps.append(f"git pull: ok")
    except Exception as e:
        subprocess.run(["git", "stash", "pop"], cwd=agent_dir, capture_output=True)
        return False, f"git pull failed: {e}"

    # 3. Install deps
    venv_pip = os.path.join(agent_dir, "venv", "bin", "pip3")
    reqs = os.path.join(agent_dir, "requirements.txt")
    if os.path.exists(venv_pip) and os.path.exists(reqs):
        try:
            r = subprocess.run(
                [venv_pip, "install", "-q", "-r", reqs],
                capture_output=True, text=True, timeout=120,
                cwd=agent_dir,
            )
            steps.append(f"pip install: {'ok' if r.returncode == 0 else 'warning'}")
        except Exception as e:
            steps.append(f"pip install: failed ({e})")

    # 4. Re-apply local patches (pop stash)
    try:
        r = subprocess.run(
            ["git", "stash", "pop"],
            capture_output=True, text=True, timeout=15,
            cwd=agent_dir,
        )
        if r.returncode == 0 and "No stash" not in r.stdout:
            steps.append("git stash pop: re-applied local patches")
    except Exception:
        pass

    # 5. Restart service
    restart_cmd = ["systemctl"]
    if systemd_user:
        restart_cmd.append("--user")
    restart_cmd += ["restart", service]

    try:
        r = subprocess.run(restart_cmd, capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return False, f"service restart failed: {r.stderr}. Steps: {'; '.join(steps)}"
        steps.append("service restart: ok")
    except Exception as e:
        return False, f"service restart failed: {e}"

    return True, "; ".join(steps)
