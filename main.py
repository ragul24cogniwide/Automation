import os
import json
from contextlib import asynccontextmanager
from typing import Optional, List
from datetime import datetime

from fastapi import FastAPI, HTTPException, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import text, desc
from dotenv import load_dotenv

from database import engine, get_db, init_db
from models import EmailTriageRecord, MissedCallRecord

load_dotenv()

# Gemini setup
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
    try:
        init_db()
        print("Neon PostgreSQL tables verified successfully.")
    except Exception as e:
        print(f"Database initialization warning: {e}")
    yield


app = FastAPI(
    title="Personal AI Agent Router",
    description="Automated Email Triage & Missed Call SMS Agent powered by Gemini & Neon PostgreSQL",
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


class MissedCallStatusUpdate(BaseModel):
    status: str


# --- Endpoints ---

@app.get("/")
def root():
    return {
        "service": "Personal AI Agent Router",
        "status": "online",
        "endpoints": {
            "health": "/api/health",
            "stats": "/api/stats",
            "triage_email": "/api/triage-email",
            "emails": "/api/emails",
            "missed_call": "/api/missed-call",
            "missed_calls": "/api/missed-calls",
        }
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
        "gemini_configured": gemini_client is not None,
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

    if gemini_client:
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
    Handles a missed call event, generates an SMS response via Gemini,
    and logs the call into Neon PostgreSQL.
    """
    prompt = f"""
    Generate a polite, concise SMS auto-reply for a missed call.
    Caller Name: {data.caller_name}
    Caller Number: {data.caller_number}
    Time: {data.missed_at}

    Rules:
    - Under 140 characters.
    - Mention I am currently occupied and ask if it is urgent.
    - Return ONLY the exact text string to send.
    """

    sms_reply = "Hi! I missed your call. I am currently occupied—please let me know if it's urgent, and I'll get back to you shortly."

    if gemini_client:
        try:
            response = gemini_client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
            )
            sms_reply = response.text.strip()
        except Exception as e:
            print(f"Gemini missed-call error: {e}")

    # Save to Neon PostgreSQL
    call_record = MissedCallRecord(
        caller_number=data.caller_number,
        caller_name=data.caller_name or "Unknown",
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
        "missed_at": call_record.missed_at,
        "sms_reply": call_record.sms_reply,
        "status": call_record.status,
        "created_at": call_record.created_at.isoformat() if call_record.created_at else None
    }


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