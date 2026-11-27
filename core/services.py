from datetime import timedelta

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.contrib.auth import get_user_model
from django.db import models, transaction
from django.utils import timezone

from .models import (
    AbuseReport, ChatQuestion, ClaimApplication, Conversation, InAppNotification, ItemPost,
    PostStatusEvent, Profile, QuestionTemplate, StaffAuditLog, SystemSettings, VerificationQuestion,
)
from .permissions import is_staff_or_admin

User = get_user_model()


def actor_name(user):
    return user.get_full_name() or user.username if user else "Find-IT"


def audit(actor, action, target, details=""):
    return StaffAuditLog.objects.create(
        actor=actor,
        action=action[:80],
        target_type=target.__class__.__name__,
        target_id=str(target.pk),
        details=details[:500],
    )


def record_status(post, new_status, actor=None, note="", related_claim=None):
    old_status = post.status
    if old_status == new_status:
        return None
    post.status = new_status
    post.save(update_fields=["status", "updated_at"])
    return PostStatusEvent.objects.create(
        post=post,
        previous_status=old_status,
        new_status=new_status,
        actor=actor,
        note=note[:240],
        related_claim=related_claim,
    )


MIN_POST_QUESTIONS = 3
MAX_POST_QUESTIONS = 5


def resolve_post_questions(picked):
    """Return 3-5 question templates: the finder's picks (max 5) topped up with
    active default questions until there are at least three."""
    chosen = list(picked)[:MAX_POST_QUESTIONS]
    if len(chosen) < MIN_POST_QUESTIONS:
        chosen_ids = {q.pk for q in chosen}
        defaults = QuestionTemplate.objects.filter(is_default=True, is_active=True).exclude(pk__in=chosen_ids)
        for template in defaults:
            if len(chosen) >= MIN_POST_QUESTIONS:
                break
            chosen.append(template)
    if len(chosen) < MIN_POST_QUESTIONS:
        fallback = QuestionTemplate.objects.filter(is_active=True, is_general=True).exclude(pk__in={q.pk for q in chosen})
        for template in fallback:
            if len(chosen) >= MIN_POST_QUESTIONS:
                break
            chosen.append(template)
    return chosen


def apply_post_questions(post, picked):
    """Replace a found post's questions with snapshots of the chosen templates.
    Callers must not do this once a claim exists."""
    post.verification_questions.all().delete()
    if post.post_type != ItemPost.Type.FOUND:
        return []
    created = []
    for order, template in enumerate(resolve_post_questions(picked), start=1):
        created.append(VerificationQuestion.objects.create(
            post=post,
            order=order,
            text=template.text,
            template=template,
            answer_type=template.answer_type,
            choices=list(template.choices or []),
        ))
    return created


def notify_user(user, post, text, claim=None, priority=False):
    text = text[:240]
    recent = InAppNotification.objects.filter(
        recipient=user, post=post, claim=claim, text=text,
        created_at__gte=timezone.now() - timedelta(seconds=60),
    ).first()
    if recent:
        # Identical notice within a minute (double click, retry): merge instead of duplicating.
        if priority and not recent.is_priority:
            recent.is_priority = True
            recent.save(update_fields=["is_priority"])
        return recent
    notification = InAppNotification.objects.create(
        recipient=user, post=post, claim=claim, text=text, is_priority=priority,
    )
    layer = get_channel_layer()
    if layer:
        async_to_sync(layer.group_send)(
            f"notifications_{user.pk}",
            {"type": "notification.created", "text": notification.text, "id": notification.pk},
        )
    return notification


def notify_claimants(post, text, include_open=True):
    claims = post.claims.select_related("claimant").all()
    if not include_open:
        claims = claims.filter(status__in=ClaimApplication.ACTIVE_STATUSES)
    for claim in claims:
        notify_user(claim.claimant, post, text, claim)


def create_claim_conversation(claim):
    return Conversation.objects.get_or_create(
        claim=claim,
        defaults={
            "post": claim.post,
            "initiator": claim.claimant,
            "recipient": claim.finder_user,
        },
    )[0]


def create_lost_post_conversation(post, finder):
    return Conversation.objects.get_or_create(
        post=post,
        claim=None,
        initiator=finder,
        recipient=post.author,
    )[0]


