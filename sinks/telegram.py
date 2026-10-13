"""Telegram notifications."""
import logging

import requests

logger = logging.getLogger("logging_config")


def send_telegram_notification(bot_token: str, chat_id: str, message: str) -> bool:
    """Send a message via Telegram Bot API. Returns True on success."""
    if not bot_token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        resp = requests.post(url, json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"}, timeout=10)
        if resp.status_code == 200:
            logger.info("📱 Telegram notification sent")
            return True
        logger.warning(f"Telegram API error {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        logger.warning(f"Telegram notification failed: {e}")
    return False
