#!/usr/bin/env python3
"""
Sidor Avoda Maor — Task & Calendar Sync
- tasks  → Google Tasks
- events → Google Calendar
Runs twice daily via GitHub Actions (10:00 & 22:00 Israel time).
"""

import os
import json
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

Extract ALL actionable items (tasks and events) from the message.

Rules:
- "task" = to-do without a specific hour (e.g., "לקנות חלב") -> date = today unless specified. start_time = null, end_time = null.
- "event" = has a specific hour (e.g., "אימון ב-6 בבוקר") -> extract date and time. Assume 1 hour duration if no end time.
- "ignore" = greeting, non-actionable text.

You MUST return ONLY a valid JSON object with a single key "items", containing an array of objects.
Example output:
{{
  "items": [
    {{
      "type": "task",
      "title": "ללכת לקנות מוצרי חלבון",
      "date": "{today}",
      "start_time": null,
      "end_time": null
    }},
    {{
      "type": "event",
      "title": "פגישה עם חבר שלי יוסי",
      "date": "{today}",
      "start_time": "19:00",
      "end_time": "20:00"
    }}
  ]
}}

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
        response_format={ "type": "json_object" }, # שדרוג: מכריח את המודל להחזיר JSON תקין תמיד
        messages=[{"role": "user", "content": prompt}],
        max_tokens=1500, # שדרוג: מאפשר עיבוד של הודעות ארוכות מאוד
        temperature=0.1,
    )
    
    raw = response.choices[0].message.content.strip()
    result = json.loads(raw)
    return result.get("items", [])

# ── Google Calendar (Service Account) ────────────────────────────────────────

def _calendar_service():
    info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = SACredentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/calendar"]
    )
    return build("calendar", "v3", credentials=creds)

def _parse_date(date_str: str) -> str:
    if not date_str or "/" not in date_str:
        date_str = datetime.now(ISRAEL_TZ).strftime("%d/%m/%Y")
    parts = date_str.split("/")
    day, month = parts[0], parts[1]
    year = parts[2] if len(parts) == 3 else str(datetime.now().year)
    return f"{'20'+year if len(year)==2 else year}-{month.zfill(2)}-{day.zfill(2)}"

def add_calendar_event(parsed: dict) -> str:
    service = _calendar_service()
    date_iso = _parse_date(parsed.get("date", ""))
    
    start_time = parsed.get("start_time") or "09:00"
    start_dt = ISRAEL_TZ.localize(
        datetime.strptime(f"{date_iso} {start_time}", "%Y-%m-%d %H:%M")
    )
    
    end_time = parsed.get("end_time")
    if end_time:
        end_dt = ISRAEL_TZ.localize(datetime.strptime(f"{date_iso} {end_time}", "%Y-%m-%d %H:%M"))
    else:
        end_dt = start_dt + timedelta(hours=1)
        
    event_body = {
        "summary": f"📅 {parsed.get('title', 'אירוע ללא שם')}",
        "start": {"dateTime": start_dt.isoformat(), "timeZone": "Asia/Jerusalem"},
        "end":   {"dateTime": end_dt.isoformat(),   "timeZone": "Asia/Jerusalem"},
        "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 30}]},
    }
    service.events().insert(calendarId=CALENDAR_ID, body=event_body).execute()
    return f"{parsed.get('date')} {start_time}–{end_dt.strftime('%H:%M')}"

# ── Google Tasks (OAuth2) ─────────────────────────────────────────────────────

def _tasks_service():
    tasks_creds_json = os.environ.get("GOOGLE_TASKS_CREDENTIALS")
    if not tasks_creds_json:
        raise ValueError("Missing GOOGLE_TASKS_CREDENTIALS environment variable.")
        
    tasks_info = json.loads(tasks_creds_json)
    
    creds = OAuthCredentials(
        token=None,
        refresh_token=tasks_info["refresh_token"],
        client_id=tasks_info["client_id"],
        client_secret=tasks_info["client_secret"],
        token_uri=tasks_info.get("token_uri", "https://oauth2.googleapis.com/token"),
        scopes=["https://www.googleapis.com/auth/tasks"],
    )
    creds.refresh(Request())
    return build("tasks", "v1", credentials=creds)

def add_task(parsed: dict) -> str:
    service = _tasks_service()
    date_iso = _parse_date(parsed.get("date", ""))
    due_rfc = f"{date_iso}T00:00:00.000Z"
    task_body = {
        "title": parsed.get("title", "משימה ללא שם"),
        "due": due_rfc,
    }
    service.tasks().insert(tasklist="@default", body=task_body).execute()
    return f"{parsed.get('date')} — Google Tasks"

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

        logger.info(f"Processing message...")
        try:
            items = parse_message(text)
        except Exception as e:
            logger.error(f"Parse failed: {e}")
            failed.append(f'• תגובה שגויה מהבינה המלאכותית: {e}')
            continue

        for parsed in items:
            # שדרוג: שימוש ב-get כדי למנוע קריסה אם המודל החסיר שדה
            item_type = parsed.get("type", "task")
            if item_type == "ignore":
                continue
                
            title = parsed.get("title", "משימה ללא שם")
            parsed["title"] = title 
            
            # אם זה אירוע אבל אין לו שעה, נהפוך אותו למשימה כדי שלא יקרוס
            if item_type == "event" and not parsed.get("start_time"):
                item_type = "task"
                
            try:
                if item_type == "task":
                    when = add_task(parsed)
                    added.append(f'✅ "{title}"')
                    logger.info(f"Task added: {title}")
                else:
                    when = add_calendar_event(parsed)
                    added.append(f'📅 "{title}" — {when.split("—")[0]}')
                    logger.info(f"Event added: {title}")
            except Exception as e:
                logger.error(f"Failed {title}: {e}")
                failed.append(f'• "{title}" — שגיאת התחברות לגוגל')

    acknowledge_updates(updates[-1]["update_id"])

    total = len(messages) - len(ignored)
    lines = [f"✅ <b>עיבדתי בהצלחה הודעה עם {len(added) + len(failed)} פריטים:</b>\n"]
    if added:
        lines.append("➕ <b>נוספו:</b>")
        lines.extend(added)
    if failed:
        lines.append("\n⚠️ <b>נכשל:</b>")
        lines.extend(failed)

    send_telegram("\n".join(lines))
    logger.info("Done.")

if __name__ == "__main__":
    main()