def close_conversation(conversation, reason=""):
    conversation.is_closed = True
    conversation.pause_reason = reason[:240]
    conversation.save(update_fields=["is_closed", "pause_reason", "updated_at"])


@transaction.atomic
def close_claim(claim, actor=None, reason="Closed by participant"):
    claim = ClaimApplication.objects.select_for_update().select_related("post", "claimant").get(pk=claim.pk)
    if claim.status not in ClaimApplication.ACTIVE_STATUSES:
        return claim
    claim.status = ClaimApplication.Status.CLOSED
    claim.closed_at = timezone.now()
    claim.close_reason = reason[:240]
    claim.save(update_fields=["status", "closed_at", "close_reason"])
    if claim.post.returned_application_id == claim.pk:
        claim.post.returned_application = None
        claim.post.save(update_fields=["returned_application", "updated_at"])
        record_status(claim.post, ItemPost.Status.OPEN, actor, "The selected claim was closed; the item is available again.")
    if hasattr(claim, "conversation"):
        close_conversation(claim.conversation, reason)
    reopen_if_no_active_claims(claim.post, actor)
    notify_user(claim.claimant, claim.post, "Your claim was closed.", claim)
    if claim.finder_user.pk != claim.claimant_id:
        notify_user(claim.finder_user, claim.post, "A claim was closed.", claim)
    if actor and is_staff_or_admin(actor):
        audit(actor, "closed_claim", claim, reason)
    return claim


@transaction.atomic
def turn_over_to_guardhouse(post, actor):
    post = ItemPost.objects.select_for_update().get(pk=post.pk)
    record_status(post, ItemPost.Status.TURNED_OVER, actor, "Item turned over to the guard house.")
    post.turned_over_at = timezone.now()
    post.returned_application = None
    post.save(update_fields=["turned_over_at", "returned_application", "updated_at"])
    active_claims = list(
        post.claims.filter(status__in=ClaimApplication.ACTIVE_STATUSES).select_related("claimant")
    )
    for claim in active_claims:
        claim.status = ClaimApplication.Status.CLOSED
        claim.closed_at = timezone.now()
        claim.close_reason = "The item was turned over to the guard house; pickup is in person."
        claim.save(update_fields=["status", "closed_at", "close_reason"])
    for conversation in post.conversations.all():
        close_conversation(conversation, "Pickup is now handled in person at the guard house.")
    notify_claimants(
        post,
        "Status update: this item is now at the guard house. Online claims are closed; pickup is handled in person.",
    )
    return post


@transaction.atomic
def return_to_claimant(post, claim, actor):
    post = ItemPost.objects.select_for_update().get(pk=post.pk)
    claim = ClaimApplication.objects.select_for_update().select_related("claimant").get(pk=claim.pk, post=post)
    if claim.status != ClaimApplication.Status.APPROVED:
        raise ValueError("Approve this claim before recording the return.")
    if post.status in {ItemPost.Status.CLAIMED, ItemPost.Status.CLOSED, ItemPost.Status.RETURNED}:
        raise ValueError("This item has already been returned or closed.")
    if post.status == ItemPost.Status.TURNED_OVER:
        raise ValueError("This item is at the guard house; staff record the release there.")
    record_status(post, ItemPost.Status.RETURNED, actor, "Finder recorded the hand-off.", claim)
    post.returned_application = claim
    post.save(update_fields=["returned_application", "updated_at"])
    claim.status = ClaimApplication.Status.RETURNED
    claim.last_activity_at = timezone.now()
    claim.save(update_fields=["status", "last_activity_at"])
    for other in post.claims.exclude(pk=claim.pk).filter(status__in=["SUBMITTED", "APPROVED"]):
        other.status = ClaimApplication.Status.CLOSED
        other.closed_at = timezone.now()
        other.close_reason = "The item was returned to another claimant."
        other.save(update_fields=["status", "closed_at", "close_reason"])
        if hasattr(other, "conversation"):
            close_conversation(other.conversation, "The item was returned.")
        notify_user(other.claimant, post, "Status update: this item has been returned. Other claimant details are private.", other)
    notify_user(claim.claimant, post, "The finder recorded that the item was returned to you. Confirm receipt when you have it.", claim, priority=True)
    if hasattr(claim, "conversation"):
        claim.conversation.is_closed = False
        claim.conversation.save(update_fields=["is_closed", "updated_at"])
    return claim


