"""GitHub upstream integration — search for existing issues/PRs, file new ones."""

import json
import logging
import subprocess

logger = logging.getLogger("argus.upstream")


def _get_python_version() -> str:
    import sys
    return f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"


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

    Uses tight queries (error_type + file) first, then progressively broader.
    Validates results by checking that titles/URLs contain relevant keywords
    to avoid false positives from unrelated matches.

    Returns the URL of the best match, or None.
    """
    error_type = issue.get("error_type", "")
    error_message = issue.get("error_message", "")
    file_path = issue.get("file", "")
    function_name = issue.get("function", "")

    # Relevance keywords — results must contain at least one to count
    relevance_terms = set()
    if error_type:
        # e.g. "ValueError" → "valueerror"
        relevance_terms.add(error_type.lower().split(".")[-1])
    if file_path:
        # e.g. "agent/display.py" → "display"
        basename = file_path.split("/")[-1].replace(".py", "")
        if basename and len(basename) > 2:
            relevance_terms.add(basename.lower())
    if function_name and len(function_name) > 3:
        relevance_terms.add(function_name.lower())

    # Build queries from most specific to least
    queries = []
    if file_path and error_type:
        queries.append(f"{error_type} {file_path}")
    if error_type and function_name:
        queries.append(f"{error_type} {function_name}")
    if error_message:
        # Use first 60 chars, but strip noisy parts (paths, hashes)
        clean_msg = error_message.split("\n")[0][:60]
        queries.append(clean_msg)

    def _is_relevant(title: str) -> bool:
        """Check if a search result title is actually relevant to our error."""
        title_lower = title.lower()
        return any(term in title_lower for term in relevance_terms)

    for query in queries:
        for search_type in ("issues", "prs"):
            cmd = [
                "search", search_type,
                "--repo", repo,
                query,
                "--limit", "5",
                "--json", "url,title,state",
            ]
            out, rc = _gh(cmd)
            if rc != 0 or not out:
                continue
            try:
                results = json.loads(out)
            except json.JSONDecodeError:
                continue

            # Filter to relevant results only
            relevant = [r for r in results if _is_relevant(r.get("title", ""))]
            if not relevant:
                continue

            # Prefer open issues/PRs
            for r in relevant:
                if r.get("state") == "OPEN":
                    logger.info("Found upstream match: %s", r["url"])
                    return r["url"]
            # Fall back to closed
            logger.info("Found upstream match (closed): %s", relevant[0]["url"])
            return relevant[0]["url"]

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

This issue was automatically detected by [argus-watchdog](https://github.com/saurabh/hermes-watchdog), a self-healing companion for Hermes Agent Gateway. It has occurred {issue['count']} times and no existing upstream issue or PR was found matching this error pattern.

**Error signature:** `{issue['signature']}`

## Environment

- **Platform:** Gateway mode
- **Hermes version:** {hermes_version or 'unknown'}
- **Python:** {_get_python_version()}

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
    """Check if a known issue has been fixed in upstream commits we haven't pulled yet.

    Requires at least 2 search terms to match in the same commit (via --all-match)
    to reduce false positives from generic terms like 'ValueError'.
    """
    import os
    agent_dir = os.path.join(os.path.expanduser(hermes_home), "hermes-agent")

    search_terms = [
        issue.get("error_type", ""),
        issue.get("file", "").split("/")[-1] if issue.get("file") else "",
        issue.get("function", ""),
    ]
    terms = [t for t in search_terms if t and len(t) > 2]

    if len(terms) < 2:
        return False

    # Require all terms to appear in the same commit message
    try:
        cmd = ["git", "log", "HEAD..origin/main", "--oneline", "--all-match"]
        for term in terms:
            cmd.append(f"--grep={term}")
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10, cwd=agent_dir,
        )
        if r.returncode == 0 and r.stdout.strip():
            logger.info(
                "Issue %s may be fixed upstream: %s",
                issue.get("id", issue.get("signature", "?")), r.stdout.strip().split("\n")[0],
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
    did_stash = False

    # 1. Stash local changes
    try:
        r = subprocess.run(
            ["git", "stash"],
            capture_output=True, text=True, timeout=15,
            cwd=agent_dir,
        )
        stash_out = r.stdout.strip()
        did_stash = r.returncode == 0 and "No local changes" not in stash_out
        steps.append(f"git stash: {stash_out}")
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
            if did_stash:
                subprocess.run(["git", "stash", "pop"], cwd=agent_dir, capture_output=True)
            return False, f"git pull failed: {r.stderr}"
        steps.append(f"git pull: ok")
    except Exception as e:
        if did_stash:
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

    # 4. Re-apply local patches (pop stash) — only if we actually stashed
    if did_stash:
        try:
            r = subprocess.run(
                ["git", "stash", "pop"],
                capture_output=True, text=True, timeout=15,
                cwd=agent_dir,
            )
            if r.returncode == 0:
                steps.append("git stash pop: re-applied local patches")
            else:
                steps.append(f"git stash pop: conflict ({r.stderr.strip()})")
        except Exception as e:
            steps.append(f"git stash pop: failed ({e})")

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
