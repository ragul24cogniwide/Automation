import os
import io
import json
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
import httpx
from sqlalchemy.orm import Session
from sqlalchemy import desc, func
from dotenv import load_dotenv

load_dotenv()

from models import EmailTriageRecord, MissedCallRecord, WhatsAppReminderRecord

logger = logging.getLogger("whatsapp_service")
logging.basicConfig(level=logging.INFO)

# Environment variables
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER", "whatsapp:+14155238886")

WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
WHATSAPP_ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN")

USER_WHATSAPP_NUMBER = os.getenv("USER_WHATSAPP_NUMBER")
DEFAULT_BRIDGE_URL = "https://automation-whatsapp-ueor.onrender.com"
WHATSAPP_BRIDGE_URL = os.getenv("WHATSAPP_BRIDGE_URL") or DEFAULT_BRIDGE_URL


async def send_whatsapp_message(to_number: str, message: str, remote_jid: Optional[str] = None) -> Dict[str, Any]:
    """
    Sends a WhatsApp message via:
    1. Baileys WhatsApp Bridge (Self-hosted, persistent in Neon DB, zero fees)
    2. Twilio WhatsApp Sandbox (if configured)
    3. Meta Cloud API (if configured)
    4. Dry-run fallback log
    """
    clean_to = to_number.strip() if to_number else ""
    if not clean_to and USER_WHATSAPP_NUMBER:
        clean_to = USER_WHATSAPP_NUMBER.strip()

    if not clean_to:
        logger.warning("No recipient phone number provided for WhatsApp message.")
        return {"status": "error", "message": "No recipient phone number specified."}

    # 1. Try Baileys Self-Hosted Bridge First (Avoids Twilio/Meta blockers)
    bridge_url = os.getenv("WHATSAPP_BRIDGE_URL") or WHATSAPP_BRIDGE_URL or DEFAULT_BRIDGE_URL
    if bridge_url:
        try:
            url = f"{bridge_url.rstrip('/')}/send"
            clean_digits = clean_to.replace("whatsapp:", "").replace("+", "").strip()
            payload = {"to": clean_digits or "self", "message": message}
            if remote_jid:
                payload["remote_jid"] = remote_jid
            async with httpx.AsyncClient(timeout=25.0) as client:
                res = await client.post(
                    url,
                    json=payload,
                )
                if res.status_code == 200:
                    logger.info(f"WhatsApp message dispatched via Baileys Bridge to {remote_jid or clean_digits or 'self'}")
                    return {"status": "success", "provider": "baileys_bridge", "response": res.json()}
                elif res.status_code == 503:
                    logger.warning(f"Baileys Bridge not ready (503): {res.text}")
                    return {"status": "error", "provider": "baileys_bridge", "detail": res.text}
                else:
                    logger.error(f"Baileys Bridge returned status {res.status_code}: {res.text}")
                    return {"status": "error", "provider": "baileys_bridge", "detail": res.text}
        except httpx.ConnectError as e:
            logger.warning(f"Baileys Bridge offline at {bridge_url}: {e}")
        except Exception as e:
            logger.warning(f"Error dispatching via Baileys Bridge: {e}")

    # Ensure format for Twilio (starts with 'whatsapp:')
    twilio_recipient = clean_to if clean_to.startswith("whatsapp:") else f"whatsapp:{clean_to}"
    # Raw digits for Meta API
    meta_recipient = clean_to.replace("whatsapp:", "").replace("+", "").strip()

    # 2. Try Twilio if credentials are set
    if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_ACCOUNT_SID != "your_twilio_account_sid":
        try:
            url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"
            async with httpx.AsyncClient(timeout=15.0) as client:
                res = await client.post(
                    url,
                    data={
                        "From": TWILIO_WHATSAPP_NUMBER,
                        "To": twilio_recipient,
                        "Body": message,
                    },
                    auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
                )
                if res.status_code in (200, 201):
                    logger.info(f"WhatsApp message dispatched via Twilio to {twilio_recipient}")
                    return {"status": "success", "provider": "twilio", "response": res.json()}
                else:
                    logger.error(f"Twilio WhatsApp dispatch failed ({res.status_code}): {res.text}")
                    return {"status": "error", "provider": "twilio", "detail": res.text}
        except Exception as e:
            logger.error(f"Exception sending Twilio WhatsApp message: {e}")
            return {"status": "error", "provider": "twilio", "error": str(e)}

    # 2. Try Meta Cloud API if configured
    if WHATSAPP_ACCESS_TOKEN and WHATSAPP_PHONE_NUMBER_ID:
        try:
            url = f"https://graph.facebook.com/v20.0/{WHATSAPP_PHONE_NUMBER_ID}/messages"
            payload = {
                "messaging_product": "whatsapp",
                "to": meta_recipient,
                "type": "text",
                "text": {"body": message},
            }
            async with httpx.AsyncClient(timeout=15.0) as client:
                res = await client.post(
                    url,
                    json=payload,
                    headers={"Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}"},
                )
                if res.status_code in (200, 201):
                    logger.info(f"WhatsApp message dispatched via Meta API to {meta_recipient}")
                    return {"status": "success", "provider": "meta", "response": res.json()}
                else:
                    logger.error(f"Meta WhatsApp dispatch failed ({res.status_code}): {res.text}")
                    return {"status": "error", "provider": "meta", "detail": res.text}
        except Exception as e:
            logger.error(f"Exception sending Meta WhatsApp message: {e}")
            return {"status": "error", "provider": "meta", "error": str(e)}

    # 3. Dry-run fallback mode (keys not set yet)
    logger.info(f"[WHATSAPP DRY-RUN to {clean_to}]:\n{message}")
    return {
        "status": "dry_run",
        "notice": "WhatsApp credentials not set in .env. Message logged to console.",
        "recipient": clean_to,
        "preview": message,
    }


