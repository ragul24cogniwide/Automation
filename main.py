import os
import json
import asyncio
from contextlib import asynccontextmanager
from typing import Optional, List
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException, Depends, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import text, desc
from dotenv import load_dotenv

from database import engine, get_db, init_db
from models import EmailTriageRecord, MissedCallRecord, SmsMessageRecord, WhatsAppReminderRecord
from whatsapp_service import (
    send_whatsapp_message,
    download_whatsapp_audio,
    transcribe_audio_bytes,
    generate_daily_briefing_text,
)
from reminder_engine import (
    parse_whatsapp_intent,
    save_reminder_to_db,
    reminder_scheduler_loop,
    get_current_local_time,
)

load_dotenv()

# DeepSeek setup (Primary LLM)
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
deepseek_client = None
if DEEPSEEK_API_KEY and DEEPSEEK_API_KEY != "your_deepseek_api_key_here":
    try:
        from openai import OpenAI
        deepseek_client = OpenAI(
            api_key=DEEPSEEK_API_KEY,
            base_url="https://api.deepseek.com"
        )
        print("DeepSeek client initialized successfully.")
    except Exception as e:
        print(f"Warning: Could not initialize DeepSeek client: {e}")

# Gemini setup (Fallback LLM)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
gemini_client = None
if GEMINI_API_KEY and GEMINI_API_KEY != "your_gemini_api_key_here":
    try:
        from google import genai
        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    except Exception as e:
        print(f"Warning: Could not initialize Gemini client: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize database tables on server startup
    scheduler_task = None
    try:
        init_db()
        with engine.connect() as conn:
            conn.execute(text("ALTER TABLE missed_calls ADD COLUMN IF NOT EXISTS call_type VARCHAR(50) DEFAULT 'MISSED';"))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS sms_messages (
                    id SERIAL PRIMARY KEY,
                    phone_number VARCHAR(50) NOT NULL,
                    contact_name VARCHAR(255) DEFAULT 'Unknown',
                    role VARCHAR(20) NOT NULL,
                    message TEXT NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                );
            """))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_sms_messages_phone_number ON sms_messages(phone_number);"))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS whatsapp_reminders (
                    id SERIAL PRIMARY KEY,
                    user_phone VARCHAR(50) NOT NULL,
                    reminder_text TEXT NOT NULL,
                    raw_input TEXT,
                    is_voice BOOLEAN DEFAULT FALSE,
                    remind_at TIMESTAMPTZ NOT NULL,
                    status VARCHAR(20) DEFAULT 'PENDING',
                    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                );
            """))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_whatsapp_reminders_status ON whatsapp_reminders(status);"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_whatsapp_reminders_remind_at ON whatsapp_reminders(remind_at);"))
            conn.commit()
        print("Neon PostgreSQL tables verified successfully.")

        # Start background reminder scheduler & morning briefing loop
        scheduler_task = asyncio.create_task(reminder_scheduler_loop())
    except Exception as e:
        print(f"Database initialization warning: {e}")
    yield
    if scheduler_task:
        scheduler_task.cancel()


app = FastAPI(
    title="Sage AI Personal Agent Router",
    description="Automated Email Triage & Missed Call SMS Agent powered by Gemini, DeepSeek & Neon PostgreSQL",
    version="1.0.0",
    lifespan=lifespan
)

# CORS configuration for React Native mobile app and web clients
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Request/Response Schemas ---
class EmailPayload(BaseModel):
    id: str
    sender: str
    subject: Optional[str] = "(No Subject)"
    snippet: Optional[str] = ""
    body: Optional[str] = None
    account_email: Optional[str] = None



class EmailStatusUpdate(BaseModel):
    status: Optional[str] = None
    action_required: Optional[bool] = None


class MissedCallPayload(BaseModel):
    caller_number: str
    caller_name: Optional[str] = "Unknown"
    missed_at: Optional[str] = "Just now"
    call_type: Optional[str] = "MISSED"  # MISSED or DECLINED


class MissedCallStatusUpdate(BaseModel):
    status: str


class SmsChatPayload(BaseModel):
    phone_number: str
    contact_name: Optional[str] = "Unknown"
    message: str


# --- Endpoints ---

