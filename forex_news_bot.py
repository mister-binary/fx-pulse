"""
Forex Factory News Bot — scheduled side
-----------------------------------------
Two jobs, both broadcasting to everyone who has selected /start:
  1. A daily digest at a fixed time.
  2. A reminder ~30 min before EACH individual high-impact release today
     (not tied to session opens — a release gets its own reminder
     regardless of which session it falls in; same-time releases are
     grouped into one message).

Also caches today's high-impact events into Cloudflare KV every run, so
the Cloudflare Worker (instant /check responses) can read reliable data
without needing to fetch ForexFactory directly — ForexFactory appears to
block/reject requests coming from Cloudflare's own network, which is why
on-demand checks were silently coming back empty.

Designed to run every ~5 min via GitHub Actions (see the interval note in
TOLERANCE_MINUTES below — this matters for not missing odd-minute release
times like :15 or :45).
"""

import json
import logging
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("ff_bot")

FF_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
CHECK_BUTTON_LABEL = "🔍 Check News Now"

WEEKEND_REMINDERS = [
    "📖 Weekend check-in: review this week's trades. What worked, what didn't",
    "🧘 Markets are closed — a good day to step away from the charts and rest.",
    "📚 Study idea: pick one setup from this week and break down why it worked (or didn't).",
    "🚶 Go outside, get some fresh air, come back sharper on Monday.",
    "📏 Weekend reminder: re-read your own trading rules before Monday's open.",
    "🧠 Overtrading usually starts with under-resting. Take today off from the charts.",
]


# --------------------------------------------------------------------------
# Shared user data — read-only here (Cloudflare Worker owns the writes)
# --------------------------------------------------------------------------
def _kv_base() -> str:
    return (
        f"https://api.cloudflare.com/client/v4/accounts/{config.CF_ACCOUNT_ID}"
        f"/storage/kv/namespaces/{config.CF_KV_NAMESPACE_ID}"
    )


def load_users() -> dict:
    headers = {"Authorization": f"Bearer {config.CF_API_TOKEN}"}
    try:
        r = requests.get(f"{_kv_base()}/keys", headers=headers, timeout=15)
        r.raise_for_status()
        keys = [k["name"] for k in r.json().get("result", [])]
    except (requests.RequestException, ValueError) as e:
        log.error("Failed to list Cloudflare KV keys: %s", e)
        return {}

    users = {}
    for key in keys:
        if not key.startswith("user:"):
            continue
        chat_id = key[len("user:"):]
        try:
            vr = requests.get(f"{_kv_base()}/values/{key}", headers=headers, timeout=15)
            vr.raise_for_status()
            users[chat_id] = vr.json()
        except (requests.RequestException, ValueError) as e:
            log.error("Failed to read Cloudflare KV key %s: %s", key, e)
    return users


def cache_calendar_to_kv(todays_events: list[dict]) -> None:
    """Write today's high-impact events to KV so the Worker can read them
    reliably instead of fetching ForexFactory directly (see module docstring)."""
    headers = {"Authorization": f"Bearer {config.CF_API_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "updated_at": datetime.now(ZoneInfo("UTC")).isoformat(),
        "events": [
            {
                "date": e["date"],
                "country": e.get("country", ""),
                "title": e.get("title", ""),
                "forecast": e.get("forecast", ""),
                "previous": e.get("previous", ""),
            }
            for e in todays_events
        ],
    }
    try:
        r = requests.put(
            f"{_kv_base()}/values/calendar_cache",
            headers=headers,
            data=json.dumps(payload),
            timeout=15,
        )
        r.raise_for_status()
    except requests.RequestException as e:
        log.error("Failed to cache calendar to KV: %s", e)


def with_rules(text: str, users: dict, chat_id) -> str:
    rules = users.get(str(chat_id), {}).get("rules", "")
    if rules:
        return f"{text}\n\n📋 <b>Your rules:</b>\n{rules}"
    return text


def get_recipients(users: dict) -> set:
    return set(users.keys()) | {str(config.CHAT_ID)}


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------
def send_telegram_message(text: str, chat_id=None, with_keyboard: bool = True) -> None:
    target_chat = chat_id if chat_id is not None else config.CHAT_ID
    url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": target_chat,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if with_keyboard:
        payload["reply_markup"] = {
            "keyboard": [[CHECK_BUTTON_LABEL]],
            "resize_keyboard": True,
            "is_persistent": True,
        }
    try:
        r = requests.post(url, json=payload, timeout=15)
        r.raise_for_status()
    except requests.RequestException as e:
        log.error("Failed to send Telegram message to %s: %s", target_chat, e)