@transaction.atomic
def confirm_claimed(post, actor, claim=None, witnessed=False, recipient=None):
    post = ItemPost.objects.select_for_update().get(pk=post.pk)
    if claim is None:
        if not witnessed or not is_staff_or_admin(actor):
            raise PermissionError("A staff member must witness a guard-house pickup.")
        recipient_user = recipient
        related_claim = None
    else:
        claim = ClaimApplication.objects.select_for_update().select_related("claimant").get(pk=claim.pk, post=post)
        if post.status == ItemPost.Status.CLAIMED:
            return post
        guardhouse_release = (
            post.status == ItemPost.Status.TURNED_OVER and witnessed and is_staff_or_admin(actor)
            and claim.status == ClaimApplication.Status.APPROVED
        )
        if post.returned_application_id != claim.pk and not guardhouse_release:
            raise PermissionError("Only the selected claimant can confirm this hand-off.")
        if actor.pk != claim.claimant_id and not (witnessed and is_staff_or_admin(actor)):
            raise PermissionError("Only the claimant can confirm receipt.")
        recipient_user = claim.claimant
        related_claim = claim
    post.claimed_by_user = recipient_user
    post.claimed_at = timezone.now()
    post.closed_at = timezone.now()
    post.closed_reason = "Claim completed."
    record_status(post, ItemPost.Status.CLAIMED, actor, "Receipt confirmed.", related_claim)
    post.is_archived = True
    post.archived_at = timezone.now()
    post.save(update_fields=["claimed_by_user", "claimed_at", "closed_at", "closed_reason", "is_archived", "archived_at", "updated_at"])
    if claim:
        claim.status = ClaimApplication.Status.COMPLETED
        claim.closed_at = timezone.now()
        claim.save(update_fields=["status", "closed_at"])
    for other in post.claims.exclude(status__in=[ClaimApplication.Status.CLOSED, ClaimApplication.Status.EXPIRED, ClaimApplication.Status.COMPLETED]):
        other.status = ClaimApplication.Status.CLOSED
        other.closed_at = timezone.now()
        other.close_reason = "The item has been claimed."
        other.save(update_fields=["status", "closed_at", "close_reason"])
    for conversation in post.conversations.all():
        close_conversation(conversation, "The claim is complete.")
    notify_claimants(post, "Status update: this item has been claimed. Other claimant details are private.")
    if witnessed:
        audit(actor, "witnessed_guardhouse_pickup", post, f"Claimed by account {recipient_user.pk}.")
    return post


@transaction.atomic
def close_post(post, actor, reason=""):
    post = ItemPost.objects.select_for_update().get(pk=post.pk)
    record_status(post, ItemPost.Status.CLOSED, actor, reason or "Listing closed.")
    post.closed_at = timezone.now()
    post.closed_reason = reason[:240]
    post.is_archived = True
    post.archived_at = timezone.now()
    post.save(update_fields=["closed_at", "closed_reason", "is_archived", "archived_at", "updated_at"])
    for claim in post.claims.filter(status__in=ClaimApplication.ACTIVE_STATUSES):
        claim.status = ClaimApplication.Status.CLOSED
        claim.closed_at = timezone.now()
        claim.close_reason = reason[:240] or "Listing closed."
        claim.save(update_fields=["status", "closed_at", "close_reason"])
        if hasattr(claim, "conversation"):
            close_conversation(claim.conversation, "Listing closed.")
    for conversation in post.conversations.all():
        close_conversation(conversation, "Listing closed.")
    notify_claimants(post, "Status update: this listing is closed.")
    if is_staff_or_admin(actor):
        audit(actor, "closed_post", post, reason)
    return post


def archive_post(post, actor):
    if not post.is_archived:
        post.is_archived = True
        post.archived_at = timezone.now()
        post.save(update_fields=["is_archived", "archived_at", "updated_at"])
    if is_staff_or_admin(actor):
        audit(actor, "archived_post", post, "Removed from active listings; history retained.")
    return post


