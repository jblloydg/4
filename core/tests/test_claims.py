from core.models import ChatQuestion, ClaimApplication, Conversation, InAppNotification, ItemPost, QuestionTemplate
from core.services import process_due_actions

from .base import FindItTestCase


class ApprovalTests(FindItTestCase):
    def setUp(self):
        super().setUp()
        self.post = self.create_post()
        for user in (self.alice, self.bob, self.carol):
            self.claim(self.post, user)
        self.a, self.b, self.c = (self.post.claims.get(claimant=u) for u in (self.alice, self.bob, self.carol))
        self.finder_client = self.client_for(self.finder)

    def test_only_finder_or_staff_decide(self):
        self.assertEqual(self.client_for(self.bob).post(f"/claims/{self.a.pk}/approve/").status_code, 403)
        self.client_for(self.staff).post(f"/claims/{self.a.pk}/approve/")
        self.a.refresh_from_db()
        self.assertEqual(self.a.status, "APPROVED")

    def test_approval_order_counts_and_private_rank(self):
        self.finder_client.post(f"/claims/{self.b.pk}/approve/")
        self.finder_client.post(f"/claims/{self.a.pk}/approve/")
        self.a.refresh_from_db(); self.b.refresh_from_db()
        self.assertEqual((self.b.approval_rank, self.a.approval_rank), (1, 2))
        page = self.client_for(self.carol).get(f"/p/{self.post.pk}/").content.decode()
        self.assertIn("3</strong> claiming", page)
        self.assertIn("2</strong> approved", page)
        self.assertNotIn("#1 approved", page)
        self.assertIn("#2 approved", self.client_for(self.alice).get(f"/p/{self.post.pk}/").content.decode())

    def test_decline_reason_read_only_chat_and_reapply_once(self):
        self.finder_client.post(f"/claims/{self.c.pk}/decline/", {"reason": "Wrong tag color"})
        self.c.refresh_from_db()
        self.assertEqual((self.c.status, self.c.decline_reason), ("DECLINED", "Wrong tag color"))
        self.assertTrue(self.c.conversation.is_closed)
        self.assertTrue(InAppNotification.objects.filter(recipient=self.carol, text__contains="Wrong tag color", is_priority=True).exists())
        self.claim(self.post, self.carol, "Second try")
        second = self.post.claims.get(claimant=self.carol, reapply_of=self.c)
        self.finder_client.post(f"/claims/{second.pk}/decline/")
        self.claim(self.post, self.carol, "Third try")
        self.assertEqual(self.post.claims.filter(claimant=self.carol).count(), 2)

    def test_return_only_to_approved_and_others_close(self):
        r = self.finder_client.post(f"/posts/{self.post.pk}/returned/", {"claim_id": self.a.pk}, follow=True)
        self.assertContains(r, "Approve this claim before")
        self.finder_client.post(f"/claims/{self.b.pk}/approve/")
        self.finder_client.post(f"/claims/{self.a.pk}/approve/")
        self.finder_client.post(f"/posts/{self.post.pk}/returned/", {"claim_id": self.a.pk})
        self.b.refresh_from_db()
        self.assertEqual(self.b.status, "CLOSED")
        self.client_for(self.alice).post(f"/posts/{self.post.pk}/claimed/")
        self.post.refresh_from_db()
        self.assertEqual(self.post.status, "CLAIMED")

    def test_seven_day_expiry_includes_approved(self):
        self.finder_client.post(f"/claims/{self.a.pk}/approve/")
        from django.utils import timezone
        from datetime import timedelta
        ClaimApplication.objects.filter(pk=self.a.pk).update(last_activity_at=timezone.now() - timedelta(days=8))
        process_due_actions()
        self.a.refresh_from_db()
        self.assertEqual(self.a.status, "EXPIRED")
        self.assertIsNone(self.a.approval_rank)

    def test_duplicate_submit_is_ignored(self):
        key = "same-key-123"
        self.finder_client.post(f"/claims/{self.c.pk}/decline/", {"reason": "first", "idempotency_key": key})
        r = self.finder_client.post(f"/claims/{self.a.pk}/decline/", {"reason": "second", "idempotency_key": key})
        self.assertEqual(r.status_code, 302)
        self.a.refresh_from_db()
        self.assertEqual(self.a.status, "SUBMITTED")
        self.assertEqual(InAppNotification.objects.filter(recipient=self.carol, text__startswith="Your claim was declined").count(), 1)


class LostVerificationTests(FindItTestCase):
    def setUp(self):
        super().setUp()
        self.lost = self.create_post(self.alice, "LOST")
        self.finder_client = self.client_for(self.finder)
        self.owner_client = self.client_for(self.alice)
        self.finder_client.get(f"/p/{self.lost.pk}/?resume=message")
        self.conv = Conversation.objects.get(post=self.lost, initiator=self.finder)
        self.templates = list(QuestionTemplate.objects.filter(is_default=True))

    def ask(self, templates, client=None):
        return (client or self.finder_client).post(f"/conversations/{self.conv.pk}/questions/", {"questions": [t.pk for t in templates]})

    def test_question_cards_and_three_answered_before_approve(self):
        self.ask(self.templates[:2])
        self.conv.refresh_from_db()
        report = self.conv.claim
        self.assertEqual((report.claimant, report.finder), (self.alice, self.finder))
        self.finder_client.post(f"/claims/{report.pk}/approve/")
        report.refresh_from_db()
        self.assertEqual(report.status, "SUBMITTED")
        self.ask(self.templates[2:3])
        self.assertEqual(self.ask(self.templates[:1], self.owner_client).status_code, 403)
        for q in self.conv.questions.all():
            self.owner_client.post(f"/chat-questions/{q.pk}/answer/", {"answer": q.choices[0] if q.choices else "silver"})
        self.assertEqual(ChatQuestion.objects.filter(conversation=self.conv, answered_at__isnull=False).count(), 3)
        page = self.finder_client.get(f"/conversations/{self.conv.pk}/").content.decode()
        self.assertEqual(page.count("question-card is-answered"), 3)
        self.assertTrue(InAppNotification.objects.filter(recipient=self.finder, is_priority=True).exists())
        self.finder_client.post(f"/claims/{report.pk}/approve/")
        report.refresh_from_db()
        self.assertEqual(report.status, "APPROVED")
        self.finder_client.post(f"/posts/{self.lost.pk}/returned/", {"claim_id": report.pk})
        self.owner_client.post(f"/posts/{self.lost.pk}/claimed/")
        self.lost.refresh_from_db()
        self.assertEqual(self.lost.status, "CLAIMED")

    def test_owner_reverify_once(self):
        self.ask(self.templates[:3])
        self.conv.refresh_from_db()
        report = self.conv.claim
        self.finder_client.post(f"/claims/{report.pk}/decline/", {"reason": "Answers didn't match"})
        self.owner_client.post(f"/conversations/{self.conv.pk}/reverify/")
        self.conv.refresh_from_db()
        self.assertEqual(self.conv.claim.reapply_of, report)
        self.assertFalse(self.conv.is_closed)
        self.finder_client.post(f"/claims/{self.conv.claim.pk}/decline/")
        self.owner_client.post(f"/conversations/{self.conv.pk}/reverify/")
        self.assertEqual(self.lost.claims.count(), 2)  # no third verification