@app.get("/")
def root():
    return {
        "service": "Sage AI Personal Agent Router",
        "status": "online",
        "endpoints": {
            "ping": "/api/ping",
            "health": "/api/health",
            "stats": "/api/stats",
            "triage_email": "/api/triage-email",
            "emails": "/api/emails",
            "missed_call": "/api/missed-call",
            "missed_calls": "/api/missed-calls",
        }
    }


@app.get("/api/ping")
def ping():
    """Ultra-fast keepalive ping endpoint to prevent Render spin-down."""
    return {
        "status": "ok",
        "service": "Sage AI Personal Agent Router",
        "timestamp": datetime.utcnow().isoformat()
    }


@app.get("/api/health")
def health_check(db: Session = Depends(get_db)):
    """Health check validating server status and Neon PostgreSQL database connectivity."""
    db_status = "disconnected"
    try:
        db.execute(text("SELECT 1"))
        db_status = "connected"
    except Exception as e:
        db_status = f"error: {str(e)}"

    return {
        "status": "online",
        "database": db_status,
        "deepseek_configured": deepseek_client is not None,
        "gemini_configured": gemini_client is not None,
        "active_model": "deepseek-chat" if deepseek_client else ("gemini-2.5-flash" if gemini_client else "rule-based-fallback"),
        "timestamp": datetime.utcnow().isoformat()
    }


@app.post("/api/triage-email")
async def triage_email(data: EmailPayload, db: Session = Depends(get_db)):
    """
    Triages an incoming email via Gemini and persists the record into Neon PostgreSQL.
    Deduplicates incoming Gmail messages by message ID.
    """
    # 1. Deduplication check
    existing_record = db.query(EmailTriageRecord).filter(EmailTriageRecord.email_id == data.id).first()
    if existing_record:
        return {
            "email_id": existing_record.email_id,
            "priority": existing_record.priority,
            "summary": existing_record.summary,
            "action_required": existing_record.action_required,
            "suggested_reply": existing_record.suggested_reply,
            "status": existing_record.status,
            "created_at": existing_record.created_at.isoformat() if existing_record.created_at else None,
            "is_duplicate": True
        }

    # 2. Analyze with Gemini
    content_text = data.body if data.body else data.snippet
    prompt = f"""
    You are an executive personal assistant. Analyze the incoming email and return JSON with:
    - priority: "URGENT", "IMPORTANT", or "LOW"
    - summary: 1-2 sentence overview of what the sender needs
    - action_required: true or false
    - suggested_reply: A crisp, professional response draft if action_required is true, else empty string.

    Sender: {data.sender}
    Subject: {data.subject}
    Content: {content_text}
    """

    priority = "LOW"
    summary = data.snippet or "No preview available"
    action_required = False
    suggested_reply = ""

    if deepseek_client:
        try:
            completion = deepseek_client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {
                        "role": "system",
                        "content": "You are an executive personal assistant. Analyze the incoming email and return a valid JSON object with keys: priority ('URGENT', 'IMPORTANT', or 'LOW'), summary (1-2 sentence overview of what sender needs), action_required (boolean), suggested_reply (crisp professional response draft if action_required is true, else empty string)."
                    },
                    {
                        "role": "user",
                        "content": f"Sender: {data.sender}\nSubject: {data.subject}\nContent: {content_text}"
                    }
                ],
                response_format={"type": "json_object"},
                temperature=0.2,
            )
            parsed = json.loads(completion.choices[0].message.content)
            priority = parsed.get("priority", "LOW").upper()
            summary = parsed.get("summary", summary)
            action_required = bool(parsed.get("action_required", False))
            suggested_reply = parsed.get("suggested_reply", "")
        except Exception as e:
            print(f"DeepSeek triage error: {e}")
            summary = f"Auto-triaged: {summary[:100]}"
    elif gemini_client:
        try:
            from google.genai import types
            response = gemini_client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json"
                ),
            )
            parsed = json.loads(response.text)
            priority = parsed.get("priority", "LOW").upper()
            summary = parsed.get("summary", summary)
            action_required = bool(parsed.get("action_required", False))
            suggested_reply = parsed.get("suggested_reply", "")
        except Exception as e:
            print(f"Gemini triage error: {e}")
            summary = f"Auto-triaged: {summary[:100]}"
    else:
        # Fallback if GEMINI_API_KEY is not configured yet
        if any(keyword in (data.subject or "").lower() for keyword in ["urgent", "asap", "emergency", "action required"]):
            priority = "URGENT"
            action_required = True
            suggested_reply = "Hi, thank you for reaching out. I am reviewing your request urgently and will follow up shortly."
        elif any(keyword in (data.subject or "").lower() for keyword in ["meeting", "invoice", "schedule"]):
            priority = "IMPORTANT"
            action_required = True
            suggested_reply = "Hi, thank you for the update. I will review and get back to you soon."
        summary = f"Summary: {data.snippet[:150]}"

    # 3. Persist to Neon PostgreSQL
    new_email = EmailTriageRecord(
        email_id=data.id,
        account_email=data.account_email,
        sender=data.sender,
        subject=data.subject or "(No Subject)",
        snippet=data.snippet or "",
        body=data.body,
        priority=priority,
        summary=summary,
        action_required=action_required,
        suggested_reply=suggested_reply,
        status="NEW"
    )
    db.add(new_email)
    db.commit()
    db.refresh(new_email)

    return {
        "id": new_email.id,
        "email_id": new_email.email_id,
        "account_email": new_email.account_email,
        "sender": new_email.sender,
        "subject": new_email.subject,
        "priority": new_email.priority,
        "summary": new_email.summary,
        "action_required": new_email.action_required,
        "suggested_reply": new_email.suggested_reply,
        "status": new_email.status,
        "created_at": new_email.created_at.isoformat() if new_email.created_at else None,
        "is_duplicate": False
    }


