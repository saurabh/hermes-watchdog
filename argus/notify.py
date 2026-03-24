"""Notifications — two-tier system integrated with Hermes.

Tier 1 (recovery): Write events to events.jsonl for Hermes to pick up and relay.
Tier 2 (escalation): Send directly using Hermes's own platform credentials.
"""

import json
import logging
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("argus.notify")

ESCALATION_FILE = "ESCALATION"
EVENTS_FILE = "events.jsonl"
MAX_EVENTS_BYTES = 1_000_000  # 1 MB — rotate if exceeded


# ---------------------------------------------------------------------------
# Tier 1: Event writer (Hermes picks these up and relays to user)
# ---------------------------------------------------------------------------

def write_event(data_dir: str, event_type: str, message: str, context: dict | None = None) -> None:
    """Write a structured event for Hermes to pick up and relay to the user.

    Event types: recovery, warning, info, escalation
    """
    events_path = Path(data_dir) / EVENTS_FILE
    events_path.parent.mkdir(parents=True, exist_ok=True)

    # Rotate if too large
    if events_path.exists() and events_path.stat().st_size > MAX_EVENTS_BYTES:
        lines = events_path.read_text().splitlines()
        events_path.write_text("\n".join(lines[-100:]) + "\n")

    event = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "type": event_type,
        "message": message,
        "context": context or {},
        "delivered": False,
    }
    with open(events_path, "a") as f:
        f.write(json.dumps(event) + "\n")
    logger.info("Wrote %s event: %s", event_type, message[:80])


# ---------------------------------------------------------------------------
# Tier 2: Direct escalation (Hermes is dead, send via its credentials)
# ---------------------------------------------------------------------------

def send_escalation(config: dict, data_dir: str, message: str, context: dict | None = None) -> bool:
    """Escalation notification — Hermes is dead, send directly.

    1. Writes ESCALATION marker file (for skill detection when Hermes recovers)
    2. Writes event to events.jsonl (Hermes reads on recovery)
    3. Sends directly using Hermes's platform credentials
    """
    _write_escalation_file(data_dir, message, context)
    write_event(data_dir, "escalation", message, context)

    notify_cfg = config.get("notify", {})

    # Legacy v0.1 config support (deprecated)
    legacy_method = notify_cfg.get("method")
    if legacy_method and legacy_method != "none":
        logger.warning("Using legacy notify.method='%s' — migrate to v0.2 config (hermes_home)", legacy_method)
        if legacy_method == "telegram":
            return _send_telegram_direct(
                notify_cfg.get("telegram_bot_token", ""),
                notify_cfg.get("telegram_chat_id", ""),
                message,
            )
        if legacy_method == "discord":
            return _send_discord_webhook(notify_cfg.get("discord_webhook", ""), message)
        return False

    # v0.2: Read Hermes's own credentials
    hermes_home = os.path.expanduser(
        notify_cfg.get("hermes_home", config.get("hermes", {}).get("home", "~/.hermes"))
    )

    # Check for override target
    override_platform = notify_cfg.get("override_platform")
    override_chat_id = notify_cfg.get("override_chat_id")

    return _send_via_hermes(hermes_home, message, override_platform, override_chat_id)


def clear_escalation(data_dir: str) -> None:
    """Remove escalation marker when health returns to normal."""
    p = Path(data_dir) / ESCALATION_FILE
    if p.exists():
        p.unlink()
        logger.info("Cleared escalation marker — health restored")


def has_escalation(data_dir: str) -> bool:
    """Check if there's an active escalation."""
    return (Path(data_dir) / ESCALATION_FILE).exists()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _write_escalation_file(data_dir: str, message: str, context: dict | None = None) -> None:
    """Write escalation marker with details for the Hermes skill."""
    p = Path(data_dir) / ESCALATION_FILE
    p.parent.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    content = {
        "timestamp": now,
        "message": message,
        "context": context or {},
    }
    p.write_text(json.dumps(content, indent=2) + "\n")
    logger.info("Wrote escalation marker: %s", p)


def _send_via_hermes(hermes_home: str, message: str,
                     override_platform: str | None = None,
                     override_chat_id: str | None = None) -> bool:
    """Send a message using Hermes's own platform credentials.

    Reads ~/.hermes/.env for tokens and ~/.hermes/channel_directory.json
    for the most recently active DM user.
    """
    env_vars = _parse_env_file(os.path.join(hermes_home, ".env"))

    # If override specified, use it directly
    if override_platform and override_chat_id:
        return _send_to_target(override_platform, override_chat_id, env_vars, message)

    # Auto-detect best notification target from channel directory
    platform, chat_id = _find_best_target(hermes_home, env_vars)
    if platform and chat_id:
        return _send_to_target(platform, chat_id, env_vars, message)

    logger.error("No notification target found — check Hermes platform config at %s", hermes_home)
    return False