def process_due_actions():
    now = timezone.now()
    settings_row = SystemSettings.current()
    expired_before = now - timedelta(days=7)
    expired_claims = ClaimApplication.objects.filter(
        status__in=ClaimApplication.ACTIVE_STATUSES,
        last_activity_at__lt=expired_before,
    ).select_related("post", "claimant")
    for claim in expired_claims:
        was_selected_return = claim.post.returned_application_id == claim.pk
        claim.status = ClaimApplication.Status.EXPIRED
        claim.closed_at = now
        claim.close_reason = "No activity for seven days."
        claim.save(update_fields=["status", "closed_at", "close_reason"])
        if was_selected_return:
            claim.post.returned_application = None
            claim.post.save(update_fields=["returned_application", "updated_at"])
            record_status(claim.post, ItemPost.Status.OPEN, None, "The selected claim expired; the item is available again.")
        if hasattr(claim, "conversation"):
            close_conversation(claim.conversation, "No activity for seven days.")
        notify_user(claim.claimant, claim.post, "Your claim closed after seven days without activity.", claim)
        notify_user(claim.finder_user, claim.post, "A claim on your post closed after seven days without activity.", claim)
        reopen_if_no_active_claims(claim.post, None)

    if settings_row.guardhouse_grace_days:
        cutoff = now - timedelta(days=settings_row.guardhouse_grace_days)
        for post in ItemPost.objects.filter(status=ItemPost.Status.TURNED_OVER, turned_over_at__lt=cutoff):
            close_post(post, None, "Guard-house grace period ended.")

    if settings_row.archive_purge_days:
        cutoff = now - timedelta(days=settings_row.archive_purge_days)
        eligible = ItemPost.objects.filter(is_archived=True, archived_at__lt=cutoff)
        for post in eligible:
            has_open_report = AbuseReport.objects.filter(
                status=AbuseReport.Status.PENDING,
                conversation__post=post,
            ).exists()
            if not has_open_report:
                post.delete()


# ---------------------------------------------------------------------------
# Approve / decline / re-apply
# ---------------------------------------------------------------------------

def reopen_if_no_active_claims(post, actor):
    post.refresh_from_db(fields=["status"])
    if post.status == ItemPost.Status.CLAIM_IN_PROGRESS:
        if not post.claims.filter(status__in=["SUBMITTED", "APPROVED"]).exists():
            record_status(post, ItemPost.Status.OPEN, actor, "No active claims remain.")


def can_decide_claim(user, claim):
    """FOUND posts: the post author or staff. LOST posts: the finder in that chat or staff."""
    if not getattr(user, "is_authenticated", False):
        return False
    return user.pk == claim.finder_user.pk or is_staff_or_admin(user)


def claim_counts(post):
    claims = post.claims.all()
    return {
        "claiming": claims.filter(status__in=ClaimApplication.ACTIVE_STATUSES).count(),
        "approved": claims.filter(status__in=["APPROVED", "RETURNED"]).count(),
    }


def current_round_questions(conversation):
    """Questions asked for the conversation's current claim (a re-application starts a new round)."""
    questions = conversation.questions.all()
    if conversation.claim_id:
        questions = questions.filter(asked_at__gte=conversation.claim.submitted_at)
    return questions


def chat_questions_ready(claim):
    """LOST reports need at least three answered question cards (none pending) before approval."""
    conversation = getattr(claim, "conversation", None)
    if conversation is None:
        return False
    questions = current_round_questions(conversation)
    answered = questions.filter(answered_at__isnull=False).count()
    return answered >= MIN_POST_QUESTIONS and answered == questions.count()


@transaction.atomic
def approve_claim(claim, actor):
    claim = ClaimApplication.objects.select_for_update().select_related("post", "claimant", "finder").get(pk=claim.pk)
    if not can_decide_claim(actor, claim):
        raise PermissionError("Only the finder or authorized staff can approve this claim.")
    if claim.status == ClaimApplication.Status.APPROVED:
        return claim
    if claim.status != ClaimApplication.Status.SUBMITTED:
        raise ValueError("Only a submitted claim can be approved.")
    if claim.post.status in {ItemPost.Status.CLAIMED, ItemPost.Status.CLOSED}:
        raise ValueError("This listing is already resolved.")
    if claim.is_lost_report and not chat_questions_ready(claim):
        raise ValueError("Send at least three questions and wait until all of them are answered before approving.")
    claim.status = ClaimApplication.Status.APPROVED
    claim.approved_at = timezone.now()
    claim.approved_by = actor
    claim.last_activity_at = timezone.now()
    claim.save(update_fields=["status", "approved_at", "approved_by", "last_activity_at"])
    rank = claim.approval_rank
    notify_user(
        claim.claimant, claim.post,
        f"Your claim was approved. You are #{rank} in the approval order. Coordinate the hand-off in the chat.",
        claim, priority=True,
    )
    if is_staff_or_admin(actor) and actor.pk != claim.finder_user.pk:
        audit(actor, "approved_claim", claim, f"Approval #{rank}.")
        notify_user(claim.finder_user, claim.post, "Staff approved a claim on this item.", claim)
    return claim