# --------------------------------------------------------------------------
# ForexFactory calendar
# --------------------------------------------------------------------------
def fetch_calendar() -> list[dict]:
    try:
        r = requests.get(FF_CALENDAR_URL, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        log.error("Failed to fetch ForexFactory calendar: %s", e)
        return []
    except ValueError as e:
        log.error("Failed to parse ForexFactory calendar JSON: %s", e)
        return []


def parse_event_time(event: dict) -> datetime | None:
    raw = event.get("date")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def is_high_impact(event: dict) -> bool:
    return str(event.get("impact", "")).strip().lower() == "high"


def get_high_impact_events(events: list[dict]) -> list[dict]:
    out = []
    for e in events:
        if is_high_impact(e):
            dt = parse_event_time(e)
            if dt:
                out.append({**e, "_dt": dt})
    out.sort(key=lambda e: e["_dt"])
    return out


def get_todays_high_impact_events(tz: ZoneInfo) -> list[dict]:
    today = datetime.now(tz).date()
    events = get_high_impact_events(fetch_calendar())
    return [e for e in events if e["_dt"].astimezone(tz).date() == today]


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------
def format_event_line(event: dict, tz: ZoneInfo) -> str:
    local_dt = event["_dt"].astimezone(tz)
    time_str = local_dt.strftime("%H:%M")
    currency = event.get("country", "")
    title = event.get("title", "")
    forecast = event.get("forecast", "") or "-"
    previous = event.get("previous", "") or "-"
    return f"🔴 {time_str} | <b>{currency}</b> — {title}\n     Forecast: {forecast} | Previous: {previous}"


def get_weekend_reminder_text(today) -> str:
    idx = today.toordinal() % len(WEEKEND_REMINDERS)
    return f"🗓️ <b>{today.strftime('%A, %d %b %Y')}</b>\n{WEEKEND_REMINDERS[idx]}"


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------
def send_daily_digest(todays_events: list[dict], tz: ZoneInfo):
    log.info("Running daily digest job")
    now_local = datetime.now(tz)
    today = now_local.date()

    users = load_users()
    recipients = get_recipients(users)

    if now_local.weekday() >= 5:  # Saturday=5, Sunday=6
        base_text = get_weekend_reminder_text(today)
    elif not todays_events:
        base_text = f"📅 <b>{today.strftime('%A, %d %b %Y')}</b>\nNo high-impact news scheduled today."
    else:
        lines = [f"📅 <b>High-Impact News — {today.strftime('%A, %d %b %Y')}</b>\n"]
        lines += [format_event_line(e, tz) for e in todays_events]
        base_text = "\n\n".join(lines)

    for chat_id in recipients:
        send_telegram_message(with_rules(base_text, users, chat_id), chat_id=chat_id)


def send_pre_news_alerts(now_utc: datetime, todays_events: list[dict], tz: ZoneInfo) -> None:
    """Fire ~ALERT_MINUTES_BEFORE minutes before EACH individual release
    (grouping releases that share an exact time into one message)."""
    now_local = now_utc.astimezone(tz)
    if now_local.weekday() == 5 or not todays_events:  # Saturday, or nothing today
        return

    groups: dict[datetime, list[dict]] = {}
    for e in todays_events:
        key = e["_dt"].replace(second=0, microsecond=0)
        groups.setdefault(key, []).append(e)

    users = load_users()
    recipients = get_recipients(users)

    for event_time, group in groups.items():
        target = event_time - timedelta(minutes=config.ALERT_MINUTES_BEFORE)
        if not _within_tolerance(now_utc, target):
            continue
        local_time_str = event_time.astimezone(tz).strftime("%H:%M")
        lines = [f"⏰ <b>News in {config.ALERT_MINUTES_BEFORE} min ({local_time_str})</b>\n"]
        lines += [format_event_line(e, tz) for e in group]
        base_text = "\n\n".join(lines)
        for chat_id in recipients:
            send_telegram_message(with_rules(base_text, users, chat_id), chat_id=chat_id)


def send_all_todays_news_now(todays_events: list[dict], tz: ZoneInfo) -> None:
    """Used by the force-send test path — broadcast everything today, ignoring timing."""
    users = load_users()
    recipients = get_recipients(users)
    if not todays_events:
        base_text = "🔍 Force test — no high-impact news scheduled for today."
    else:
        lines = ["🔍 <b>Force test — today's high-impact news</b>\n"]
        lines += [format_event_line(e, tz) for e in todays_events]
        base_text = "\n\n".join(lines)
    for chat_id in recipients:
        send_telegram_message(with_rules(base_text, users, chat_id), chat_id=chat_id)


# --------------------------------------------------------------------------
# Stateless "is it time yet?" checks
# --------------------------------------------------------------------------
# Individual news releases can land on any quarter-hour (:00/:15/:30/:45),
# not just round hours, so this needs a tighter grid+tolerance than the old
# session-open-only design: every 5 min with 3 min tolerance guarantees any
# quarter-hour target is always within reach of the nearest check, with no
# double-fire risk (tolerance stays under half the interval).
TOLERANCE_MINUTES = 3


def _within_tolerance(now: datetime, target: datetime) -> bool:
    return abs((now - target).total_seconds()) <= TOLERANCE_MINUTES * 60


def digest_due(now_utc: datetime) -> bool:
    tz = ZoneInfo(config.LOCAL_TZ)
    now_local = now_utc.astimezone(tz)
    hour, minute = map(int, config.DAILY_DIGEST_TIME.split(":"))
    target_local = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return _within_tolerance(now_local, target_local)


# --------------------------------------------------------------------------
# Entry point — run once per invocation
# --------------------------------------------------------------------------
def main():
    now_utc = datetime.now(ZoneInfo("UTC"))
    log.info("Run check at %s UTC", now_utc.isoformat())

    tz = ZoneInfo(config.LOCAL_TZ)
    todays_events = get_todays_high_impact_events(tz)
    cache_calendar_to_kv(todays_events)

    force_send = str(config.FORCE_SEND).strip().lower() in ("1", "true", "yes")
    if force_send:
        log.info("FF_FORCE_SEND is set — sending digest and all of today's news now, ignoring time checks.")
        send_daily_digest(todays_events, tz)
        send_all_todays_news_now(todays_events, tz)
        return

    if digest_due(now_utc):
        send_daily_digest(todays_events, tz)

    send_pre_news_alerts(now_utc, todays_events, tz)


if __name__ == "__main__":
    main()