def _find_best_target(hermes_home: str, env_vars: dict) -> tuple[str | None, str | None]:
    """Find the most recently active DM user from Hermes's channel directory.

    Returns (platform, chat_id) or (None, None).
    """
    channel_dir_path = os.path.join(hermes_home, "channel_directory.json")
    if not os.path.exists(channel_dir_path):
        # Fall back to config allowlists
        return _find_target_from_config(hermes_home, env_vars)

    try:
        with open(channel_dir_path) as f:
            channels = json.load(f)
    except (json.JSONDecodeError, OSError):
        return _find_target_from_config(hermes_home, env_vars)

    # Look for DM conversations, sorted by most recent activity
    best_ts = ""
    best_platform = None
    best_chat_id = None

    for platform_name, platform_data in channels.items():
        if not isinstance(platform_data, dict):
            continue

        # channel_directory.json structure varies — look for DMs
        dms = platform_data.get("dms", platform_data.get("direct_messages", []))
        if isinstance(dms, list):
            for dm in dms:
                if not isinstance(dm, dict):
                    continue
                last_activity = dm.get("last_activity", dm.get("last_message_ts", ""))
                chat_id = str(dm.get("chat_id", dm.get("id", dm.get("user_id", ""))))
                if last_activity > best_ts and chat_id:
                    # Verify we have credentials for this platform
                    if _has_credentials(platform_name, env_vars):
                        best_ts = last_activity
                        best_platform = platform_name
                        best_chat_id = chat_id
        elif isinstance(dms, dict):
            for user_id, dm_info in dms.items():
                if not isinstance(dm_info, dict):
                    continue
                last_activity = dm_info.get("last_activity", dm_info.get("last_message_ts", ""))
                chat_id = str(dm_info.get("chat_id", user_id))
                if last_activity > best_ts and chat_id:
                    if _has_credentials(platform_name, env_vars):
                        best_ts = last_activity
                        best_platform = platform_name
                        best_chat_id = chat_id

    if best_platform:
        logger.info("Auto-detected notification target: %s chat %s", best_platform, best_chat_id)
        return best_platform, best_chat_id

    return _find_target_from_config(hermes_home, env_vars)


def _find_target_from_config(hermes_home: str, env_vars: dict) -> tuple[str | None, str | None]:
    """Fall back to finding a notification target from Hermes config allowlists."""
    try:
        import yaml
    except ImportError:
        logger.warning("pyyaml not available — cannot read Hermes config for fallback target")
        return None, None

    config_path = os.path.join(hermes_home, "config.yaml")
    if not os.path.exists(config_path):
        return None, None

    try:
        with open(config_path) as f:
            hermes_config = yaml.safe_load(f) or {}
    except Exception:
        return None, None

    # Try Telegram first (most common, has push notifications)
    tg_config = hermes_config.get("telegram", {})
    allowlist = tg_config.get("allowlist", tg_config.get("allowed_users", []))
    if allowlist and _has_credentials("telegram", env_vars):
        chat_id = str(allowlist[0]) if isinstance(allowlist[0], (int, str)) else None
        if chat_id:
            return "telegram", chat_id

    # Try Discord
    dc_config = hermes_config.get("discord", {})
    allowlist = dc_config.get("allowlist", dc_config.get("allowed_users", []))
    if allowlist and _has_credentials("discord", env_vars):
        return "discord", str(allowlist[0])

    # Try Slack
    slack_config = hermes_config.get("slack", {})
    allowlist = slack_config.get("allowlist", slack_config.get("allowed_users", []))
    if allowlist and _has_credentials("slack", env_vars):
        return "slack", str(allowlist[0])

    return None, None


def _has_credentials(platform: str, env_vars: dict) -> bool:
    """Check if we have the necessary credentials for a platform."""
    platform = platform.lower()
    if platform == "telegram":
        return bool(env_vars.get("TELEGRAM_BOT_TOKEN"))
    if platform == "discord":
        return bool(env_vars.get("DISCORD_BOT_TOKEN") or env_vars.get("DISCORD_TOKEN"))
    if platform == "slack":
        return bool(env_vars.get("SLACK_BOT_TOKEN"))
    return False