@transaction.atomic
def decline_claim(claim, actor, reason=""):
    claim = ClaimApplication.objects.select_for_update().select_related("post", "claimant", "finder").get(pk=claim.pk)
    if not can_decide_claim(actor, claim):
        raise PermissionError("Only the finder or authorized staff can decline this claim.")
    if claim.status == ClaimApplication.Status.DECLINED:
        return claim
    if claim.status not in {ClaimApplication.Status.SUBMITTED, ClaimApplication.Status.APPROVED}:
        raise ValueError("This claim can no longer be declined.")
    reason = (reason or "").strip()[:500]
    claim.status = ClaimApplication.Status.DECLINED
    claim.declined_at = timezone.now()
    claim.declined_by = actor
    claim.decline_reason = reason
    claim.closed_at = timezone.now()
    claim.close_reason = "Declined."
    claim.save(update_fields=["status", "declined_at", "declined_by", "decline_reason", "closed_at", "close_reason"])
    if hasattr(claim, "conversation"):
        close_conversation(claim.conversation, "This claim was declined. The chat is read-only.")
    reopen_if_no_active_claims(claim.post, actor)
    text = "Your claim was declined." + (f" Reason: {reason}" if reason else "")
    if not claim.reapply_of_id:
        text += " You may re-apply once from My Claims."
    notify_user(claim.claimant, claim.post, text, claim, priority=True)
    if is_staff_or_admin(actor) and actor.pk != claim.finder_user.pk:
        audit(actor, "declined_claim", claim, reason or "No reason given.")
        notify_user(claim.finder_user, claim.post, "Staff declined a claim on this item.", claim)
    return claim


def reapply_status(post, user):
    """Return (can_apply, declined_claim_to_link). Declined claimants may re-apply once."""
    mine = post.claims.filter(claimant=user)
    if mine.filter(status__in=ClaimApplication.ACTIVE_STATUSES).exists():
        return False, None
    declined = mine.filter(status=ClaimApplication.Status.DECLINED).order_by("-declined_at")
    if not declined.exists():
        return True, None
    if mine.filter(reapply_of__isnull=False).exists():
        return False, None
    return True, declined.first()


# ---------------------------------------------------------------------------
# Chat question cards (FOUND claim chats and LOST finder chats)
# ---------------------------------------------------------------------------

def chat_roles(conversation):
    """(asker, answerer): the finder side asks, the claimant/owner side answers."""
    post = conversation.post
    if post.post_type == ItemPost.Type.LOST:
        finder = conversation.initiator if conversation.recipient_id == post.author_id else conversation.recipient
        return finder, post.author
    claim = conversation.claim
    if claim is None:
        return None, None
    return claim.finder_user, claim.claimant


def broadcast_conversation(conversation, payload):
    layer = get_channel_layer()
    if layer:
        async_to_sync(layer.group_send)(f"conversation_{conversation.pk.hex}", {"type": "chat.event", "payload": payload})


def question_payload(question):
    return {
        "event": "question",
        "id": question.pk,
        "order": question.order,
        "text": question.text,
        "answer_type": question.answer_type,
        "choices": question.choices,
        "answered": question.is_answered,
        "answer": question.answer_text,
    }