@app.get("/api/dashboard/emails")
@app.get("/api/emails")
def get_emails(
    priority: Optional[str] = None,
    action_required: Optional[bool] = None,
    status: Optional[str] = None,
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db)
):
    """Retrieve triaged emails with optional filtering."""
    query = db.query(EmailTriageRecord)
    if priority:
        query = query.filter(EmailTriageRecord.priority == priority.upper())
    if action_required is not None:
        query = query.filter(EmailTriageRecord.action_required == action_required)
    if status:
        query = query.filter(EmailTriageRecord.status == status.upper())

    total = query.count()
    records = query.order_by(desc(EmailTriageRecord.created_at)).offset(offset).limit(limit).all()

    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "emails": [
            {
                "id": r.id,
                "email_id": r.email_id,
                "account_email": r.account_email,
                "sender": r.sender,
                "subject": r.subject,
                "snippet": r.snippet,
                "priority": r.priority,
                "summary": r.summary,
                "action_required": r.action_required,
                "suggested_reply": r.suggested_reply,
                "status": r.status,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in records
        ]
    }


@app.get("/api/emails/{email_id}")
def get_email_detail(email_id: str, db: Session = Depends(get_db)):
    """Retrieve details for a specific email by database ID or Gmail message ID."""
    record = None
    if email_id.isdigit():
        record = db.query(EmailTriageRecord).filter(EmailTriageRecord.id == int(email_id)).first()
    if not record:
        record = db.query(EmailTriageRecord).filter(EmailTriageRecord.email_id == email_id).first()

    if not record:
        raise HTTPException(status_code=404, detail="Email record not found")

    return {
        "id": record.id,
        "email_id": record.email_id,
        "sender": record.sender,
        "subject": record.subject,
        "snippet": record.snippet,
        "body": record.body,
        "priority": record.priority,
        "summary": record.summary,
        "action_required": record.action_required,
        "suggested_reply": record.suggested_reply,
        "status": record.status,
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "updated_at": record.updated_at.isoformat() if record.updated_at else None,
    }


@app.patch("/api/emails/{email_id}")
def update_email_status(email_id: str, payload: EmailStatusUpdate, db: Session = Depends(get_db)):
    """Update status (e.g. READ, REPLIED, ARCHIVED) or action_required for an email."""
    record = None
    if email_id.isdigit():
        record = db.query(EmailTriageRecord).filter(EmailTriageRecord.id == int(email_id)).first()
    if not record:
        record = db.query(EmailTriageRecord).filter(EmailTriageRecord.email_id == email_id).first()

    if not record:
        raise HTTPException(status_code=404, detail="Email record not found")

    if payload.status is not None:
        record.status = payload.status.upper()
    if payload.action_required is not None:
        record.action_required = payload.action_required

    db.commit()
    return {"message": "Email updated successfully", "id": record.id, "status": record.status}


