"""Escalation notifications — alert operators when auto-remediation fails."""

import json
import logging
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("argus.notify")

ESCALATION_FILE = "ESCALATION"


def send_escalation(config: dict, data_dir: str, message: str, context: dict | None = None) -> bool:
    """Send escalation notification via configured method.

    Also writes an ESCALATION marker file so the Hermes skill can detect it.
    Returns True if notification was sent (or no method configured).
    """
    # Always write marker file
    _write_escalation_file(data_dir, message, context)

    notify_cfg = config.get("notify", {})
    method = notify_cfg.get("method", "none")

    if method == "none":
        logger.warning("Escalation (no notification configured): %s", message)
        return True

    if method == "telegram":
        return _send_telegram(notify_cfg, message)

    if method == "discord":
        return _send_discord(notify_cfg, message)

    logger.warning("Unknown notify method '%s', escalation not sent", method)
    return False


def clear_escalation(data_dir: str) -> None:
    """Remove escalation marker when health returns to normal."""
    p = Path(data_dir) / ESCALATION_FILE
    if p.exists():
        p.unlink()
        logger.info("Cleared escalation marker — health restored")


def has_escalation(data_dir: str) -> bool:
    """Check if there's an active escalation."""
    return (Path(data_dir) / ESCALATION_FILE).exists()


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


def _send_telegram(cfg: dict, message: str) -> bool:
    """Send via Telegram Bot API (no dependencies needed)."""
    token = cfg.get("telegram_bot_token", "")
    chat_id = cfg.get("telegram_chat_id", "")

    if not token or not chat_id:
        logger.error("Telegram notification configured but bot_token or chat_id missing")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": f"🚨 Argus Escalation\n\n{message}", "parse_mode": "HTML"}).encode()

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


def _send_discord(cfg: dict, message: str) -> bool:
    """Send via Discord webhook (no dependencies needed)."""
    webhook_url = cfg.get("discord_webhook", "")

    if not webhook_url:
        logger.error("Discord notification configured but webhook URL missing")
        return False

    payload = json.dumps({"content": f"**Argus Escalation**\n\n{message}"}).encode()

    try:
        req = urllib.request.Request(webhook_url, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status in (200, 204):
                logger.info("Discord notification sent")
                return True
            logger.error("Discord webhook returned %d", resp.status)
            return False
    except (urllib.error.URLError, OSError) as e:
        logger.error("Discord notification failed: %s", e)
        return False
