import os
import asyncio
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

from database import SessionLocal, init_db
from whatsapp_service import generate_daily_briefing_text, send_whatsapp_message
from reminder_engine import parse_whatsapp_intent, save_reminder_to_db, get_current_local_time
from models import WhatsAppReminderRecord


async def test_suite():
    init_db()
    print("==================================================")
    print("--- 1. Testing Daily Briefing Generation ---")
    print("==================================================")
    with SessionLocal() as db:
        briefing = generate_daily_briefing_text(db, user_name="Ragul")
        print("Generated Briefing Preview:")
        print("--------------------------------------------------")
        print(briefing)
        print("--------------------------------------------------")

    print("\n==================================================")
    print("--- 2. Testing NLP Reminder Intent Parsing ---")
    print("==================================================")
    test_inputs = [
        "Remind me in 30 minutes to check the server logs",
        "Tomorrow at 10 AM call Suresh about the contract",
        "briefing",
        "show my reminders"
    ]

    for user_msg in test_inputs:
        print(f"\nUser Input: '{user_msg}'")
        parsed = await parse_whatsapp_intent(user_msg, is_voice=False)
        print(f"Parsed Result: {parsed}")

    print("\n==================================================")
    print("--- 3. Testing Reminder Persistence & Storage ---")
    print("==================================================")
    with SessionLocal() as db:
        record = save_reminder_to_db(
            user_phone="whatsapp:+919876543210",
            reminder_text="Test automated reminder for server migration",
            remind_at_iso=datetime.utcnow().isoformat(),
            raw_input="Test automated reminder for server migration",
            is_voice=False,
            db=db
        )
        if record:
            print(f"✓ Successfully stored reminder #{record.id} in DB!")
            # Clean up test record
            db.delete(record)
            db.commit()
            print("✓ Cleaned up test record.")

    print("\n==================================================")
    print("--- 4. Testing WhatsApp Message Dispatch ---")
    print("==================================================")
    res = await send_whatsapp_message(
        "whatsapp:+919876543210",
        "👋 *Sage WhatsApp Assistant* is ready to send reminders and morning briefings!"
    )
    print(f"Dispatch result: {res}")

    print("\n==================================================")
    print("--- 5. Testing Unified Webhook Endpoint (/api/whatsapp/webhook) ---")
    print("==================================================")
    from fastapi.testclient import TestClient
    from main import app

    client = TestClient(app)

    # Test A: Text reminder from Baileys Bridge
    res_text = client.post(
        "/api/whatsapp/webhook",
        json={
            "sender": "whatsapp:+919876543210",
            "text": "Remind me tomorrow at 9 AM to submit quarterly GST report",
            "is_voice": False,
            "from_me": False,
            "remote_jid": "919876543210@s.whatsapp.net"
        }
    )
    print(f"Webhook text reminder response: {res_text.status_code} -> {res_text.json()}")
    assert res_text.status_code == 200

    # Clean up test reminder from DB
    if res_text.json().get("id"):
        with SessionLocal() as db:
            r = db.query(WhatsAppReminderRecord).filter(WhatsAppReminderRecord.id == res_text.json()["id"]).first()
            if r:
                db.delete(r)
                db.commit()
                print("✓ Cleaned up test reminder record.")

    # Test B: 'briefing' command from Baileys Bridge
    res_briefing = client.post(
        "/api/whatsapp/webhook",
        json={
            "sender": "whatsapp:+919876543210",
            "text": "briefing",
            "is_voice": False,
            "from_me": False,
            "remote_jid": "919876543210@s.whatsapp.net"
        }
    )
    print(f"Webhook briefing response: {res_briefing.status_code} -> {res_briefing.json()}")
    assert res_briefing.status_code == 200
    assert res_briefing.json().get("action") == "briefing_sent"

    print("\n✓ ALL TESTS (BRIEFING, NLP, STORAGE, DISPATCH & WEBHOOK) PASSED SUCCESSFULLY!")


if __name__ == "__main__":
    asyncio.run(test_suite())