@app.post("/api/missed-call")
async def handle_missed_call(data: MissedCallPayload, db: Session = Depends(get_db)):
    """
    Handles a missed or cut/declined call event, generates a carrier SMS response via DeepSeek,
    and logs the call into Neon PostgreSQL.
    """
    call_type = (data.call_type or "MISSED").upper()

    has_name = bool(data.caller_name and data.caller_name.strip() and data.caller_name.strip().lower() != "unknown")
    caller_first_name = data.caller_name.strip() if has_name else ""
    salutation = f"Hi {caller_first_name}," if has_name else "Hi,"

    if call_type == "DECLINED":
        system_prompt = (
            "You are Sage, Ragul's personal AI executive assistant. Generate a polite, concise SMS auto-reply "
            "for an incoming phone call that Ragul had to decline because he is occupied or in a meeting. "
            f"Address the caller as {salutation} and introduce yourself as Sage (e.g. '{salutation} I am Sage. Ragul is unable to attend the call right now...'). "
            "Invite them to text their purpose or query so Ragul can follow up. "
            "Rules: Under 140 characters. Professional, natural, helpful tone. Return ONLY the exact text string to send without quotation marks."
        )
        default_reply = f"{salutation} I am Sage. Ragul is currently occupied and unable to attend the call right now. Please feel free to text your message here, and he will follow up shortly."
    else:
        system_prompt = (
            "You are Sage, Ragul's personal AI executive assistant. Generate a polite, concise SMS auto-reply "
            "for a missed phone call. "
            f"Address the caller as {salutation} and introduce yourself as Sage (e.g. '{salutation} I am Sage. Ragul is unable to attend the call right now...'). "
            "Invite them to text their purpose or message so Ragul can follow up. "
            "Rules: Under 140 characters. Professional, natural, helpful tone. Return ONLY the exact text string to send without quotation marks."
        )
        default_reply = f"{salutation} I am Sage. Ragul is unable to attend the call right now. Please feel free to text your purpose or message here, and he will follow up shortly."

    sms_reply = default_reply

    if deepseek_client:
        try:
            completion = deepseek_client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {
                        "role": "system",
                        "content": system_prompt
                    },
                    {
                        "role": "user",
                        "content": f"Caller Name: {data.caller_name}\nCaller Number: {data.caller_number}\nTime: {data.missed_at}\nCall Action: {call_type}"
                    }
                ],
                temperature=0.3,
                max_tokens=60
            )
            sms_reply = completion.choices[0].message.content.strip().strip('"')
        except Exception as e:
            print(f"DeepSeek missed-call error: {e}")
    elif gemini_client:
        try:
            response = gemini_client.models.generate_content(
                model="gemini-2.5-flash",
                contents=f"{system_prompt}\n\nCaller Name: {data.caller_name}\nCaller Number: {data.caller_number}\nTime: {data.missed_at}",
            )
            sms_reply = response.text.strip().strip('"')
        except Exception as e:
            print(f"Gemini missed-call error: {e}")

    # Save to Neon PostgreSQL
    call_record = MissedCallRecord(
        caller_number=data.caller_number,
        caller_name=data.caller_name or "Unknown",
        call_type=call_type,
        missed_at=data.missed_at,
        sms_reply=sms_reply,
        status="GENERATED"
    )
    db.add(call_record)
    db.commit()
    db.refresh(call_record)

    return {
        "id": call_record.id,
        "caller_number": call_record.caller_number,
        "caller_name": call_record.caller_name,
        "call_type": call_record.call_type,
        "missed_at": call_record.missed_at,
        "sms_reply": call_record.sms_reply,
        "status": call_record.status,
        "created_at": call_record.created_at.isoformat() if call_record.created_at else None
    }


@app.get("/api/dashboard/calls")
@app.get("/api/missed-calls")
def get_missed_calls(
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db)
):
    """Retrieve history of missed calls and generated replies."""
    query = db.query(MissedCallRecord)
    total = query.count()
    records = query.order_by(desc(MissedCallRecord.created_at)).offset(offset).limit(limit).all()

    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "calls": [
            {
                "id": r.id,
                "caller_number": r.caller_number,
                "caller_name": r.caller_name,
                "call_type": getattr(r, "call_type", "MISSED") or "MISSED",
                "missed_at": r.missed_at,
                "sms_reply": r.sms_reply,
                "status": r.status,
                "created_at": r.created_at.isoformat() if r.created_at else None
            }
            for r in records
        ]
    }


