import io
import json
import time
import base64
from pathlib import Path
import openpyxl

from bot.database import DatabaseManager
from bot.whatsapp_client import WhatsAppBot
from bot.web_ui import WebUIServer, _generate_excel_workbook, _parse_excel_workbook
from bot.utils import clean_recipient_jid
from neonize.proto.Neonize_pb2 import JID


class MockNeonizeClient:
    def __init__(self):
        self.sent_messages = []
        self.sent_images = []
        self.sent_documents = []

    def send_message(self, to, message):
        # Strict whatsmeow assertion
        if hasattr(to, "Device") and to.Device != 0:
            raise Exception("message recipient must be a user JID with no device part")
        self.sent_messages.append({"to": to, "message": message})
        class Resp:
            ID = "MOCK_MSG_ID"
        return Resp()

    def send_image(self, to, file, caption=""):
        if hasattr(to, "Device") and to.Device != 0:
            raise Exception("message recipient must be a user JID with no device part")
        self.sent_images.append({"to": to, "file": file, "caption": caption})
        class Resp:
            ID = "MOCK_IMG_ID"
        return Resp()

    def send_document(self, to, file, filename="", mimetype="", caption=""):
        if hasattr(to, "Device") and to.Device != 0:
            raise Exception("message recipient must be a user JID with no device part")
        self.sent_documents.append({"to": to, "file": file, "filename": filename})
        class Resp:
            ID = "MOCK_DOC_ID"
        return Resp()


