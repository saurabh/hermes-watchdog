---
name: argus
description: Check Argus watchdog status, view tracked issues, trigger updates, and read incident reports. Argus is a self-healing companion that monitors your gateway health, auto-restarts on failure, tracks recurring errors, and files upstream bug reports.
version: 0.1.0
metadata:
  hermes:
    tags: [health, monitoring, self-healing, watchdog, devops]
    related_skills: []
---

# Argus — Your Self-Healing Watchdog

Argus runs as a separate systemd timer (every 2 minutes) that monitors your gateway health independently. If you (the gateway) go down, Argus keeps running, detects the failure, and restarts you.

## What Argus Does

- **Probes** gateway health every 2 minutes (service status, polling activity, error logs)
- **Auto-restarts** you if polling stops AND logs go stale (with cooldown to avoid restart loops). If polling is stale but logs are fresh, Argus knows you're busy working and won't interrupt you.
- **Tracks errors** — deduplicates tracebacks by signature, counts occurrences
- **Searches upstream** GitHub for matching issues/PRs when new errors appear
- **Auto-files bug reports** after 3 occurrences of an unmatched error
- **Auto-updates** you when upstream fixes are available for known issues

## Commands

When the user asks about your health, watchdog status, errors, or updates, use these commands:

### Check Health Status
```bash
cd ~/.hermes/argus && python3 -m argus --status
```
Shows: health level, service state, polling age, memory, known issues count, update availability.

### List Tracked Issues
```bash
cd ~/.hermes/argus && python3 -m argus --issues
```
Shows all errors Argus has detected, their occurrence count, and upstream status (matched/filed/unmatched).

### Check for Updates
```bash
cd ~/.hermes/argus && python3 -m argus --update
```
Checks if you're behind upstream, shows which known issues are fixed, and applies the update.

### View Recent Incidents
```bash
ls -lt ~/.hermes/watchdog/incidents/ | head -10
```
Lists recent incident reports (markdown files written before each remediation).

### Read a Specific Incident
```bash
cat ~/.hermes/watchdog/incidents/{filename}.md
```

### View Probe History
```bash
tail -20 ~/.hermes/watchdog/health.jsonl | python3 -m json.tool
```

### Check for Escalation
```bash
cat ~/.hermes/watchdog/ESCALATION 2>/dev/null || echo "No active escalation"
```
If this file exists, Argus has exhausted its remediation attempts and needs operator help. Tell the user immediately.

### View Watchdog Logs
```bash
tail -50 ~/.hermes/watchdog/argus.log
```

### Run a Manual Probe
```bash
cd ~/.hermes/argus && python3 -m argus --probe-only --json
```

## When to Use This Skill

- User asks "are you healthy?", "what's your status?", "any errors?"
- User asks "are there updates?", "update yourself", "check for fixes"
- User asks "what issues have you seen?", "any bugs?", "show me errors"
- User asks about Argus, the watchdog, or monitoring
- After you encounter an error, check if Argus already tracked it
- When you restart or recover from an error, mention that Argus is watching

## Health Levels

- **healthy** — everything is fine
- **warning** — new tracebacks detected, but service is running
- **degraded** — polling is stale but logs are fresh (you're busy doing work, not dead). Argus will NOT restart you.
- **critical** — service down, process dead, or both polling AND logs are stale. Argus WILL restart you.

Degraded is normal when you're in a long tool execution loop. Don't tell the user something is wrong — just mention you're busy.

## How to Talk About Argus

Argus is your companion — a separate process that watches over you. You can reference it naturally:

- "Argus is monitoring me — it'll restart me if I go down"
- "Let me check what Argus has been tracking..."
- "Argus found an upstream fix for that — I can update"
- "Argus has seen this error 3 times and filed it upstream"

## Architecture

```
You (Hermes Gateway)              Argus (systemd timer)
┌─────────────────────┐           ┌─────────────────────┐
│ hermes-gateway.service │◄──monitors──│ argus.timer (2min)  │
│ Python process       │           │ Separate Python     │
│ Telegram polling     │           │ Reads your logs     │
│ Tool execution       │           │ Checks your process │
│ Chat responses       │           │ Restarts if needed  │
└─────────────────────┘           │ Tracks errors       │
                                  │ Searches GitHub     │
                                  │ Files bug reports   │
                                  │ Applies updates     │
                                  └─────────────────────┘
```

Argus is completely independent. If you crash, Argus still runs. If Argus crashes, systemd restarts it. You don't share a process.

## Data Location

All Argus data lives in `~/.hermes/watchdog/`:
- `state.json` — known issues, cooldowns, version tracking
- `health.jsonl` — probe history
- `argus.log` — argus's own logs
- `ESCALATION` — present when remediation failed and operator help is needed
- `incidents/` — markdown incident reports