@app.patch("/api/missed-calls/{call_id}")
def update_missed_call_status(call_id: int, payload: MissedCallStatusUpdate, db: Session = Depends(get_db)):
    """Update status of a missed call record (e.g. SENT, FAILED)."""
    record = db.query(MissedCallRecord).filter(MissedCallRecord.id == call_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="Missed call record not found")

    record.status = payload.status.upper()
    db.commit()
    return {"message": "Call record updated successfully", "id": record.id, "status": record.status}


@app.get("/api/stats")
def get_stats(db: Session = Depends(get_db)):
    """Summary metrics for mobile app or admin dashboard."""
    total_emails = db.query(EmailTriageRecord).count()
    urgent_emails = db.query(EmailTriageRecord).filter(EmailTriageRecord.priority == "URGENT").count()
    important_emails = db.query(EmailTriageRecord).filter(EmailTriageRecord.priority == "IMPORTANT").count()
    low_emails = db.query(EmailTriageRecord).filter(EmailTriageRecord.priority == "LOW").count()
    action_required_emails = db.query(EmailTriageRecord).filter(EmailTriageRecord.action_required == True).count()
    new_emails = db.query(EmailTriageRecord).filter(EmailTriageRecord.status == "NEW").count()
    total_missed_calls = db.query(MissedCallRecord).count()

    latest_tracked = (
        db.query(EmailTriageRecord)
        .filter(EmailTriageRecord.account_email.isnot(None))
        .order_by(desc(EmailTriageRecord.created_at))
        .first()
    )
    monitored_account = latest_tracked.account_email if latest_tracked else None

    return {
        "monitored_account": monitored_account,
        "total_emails": total_emails,
        "urgent_emails": urgent_emails,
        "important_emails": important_emails,
        "low_emails": low_emails,
        "action_required_emails": action_required_emails,
        "new_emails": new_emails,
        "total_missed_calls": total_missed_calls
    }


@app.post("/api/sms-chat")
async def handle_sms_chat(data: SmsChatPayload, db: Session = Depends(get_db)):
    """
    Handles an incoming SMS reply from a contact/caller, maintains isolated conversation
    memory strictly by phone number, generates a response using DeepSeek AI, and stores the interaction.
    """
    clean_phone = data.phone_number.strip()
    contact_name = data.contact_name.strip() if data.contact_name else "Unknown"
    has_name = bool(contact_name and contact_name.lower() != "unknown")

    # 1. Fetch recent history strictly for this phone number (Guarantees zero multi-user cross-talk)
    history_records = (
        db.query(SmsMessageRecord)
        .filter(SmsMessageRecord.phone_number == clean_phone)
        .order_by(desc(SmsMessageRecord.created_at))
        .limit(8)
        .all()
    )
    # Reverse to chronological order (oldest first)
    chronological_history = list(reversed(history_records))

    has_name = bool(contact_name and contact_name.lower() != "unknown")
    salutation = f"Hi {contact_name}," if has_name else "Hi,"

    # 2. Build isolated DeepSeek conversation payload
    system_instruction = (
        f"You are Sage, Ragul's personal AI executive assistant chatting with {contact_name if has_name else 'the caller'} over SMS. "
        "Ragul is currently occupied and will review this conversation shortly. "
        "Politely answer their message, acknowledge their request, take down important notes, or provide brief assistance. "
        "Introduce yourself as Sage if the user asks who this is or in the opening exchange. "
        "Rules: Keep under 160 characters (1 standard SMS page). Do not confuse this person with anyone else. "
        "Return ONLY the exact text string to send as an SMS without quotation marks."
    )

    messages = [{"role": "system", "content": system_instruction}]
    for rec in chronological_history:
        messages.append({"role": rec.role, "content": rec.message})
    messages.append({"role": "user", "content": data.message})

    reply_text = (
        f"{salutation} I am Sage. Thanks for the message! Ragul is occupied right now, but I've noted this down and he will follow up shortly."
    )

    if deepseek_client:
        try:
            completion = deepseek_client.chat.completions.create(
                model="deepseek-chat",
                messages=messages,
                temperature=0.3,
                max_tokens=80
            )
            reply_text = completion.choices[0].message.content.strip().strip('"')
        except Exception as e:
            print(f"DeepSeek SMS chat error: {e}")
    elif gemini_client:
        try:
            prompt = f"{system_instruction}\n\nSender ({clean_phone}): {data.message}"
            response = gemini_client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
            )
            reply_text = response.text.strip().strip('"')
        except Exception as e:
            print(f"Gemini SMS chat error: {e}")

    # 3. Persist incoming user message and assistant reply with phone_number index
    user_msg = SmsMessageRecord(
        phone_number=clean_phone,
        contact_name=contact_name,
        role="user",
        message=data.message
    )
    assistant_msg = SmsMessageRecord(
        phone_number=clean_phone,
        contact_name=contact_name,
        role="assistant",
        message=reply_text
    )
    db.add(user_msg)
    db.add(assistant_msg)
    db.commit()

    return {
        "phone_number": clean_phone,
        "contact_name": contact_name,
        "reply": reply_text,
        "status": "GENERATED"
    }


