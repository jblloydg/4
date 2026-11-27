from datetime import timedelta

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from .models import (
    AbuseReport, ClaimApplication, Conversation, InAppNotification, ItemPost,
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


def notify_user(user, post, text, claim=None):
    notification = InAppNotification.objects.create(
        recipient=user, post=post, claim=claim, text=text[:240]
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
        claims = claims.filter(status=ClaimApplication.Status.SUBMITTED)
    for claim in claims:
        notify_user(claim.claimant, post, text, claim)


def create_claim_conversation(claim):
    return Conversation.objects.get_or_create(
        claim=claim,
        defaults={
            "post": claim.post,
            "initiator": claim.claimant,
            "recipient": claim.post.author,
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
    if claim.status not in {ClaimApplication.Status.SUBMITTED, ClaimApplication.Status.RETURNED}:
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
    if claim.post.status == ItemPost.Status.CLAIM_IN_PROGRESS:
        if not claim.post.claims.filter(status=ClaimApplication.Status.SUBMITTED).exists():
            record_status(claim.post, ItemPost.Status.OPEN, actor, "No active claims remain.")
    notify_user(claim.claimant, claim.post, "Your claim was closed.", claim)
    if claim.post.author_id != claim.claimant_id:
        notify_user(claim.post.author, claim.post, "A claimant closed their application.", claim)
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
        post.claims.filter(
            status__in=[ClaimApplication.Status.SUBMITTED, ClaimApplication.Status.RETURNED]
        ).select_related("claimant")
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
    if post.post_type != ItemPost.Type.FOUND:
        raise ValueError("Only a found-item post can be returned to a claimant.")
    if claim.status != ClaimApplication.Status.SUBMITTED:
        raise ValueError("That claim is no longer active.")
    record_status(post, ItemPost.Status.RETURNED, actor, "Finder recorded the hand-off.", claim)
    post.returned_application = claim
    post.save(update_fields=["returned_application", "updated_at"])
    claim.status = ClaimApplication.Status.RETURNED
    claim.last_activity_at = timezone.now()
    claim.save(update_fields=["status", "last_activity_at"])
    for other in post.claims.exclude(pk=claim.pk).filter(status=ClaimApplication.Status.SUBMITTED):
        other.status = ClaimApplication.Status.CLOSED
        other.closed_at = timezone.now()
        other.close_reason = "The item was returned to another claimant."
        other.save(update_fields=["status", "closed_at", "close_reason"])
        if hasattr(other, "conversation"):
            close_conversation(other.conversation, "The item was returned.")
        notify_user(other.claimant, post, "Status update: this item has been returned. Other claimant details are private.", other)
    notify_user(claim.claimant, post, "The finder recorded that the item was returned to you. Confirm receipt when you have it.", claim)
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
        if post.returned_application_id != claim.pk:
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
    for claim in post.claims.filter(status__in=[ClaimApplication.Status.SUBMITTED, ClaimApplication.Status.RETURNED]):
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
        status__in=[ClaimApplication.Status.SUBMITTED, ClaimApplication.Status.RETURNED],
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
        notify_user(claim.post.author, claim.post, "A claim on your post closed after seven days without activity.", claim)
        if claim.post.status == ItemPost.Status.CLAIM_IN_PROGRESS:
            if not claim.post.claims.filter(status=ClaimApplication.Status.SUBMITTED).exists():
                record_status(claim.post, ItemPost.Status.OPEN, None, "No active claims remain.")

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