def _send_to_target(platform: str, chat_id: str, env_vars: dict, message: str) -> bool:
    """Dispatch to platform-specific sender."""
    platform = platform.lower()
    prefixed = f"[Argus] {message}"

    if platform == "telegram":
        token = env_vars.get("TELEGRAM_BOT_TOKEN", "")
        return _send_telegram_direct(token, chat_id, prefixed)
    if platform == "discord":
        # Discord DMs require opening a DM channel first, then sending
        token = env_vars.get("DISCORD_BOT_TOKEN") or env_vars.get("DISCORD_TOKEN", "")
        return _send_discord_dm(token, chat_id, prefixed)
    if platform == "slack":
        token = env_vars.get("SLACK_BOT_TOKEN", "")
        return _send_slack_dm(token, chat_id, prefixed)

    logger.error("Unsupported platform for direct send: %s", platform)
    return False


def _parse_env_file(env_path: str) -> dict:
    """Parse a .env file into a dict. Handles KEY=value and KEY="value" formats."""
    result = {}
    if not os.path.exists(env_path):
        return result
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                # Strip surrounding quotes
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                    value = value[1:-1]
                result[key] = value
    except OSError:
        pass
    return result


# ---------------------------------------------------------------------------
# Platform senders (using urllib — no dependencies)
# ---------------------------------------------------------------------------

def _send_telegram_direct(token: str, chat_id: str, message: str) -> bool:
    """Send via Telegram Bot API."""
    if not token or not chat_id:
        logger.error("Telegram send failed: missing token or chat_id")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
    }).encode()

    try:
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                logger.info("Telegram notification sent to chat %s", chat_id)
                return True
            logger.error("Telegram API returned %d", resp.status)
            return False
    except (urllib.error.URLError, OSError) as e:
        logger.error("Telegram notification failed: %s", e)
        return False


def _send_discord_dm(token: str, user_id: str, message: str) -> bool:
    """Send a Discord DM by opening a DM channel then posting."""
    if not token or not user_id:
        logger.error("Discord send failed: missing token or user_id")
        return False

    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
    }

    # Step 1: Open DM channel
    try:
        dm_payload = json.dumps({"recipient_id": user_id}).encode()
        req = urllib.request.Request(
            "https://discord.com/api/v10/users/@me/channels",
            data=dm_payload, headers=headers,
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            dm_channel = json.loads(resp.read())
            channel_id = dm_channel["id"]
    except (urllib.error.URLError, OSError, KeyError, json.JSONDecodeError) as e:
        logger.error("Discord DM channel creation failed: %s", e)
        return False

    # Step 2: Send message
    try:
        msg_payload = json.dumps({"content": message}).encode()
        req = urllib.request.Request(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            data=msg_payload, headers=headers,
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                logger.info("Discord DM sent to user %s", user_id)
                return True
            logger.error("Discord message send returned %d", resp.status)
            return False
    except (urllib.error.URLError, OSError) as e:
        logger.error("Discord DM send failed: %s", e)
        return False


def _send_discord_webhook(webhook_url: str, message: str) -> bool:
    """Send via Discord webhook (legacy v0.1 path)."""
    if not webhook_url:
        logger.error("Discord webhook URL missing")
        return False

    payload = json.dumps({"content": f"**Argus Escalation**\n\n{message}"}).encode()
    try:
        req = urllib.request.Request(webhook_url, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status in (200, 204):
                logger.info("Discord webhook notification sent")
                return True
            logger.error("Discord webhook returned %d", resp.status)
            return False
    except (urllib.error.URLError, OSError) as e:
        logger.error("Discord webhook failed: %s", e)
        return False


def _send_slack_dm(token: str, user_id: str, message: str) -> bool:
    """Send a Slack DM via chat.postMessage."""
    if not token or not user_id:
        logger.error("Slack send failed: missing token or user_id")
        return False

    url = "https://slack.com/api/chat.postMessage"
    payload = json.dumps({
        "channel": user_id,  # Slack accepts user_id as channel for DMs
        "text": message,
    }).encode()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    try:
        req = urllib.request.Request(url, data=payload, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read())
            if body.get("ok"):
                logger.info("Slack DM sent to user %s", user_id)
                return True
            logger.error("Slack API error: %s", body.get("error", "unknown"))
            return False
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        logger.error("Slack DM send failed: %s", e)
        return False
