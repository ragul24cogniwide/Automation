import os
import json
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, Optional
import pytz
from dateutil import parser as date_parser

from sqlalchemy.orm import Session
from sqlalchemy import text, desc
from database import SessionLocal
from models import WhatsAppReminderRecord
from whatsapp_service import send_whatsapp_message, generate_daily_briefing_text

logger = logging.getLogger("reminder_engine")
logging.basicConfig(level=logging.INFO)

# User's local timezone (Default: IST / Asia/Kolkata)
USER_TIMEZONE = os.getenv("USER_TIMEZONE", "Asia/Kolkata")
USER_WHATSAPP_NUMBER = os.getenv("USER_WHATSAPP_NUMBER")
DAILY_BRIEFING_HOUR = int(os.getenv("DAILY_BRIEFING_HOUR", "8"))
DAILY_BRIEFING_MINUTE = int(os.getenv("DAILY_BRIEFING_MINUTE", "30"))

last_briefing_date: Optional[str] = None


def get_current_local_time() -> datetime:
    """Returns current datetime in user's configured timezone (e.g. Asia/Kolkata)."""
    try:
        tz = pytz.timezone(USER_TIMEZONE)
        return datetime.now(tz)
    except Exception:
        return datetime.utcnow() + timedelta(hours=5, minutes=30)


async def parse_whatsapp_intent(user_input: str, is_voice: bool = False) -> Dict[str, Any]:
    """
    Uses DeepSeek / Gemini to parse incoming WhatsApp text or voice note transcript.
    Classifies intent: REMINDER, BRIEFING, LIST_REMINDERS, or GENERAL_CHAT.
    """
    clean_text = user_input.strip()
    now_local = get_current_local_time()
    now_str = now_local.strftime("%Y-%m-%d %H:%M:%S (%A, %Z)")

    # Fast keyword shortcut for briefing
    lower = clean_text.lower()
    if lower in ("briefing", "summary", "today", "today's work", "todays work", "/briefing", "update"):
        return {
            "intent": "BRIEFING",
            "reply": "Generating your executive briefing...",
        }

    if lower in ("reminders", "my reminders", "list reminders", "show reminders", "/reminders"):
        return {
            "intent": "LIST_REMINDERS",
            "reply": "Fetching your active reminders...",
        }

    # Use LLM (DeepSeek or Gemini) for smart NLP parsing of reminders & questions
    deepseek_key = os.getenv("DEEPSEEK_API_KEY")
    gemini_key = os.getenv("GEMINI_API_KEY")

    system_prompt = f"""You are Sage's Intelligent WhatsApp Assistant for Ragul.
The current local time is: {now_str}.

Analyze the user's message (which may be a typed text or transcribed voice note) and output STRICT JSON:
{{
  "intent": "REMINDER" | "BRIEFING" | "LIST_REMINDERS" | "GENERAL_CHAT",
  "is_reminder": true/false,
  "reminder_text": "Clean, actionable description of the task to be reminded about",
  "remind_at_iso": "YYYY-MM-DDTHH:MM:SS in local time (resolve relative times like 'in 20 minutes', 'at 5pm', 'tomorrow at 9:30 AM')",
  "reply": "Friendly, concise WhatsApp response with emojis confirming the action or answering the query"
}}

Rules:
1. If the user mentions any task, reminder, follow-up, or schedule, set intent="REMINDER" and compute the exact 'remind_at_iso'.
2. If no specific time is mentioned for a task, default to 1 hour from current time or 09:00 AM the next day if it is late evening.
3. If intent="REMINDER", ensure "reply" confirms the task name and the exact human-readable time you set.
4. If intent="GENERAL_CHAT", provide a helpful, polite executive assistant response.
5. Return ONLY raw valid JSON, no markdown codeblocks."""

    # 1. Try OpenAI (ChatGPT)
    openai_key = os.getenv("OPENAI_API_KEY")
    if openai_key and openai_key != "your_openai_api_key_here":
        try:
            from openai import AsyncOpenAI
            client = AsyncOpenAI(api_key=openai_key)
            response = await client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": clean_text},
                ],
                temperature=0.1,
            )
            content = response.choices[0].message.content.strip()
            if content.startswith("```json"):
                content = content[7:]
            if content.startswith("```"):
                content = content[3:]
            if content.endswith("```"):
                content = content[:-3]
            parsed = json.loads(content.strip())
            return parsed
        except Exception as e:
            logger.warning(f"OpenAI intent parsing failed: {e}")

    # 2. Try DeepSeek
    if deepseek_key and deepseek_key != "your_deepseek_api_key_here":
        try:
            from openai import AsyncOpenAI
            client = AsyncOpenAI(api_key=deepseek_key, base_url="https://api.deepseek.com")
            response = await client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": clean_text},
                ],
                temperature=0.1,
            )
            content = response.choices[0].message.content.strip()
            # Clean markdown wrappers if any
            if content.startswith("```json"):
                content = content[7:]
            if content.startswith("```"):
                content = content[3:]
            if content.endswith("```"):
                content = content[:-3]
            parsed = json.loads(content.strip())
            return parsed
        except Exception as e:
            logger.warning(f"DeepSeek intent parsing failed: {e}")

    # 3. Try Gemini
    if gemini_key and gemini_key != "your_gemini_api_key_here":
        try:
            from google import genai
            client = genai.Client(api_key=gemini_key)
            res = client.models.generate_content(
                model="gemini-2.0-flash",
                contents=f"{system_prompt}\n\nUser Input: {clean_text}",
            )
            content = res.text.strip()
            if content.startswith("```json"):
                content = content[7:]
            if content.startswith("```"):
                content = content[3:]
            if content.endswith("```"):
                content = content[:-3]
            parsed = json.loads(content.strip())
            return parsed
        except Exception as e:
            logger.warning(f"Gemini intent parsing failed: {e}")

    # 3. Rule-based fallback if LLM is unavailable
    default_remind_time = (now_local + timedelta(hours=1)).isoformat()
    return {
        "intent": "REMINDER",
        "is_reminder": True,
        "reminder_text": clean_text,
        "remind_at_iso": default_remind_time,
        "reply": f"✓ Saved reminder: *{clean_text}* (scheduled in 1 hour).",
    }


