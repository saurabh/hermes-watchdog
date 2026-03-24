# Feature: Multi-Platform Health Detection & Smart Notifications (v0.2)

## Feature Description
Argus v0.2 addresses four interconnected gaps:

1. **Telegram-only polling detection** — `check_polling()` hardcodes `"getUpdates"`, breaking on all 13 other Hermes platforms.
2. **Polling zombie loophole** — stale polling + fresh logs = `degraded` forever, never restarted.
3. **No degraded duration tracking** — each probe cycle is independent, no persistent "how long has this been degraded?"
4. **Notification architecture** — v0.1 requires separate bot tokens in Argus config. v0.2 piggybacks on Hermes's own platform credentials, with a two-tier model: recovery events flow through Hermes (natural conversation), escalations go direct (Hermes is dead).

The goal is upstream-mergeable code for NousResearch/hermes-agent — clean, minimal dependencies, works for all 14 platforms.

## User Story
As a Hermes operator running any platform (not just Telegram), I want Argus to correctly detect whether my gateway is actively polling, so that it can distinguish "busy doing work" from "polling silently died" and take appropriate action.

## Problem Statement

### Problem 1: Telegram-Only Polling Detection
`check_polling()` in `probe.py` (line 102) searches for the literal string `"getUpdates"` — Telegram's long-poll API endpoint. For Discord (WebSocket heartbeats), Slack (RTM/Socket Mode), WhatsApp (libwebp events), Matrix (sync API), and all other platforms, this check always returns `polling_active=False, polling_age=-1`, making Argus think polling is permanently stale.

### Problem 2: Polling Zombie Loophole
The v0.1 health evaluation logic (probe.py lines 298-313):
```python
elif not result.polling_active and result.polling_age_seconds > stale_poll:
    if result.log_fresh:
        result.level = "degraded"   # <-- zombie sits here forever
    else:
        result.level = "critical"
```
If the gateway process is alive, writing logs (e.g., error retries, keepalive messages), but polling has silently died (e.g., WebSocket disconnected, auth token expired), Argus marks it `degraded` and **never restarts it**. The gateway appears "busy" but is actually a zombie that will never recover on its own.

### Problem 3: No Degraded Duration Tracking
Argus has no concept of "how long has this been degraded?" — each 2-minute probe cycle evaluates health independently. There's no persistent tracking of when the gateway first entered degraded state, so there's no way to implement a timeout.

### Problem 4: Notification Architecture Mismatch
Argus v0.1 has its own `notify` config with separate `telegram_bot_token` and `discord_webhook` fields. This means:
- Users must configure credentials twice (once for Hermes, once for Argus)
- Argus doesn't know which platform the user is most active on
- Recovery messages come from "some bot" rather than Hermes itself
- For multi-user setups, Argus has no concept of who to notify

Meanwhile, Hermes already has all platform credentials, knows all users, and tracks recent activity via `channel_directory.json`.

## Solution Statement

### 1. Configurable Heartbeat Pattern
Replace the hardcoded `"getUpdates"` search with a configurable `heartbeat_pattern` in `config.yaml`. Each platform has its own characteristic log signature:
- Telegram: `getUpdates`
- Discord: `HEARTBEAT` or `heartbeat_ack`
- Slack: `apps.connections.open` or `socket_mode`
- WhatsApp: `recv` or `message_received`
- Matrix: `/sync`
- Generic fallback: a configurable regex pattern

The config defaults to `getUpdates` for backwards compatibility.

### 2. Max Degraded Duration
Track when the gateway first entered `degraded` state in `state.json`. If it stays degraded for longer than `max_degraded_seconds` (default: 1800 = 30 minutes), escalate to `critical` and trigger remediation. This catches polling zombies while still allowing legitimate long-running tool executions (which typically resolve within 10-15 minutes).

### 3. Degraded State Tracking
Add `degraded_since` timestamp to `state.json`. Set it on first `degraded` evaluation, clear it when health returns to `healthy`/`warning`. Read it (without lock) during health evaluation to decide whether to escalate.

### 4. Two-Tier Smart Notifications
Replace Argus's independent notification system with a Hermes-integrated approach:

**Tier 1 — Recovery events (Hermes is alive):**
Argus writes structured events to `~/.hermes/watchdog/events.jsonl`. When Hermes comes back up, the Argus skill picks up unread events and tells the user naturally: "I was down for 4 minutes — Argus restarted me. Here's what happened."

