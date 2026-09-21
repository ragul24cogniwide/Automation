from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime
from sqlalchemy.sql import func
from database import Base


class EmailTriageRecord(Base):
    __tablename__ = "email_triages"

    id = Column(Integer, primary_key=True, index=True)
    email_id = Column(String(255), unique=True, index=True, nullable=False)
    account_email = Column(String(255), nullable=True, index=True)
    sender = Column(String(255), nullable=False)
    subject = Column(String(500), nullable=False)
    snippet = Column(Text, nullable=True)
    body = Column(Text, nullable=True)
    priority = Column(String(50), index=True, default="LOW")  # URGENT, IMPORTANT, LOW
    summary = Column(Text, nullable=True)
    action_required = Column(Boolean, default=False)
    suggested_reply = Column(Text, nullable=True)
    status = Column(String(50), default="NEW", index=True)  # NEW, READ, REPLIED, ARCHIVED
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class MissedCallRecord(Base):
    __tablename__ = "missed_calls"

    id = Column(Integer, primary_key=True, index=True)
    caller_number = Column(String(50), index=True, nullable=False)
    caller_name = Column(String(255), default="Unknown")
    call_type = Column(String(50), default="MISSED")  # MISSED, DECLINED
    missed_at = Column(String(100), nullable=True)
    sms_reply = Column(Text, nullable=True)
    status = Column(String(50), default="GENERATED", index=True)  # GENERATED, SENT, FAILED
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class SmsMessageRecord(Base):
    __tablename__ = "sms_messages"

    id = Column(Integer, primary_key=True, index=True)
    phone_number = Column(String(50), index=True, nullable=False)
    contact_name = Column(String(255), default="Unknown")
    role = Column(String(20), nullable=False)  # 'user' or 'assistant'
    message = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class WhatsAppReminderRecord(Base):
    __tablename__ = "whatsapp_reminders"

    id = Column(Integer, primary_key=True, index=True)
    user_phone = Column(String(50), index=True, nullable=False)
    reminder_text = Column(Text, nullable=False)
    raw_input = Column(Text, nullable=True)
    is_voice = Column(Boolean, default=False)
    remind_at = Column(DateTime(timezone=True), nullable=False, index=True)
    status = Column(String(20), default="PENDING", index=True)  # PENDING, SENT, CANCELLED
    created_at = Column(DateTime(timezone=True), server_default=func.now())