def save_reminder_to_db(
    user_phone: str,
    reminder_text: str,
    remind_at_iso: str,
    raw_input: str,
    is_voice: bool,
    db: Session,
) -> Optional[WhatsAppReminderRecord]:
    """Persists a new scheduled reminder into the database."""
    try:
        # Parse ISO string
        local_tz = pytz.timezone(USER_TIMEZONE)
        parsed_dt = date_parser.parse(remind_at_iso)
        if parsed_dt.tzinfo is None:
            # Assume user timezone if naive
            parsed_dt = local_tz.localize(parsed_dt)

        # Convert to UTC for database storage
        utc_dt = parsed_dt.astimezone(pytz.utc)

        record = WhatsAppReminderRecord(
            user_phone=user_phone,
            reminder_text=reminder_text,
            raw_input=raw_input,
            is_voice=is_voice,
            remind_at=utc_dt,
            status="PENDING",
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        logger.info(f"Saved WhatsApp reminder #{record.id} for {user_phone} at {utc_dt}")
        return record
    except Exception as e:
        logger.error(f"Failed to save reminder to DB: {e}")
        db.rollback()
        return None


async def reminder_scheduler_loop():
    """
    Background worker that runs continuously every 30 seconds:
    1. Checks for due reminders and dispatches WhatsApp alerts.
    2. Checks if it is 8:30 AM local time and dispatches the Daily Morning Briefing once per day.
    """
    global last_briefing_date
    logger.info("Sage WhatsApp Reminder & Briefing Scheduler started.")

    while True:
        try:
            now_utc = datetime.now(pytz.utc)
            now_local = get_current_local_time()
            today_date_str = now_local.strftime("%Y-%m-%d")

            # --- A. Check Due Reminders ---
            with SessionLocal() as db:
                due_reminders = (
                    db.query(WhatsAppReminderRecord)
                    .filter(
                        WhatsAppReminderRecord.status == "PENDING",
                        WhatsAppReminderRecord.remind_at <= now_utc,
                    )
                    .all()
                )

                for r in due_reminders:
                    logger.info(f"Triggering due reminder #{r.id}: {r.reminder_text}")
                    msg = (
                        f"⏰ *REMINDER FROM SAGE*\n\n"
                        f"📌 *{r.reminder_text}*\n\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"✓ _Marked as completed. Have you finished this?_"
                    )
                    await send_whatsapp_message(r.user_phone, msg)
                    r.status = "SENT"
                    db.commit()

            # --- B. Check Daily Morning Briefing (8:30 AM IST) ---
            target_hour = DAILY_BRIEFING_HOUR
            target_min = DAILY_BRIEFING_MINUTE

            if (
                now_local.hour == target_hour
                and now_local.minute >= target_min
                and last_briefing_date != today_date_str
            ):
                if USER_WHATSAPP_NUMBER:
                    logger.info(f"Dispatching scheduled Daily Briefing to {USER_WHATSAPP_NUMBER}...")
                    with SessionLocal() as db:
                        briefing_text = generate_daily_briefing_text(db, user_name="Ragul")
                        await send_whatsapp_message(USER_WHATSAPP_NUMBER, briefing_text)
                    last_briefing_date = today_date_str
                    logger.info(f"Daily Briefing sent successfully for {today_date_str}")

        except Exception as e:
            logger.error(f"Error in reminder_scheduler_loop: {e}")

        await asyncio.sleep(30)