**Tier 2 — Escalation (Hermes is dead, can't relay):**
Argus reads Hermes's own config (`~/.hermes/config.yaml`, `~/.hermes/.env`) for platform credentials and `~/.hermes/channel_directory.json` for user activity. It picks the most-recently-active DM user on the most-recently-active platform and sends directly: "Hermes is down. Argus tried 3 restarts but couldn't recover. Need operator help."

This means zero separate notification credentials in Argus config.

## Relevant Files

- `argus/probe.py` — Health probes. Contains `check_polling()` (Telegram-specific), `run_probes()` (health evaluation logic), and `ProbeResult` dataclass. **Primary change target.**
- `argus/__main__.py` — CLI entry point. Contains `load_config()` with hardcoded defaults. Needs new config keys.
- `argus/incidents.py` — State management with file-locked `state.json`. Needs new helpers for degraded tracking.
- `argus/remediate.py` — Remediation chain. Currently skips `degraded` (only acts on `critical`). Needs awareness of zombie-escalated-critical.
- `argus/notify.py` — Current notification system. **Major rewrite** — replace standalone notification with Hermes-integrated two-tier approach.
- `config.example.yaml` — Example config. Needs new `probe.heartbeat_pattern`, `probe.max_degraded_seconds`, simplified `notify` section.
- `skill/SKILL.md` — Hermes skill file. Needs updated health level docs and event pickup instructions.

### New Files
None (events.jsonl is created at runtime in `~/.hermes/watchdog/`).

## Implementation Plan

### Phase 1: Foundation — Configurable Heartbeat
Replace the hardcoded Telegram polling check with a pattern-driven approach.

### Phase 2: Core — Degraded Duration Tracking & Zombie Escalation
Add persistent degraded-since tracking and the max-degraded-duration escalation path.

### Phase 3: Integration — Config, Defaults, Docs
Wire everything into config, update defaults, update skill docs.

### Phase 4: Smart Notifications
Replace standalone notify.py with two-tier Hermes-integrated notifications.

## Step by Step Tasks

IMPORTANT: Execute every step in order, top to bottom.

### Step 1: Add heartbeat_pattern to config schema

In `config.example.yaml`, add under `probe:`:
```yaml
probe:
  polling_stale_seconds: 600
  log_stale_seconds: 300
  # Regex pattern to detect active polling in gateway log.
  # Set this to match your platform's polling signature.
  # Examples:
  #   Telegram:  getUpdates           (default)
  #   Discord:   HEARTBEAT|heartbeat_ack
  #   Slack:     apps\.connections\.open|socket_mode
  #   WhatsApp:  recv|message_received
  #   Matrix:    /sync
  #   Generic:   poll|heartbeat|keepalive
  heartbeat_pattern: "getUpdates"
  # Max seconds to stay in 'degraded' before escalating to 'critical'.
  # Catches polling zombies (process alive, logs fresh, but polling dead).
  # Set to 0 to disable (degraded stays forever — NOT recommended).
  # Default: 1800 (30 minutes)
  max_degraded_seconds: 1800
```

In `argus/__main__.py` `load_config()`, add the same defaults to the fallback config dict:
```python
"probe": {
    "polling_stale_seconds": 600,
    "log_stale_seconds": 300,
    "heartbeat_pattern": "getUpdates",
    "max_degraded_seconds": 1800,
},
```

### Step 2: Refactor check_polling() to use configurable pattern

In `argus/probe.py`, change `check_polling()` signature:
```python
def check_polling(log_path: str, pattern: str = "getUpdates") -> tuple[bool, int, str]:
```

Replace the hardcoded `if "getUpdates" in line:` check (line 102) with:
```python
import re
# Compile once outside the loop
heartbeat_re = re.compile(pattern)

for line in reversed(out.split("\n")):
    if heartbeat_re.search(line):
        # ... existing timestamp extraction ...
```

This is a minimal change — same function signature shape, just parameterized.

### Step 3: Thread heartbeat_pattern through run_probes()

In `run_probes()`, read the new config key and pass it to `check_polling()`:
```python
heartbeat_pattern = probe_cfg.get("heartbeat_pattern", "getUpdates")

# Polling check
result.polling_active, result.polling_age_seconds, result.last_poll_timestamp = (
    check_polling(gateway_log, pattern=heartbeat_pattern)
)
```

### Step 4: Add degraded tracking helpers to incidents.py

Add two new functions to `argus/incidents.py`:

```python
def set_degraded_since(data_dir: str) -> None:
    """Record when gateway first entered degraded state (if not already set)."""
    with locked_state(data_dir) as state:
        if not state.get("degraded_since"):
            state["degraded_since"] = time.time()


def clear_degraded_since(data_dir: str) -> None:
    """Clear degraded timestamp when health recovers."""
    with locked_state(data_dir) as state:
        state.pop("degraded_since", None)


def get_degraded_duration(data_dir: str) -> float:
    """Return how long the gateway has been in degraded state (seconds). 0 if not degraded."""
    state = _load_state(data_dir)  # read-only, no lock
    since = state.get("degraded_since")
    if since:
        return time.time() - since
    return 0.0
```

### Step 5: Implement zombie escalation in probe health evaluation

In `run_probes()` in `probe.py`, the function needs access to `data_dir` and `max_degraded_seconds`. Change its signature:
```python
def run_probes(config: dict, data_dir: str | None = None) -> ProbeResult:
```

If `data_dir` is None, compute it from config (for backwards compat with `--probe-only`):
```python
if data_dir is None:
    data_dir = os.path.expanduser(
        config.get("incidents", {}).get("data_dir", "~/.hermes/watchdog")
    )
```

Read `max_degraded_seconds` from config:
```python
max_degraded = probe_cfg.get("max_degraded_seconds", 1800)
```

Update the health evaluation block — after the existing degraded assignment, check duration:
```python
# Evaluate health level
if not result.service_active or not result.process_alive:
    result.level = "critical"
elif not result.polling_active and result.polling_age_seconds > stale_poll:
    if result.log_fresh:
        # Polling stale but logs fresh — could be busy OR a zombie.
        # Check how long we've been in this state.
        degraded_duration = incidents.get_degraded_duration(data_dir)
        if max_degraded > 0 and degraded_duration > max_degraded:
            # Been degraded too long — likely a polling zombie, not just busy.
            result.level = "critical"
        else:
            result.level = "degraded"
    else:
        result.level = "critical"
elif not result.log_fresh:
    result.level = "degraded"
elif len(result.new_tracebacks) > 0:
    result.level = "warning"
else:
    result.level = "healthy"
```

### Step 6: Wire degraded tracking into the probe cycle

In `argus/__main__.py` `run_cycle()`, after the probe runs and health is evaluated, update degraded tracking:

```python
# After: result = probe.run_probes(config, data_dir)

# Track degraded duration
if result.level == "degraded":
    incidents.set_degraded_since(data_dir)
elif result.level in ("healthy", "warning"):
    incidents.clear_degraded_since(data_dir)
# Note: don't clear on "critical" — let remediation handle the reset
```

Also clear `degraded_since` alongside `reset_remediation_attempts` in `remediate()` when health returns to healthy/warning (line 100-103 of remediate.py):
```python
if level in ("healthy", "warning"):
    incidents.reset_remediation_attempts(data_dir)
    incidents.clear_degraded_since(data_dir)
    clear_escalation(data_dir)
    return result
```

### Step 7: Update callers of run_probes() to pass data_dir

In `__main__.py`:
- `run_cycle()` already has `data_dir` — pass it: `result = probe.run_probes(config, data_dir)`
- `show_status()` — pass `data_dir`: `result = probe.run_probes(config, data_dir)`
- `--probe-only` path — let it default to None (computed from config internally)

### Step 8: Add degraded duration to ProbeResult

Add `degraded_duration_seconds` to `ProbeResult` dataclass:
```python
degraded_duration_seconds: float = 0.0
```

And to `to_dict()`:
```python
"degraded_duration_s": self.degraded_duration_seconds,
```

Set it during health evaluation:
```python
result.degraded_duration_seconds = degraded_duration
```

This makes the info available in health.jsonl, `--status` output, and `--json` output.

### Step 9: Show degraded duration in --status output

In `show_status()`, add after the polling age line:
```python
if result.level == "degraded" and result.degraded_duration_seconds > 0:
    mins = int(result.degraded_duration_seconds // 60)
    print(f"  Degraded for: {mins} min (escalates at {max_degraded // 60} min)")
```

### Step 10: Update skill/SKILL.md

Update the Health Levels section to document the zombie detection:
```markdown
## Health Levels

- **healthy** — everything is fine
- **warning** — new tracebacks detected, but service is running
- **degraded** — polling is stale but logs are fresh (you're busy doing work, not dead). Argus will NOT restart you — unless you stay degraded for over 30 minutes, at which point Argus assumes you're a polling zombie and escalates to critical.
- **critical** — service down, process dead, polling AND logs stale, or degraded for too long. Argus WILL restart you.
```

### Step 11: Update default state schema

In `incidents.py` `_default_state()`, add the new field:
```python
def _default_state() -> dict:
    return {
        "known_issues": {},
        "cooldowns": {},
        "last_update_check": 0,
        "hermes_version": "",
        "degraded_since": None,
    }
```

### Step 12: Add event writer for recovery notifications

Create `write_event()` in `argus/notify.py` that appends structured events to `~/.hermes/watchdog/events.jsonl`:

```python
def write_event(data_dir: str, event_type: str, message: str, context: dict | None = None) -> None:
    """Write a structured event for Hermes to pick up and relay to the user.

    Event types: recovery, warning, info, escalation
    """
    events_path = Path(data_dir) / "events.jsonl"
    events_path.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "type": event_type,
        "message": message,
        "context": context or {},
        "delivered": False,
    }
    with open(events_path, "a") as f:
        f.write(json.dumps(event) + "\n")
```

Call this from `remediate()` whenever Argus takes action:
- After successful restart: `write_event(data_dir, "recovery", "I was down for {duration}. Argus restarted me via {action}.")`
- After applying update: `write_event(data_dir, "info", "Argus applied an upstream update ({behind} commits) and restarted me.")`
- After zombie escalation: `write_event(data_dir, "warning", "Polling was dead for {mins} minutes. Argus escalated and restarted me.")`

### Step 13: Rewrite escalation to use Hermes's credentials

Replace the current `_send_telegram()` and `_send_discord()` in `notify.py` with a single `_send_direct()` that reads Hermes's own config:

```python
def _send_direct(hermes_home: str, message: str) -> bool:
    """Send a message directly using Hermes's platform credentials.

    Reads ~/.hermes/config.yaml for platform tokens and
    ~/.hermes/channel_directory.json for the most recently active DM user.
    Falls back to first allowlisted user if no activity history.
    """
    hermes_home = os.path.expanduser(hermes_home)

    # 1. Load Hermes config for platform credentials
    config_path = os.path.join(hermes_home, "config.yaml")
    if not os.path.exists(config_path):
        logger.error("Cannot find Hermes config at %s", config_path)
        return False
    with open(config_path) as f:
        hermes_config = yaml.safe_load(f) or {}

    # 2. Load .env for tokens
    env_path = os.path.join(hermes_home, ".env")
    env_vars = _parse_env_file(env_path) if os.path.exists(env_path) else {}

    # 3. Load channel directory for recent activity
    channel_dir_path = os.path.join(hermes_home, "channel_directory.json")
    channels = {}
    if os.path.exists(channel_dir_path):
        with open(channel_dir_path) as f:
            channels = json.load(f)

    # 4. Find most recently active DM and send
    best_platform, best_target = _find_best_notification_target(hermes_config, channels, env_vars)
    if best_platform and best_target:
        return _send_via_platform(best_platform, best_target, env_vars, message)

    logger.error("No notification target found — check Hermes platform config")
    return False
```

Helper `_find_best_notification_target()` scans channel_directory.json for DM conversations sorted by last activity timestamp. Returns `(platform_name, target_info)` where target_info contains the chat_id/channel_id/user_id needed to send.

Helper `_parse_env_file()` reads the `.env` file and returns a dict of key-value pairs (handles `KEY=value` and `KEY="value"` formats).

Helper `_send_via_platform()` dispatches to platform-specific senders:
- Telegram: `TELEGRAM_BOT_TOKEN` from env + `chat_id` from channel directory
- Discord: `DISCORD_BOT_TOKEN` from env + DM channel via Discord API
- Slack: `SLACK_BOT_TOKEN` from env + `user_id` via Slack API

### Step 14: Simplify Argus notify config

Replace the current `notify` section in `config.example.yaml`:

```yaml
notify:
  # Argus reads Hermes's own platform credentials for notifications.
  # No separate tokens needed.
  hermes_home: ~/.hermes
  # Optional: override the auto-detected notification target
  # override_platform: telegram
  # override_chat_id: "123456789"
```

Remove `telegram_bot_token`, `telegram_chat_id`, `discord_webhook`, and `method` from the config schema. Keep the override fields for users who want explicit control.

### Step 15: Update send_escalation() to use two-tier approach

```python
def send_escalation(config: dict, data_dir: str, message: str, context: dict | None = None) -> bool:
    """Escalation notification — Hermes is dead, send directly."""
    # Always write marker file (for skill detection when Hermes recovers)
    _write_escalation_file(data_dir, message, context)
    # Always write event (Hermes will read this when it comes back)
    write_event(data_dir, "escalation", message, context)

    # Send directly using Hermes's credentials
    hermes_home = config.get("notify", {}).get("hermes_home",
                  config.get("hermes", {}).get("home", "~/.hermes"))
    return _send_direct(hermes_home, f"[Argus Escalation] {message}")
```

### Step 16: Update Argus skill to pick up events

Add to `skill/SKILL.md` a new command for checking pending events:

```markdown
### Check Pending Argus Events
\`\`\`bash
cat ~/.hermes/watchdog/events.jsonl | python3 -c "
import sys, json
for line in sys.stdin:
    e = json.loads(line.strip())
    if not e.get('delivered'):
        print(f'[{e[\"type\"]}] {e[\"ts\"]}: {e[\"message\"]}')
"
\`\`\`
```

Add to the "When to Use This Skill" section:
```markdown
- On startup or after recovery, check for pending Argus events and relay them to the user
- If there are undelivered events, tell the user what happened in natural conversation
```

Add a note about the notification flow:
```markdown
## How Notifications Work

Argus communicates with you (the gateway) through events:
- **Recovery/info events**: Written to `~/.hermes/watchdog/events.jsonl`. You read these on startup or periodically and tell the user what happened in natural conversation.
- **Escalation events**: When you're dead and can't relay, Argus sends directly to the user using your platform credentials. This is the "break glass" path.

When you come back from a restart, always check for pending events and acknowledge them to the user.
```

## Testing Strategy

### Unit Tests

No test framework is currently in place. Manual testing via `--probe-only --json`:

1. **Heartbeat pattern matching**: Set `heartbeat_pattern: "HEARTBEAT"` in config, add a line containing `HEARTBEAT` to the gateway log, run `--probe-only --json` — verify `polling_active: true`.
2. **Pattern miss**: Set pattern to something not in the log — verify `polling_active: false`.
3. **Regex pattern**: Set pattern to `HEART.*ACK` — verify regex matching works.
4. **Degraded duration tracking**: Manually set `degraded_since` in state.json to a past timestamp, run a probe — verify `degraded_duration_s` in output.
5. **Zombie escalation**: Set `degraded_since` to >30 min ago and `max_degraded_seconds: 1800` — run probe, verify level escalates to `critical`.
6. **Zombie escalation disabled**: Set `max_degraded_seconds: 0` — verify degraded stays degraded regardless of duration.

### Edge Cases

1. **Empty heartbeat_pattern**: If user sets `heartbeat_pattern: ""`, `re.compile("")` matches everything — polling will always appear active. Validate non-empty in `check_polling()` or document this behavior.
2. **Invalid regex**: If the pattern is invalid regex, `re.compile()` will raise `re.error`. Catch it and fall back to literal string matching with a warning.
3. **Backwards compatibility**: Old configs without `heartbeat_pattern` default to `"getUpdates"` — existing Telegram setups work unchanged.
4. **State migration**: Old `state.json` files lack `degraded_since`. `_load_state()` handles this — `state.get("degraded_since")` returns `None`, which is the correct "not degraded" state.
5. **Clock skew**: If system clock jumps forward, `degraded_since` calculation could produce a huge duration. Not a real concern on a single machine, but worth noting.
6. **Race between probe and cycle**: `run_probes()` reads `degraded_since` (no lock), `run_cycle()` writes it (with lock). This is fine — worst case, one cycle's read is slightly stale, and the next cycle corrects it.
7. **Hermes config missing or unreadable**: `_send_direct()` must handle missing `config.yaml`, missing `.env`, missing `channel_directory.json`, or no platforms configured. Fall back gracefully with clear log messages.
8. **No DM history in channel_directory.json**: If user only talks in groups (not DMs), fall back to the first user in the platform's allowlist. If no allowlist, fall back to the ESCALATION marker file only.
9. **events.jsonl grows unbounded**: Add rotation — if > 1MB, truncate to last 100 events. Events are ephemeral; the incident reports are the permanent record.
10. **Multi-user race**: If two users are active, Argus picks the most recent. This is fine — the other user can check status via the skill. Don't try to notify everyone.
11. **Old notify config migration**: Users with v0.1 `notify.method: telegram` + separate tokens should still work. If `notify.method` exists and isn't `none`, fall back to the old direct-send path as a compatibility shim. Log a deprecation warning.

## Acceptance Criteria

### Heartbeat & Health Detection
1. `check_polling()` uses `heartbeat_pattern` from config, not hardcoded `"getUpdates"`
2. `config.example.yaml` documents `heartbeat_pattern` with examples for all major platforms
3. Gateway in degraded state for >30 min (default) escalates to critical
4. `max_degraded_seconds: 0` disables zombie escalation (degraded stays forever)
5. `degraded_since` is tracked in state.json, set on first degraded probe, cleared on recovery
6. `--status` shows degraded duration when applicable
7. `--probe-only --json` includes `degraded_duration_s` in output
8. Invalid regex in `heartbeat_pattern` is caught gracefully (warning + fallback)
9. Existing Telegram deployments work unchanged (defaults to `getUpdates`)

### Notifications
10. Recovery events are written to `events.jsonl`, not sent directly
11. SKILL.md documents event pickup so Hermes can relay to users naturally
12. Escalation notifications use Hermes's platform credentials (no separate tokens in Argus config)
13. Escalation finds the most-recently-active DM user from `channel_directory.json`
14. If Hermes config is unreadable, Argus logs a clear error and writes ESCALATION marker file
15. Old `notify.method: telegram` configs still work (compatibility shim with deprecation warning)
16. `notify` config section simplified to just `hermes_home` + optional overrides

## Validation Commands

```bash
# Verify no syntax errors
cd ~/.hermes/argus && python3 -c "from argus import probe, incidents, remediate; print('OK')"

# Probe with JSON output — check for new fields
cd ~/.hermes/argus && python3 -m argus --probe-only --json

# Full status — check for degraded duration display
cd ~/.hermes/argus && python3 -m argus --status

# Verify config loads with new keys
cd ~/.hermes/argus && python3 -c "
from argus.__main__ import load_config
c = load_config()
print('heartbeat_pattern:', c['probe'].get('heartbeat_pattern'))
print('max_degraded_seconds:', c['probe'].get('max_degraded_seconds'))
"
```

## Notes

- **Why 30 minutes?** Hermes tool executions (file editing, web browsing, code analysis) typically complete within 10-15 minutes. Even the longest sessions (complex multi-step research) resolve within 20 minutes. 30 minutes provides generous headroom while still catching true zombies within a reasonable window.
- **Why not just check the process for polling activity?** Platform-specific polling implementations vary wildly — Telegram uses HTTP long-poll, Discord uses WebSocket, Slack uses RTM or Socket Mode. Introspecting the process would require platform-specific knowledge. Log-based heartbeat detection is platform-agnostic and works with any gateway that logs its polling activity.
- **Why regex instead of substring?** Some platforms have multiple possible heartbeat signatures (e.g., Discord logs both `HEARTBEAT` and `heartbeat_ack`). Regex lets operators match any of them with one pattern: `HEARTBEAT|heartbeat_ack`. The performance difference on 500 lines is negligible.
- **`run_probes()` gaining `data_dir` parameter**: This slightly couples the probe function to the state layer, but the alternative (returning the level without zombie checking and letting the caller override it) is worse — it splits the health evaluation logic across two files and makes `--probe-only --json` output inaccurate.
- **Why piggyback on Hermes's credentials?** Zero extra config for users. Hermes already has the platform tokens and knows its users. Argus reading them is safe (same machine, same owner). This also makes Argus more upstream-mergeable — NousResearch won't want a separate notification system that duplicates their gateway's platform layer.
- **Why events.jsonl instead of a socket/API?** Argus and Hermes don't share a process. File-based IPC is the simplest reliable approach — survives restarts, needs no daemon, works with the existing skill system. The Hermes skill already reads files from `~/.hermes/watchdog/`.
- **Why still send directly on escalation?** If Hermes is dead, it can't read events. The user needs to know. Direct send is the "break glass" path — it's ugly (a raw bot message) but it works when nothing else can.