@app.get("/api/sms-conversations")
def get_sms_conversations(
    phone_number: Optional[str] = None,
    limit: int = Query(default=50, le=200),
    db: Session = Depends(get_db)
):
    """Retrieve SMS conversation history, optionally filtered by phone number."""
    query = db.query(SmsMessageRecord)
    if phone_number:
        query = query.filter(SmsMessageRecord.phone_number == phone_number.strip())
    records = query.order_by(desc(SmsMessageRecord.created_at)).limit(limit).all()
    return {
        "total": len(records),
        "messages": [
            {
                "id": r.id,
                "phone_number": r.phone_number,
                "contact_name": r.contact_name,
                "role": r.role,
                "message": r.message,
                "created_at": r.created_at.isoformat() if r.created_at else None
            }
            for r in records
        ]
    }


@app.post("/api/seed-sample-email")
def seed_sample_email(db: Session = Depends(get_db)):
    """Convenience endpoint to inject realistic demo emails for UI preview."""
    sample = EmailTriageRecord(
        email_id=f"demo-urgent-{datetime.utcnow().strftime('%M%S')}",
        account_email="mymailbox@gmail.com",
        sender="Alex Rivera <alex.rivera@techcorp.io>",
        subject="URGENT: Server migration & DNS cutover scheduled tonight",
        snippet="Please confirm DNS propagation approval before 9 PM EST. Downtime window is 15 minutes.",
        priority="URGENT",
        summary="Urgent approval needed for tonight's server migration and DNS cutover before 9 PM EST.",
        action_required=True,
        suggested_reply="Hi Alex, I have reviewed the migration schedule and give full approval for the 9 PM EST cutover window.",
        status="NEW"
    )
    db.add(sample)
    db.commit()
    db.refresh(sample)
    return {"message": "Sample urgent email seeded successfully!", "id": sample.id}


# =====================================================================
# --- SAGE WHATSAPP ASSISTANT ROUTES (Briefing & Voice Reminders) ---
# =====================================================================

@app.get("/api/whatsapp/webhook")
def verify_whatsapp_webhook(
    hub_mode: Optional[str] = Query(None, alias="hub.mode"),
    hub_challenge: Optional[str] = Query(None, alias="hub.challenge"),
    hub_verify_token: Optional[str] = Query(None, alias="hub.verify_token")
):
    """Handles Meta WhatsApp Cloud API Webhook Verification."""
    expected_token = os.getenv("WHATSAPP_VERIFY_TOKEN", "sage_whatsapp_secret")
    if hub_mode == "subscribe" and hub_verify_token == expected_token:
        print("Meta WhatsApp Webhook verified successfully!")
        return int(hub_challenge) if hub_challenge and hub_challenge.isdigit() else hub_challenge
    raise HTTPException(status_code=403, detail="Verification token mismatch")