async def download_whatsapp_audio(media_url: str) -> Optional[bytes]:
    """Downloads voice note / audio payload from Twilio or Meta media URL."""
    try:
        auth = None
        headers = {}
        if "twilio.com" in media_url and TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
            auth = (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        elif "facebook.com" in media_url or "fbcdn.net" in media_url:
            if WHATSAPP_ACCESS_TOKEN:
                headers["Authorization"] = f"Bearer {WHATSAPP_ACCESS_TOKEN}"

        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            res = await client.get(media_url, auth=auth, headers=headers)
            if res.status_code == 200:
                return res.content
            logger.error(f"Audio download failed ({res.status_code}) from {media_url}")
            return None
    except Exception as e:
        logger.error(f"Error downloading WhatsApp audio: {e}")
        return None


async def transcribe_audio_bytes(audio_bytes: bytes, mime_type: str = "audio/ogg") -> str:
    """
    Transcribes audio bytes into text using Google Gemini or OpenAI/Whisper.
    """
    # 1. Try Gemini GenAI API
    gemini_key = os.getenv("GEMINI_API_KEY")
    if gemini_key and gemini_key != "your_gemini_api_key_here":
        try:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=gemini_key)
            prompt = "Transcribe this voice note exactly as spoken. Return only the raw transcription without commentary."
            response = client.models.generate_content(
                model="gemini-2.0-flash",
                contents=[
                    types.Part.from_bytes(data=audio_bytes, mime_type=mime_type),
                    prompt,
                ],
            )
            transcript = response.text.strip()
            if transcript:
                logger.info(f"Gemini audio transcription successful: {transcript[:50]}...")
                return transcript
        except Exception as e:
            logger.warning(f"Gemini transcription failed: {e}")

    # 2. Try OpenAI Whisper if OpenAI key is present
    openai_key = os.getenv("OPENAI_API_KEY")
    if openai_key and openai_key != "your_openai_api_key_here":
        try:
            from openai import AsyncOpenAI
            client = AsyncOpenAI(api_key=openai_key)
            audio_file = io.BytesIO(audio_bytes)
            audio_file.name = "voicenote.ogg"
            transcription = await client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
            )
            if transcription.text:
                return transcription.text.strip()
        except Exception as e:
            logger.warning(f"OpenAI Whisper transcription failed: {e}")

    # 3. Fallback placeholder
    return "(Voice note received. Audio transcription requires GEMINI_API_KEY or OPENAI_API_KEY in .env)"


