# Argus — Self-Healing Watchdog for Hermes Agent

> *Named after Argus Panoptes — the hundred-eyed giant who never slept. Some of his eyes were always watching.*

Your Hermes gateway can crash, freeze, or lose its Telegram polling connection — and just sit there, alive but deaf. Argus watches from outside, detects the failure, and fixes it before you notice.

**Argus runs as a separate process.** It's a systemd timer, completely independent from Hermes. If your gateway goes down, Argus keeps running. If Argus somehow goes down, systemd restarts it. They never share a process.

## What You Get

- **Auto-restart** — Detects stale polling, dead processes, and silent failures. Restarts with cooldowns so it doesn't loop.
- **Auto-update** — When a fix for a known issue exists upstream, Argus pulls it before restarting.
- **Error tracking** — Deduplicates every traceback by signature, counts occurrences, writes incident reports.
- **Upstream integration** — Searches GitHub for matching issues/PRs. Auto-files a bug report after 3 occurrences.
- **Hermes skill** — Your agent knows about Argus. It can check its own health, view tracked issues, and trigger updates through natural conversation.

## Install

```bash
git clone https://github.com/saurabh/hermes-watchdog
cd hermes-watchdog
chmod +x install.sh
./install.sh
```

This does three things:
1. Installs the watchdog to `~/.hermes/argus/`
2. Sets up a systemd timer that runs every 2 minutes
3. Copies the Hermes skill so your agent can talk about its own health

### Requirements

- Python 3.10+ with `pyyaml`
- `gh` CLI authenticated (`gh auth login`) — for upstream search/filing
- Hermes Agent running via systemd (`hermes-gateway.service`)

## How It Works

```
Your Hermes Gateway                 Argus (separate process)
┌────────────────────────┐          ┌────────────────────────────────┐
│ hermes-gateway.service │◄─monitors─│ argus.timer (every 2 min)     │
│                        │          │                                │
│ Telegram polling       │          │ 1. PROBE                      │
│ Tool execution         │          │    Is the service active?      │
│ Chat responses         │          │    Is polling alive?           │
│ AI conversations       │          │    Any new tracebacks?         │
│                        │          │                                │
│ Can crash, freeze,     │          │ 2. EVALUATE                   │
│ or lose connection     │          │    healthy → do nothing        │
│                        │          │    degraded/critical → fix it  │
│                        │          │                                │
│                  ┌─────┤          │ 3. REMEDIATE                   │
│                  │skill│          │    Check upstream for fix      │
│                  │     │          │    Pull update if available    │
│  "Argus says I'm│     │          │    Restart with cooldown       │
│   healthy and   │     │          │    Escalate after 3 failures   │
│   10 commits    │     │          │                                │
│   behind"       │     │          │ 4. TRACK                       │
│                  └─────┘          │    Deduplicate errors          │
│                        │          │    Search GitHub for matches   │
│                        │          │    File issue after 3rd hit    │
└────────────────────────┘          └────────────────────────────────┘
```

## Usage

Argus runs automatically via the systemd timer. You can also run it manually:

```bash
# Check health right now
cd ~/.hermes/argus && python3 -m argus --status

# List all tracked issues with upstream status
cd ~/.hermes/argus && python3 -m argus --issues

# Check for hermes updates and apply them
cd ~/.hermes/argus && python3 -m argus --update

# Run one full probe-evaluate-remediate cycle
cd ~/.hermes/argus && python3 -m argus

# Probe only (no remediation)
cd ~/.hermes/argus && python3 -m argus --probe-only --json
```

Or just ask your Hermes agent — the skill lets it run these commands naturally:
- *"Are you healthy?"*
- *"Any errors lately?"*
- *"Are there updates?"*
- *"Update yourself"*

## Configuration

Edit `~/.hermes/watchdog/config.yaml`:

```yaml
hermes:
  home: ~/.hermes
  service: hermes-gateway
  systemd_user: true
  logs:
    gateway: ~/.hermes/logs/gateway.log
    errors: ~/.hermes/logs/errors.log

probe:
  polling_stale_seconds: 300   # 5 min before polling is "stale"
  log_stale_seconds: 300

remediation:
  cooldown_seconds: 300        # 5 min between restart attempts
  max_attempts: 3              # escalate after 3 failures
  chain:
    - systemctl_restart        # try clean restart first
    - process_kill_restart     # kill + restart
    - escalate                 # give up, notify operator

upstream:
  repo: NousResearch/hermes-agent
  auto_issue_after: 3          # file bug after 3 occurrences (0 = off)

notify:
  method: none                 # telegram, discord, or none
  telegram_bot_token: ""       # for escalation alerts
  telegram_chat_id: ""
  discord_webhook: ""
```

## Data

```
~/.hermes/watchdog/
├── config.yaml          # Your configuration
├── state.json           # Known issues, cooldowns, version info
├── health.jsonl         # Probe history (append-only)
├── argus.log            # Argus logs
└── incidents/           # Markdown incident reports
    └── 2026-03-24-134500-valueerror-stop.md
```

## Remediation Chain

When Argus detects a problem, it doesn't just blindly restart:

| Step | Action | When |
|------|--------|------|
| 0 | Check upstream | Always — if a fix exists, pull it first |
| 1 | `systemctl restart` | First attempt |
| 2 | Kill process + restart | If clean restart didn't help |
| 3 | Escalate to operator | After 3 failures |

Each action has a 5-minute cooldown. Argus won't restart you more than 3 times before giving up and escalating. On escalation, Argus writes an `ESCALATION` marker file and sends a notification via the configured method (Telegram, Discord, or none).

## Error Tracking

Every unique error gets a signature: `{ExceptionType}:{file}:{line}:{function}`

Argus tracks:
- First and last occurrence
- Total count
- Whether an upstream issue/PR exists
- Whether it filed a bug report
- Sample traceback for the report

After 3 occurrences with no upstream match, Argus files a GitHub issue with the full stack trace, environment info, and occurrence count.

## Uninstall

```bash
systemctl --user stop argus.timer
systemctl --user disable argus.timer
rm -rf ~/.hermes/argus ~/.hermes/watchdog
rm ~/.config/systemd/user/argus.service ~/.config/systemd/user/argus.timer
systemctl --user daemon-reload
```

## License

MIT