@app.post("/api/whatsapp/webhook")
async def handle_whatsapp_webhook(request: Request, db: Session = Depends(get_db)):
    """
    Unified WhatsApp Webhook:
    Supports both Twilio WhatsApp Sandbox (form-data) and Meta Cloud API (JSON).
    Handles:
    - Voice notes / voicemails (downloads & transcribes audio)
    - Reminders extraction (e.g. 'Remind me in 30 mins to check server')
    - Executive Briefing trigger on demand ('briefing' or 'summary')
    """
    content_type = request.headers.get("content-type", "")

    sender_phone = None
    user_text = ""
    is_voice = False
    media_url = None
    remote_jid = None

    # --- 1. Parse Twilio WhatsApp Request (Form-data) ---
    if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
        form_data = await request.form()
        sender_phone = form_data.get("From", "").strip()
        user_text = form_data.get("Body", "").strip()
        num_media = int(form_data.get("NumMedia", "0"))
        if num_media > 0:
            media_url = form_data.get("MediaUrl0")
            media_type = form_data.get("MediaContentType0", "")
            if "audio" in media_type or "ogg" in media_type or "mp4" in media_type:
                is_voice = True

    # --- 2. Parse Baileys Bridge or Meta WhatsApp Request (JSON) ---
    elif "application/json" in content_type:
        try:
            body_json = await request.json()
            # Check if payload is from our Baileys Bridge (whatsapp-bridge)
            if "remote_jid" in body_json or "audio_base64" in body_json or ("sender" in body_json and "text" in body_json):
                sender_phone = body_json.get("sender", "").strip()
                user_text = body_json.get("text", "") or ""
                is_voice = bool(body_json.get("is_voice", False))
                audio_base64 = body_json.get("audio_base64")
                mime_type = body_json.get("mime_type", "audio/ogg")
                remote_jid = body_json.get("remote_jid")

                if is_voice and audio_base64:
                    try:
                        import base64
                        audio_bytes = base64.b64decode(audio_base64)
                        transcribed = await transcribe_audio_bytes(audio_bytes, mime_type=mime_type)
                        if transcribed:
                            user_text = transcribed
                        print(f"Transcribed Baileys voice note: '{user_text}'")
                    except Exception as err:
                        print(f"Error decoding/transcribing audio from Baileys bridge: {err}")
            else:
                # Meta Cloud API JSON format
                entry = body_json.get("entry", [{}])[0]
                change = entry.get("changes", [{}])[0].get("value", {})
                messages = change.get("messages", [])
                if messages:
                    msg = messages[0]
                    sender_phone = f"whatsapp:+{msg.get('from', '')}"
                    msg_type = msg.get("type", "")
                    if msg_type == "text":
                        user_text = msg.get("text", {}).get("body", "")
                    elif msg_type == "audio" or msg_type == "voice":
                        is_voice = True
                        # Meta audio ID requires Graph API query
                        audio_id = msg.get("audio", {}).get("id") or msg.get("voice", {}).get("id")
                        if audio_id:
                            media_url = f"https://graph.facebook.com/v20.0/{audio_id}"
        except Exception as e:
            print(f"Error parsing WhatsApp JSON: {e}")

    if not sender_phone:
        return {"status": "ignored", "reason": "No sender identified"}

    print(f"Incoming WhatsApp message from {sender_phone} | remote_jid={remote_jid} | is_voice={is_voice} | text={user_text}")

    # --- 3. Process Voice Note / Voicemail (if media_url was provided e.g. Twilio/Meta) ---
    if is_voice and media_url and not user_text:
        # Step A: Download audio payload
        audio_bytes = await download_whatsapp_audio(media_url)
        if audio_bytes:
            # Step B: Transcribe audio using Gemini / Whisper
            user_text = await transcribe_audio_bytes(audio_bytes)
            print(f"Transcribed WhatsApp voice note: '{user_text}'")
        else:
            await send_whatsapp_message(
                sender_phone,
                "⚠️ Sage received your voice note, but could not download the audio file. Please ensure Twilio credentials are configured.",
                remote_jid=remote_jid
            )
            return {"status": "error", "reason": "Audio download failed"}

    if not user_text.strip():
        await send_whatsapp_message(
            sender_phone,
            "👋 Hi! I received your message. You can text or send a voice note with any reminder (e.g. _'Remind me at 5 PM to check server'_) or text *briefing* to see today's updates.",
            remote_jid=remote_jid
        )
        return {"status": "ok"}

    # --- 4. Handle Special Command: 'briefing' or 'summary' ---
    lower_input = user_text.strip().lower()
    if lower_input in ("briefing", "summary", "today", "today's work", "todays work", "/briefing"):
        briefing = generate_daily_briefing_text(db, user_name="Ragul")
        await send_whatsapp_message(sender_phone, briefing, remote_jid=remote_jid)
        return {"status": "ok", "action": "briefing_sent"}

    # --- 5. Handle Special Command: 'reminders' ---
    if lower_input in ("reminders", "my reminders", "list reminders", "show reminders", "/reminders"):
        active_reminders = (
            db.query(WhatsAppReminderRecord)
            .filter(WhatsAppReminderRecord.status == "PENDING")
            .order_by(WhatsAppReminderRecord.remind_at)
            .all()
        )
        if not active_reminders:
            await send_whatsapp_message(sender_phone, "✓ You have no pending reminders. Send a voice note or text to create one!", remote_jid=remote_jid)
        else:
            lines = ["⏰ *YOUR ACTIVE REMINDERS:*"]
            for idx, r in enumerate(active_reminders, 1):
                time_str = r.remind_at.strftime("%b %d, %I:%M %p")
                lines.append(f"{idx}. *{r.reminder_text}*")
                lines.append(f"   Due: {time_str} UTC")
            await send_whatsapp_message(sender_phone, "\n".join(lines), remote_jid=remote_jid)
        return {"status": "ok", "action": "reminders_listed"}

    # --- 6. Smart NLP Intent Parsing (Reminders & Tasks) ---
    parsed = await parse_whatsapp_intent(user_text, is_voice=is_voice)

    if parsed.get("is_reminder") or parsed.get("intent") == "REMINDER":
        task_text = parsed.get("reminder_text", user_text)
        remind_at_iso = parsed.get("remind_at_iso")
        if not remind_at_iso:
            remind_at_iso = (get_current_local_time() + timedelta(hours=1)).isoformat()

        # Save to database
        saved = save_reminder_to_db(
            user_phone=sender_phone,
            reminder_text=task_text,
            remind_at_iso=remind_at_iso,
            raw_input=user_text,
            is_voice=is_voice,
            db=db
        )

        reply_msg = parsed.get("reply")
        if not reply_msg:
            prefix = "🎙️ Voice reminder recorded!" if is_voice else "✓ Reminder scheduled!"
            reply_msg = f"{prefix}\n\n📌 *{task_text}*\n⏰ I will ping you right here when it's time."

        await send_whatsapp_message(sender_phone, reply_msg, remote_jid=remote_jid)
        return {"status": "ok", "action": "reminder_created", "id": saved.id if saved else None}

    # General query fallback response
    reply_msg = parsed.get("reply", "✓ Received! I am monitoring your emails, missed calls, and reminders.")
    await send_whatsapp_message(sender_phone, reply_msg, remote_jid=remote_jid)
    return {"status": "ok", "action": "chat_reply"}


