# PRD: Hermes Watchdog (Argus) v0.2

## Overview
Make Argus work for all 14 Hermes platforms (not just Telegram), catch polling zombies, and integrate notifications through Hermes itself. Goal: upstream-mergeable quality for NousResearch/hermes-agent.

## Target User
Hermes Agent self-hosters running any messaging platform. Technical users who install Argus to make their gateway "unbreakable."

## Problems
1. Polling detection only works for Telegram (hardcoded `getUpdates` grep)
2. Polling zombie loophole — gateway can be alive but deaf forever
3. Notifications require separate bot tokens instead of using Hermes's own credentials

## Success Metrics
- Argus correctly detects polling activity on non-Telegram platforms (Discord, Slack, Matrix)
- Polling zombies are caught and restarted within 30 minutes
- Zero separate notification credentials needed in Argus config
- All existing Telegram deployments continue working unchanged

## Scope

### In Scope (v0.2)
- Configurable heartbeat pattern (regex, per-platform examples)
- Max degraded duration with zombie escalation
- Degraded state tracking in state.json
- Two-tier notification: events.jsonl for recovery, direct send for escalation
- Hermes credential piggybacking for escalation notifications
- Backwards-compatible with v0.1 configs

### Out of Scope
- Admin dashboard / web UI
- Prometheus metrics export
- Docker packaging
- Test framework (deferred to v0.3)
- Multi-agent orchestration

## Implementation
See `specs/multi-platform-health-detection.md` for full 16-step implementation plan.

## Risks
- Hermes config format may vary across versions — need defensive parsing
- channel_directory.json schema is not documented by NousResearch — may change
- Platform API rate limits on direct escalation sends