def test_all():
    print("=" * 60)
    print("TEST SUITE: Role-based Broadcast, Scheduling & Web UI Endpoints")
    print("=" * 60)

    db_path = Path("./data/test_broadcast_ui.db")
    if db_path.exists():
        db_path.unlink()
    db = DatabaseManager(db_path)

    # Mock WhatsApp bot client
    mock_neonize = MockNeonizeClient()
    bot = WhatsAppBot(Path("./data/test_session.db"), db, gemini=None, command_prefix="!ask")
    bot.client = mock_neonize
    # Set companion device JID (Device = 3) to test device-stripping
    bot.my_jid = JID(User="919876543210", Device=3, Server="s.whatsapp.net")
    bot.my_user_number = "919876543210"
    bot.my_canonical_jid = "919876543210@s.whatsapp.net"
    bot.my_non_ad_jid = clean_recipient_jid(bot.my_jid)

    # 1. Test Send to WA (Regression prevention for user bug)
    print("\n1. Testing 'Send to WA' with companion device JID...")
    db.insert_message(
        msg_id="W1",
        chat_jid="919876543210@s.whatsapp.net",
        chat_name="Self",
        sender_jid="919876543210:3@s.whatsapp.net",
        sender_name="Self",
        text="Thermal paste aahe ky ?",
        timestamp=int(time.time()),
        is_from_me=True,
    )
    ok, detail = bot.send_media_by_id_to_self("W1")
    assert ok is True, f"Send to WA failed: {detail}"
    assert len(mock_neonize.sent_messages) == 1
    dest = mock_neonize.sent_messages[0]["to"]
    assert dest.Device == 0, f"Expected dest.Device==0, got {dest.Device}"
    print("   [OK] Send to WA successfully sent with clean non-AD JID (Device=0).")

    # 2. Test Roles and Entity assignments
    print("\n2. Testing Roles CRUD and Entity assignments...")
    db.add_role("VIP", "#f59e0b")
    db.add_role("Clients", "#3b82f6")
    roles = db.get_roles()
    role_names = [r["name"] for r in roles]
    assert "VIP" in role_names and "Clients" in role_names
    for r in roles:
        assert "count" in r and "assigned_count" in r, "Roles must return count and assigned_count"

    c1 = "919000000001@s.whatsapp.net"
    c2 = "919000000002@s.whatsapp.net"
    g1 = "120363000000001@g.us"

    db.upsert_contact(jid=c1, name="Alice VIP", phone_number="919000000001")
    db.upsert_contact(jid=c2, name="Bob Client", phone_number="919000000002")
    db.upsert_contact(jid=g1, name="VIP Investors Group", is_group=True)

    db.set_entity_roles(c1, ["VIP"])
    db.set_entity_roles(c2, ["Clients"])
    db.set_entity_roles(g1, ["VIP"])

    vip_both = db.lookup_recipients_by_roles(["VIP"], target_audience="both")
    assert len(vip_both) == 2, f"Expected 2 VIPs, got {len(vip_both)}"
    vip_contacts = db.lookup_recipients_by_roles(["VIP"], target_audience="contacts")
    assert len(vip_contacts) == 1 and vip_contacts[0]["jid"] == c1
    vip_groups = db.lookup_recipients_by_roles(["VIP"], target_audience="groups")
    assert len(vip_groups) == 1 and vip_groups[0]["jid"] == g1

    # Verify lookup with NO roles selected returns all in audience (Fix for user issue)
    all_both = db.lookup_recipients_by_roles([], target_audience="both")
    assert len(all_both) >= 3, f"Expected at least 3 recipients for audience=both with no roles, got {len(all_both)}"
    all_contacts = db.lookup_recipients_by_roles([], target_audience="contacts")
    assert len(all_contacts) >= 2, f"Expected at least 2 contacts for audience=contacts, got {len(all_contacts)}"
    all_groups = db.lookup_recipients_by_roles([], target_audience="groups")
    assert len(all_groups) >= 1, f"Expected at least 1 group for audience=groups, got {len(all_groups)}"

    # Verify list_entities_with_roles returns dictionary structure
    list_res = db.list_entities_with_roles(is_group=True)
    assert isinstance(list_res, dict) and "items" in list_res and "total" in list_res
    assert list_res["total"] >= 1
    print("   [OK] Role assignment and audience filtering work accurately.")

    # 3. Test Broadcast sending
    print("\n3. Testing Broadcast message dispatch...")
    targets = [{"jid": c1, "name": "Alice"}, {"jid": g1, "name": "Investors"}]
    res = bot.send_broadcast_message(
        recipients=targets,
        message="Important announcement for *VIPs*",
        min_delay=0.01,
        max_delay=0.02,
    )
    assert res["total"] == 2
    assert res["sent"] == 2
    assert res["failed"] == 0

    db.record_campaign_history(
        campaign_type="Instant Broadcast",
        message="Important announcement for *VIPs*",
        attachment_path="",
        attachment_filename="",
        roles=["VIP"],
        target_audience="both",
        recipients_count=res["total"],
        sent_count=res["sent"],
        failed_count=res["failed"],
        duration_sec=res["duration"],
        started_at=res["started_at"],
        finished_at=res["finished_at"],
        logs=res["logs"],
    )
    history = db.get_campaign_history()["items"]
    assert len(history) == 1
    assert history[0]["sent_count"] == 2
    print("   [OK] Broadcast dispatch and history recording verified.")

    # 4. Test Scheduled Campaigns
    print("\n4. Testing Scheduled Campaign creation and trigger...")
    camp_id = db.create_scheduled_campaign(
        message="Scheduled reminder",
        scheduled_time="2026-11-14T12:00",
        scheduled_ts=int(time.time()) - 10,  # Already due
        target_audience="contacts",
        roles=["Clients"],
    )
    due = db.get_due_scheduled_campaigns()
    assert len(due) >= 1 and due[0]["id"] == camp_id

    # Trigger campaign
    bot.dispatch_scheduled_campaign(camp_id)
    c_updated = db.get_scheduled_campaign(camp_id)
    assert c_updated["status"] == "sent"
    assert c_updated["sent_count"] == 1
    print("   [OK] Scheduled campaign lifecycle verified.")

    # 5. Test Excel Export and Import
    print("\n5. Testing Excel Workbook export and import...")
    export_data = db.export_workbook_data()
    xlsx_bytes = _generate_excel_workbook(export_data)
    assert len(xlsx_bytes) > 2000, "XLSX file bytes too small"

    # Verify openpyxl can read it
    wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes))
    assert "Roles" in wb.sheetnames
    assert "Contacts" in wb.sheetnames
    assert "Groups" in wb.sheetnames

    # Test parse and import
    parsed = _parse_excel_workbook(xlsx_bytes)
    assert len(parsed["roles"]) >= 2
    assert len(parsed["contacts"]) >= 2
    assert len(parsed["groups"]) >= 1

    counts = db.import_workbook_data(parsed, mode="merge")
    assert counts["roles"] >= 2
    print("   [OK] Excel export and import round-trip verified.")

    # Clean up test database
    if db_path.exists():
        db_path.unlink()

    print("\n" + "=" * 60)
    print("ALL BROADCAST, SCHEDULING, ROLES & EXCEL TESTS PASSED!")
    print("=" * 60)


if __name__ == "__main__":
    test_all()
