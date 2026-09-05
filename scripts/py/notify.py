"""
notify.py — Scout Telegram Notifier
Sends today's top qualified bets to a Telegram chat.
Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID as environment variables
(GitHub Actions secrets in production).

Dependencies: requests
"""

import os
import json
import logging
from datetime import datetime, timezone, timedelta

import requests

logging.basicConfig(level=logging.INFO, format="[notify] %(message)s")
log = logging.getLogger(__name__)

EAT = timezone(timedelta(hours=3))

# ─── CONFIG ──────────────────────────────────────────────────────────────────

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Max bets to include in the Telegram message
MAX_BETS_IN_MESSAGE = 8

# Confidence threshold — only notify on bets above this score
MIN_CONFIDENCE_FOR_NOTIFY = 60


# ─── FORMATTERS ──────────────────────────────────────────────────────────────

def confidence_emoji(score: int) -> str:
    if score >= 80:
        return "🟢"
    elif score >= 65:
        return "🟡"
    else:
        return "🔴"


def format_bets(bets: list[dict]) -> str:
    labels = [b["label"] for b in bets]
    return " · ".join(labels)


def format_match(match: dict, index: int) -> str:
    """Format a single match for Telegram."""
    conf = match.get("confidence", 0)
    emoji = confidence_emoji(conf)
    home = match.get("home", "")
    away = match.get("away", "")
    league = match.get("league", "")
    kick_off = match.get("time", "TBD")
    bets = format_bets(match.get("bets", []))
    odds = match.get("odds", {})
    low_conf = match.get("lowConfidence", False)

    lines = [
        f"{emoji} *{index}. {home} vs {away}*",
        f"🏆 {league}",
        f"⏰ {kick_off} EAT",
        f"📊 Odds: H {odds.get('home','?')} · D {odds.get('draw','?')} · A {odds.get('away','?')}",
        f"🎯 {bets}",
        f"💡 Confidence: {conf}/100",
    ]

    if low_conf:
        lines.append("⚠️ _Low data confidence — verify manually_")

    return "\n".join(lines)


def build_message(results: list[dict], scanned_count: int) -> str:
    """Build the full Telegram message."""
    today = datetime.now(EAT).strftime("%A, %d %b %Y")

    # Filter to minimum confidence threshold
    qualified = [m for m in results if m.get("confidence", 0) >= MIN_CONFIDENCE_FOR_NOTIFY]
    top = qualified[:MAX_BETS_IN_MESSAGE]

    if not top:
        return (
            f"🔍 *Scout — {today}*\n\n"
            f"No bets met the confidence threshold today.\n"
            f"_{scanned_count} fixtures scanned._"
        )

    header = (
        f"🔍 *Scout Daily Scan — {today}*\n"
        f"_{scanned_count} fixtures scanned · {len(qualified)} qualified_\n"
        f"{'─' * 28}"
    )

    match_blocks = []
    for i, match in enumerate(top, 1):
        match_blocks.append(format_match(match, i))

    footer = (
        f"{'─' * 28}\n"
        f"_Bets sorted by confidence. Always verify odds before placing._\n"
        f"_Fixed stake only during testing phase._"
    )

    return "\n\n".join([header] + match_blocks + [footer])


# ─── SEND ────────────────────────────────────────────────────────────────────

def send_message(text: str) -> bool:
    """Send a Markdown message to the configured Telegram chat."""
    if not BOT_TOKEN or not CHAT_ID:
        log.warning("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set. Skipping.")
        return False

    url = f"{TELEGRAM_API}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }

    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        log.info("Telegram message sent successfully.")
        return True
    except requests.exceptions.HTTPError as e:
        log.error(f"Telegram HTTP error: {e} — {resp.text}")
        return False
    except requests.exceptions.RequestException as e:
        log.error(f"Telegram request failed: {e}")
        return False


def notify(results: list[dict], scanned_count: int = 0) -> bool:
    """Main entry point. Build and send the daily notification."""
    message = build_message(results, scanned_count)
    log.info(f"Sending Telegram notification ({len(message)} chars)...")
    return send_message(message)


# ─── CLI TEST ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Preview message without sending (no token needed)
    sample_results = [
        {
            "home": "Arsenal", "away": "Wolves",
            "league": "Premier League", "time": "13:30",
            "odds": {"home": 1.55, "draw": 4.20, "away": 6.50},
            "confidence": 88,
            "lowConfidence": False,
            "bets": [
                {"label": "Home Win", "type": "primary"},
                {"label": "Over 2.5", "type": "secondary"},
                {"label": "1 + Over 2.5", "type": "combo"}
            ]
        },
        {
            "home": "Barcelona", "away": "Getafe",
            "league": "La Liga", "time": "16:00",
            "odds": {"home": 1.40, "draw": 4.80, "away": 8.00},
            "confidence": 82,
            "lowConfidence": False,
            "bets": [
                {"label": "Home Win", "type": "primary"},
                {"label": "Over 2.5", "type": "secondary"},
            ]
        },
        {
            "home": "Napoli", "away": "Lecce",
            "league": "Serie A", "time": "19:45",
            "odds": {"home": 1.70, "draw": 3.80, "away": 5.50},
            "confidence": 61,
            "lowConfidence": True,
            "bets": [
                {"label": "Home Win", "type": "primary"},
                {"label": "Over 1.5", "type": "secondary"},
            ]
        }
    ]

    message = build_message(sample_results, scanned_count=247)
    print("─── TELEGRAM PREVIEW ───")
    print(message)
    print("────────────────────────")
    print("\n(Set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID env vars to actually send)")