@transaction.atomic
def ask_chat_questions(conversation, actor, templates):
    conversation = Conversation.objects.select_for_update().select_related("post", "claim").get(pk=conversation.pk)
    asker, answerer = chat_roles(conversation)
    if asker is None or actor.pk != asker.pk:
        raise PermissionError("Only the finder in this chat can send ownership questions.")
    if conversation.is_closed or conversation.is_paused:
        raise ValueError("This chat is read-only.")
    claim = conversation.claim
    if claim is not None and claim.status not in {"SUBMITTED", "APPROVED"}:
        raise ValueError("Questions can only be sent while the claim is being reviewed.")
    if conversation.post.post_type == ItemPost.Type.LOST and claim is None:
        claim = open_lost_report(conversation, actor)
    round_questions = current_round_questions(conversation)
    existing_ids = set(round_questions.values_list("template_id", flat=True))
    room = MAX_POST_QUESTIONS - round_questions.count()
    new = []
    for template in templates:
        if template.pk not in existing_ids and template not in new:
            new.append(template)
    new = new[:max(room, 0)]
    if not new:
        raise ValueError(f"Each round can hold at most {MAX_POST_QUESTIONS} different questions.")
    next_order = (conversation.questions.aggregate(m=models.Max("order"))["m"] or 0) + 1
    created = []
    for offset, template in enumerate(new):
        created.append(ChatQuestion.objects.create(
            conversation=conversation, order=next_order + offset, template=template, text=template.text,
            answer_type=template.answer_type, choices=list(template.choices or []), asked_by=actor,
        ))
    if claim:
        ClaimApplication.objects.filter(pk=claim.pk).update(last_activity_at=timezone.now())
    notify_user(answerer, conversation.post, f"{len(created)} ownership question(s) are waiting for you in the chat.", claim, priority=True)
    for question in created:
        broadcast_conversation(conversation, question_payload(question))
    return created


@transaction.atomic
def answer_chat_question(question, actor, answer):
    question = ChatQuestion.objects.select_for_update().select_related("conversation", "conversation__post", "conversation__claim").get(pk=question.pk)
    conversation = question.conversation
    asker, answerer = chat_roles(conversation)
    if answerer is None or actor.pk != answerer.pk:
        raise PermissionError("Only the claimant can answer these questions.")
    if conversation.is_closed or conversation.is_paused:
        raise ValueError("This chat is read-only.")
    if question.is_answered:
        return question
    answer = (answer or "").strip()
    if not answer:
        raise ValueError("Write or choose an answer.")
    if question.answer_type == "CHOICE" and question.choices and answer not in question.choices:
        raise ValueError("Choose one of the listed answers.")
    question.answer_text = answer[:1200]
    question.answered_by = actor
    question.answered_at = timezone.now()
    question.save(update_fields=["answer_text", "answered_by", "answered_at"])
    if conversation.claim_id:
        ClaimApplication.objects.filter(pk=conversation.claim_id).update(last_activity_at=timezone.now())
    remaining = conversation.questions.filter(answered_at__isnull=True).count()
    text = "All ownership questions were answered. Review them in the chat." if remaining == 0 else "An ownership question was answered."
    notify_user(asker, conversation.post, text, conversation.claim, priority=True)
    broadcast_conversation(conversation, question_payload(question))
    return question


def open_lost_report(conversation, finder):
    """The first question a finder sends on a LOST post opens a verification record
    (a ClaimApplication with the owner as claimant and this finder as verifier)."""
    post = conversation.post
    claim = ClaimApplication.objects.create(
        post=post, claimant=post.author, finder=finder, description="",
    )
    conversation.claim = claim
    conversation.save(update_fields=["claim", "updated_at"])
    if post.status == ItemPost.Status.OPEN:
        record_status(post, ItemPost.Status.CLAIM_IN_PROGRESS, finder, "A finder started verifying the owner.", claim)
    return claim


@transaction.atomic
def reopen_lost_report(conversation, owner):
    """LOST re-apply: the owner asks the finder to verify them once more."""
    conversation = Conversation.objects.select_for_update().select_related("post", "claim").get(pk=conversation.pk)
    old = conversation.claim
    if old is None or old.status != ClaimApplication.Status.DECLINED or owner.pk != old.claimant_id:
        raise PermissionError("Only a declined owner can ask to be verified again.")
    if old.reapply_of_id or old.reapplications.exists():
        raise ValueError("You have already used your one re-application.")
    claim = ClaimApplication.objects.create(
        post=old.post, claimant=owner, finder=old.finder, reapply_of=old, description="",
    )
    conversation.claim = claim
    conversation.is_closed = False
    conversation.pause_reason = ""
    conversation.save(update_fields=["claim", "is_closed", "pause_reason", "updated_at"])
    if old.post.status == ItemPost.Status.OPEN:
        record_status(old.post, ItemPost.Status.CLAIM_IN_PROGRESS, owner, "Owner asked to be verified again.", claim)
    notify_user(old.finder, old.post, "The owner asked to be verified again. Send new questions in the chat.", claim, priority=True)
    return claim