def generate_daily_briefing_text(db: Session, user_name: str = "Ragul") -> str:
    """
    Generates a crisp, beautifully formatted WhatsApp markdown daily executive briefing
    from recent email triage, missed calls, and scheduled reminders.
    """
    # 1. Urgent Emails
    urgent_emails = (
        db.query(EmailTriageRecord)
        .filter(EmailTriageRecord.priority == "URGENT", EmailTriageRecord.status != "ARCHIVED")
        .order_by(desc(EmailTriageRecord.created_at))
        .limit(5)
        .all()
    )

    # 2. Triaged count in past 24 hours
    since_yesterday = datetime.utcnow() - timedelta(hours=24)
    total_triaged_24h = (
        db.query(EmailTriageRecord)
        .filter(EmailTriageRecord.created_at >= since_yesterday)
        .count()
    )

    # 3. Missed Calls in past 24 hours
    missed_calls = (
        db.query(MissedCallRecord)
        .filter(MissedCallRecord.created_at >= since_yesterday)
        .order_by(desc(MissedCallRecord.created_at))
        .limit(5)
        .all()
    )

    # 4. Reminders scheduled for today
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = today_start + timedelta(hours=24)
    today_reminders = (
        db.query(WhatsAppReminderRecord)
        .filter(
            WhatsAppReminderRecord.status == "PENDING",
            WhatsAppReminderRecord.remind_at >= today_start,
            WhatsAppReminderRecord.remind_at <= today_end,
        )
        .order_by(WhatsAppReminderRecord.remind_at)
        .all()
    )

    lines = []
    lines.append(f"☀️ *Good morning, {user_name}!*")
    lines.append(f"Here is your *Sage Executive Briefing* for today:\n")

    # Section 1: Urgent Action Items
    if urgent_emails:
        lines.append(f"🚨 *URGENT ACTION ITEMS ({len(urgent_emails)}):*")
        for idx, email in enumerate(urgent_emails, 1):
            sender_clean = email.sender.split("<")[0].replace('"', '').strip()
            lines.append(f"{idx}. *{email.subject}*")
            lines.append(f"   👤 From: {sender_clean}")
            if email.summary:
                summary_brief = email.summary.split(".")[0]
                lines.append(f"   💡 {summary_brief}.")
        lines.append("")
    else:
        lines.append("✓ *Inbox Priority:* All urgent emails resolved! No critical blockers.\n")

    # Section 2: General Inbox Stats
    lines.append(f"✉️ *INBOX STATUS:*")
    lines.append(f"• {total_triaged_24h} emails triaged by Sage in the last 24h.")
    lines.append("")

    # Section 3: Calls & SMS
    if missed_calls:
        lines.append(f"📞 *COMMUNICATIONS & CALLS ({len(missed_calls)}):*")
        for c in missed_calls:
            caller_display = c.caller_name if c.caller_name and c.caller_name.lower() != "unknown" else c.caller_number
            sms_status = "✓ Carrier SMS sent" if c.status == "SENT" else "Draft ready"
            lines.append(f"• {caller_display} ({c.caller_number}) — {sms_status}")
        lines.append("")
    else:
        lines.append("📞 *COMMUNICATIONS:* No missed calls overnight.\n")

    # Section 4: Reminders for today
    if today_reminders:
        lines.append(f"⏰ *SCHEDULED REMINDERS ({len(today_reminders)}):*")
        for r in today_reminders:
            time_str = r.remind_at.strftime("%I:%M %p")
            lines.append(f"• {time_str}: {r.reminder_text}")
        lines.append("")

    lines.append("━━━━━━━━━━━━━━━━━━━")
    lines.append("💬 _Tip: Send me a voice note or text anytime to set a reminder or ask for an update!_")

    return "\n".join(lines)