@app.post("/api/whatsapp/briefing/trigger")
async def trigger_whatsapp_briefing(
    phone_number: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    """
    Manually triggers your Executive Morning Briefing to WhatsApp immediately.
    Useful for testing or getting an on-demand update.
    """
    target = phone_number or os.getenv("USER_WHATSAPP_NUMBER")
    if not target:
        raise HTTPException(status_code=400, detail="USER_WHATSAPP_NUMBER not configured in .env and not provided in query.")

    briefing_text = generate_daily_briefing_text(db, user_name="Ragul")
    res = await send_whatsapp_message(target, briefing_text)
    return {
        "status": "triggered",
        "recipient": target,
        "briefing_preview": briefing_text,
        "dispatch_result": res
    }


@app.get("/api/whatsapp/reminders")
def list_whatsapp_reminders(
    status: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    """Lists scheduled WhatsApp reminders."""
    query = db.query(WhatsAppReminderRecord)
    if status:
        query = query.filter(WhatsAppReminderRecord.status == status.upper())
    records = query.order_by(desc(WhatsAppReminderRecord.created_at)).limit(50).all()
    return {
        "total": len(records),
        "reminders": [
            {
                "id": r.id,
                "user_phone": r.user_phone,
                "reminder_text": r.reminder_text,
                "is_voice": r.is_voice,
                "remind_at": r.remind_at.isoformat() if r.remind_at else None,
                "status": r.status,
                "created_at": r.created_at.isoformat() if r.created_at else None
            }
            for r in records
        ]
    }


@app.post("/api/whatsapp/send-test")
async def test_send_whatsapp(
    message: str = Query(default="Hello from Sage AI Assistant!"),
    phone_number: Optional[str] = Query(None)
):
    """Test endpoint to verify WhatsApp message dispatch."""
    target = phone_number or os.getenv("USER_WHATSAPP_NUMBER")
    if not target:
        raise HTTPException(status_code=400, detail="USER_WHATSAPP_NUMBER not set.")
    res = await send_whatsapp_message(target, message)
    return {"result": res, "sent_to": target}