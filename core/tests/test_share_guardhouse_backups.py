from datetime import timedelta
from pathlib import Path

from django.test import Client
from django.utils import timezone

from core import backups
from core.models import ChatMessage, Conversation, ItemPost, PostShare, StatusChangeRequest, SystemSettings
from core.services import close_post, process_due_actions

from .base import FindItTestCase, photo


class ShareTests(FindItTestCase):
    def test_unshared_needs_sign_in_and_copy_link_is_permanent(self):
        post = self.create_post()
        anon = Client()
        self.assertEqual(anon.get(f"/p/{post.pk}/").status_code, 302)
        r = self.client_for(self.alice).post(f"/posts/{post.pk}/share/", HTTP_X_REQUESTED_WITH="fetch")
        self.assertEqual(r.json()["share_count"], 1)
        self.client_for(self.finder).post(f"/posts/{post.pk}/share/")
        page = anon.get(f"/p/{post.pk}/").content.decode()
        self.assertIn("Shared by author", page)
        self.assertIn("Shared by 2 people", page)
        close_post(post, self.finder, "Owner found it")
        r = anon.get(f"/p/{post.pk}/")
        self.assertContains(r, "closed-modal")
        self.assertEqual(anon.post(f"/posts/{post.pk}/claim/").status_code, 302)


class GuardhouseTests(FindItTestCase):
    def setUp(self):
        super().setUp()
        self.post = self.create_post()
        self.ticket = self.post.turn_in_tickets.get()
        self.staff_client = self.client_for(self.staff)
        self.finder_client = self.client_for(self.finder)

    def test_qr_is_private_and_scan_confirms(self):
        self.assertEqual(len(self.ticket.code), 8)
        self.assertIn("<svg", self.finder_client.get(f"/p/{self.post.pk}/").content.decode())
        self.assertNotIn(self.ticket.code, self.client_for(self.alice).get(f"/p/{self.post.pk}/").content.decode())
        self.assertEqual(self.client_for(self.alice).get(f"/tickets/{self.ticket.code}/qr.svg").status_code, 403)
        public = self.client_for(self.alice).get(f"/staff/turn-in/{self.ticket.code}/")
        self.assertContains(public, "Bringing it to the guard house?")
        self.assertNotContains(public, "Confirm turned in")
        self.assertNotContains(public, self.finder.username)
        self.assertContains(Client().get(f"/staff/turn-in/{self.ticket.code}/"), self.ticket.code)
        self.assertEqual(self.client_for(self.alice).post(f"/staff/turn-in/{self.ticket.code}/", {"handed_over_by": "finder"}).status_code, 403)
        self.assertEqual(Client().post(f"/staff/turn-in/{self.ticket.code}/").status_code, 403)
        self.assertEqual(self.staff_client.get("/staff/turn-in/NOPE1234/").status_code, 404)
        self.finder_client.post(f"/posts/{self.post.pk}/turn-over/")
        self.post.refresh_from_db()
        self.assertEqual(self.post.status, "OPEN")  # finder's request alone doesn't change status
        self.staff_client.post(f"/staff/turn-in/{self.ticket.code.lower()}/", {"via": "qr", "handed_over_by": "finder"})
        self.post.refresh_from_db()
        self.assertEqual(self.post.status, "TURNED_OVER")
        self.assertEqual(self.post.intake.method, "QR")
        self.assertEqual(self.post.intake.handed_over_by_user, self.finder)

    def test_claims_stay_open_finder_requests_staff_release(self):
        self.claim(self.post, self.alice)
        claim = self.post.claims.get()
        self.staff_client.post(f"/staff/turn-in/{self.ticket.code}/", {"handed_over_by": "finder"})
        self.assertFalse(Conversation.objects.get(claim=claim).is_closed)
        self.finder_client.post(f"/claims/{claim.pk}/approve/")
        claim.refresh_from_db()
        self.assertEqual(claim.status, "APPROVED")
        self.finder_client.post(f"/posts/{self.post.pk}/close/")
        self.post.refresh_from_db()
        self.assertEqual(self.post.status, "TURNED_OVER")
        self.finder_client.post(f"/posts/{self.post.pk}/status-request/", {"requested_status": "CLOSED", "reason": ""})
        self.assertEqual(StatusChangeRequest.objects.count(), 0)
        self.finder_client.post(f"/posts/{self.post.pk}/status-request/", {"requested_status": "CLOSED", "reason": "Owner got it"})
        row = StatusChangeRequest.objects.get()
        self.staff_client.post(f"/staff/status-requests/{row.pk}/review/", {"decision": "reject"})
        row.refresh_from_db()
        self.assertEqual(row.decision, "REJECTED")
        self.staff_client.post(f"/posts/{self.post.pk}/claimed/", {"claim_id": claim.pk})
        self.post.refresh_from_db()
        self.assertEqual(self.post.status, "CLAIMED")

    def test_grace_days_close_listing_and_chats(self):
        self.claim(self.post, self.alice)
        self.staff_client.post(f"/staff/turn-in/{self.ticket.code}/", {"handed_over_by": "finder"})
        settings_row = SystemSettings.current()
        settings_row.guardhouse_grace_days = 1
        settings_row.save()
        ItemPost.objects.filter(pk=self.post.pk).update(turned_over_at=timezone.now() - timedelta(days=2))
        process_due_actions()
        self.post.refresh_from_db()
        self.assertEqual(self.post.status, "CLOSED")
        self.assertTrue(Conversation.objects.get(post=self.post).is_closed)

    def test_walk_in(self):
        r = self.staff_client.post("/staff/walk-in/", {"category": self.category.pk, "photos": [photo()], "handed_over_by_name": "Visitor"})
        post = ItemPost.objects.filter(author=self.staff).latest("created_at")
        self.assertEqual((post.post_type, post.status, post.intake.method), ("FOUND", "TURNED_OVER", "WALK_IN"))
        self.assertEqual(self.client_for(self.alice).get("/staff/walk-in/").status_code, 403)


class BackupTests(FindItTestCase):
    def test_backup_and_restore_roundtrip(self):
        post = self.create_post()
        folder = backups.create_backup("test")
        self.assertTrue((folder / "data.json").exists())
        self.assertTrue((folder / "media.zip").exists())
        ItemPost.objects.all().delete()
        backups.restore_backup(folder.name)
        self.assertTrue(ItemPost.objects.filter(pk=post.pk).exists())
        self.assertTrue(Path(ItemPost.objects.get(pk=post.pk).photos.first().image.path).exists())
        self.assertTrue(backups.last_status()["ok"])

    def test_daily_backup_runs_once_a_day(self):
        import tempfile
        from django.test import override_settings
        with override_settings(BACKUP_DIR=tempfile.mkdtemp(prefix="findit-daily-")):
            self.assertIsNotNone(backups.run_daily_backup_if_due())
            self.assertIsNone(backups.run_daily_backup_if_due())
