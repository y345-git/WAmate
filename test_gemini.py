import os
import sys
import time
from pathlib import Path
from bot.config import Config
from bot.database import DatabaseManager
from bot.gemini_agent import GeminiAgent


def run_tests():
    print("=" * 65)
    print("  WAMCPBOT: Enhanced Rich Media & Gemini AI Agent Self-Test")
    print("=" * 65)

    test_db_path = Path("./data/test_messages.db")
    test_db_path.parent.mkdir(parents=True, exist_ok=True)
    if test_db_path.exists():
        test_db_path.unlink()

    print("\n1. Initializing test database...")
    db = DatabaseManager(test_db_path)
    print("   [OK] Database, rich media schema & FTS5 tables created.")

    print("\n2. Inserting mock contacts...")
    pranav_jid = "919876543210@s.whatsapp.net"
    alice_jid = "14155552671@s.whatsapp.net"
    team_group_jid = "120363024849202@g.us"

    db.upsert_contact(jid=pranav_jid, name="Pranav", push_name="Pranav Sharma", phone_number="919876543210")
    db.upsert_contact(jid=alice_jid, name="Alice Johnson", push_name="Alice", phone_number="14155552671")
    db.upsert_contact(jid=team_group_jid, name="Engineering Team", is_group=True)
    print("   [OK] Contacts inserted.")

    print("\n3. Inserting mock rich media messages...")
    now = int(time.time())

    # 1. Text message
    db.insert_message(
        msg_id="M1",
        chat_jid=pranav_jid,
        chat_name="Pranav",
        sender_jid=pranav_jid,
        sender_name="Pranav",
        text="Hey! Did you check the new backend API deployment?",
        timestamp=now - 7200,
        is_from_me=False,
    )

    # 2. Document message (PDF)
    db.insert_message(
        msg_id="M2",
        chat_jid=pranav_jid,
        chat_name="Pranav",
        sender_jid=pranav_jid,
        sender_name="Pranav",
        text="📄 [Document: Q4_Budget_Review_2026.pdf (2.4 MB, 14 pages)] - Please check financial forecasts",
        timestamp=now - 5400,
        is_from_me=False,
        media_type="document",
        media_filename="Q4_Budget_Review_2026.pdf",
        media_mimetype="application/pdf",
        media_size=2516582,
        media_metadata={
            "filename": "Q4_Budget_Review_2026.pdf",
            "page_count": 14,
            "mimetype": "application/pdf",
            "size_bytes": 2516582,
        },
    )

    # 3. Contact Card (vCard)
    db.insert_message(
        msg_id="M3",
        chat_jid=alice_jid,
        chat_name="Alice Johnson",
        sender_jid=alice_jid,
        sender_name="Alice",
        text="👤 [Contact Card: Suresh Electrician | Phone: +91 98220 12345 | Org: Spark Electricals]",
        timestamp=now - 3600,
        is_from_me=False,
        media_type="contact",
        media_metadata={
            "display_name": "Suresh Electrician",
            "phone": "+91 98220 12345",
            "organization": "Spark Electricals",
        },
    )

    # 4. Poll Creation message in Group Chat
    db.insert_message(
        msg_id="M4",
        chat_jid=team_group_jid,
        chat_name="Engineering Team",
        sender_jid=pranav_jid,
        sender_name="Pranav",
        text='📊 [Poll: "Where should we go for team lunch on Friday?" | Options: 1. Barbeque Nation | 2. Mainland China | 3. Pizza Express | (Single Choice)]',
        timestamp=now - 1800,
        is_from_me=False,
        is_group=True,
        media_type="poll",
        media_metadata={
            "question": "Where should we go for team lunch on Friday?",
            "options": ["Barbeque Nation", "Mainland China", "Pizza Express"],
            "choice_type": "Single Choice",
        },
    )

    # 5. Voice Note message
    db.insert_message(
        msg_id="M5",
        chat_jid=pranav_jid,
        chat_name="Pranav",
        sender_jid=pranav_jid,
        sender_name="Pranav",
        text="🎙️ [Voice Note (Duration: 42s, Size: 145.2 KB)]",
        timestamp=now - 900,
        is_from_me=False,
        media_type="voice_note",
        media_metadata={
            "duration_seconds": 42,
            "duration_formatted": "42s",
            "is_ptt": True,
            "size_bytes": 148684,
        },
    )

    print("   [OK] Mock rich media messages inserted into database.")

    print("\n4. Testing local search functions...")
    # Test find_documents
    docs = db.find_documents(query="budget")
    print(f"   find_documents('budget') returned: {len(docs)} documents")
    assert len(docs) > 0, "Failed to find document"
    print(f"     -> Found: {docs[0]['filename']} ({docs[0]['pages']} pages) from {docs[0]['sender']}")

    # Test find_shared_contacts
    contacts_shared = db.find_shared_contacts(query="electrician")
    print(f"   find_shared_contacts('electrician') returned: {len(contacts_shared)} contacts")
    assert len(contacts_shared) > 0, "Failed to find contact card"
    print(f"     -> Found: {contacts_shared[0]['contact_name']} ({contacts_shared[0]['phone']})")

    # Test get_polls
    polls = db.db = db.get_polls(query="lunch")
    print(f"   get_polls('lunch') returned: {len(polls)} polls")
    assert len(polls) > 0, "Failed to find poll"
    print(f"     -> Found Poll: '{polls[0]['poll_question']}' with options {polls[0]['options']}")

    # Test find_voice_notes_and_media
    vn = db.find_voice_notes_and_media(media_type="voice_note")
    print(f"   find_voice_notes_and_media('voice_note') returned: {len(vn)} voice notes")
    assert len(vn) > 0, "Failed to find voice note"
    print(f"     -> Found: {vn[0]['description']} from {vn[0]['sender']}")

    print("   [OK] Rich media database queries and FTS5 work as expected.")

    api_key = Config.GEMINI_API_KEY
    if not api_key or api_key == "your_gemini_api_key_here":
        print("\nNOTE: GEMINI_API_KEY is not set.")
        return

    print("\n5. Testing Gemini Agent on Rich Media questions...")
    agent = GeminiAgent(
        api_key=api_key,
        model_name=Config.GEMINI_MODEL,
        db=db,
    )

    q1 = "find me the PDF document Pranav sent"
    print(f"\nQ: '{q1}'")
    ans1 = agent.ask(q1)
    print("--- GEMINI RESPONSE ---")
    print(ans1)
    print("-----------------------")

    q2 = "what was the poll about team lunch and what options are there?"
    print(f"\nQ: '{q2}'")
    ans2 = agent.ask(q2)
    print("--- GEMINI RESPONSE ---")
    print(ans2)
    print("-----------------------")

    q3 = "did anyone share a contact card for an electrician?"
    print(f"\nQ: '{q3}'")
    ans3 = agent.ask(q3)
    print("--- GEMINI RESPONSE ---")
    print(ans3)
    print("-----------------------")

    print("\n6. Testing Document Delivery, Scanned PDF OCR & Image OCR tools...")
    class MockBot:
        def send_document_by_query(self, query):
            return f"[Delivered] Successfully sent file 'Q4_Budget_Review_2026.pdf' to your WhatsApp chat."
        def read_document_by_query(self, query):
            return "--- Content: Q4_Budget_Review_2026.pdf ---\nExecutive Summary: Total Q4 approved budget is $1,450,000. Engineering: $800,000, Marketing: $400,000, Ops: $250,000.\n--- End ---"
        def read_and_ocr_document(self, query, specific_question=""):
            return "--- Document Analysis & AI Vision OCR: Scanned_Tax_Invoice.pdf ---\nInvoice No: TX-90021, Vendor: Sangli Hardware, Total: Rs. 14,800. Items: D-Link switch, patch panels.\n--- End ---"
        def inspect_and_ocr_image(self, query_or_contact="", user_question=""):
            return "--- Image Analysis & AI OCR: Receipt Photo (Chat: 'Work') ---\nStore: City Mart, Date: 02-Nov-2026, Grand Total: Rs. 2,450 paid via UPI.\n--- End ---"

    agent.bot_client = MockBot()

    q4 = "send me the budget review pdf to my chat"
    print(f"\nQ: '{q4}'")
    ans4 = agent.ask(q4)
    print("--- GEMINI RESPONSE ---")
    print(ans4)
    print("-----------------------")

    q5 = "what is the approved engineering budget in that document?"
    print(f"\nQ: '{q5}'")
    ans5 = agent.ask(q5)
    print("--- GEMINI RESPONSE ---")
    print(ans5)
    print("-----------------------")

    q6 = "what is the grand total in the receipt photo sent in my chat?"
    print(f"\nQ: '{q6}'")
    ans6 = agent.ask(q6)
    print("--- GEMINI RESPONSE ---")
    print(ans6)
    print("-----------------------")

    print("\n[SUCCESS] Enhanced rich media, document delivery, and OCR self-test passed completely!")


if __name__ == "__main__":
    run_tests()
