"""
Automated Verification Test Suite for Personal AI Agent Router & Neon PostgreSQL.
"""
from fastapi.testclient import TestClient
from main import app
from database import SessionLocal
from models import EmailTriageRecord, MissedCallRecord

client = TestClient(app)

def run_tests():
    print("==================================================")
    print("STARTING COMPREHENSIVE VERIFICATION")
    print("==================================================")

    # 1. Test Health Check
    print("\n[1] Testing GET /api/health...")
    res = client.get("/api/health")
    assert res.status_code == 200, f"Expected 200, got {res.status_code}"
    health_data = res.json()
    assert health_data["database"] == "connected", f"Database not connected: {health_data}"
    print(f" PASS: Health check OK, Database: {health_data['database']}")

    # 2. Test Root Endpoint
    print("\n[2] Testing GET /...")
    res = client.get("/")
    assert res.status_code == 200
    assert res.json()["status"] == "online"
    print(" PASS: Root endpoint OK")

    # 3. Test Email Triage - Urgent
    print("\n[3] Testing POST /api/triage-email (Urgent)...")
    email_payload_1 = {
        "id": "verify-email-urgent-01",
        "sender": "investor@venturecapital.com",
        "subject": "URGENT: Term Sheet Signed - Immediate Response Needed",
        "snippet": "We have counter-signed the term sheet. Wire instructions needed ASAP."
    }
    res = client.post("/api/triage-email", json=email_payload_1)
    assert res.status_code == 200, f"Error: {res.text}"
    data = res.json()
    assert data["priority"] == "URGENT"
    assert data["action_required"] is True
    assert data["is_duplicate"] is False
    print(f" PASS: Email triaged as {data['priority']}, Action required: {data['action_required']}")

    # 4. Test Email Triage - Deduplication
    print("\n[4] Testing POST /api/triage-email Deduplication...")
    res_dup = client.post("/api/triage-email", json=email_payload_1)
    assert res_dup.status_code == 200
    dup_data = res_dup.json()
    assert dup_data["is_duplicate"] is True
    print(" PASS: Deduplication prevented duplicate database row!")

    # 5. Test Email Triage - Low Priority
    print("\n[5] Testing POST /api/triage-email (Low Priority)...")
    email_payload_2 = {
        "id": "verify-email-low-02",
        "sender": "newsletter@techcrunch.com",
        "subject": "Weekly Tech Highlights",
        "snippet": "Here are this week's top software stories."
    }
    res_low = client.post("/api/triage-email", json=email_payload_2)
    assert res_low.status_code == 200
    assert res_low.json()["priority"] == "LOW"
    print(" PASS: Low priority email triaged correctly.")

    # 6. Test GET /api/emails & Filtering
    print("\n[6] Testing GET /api/emails with filtering...")
    res_list = client.get("/api/emails?priority=URGENT")
    assert res_list.status_code == 200
    urgent_emails = res_list.json()["emails"]
    assert any(e["email_id"] == "verify-email-urgent-01" for e in urgent_emails)
    print(f" PASS: Filtered emails successfully, returned {len(urgent_emails)} urgent email(s)")

    # 7. Test PATCH /api/emails/{id}
    print("\n[7] Testing PATCH /api/emails/{email_id}...")
    res_patch = client.patch("/api/emails/verify-email-urgent-01", json={"status": "ARCHIVED"})
    assert res_patch.status_code == 200
    assert res_patch.json()["status"] == "ARCHIVED"
    print(" PASS: Email status updated to ARCHIVED")

    # 8. Test POST /api/missed-call
    print("\n[8] Testing POST /api/missed-call...")
    call_payload = {
        "caller_number": "+14155552671",
        "caller_name": "Sarah Connor",
        "missed_at": "04:15 PM"
    }
    res_call = client.post("/api/missed-call", json=call_payload)
    assert res_call.status_code == 200
    call_data = res_call.json()
    call_id = call_data["id"]
    assert len(call_data["sms_reply"]) > 0
    print(f" PASS: Missed call logged (ID {call_id}), SMS Reply: '{call_data['sms_reply']}'")

    # 9. Test GET /api/missed-calls
    print("\n[9] Testing GET /api/missed-calls...")
    res_calls = client.get("/api/missed-calls")
    assert res_calls.status_code == 200
    assert any(c["caller_number"] == "+14155552671" for c in res_calls.json()["calls"])
    print(" PASS: Missed calls history fetched successfully.")

    # 10. Test PATCH /api/missed-calls/{id}
    print("\n[10] Testing PATCH /api/missed-calls/{id}...")
    res_call_patch = client.patch(f"/api/missed-calls/{call_id}", json={"status": "SENT"})
    assert res_call_patch.status_code == 200
    assert res_call_patch.json()["status"] == "SENT"
    print(" PASS: Missed call status updated to SENT.")

    # 11. Test GET /api/stats
    print("\n[11] Testing GET /api/stats...")
    res_stats = client.get("/api/stats")
    assert res_stats.status_code == 200
    stats = res_stats.json()
    assert stats["total_emails"] >= 2
    assert stats["total_missed_calls"] >= 1
    print(f" PASS: Dashboard stats verified: {stats}")

    # 12. Cleanup verification records from Neon DB
    print("\n[12] Cleaning up verification records from Neon PostgreSQL...")
    db = SessionLocal()
    db.query(EmailTriageRecord).filter(EmailTriageRecord.email_id.in_(["verify-email-urgent-01", "verify-email-low-02"])).delete(synchronize_session=False)
    db.query(MissedCallRecord).filter(MissedCallRecord.caller_number == "+14155552671").delete(synchronize_session=False)
    db.commit()
    db.close()
    print(" PASS: Cleanup complete. Neon DB is tidy.")

    print("\n==================================================")
    print("ALL 12 TESTS PASSED PERFECTLY!")
    print("==================================================")

if __name__ == "__main__":
    run_tests()
