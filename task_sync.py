#!/usr/bin/env python3
"""
Sidor Avoda Maor — Task & Calendar Sync
- tasks  → Google Tasks
- events → Google Calendar
Runs twice daily via GitHub Actions (10:00 & 22:00 Israel time).
"""

import os
import json
import re
import logging
from datetime import datetime, timedelta
import pytz
import requests
from openai import OpenAI
from google.oauth2.service_account import Credentials as SACredentials
from google.oauth2.credentials import Credentials as OAuthCredentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logger = logging.getLogger("task_sync")

ISRAEL_TZ   = pytz.timezone("Asia/Jerusalem")
BOT_TOKEN   = os.environ["TELEGRAM_TOKEN"]
CHAT_ID     = os.environ["CHAT_ID"]
CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "maorfurman123@gmail.com")

openai_client = OpenAI(api_key=os.environ["OPEN_API_KEY"])
TELEGRAM_API  = f"https://api.telegram.org/bot{BOT_TOKEN}"

PARSE_PROMPT = """You are a Hebrew personal assistant. The user sent you a message in Hebrew.
Today is {today} (DD/MM/YYYY). Current time (Israel): {time}.

The message may contain ONE or MULTIPLE tasks/events.
Extract ALL actionable items.

Rules:
- "task" = to-do without a specific time → date = today unless "מחר"/specific date mentioned
- "event" = has a specific time → extract date+time; assume 1hr if no end time
- "ignore" = greeting, question, command, or not actionable

Return ONLY a valid JSON array:
[
  {{
    "type": "task|event|ignore",
    "title": "short Hebrew title",
    "date": "DD/MM/YYYY",
    "start_time": "HH:MM or null",
    "end_time": "HH:MM or null"
  }}
]

Message:
{message}
"""


# ── Telegram ──────────────────────────────────────────────────────────────────

def get_pending_updates() -> list[dict]:
    resp = requests.get(f"{TELEGRAM_API}/getUpdates",
                        params={"limit": 100, "timeout": 0}, timeout=15)
    resp.raise_for_status()
    return resp.json().get("result", [])


def acknowledge_updates(last_id: int):
    requests.get(f"{TELEGRAM_API}/getUpdates",
                 params={"offset": last_id + 1, "limit": 1, "timeout": 0}, timeout=15)


def send_telegram(text: str):
    requests.post(f"{TELEGRAM_API}/sendMessage",
                  json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
                  timeout=15).raise_for_status()


# ── GPT-4o Parsing ────────────────────────────────────────────────────────────

def parse_message(text: str) -> list[dict]:
    now = datetime.now(ISRAEL_TZ)
    prompt = PARSE_PROMPT.format(
        today=now.strftime("%d/%m/%Y"),
        time=now.strftime("%H:%M"),
        message=text,
    )
    response = openai_client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=512,
        temperature=0.1,
    )
    raw = response.choices[0].message.content.strip()
    raw = re.sub(r"^```[a-z]*\n?", "", raw).rstrip("`").strip()
    result = json.loads(raw)
    return result if isinstance(result, list) else [result]


# ── Google Calendar (Service Account) ────────────────────────────────────────

def _calendar_service():
    info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = SACredentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/calendar"]
    )
    return build("calendar", "v3", credentials=creds)


def _parse_date(date_str: str) -> str:
    parts = date_str.split("/")
    day, month = parts[0], parts[1]
    year = parts[2] if len(parts) == 3 else str(datetime.now().year)
    return f"{'20'+year if len(year)==2 else year}-{month.zfill(2)}-{day.zfill(2)}"


def add_calendar_event(parsed: dict) -> str:
    service = _calendar_service()
    date_iso = _parse_date(parsed["date"])
    start_dt = ISRAEL_TZ.localize(
        datetime.strptime(f"{date_iso} {parsed['start_time']}", "%Y-%m-%d %H:%M")
    )
    end_dt = (
        ISRAEL_TZ.localize(datetime.strptime(f"{date_iso} {parsed['end_time']}", "%Y-%m-%d %H:%M"))
        if parsed.get("end_time")
        else start_dt + timedelta(hours=1)
    )
    event_body = {
        "summary": f"📅 {parsed['title']}",
        "start": {"dateTime": start_dt.isoformat(), "timeZone": "Asia/Jerusalem"},
        "end":   {"dateTime": end_dt.isoformat(),   "timeZone": "Asia/Jerusalem"},
        "reminders": {"useDefault": False,
                      "overrides": [{"method": "popup", "minutes": 30}]},
    }
    service.events().insert(calendarId=CALENDAR_ID, body=event_body).execute()
    return f"{parsed['date']} {parsed['start_time']}–{end_dt.strftime('%H:%M')}"


# ── Google Tasks (OAuth2) ─────────────────────────────────────────────────────

def _tasks_service():
    creds = OAuthCredentials(
        token=None,
        refresh_token=os.environ["GOOGLE_TASKS_REFRESH_TOKEN"],
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/tasks"],
    )
    creds.refresh(Request())
    logger.info("Google Tasks token refreshed OK")
    return build("tasks", "v1", credentials=creds)


def add_task(parsed: dict) -> str:
    service = _tasks_service()
    date_iso = _parse_date(parsed["date"])
    due_rfc = f"{date_iso}T00:00:00.000Z"
    task_body = {
        "title": parsed["title"],
        "due": due_rfc,
    }
    service.tasks().insert(tasklist="@default", body=task_body).execute()
    return f"{parsed['date']} — Google Tasks"


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    updates = get_pending_updates()
    messages = [
        u for u in updates
        if u.get("message", {}).get("text")
        and str(u["message"]["chat"]["id"]) == str(CHAT_ID)
    ]

    if not messages:
        logger.info("No pending messages.")
        return

    added, failed, ignored = [], [], []

    for update in messages:
        text = update["message"]["text"]
        if text.startswith("/"):
            ignored.append(text)
            continue

        logger.info(f"Processing: {text!r}")
        try:
            items = parse_message(text)
        except Exception as e:
            logger.error(f"Parse failed: {e}")
            failed.append(f'• "{text[:40]}" — שגיאת ניתוח')
            continue

        for parsed in items:
            if parsed.get("type") == "ignore":
                continue
            try:
                if parsed["type"] == "task":
                    when = add_task(parsed)
                    added.append(f'✅ "{parsed["title"]}" — {when}')
                    logger.info(f"Task added: {parsed['title']}")
                else:
                    when = add_calendar_event(parsed)
                    added.append(f'📅 "{parsed["title"]}" — {when}')
                    logger.info(f"Event added: {parsed['title']}")
            except Exception as e:
                logger.error(f"Failed {parsed.get('title')}: {e}")
                failed.append(f'• "{parsed.get("title","?")}" — {str(e)[:80]}')

    acknowledge_updates(updates[-1]["update_id"])

    total = len(messages) - len(ignored)
    lines = [f"✅ <b>עיבדתי {total} הודעות:</b>\n"]
    if added:
        lines.append("➕ <b>נוספו:</b>")
        lines.extend(added)
    if failed:
        lines.append("\n⚠️ <b>נכשל:</b>")
        lines.extend(failed)
    if not added and not failed:
        lines.append("לא היו הודעות לעיבוד.")

    send_telegram("\n".join(lines))
    logger.info("Done.")


if __name__ == "__main__":
    main()
