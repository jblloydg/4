import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from .validators import validate_photo


class Profile(models.Model):
    class Role(models.TextChoices):
        STUDENT = "student", "Student"
        STAFF = "staff", "Staff"
        ADMIN = "admin", "Administrator"

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile")
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.STUDENT)
    student_id = models.CharField(max_length=9, unique=True, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.user.get_full_name() or self.user.username


class Category(models.Model):
    """Item category, managed by administrators. Required on every post."""
    name = models.CharField(max_length=60, unique=True)
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=100)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sort_order", "name"]
        verbose_name_plural = "categories"

    def __str__(self):
        return self.name


class ItemPost(models.Model):
    class Type(models.TextChoices):
        LOST = "LOST", "Lost"
        FOUND = "FOUND", "Found"

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open for Claim"
        CLAIM_IN_PROGRESS = "CLAIM_IN_PROGRESS", "Claim in Progress"
        TURNED_OVER = "TURNED_OVER", "At Guard House"
        RETURNED = "RETURNED", "Returned"
        CLAIMED = "CLAIMED", "Claimed"
        CLOSED = "CLOSED", "Closed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="item_posts")
    post_type = models.CharField(max_length=5, choices=Type.choices)
    category = models.ForeignKey(
        Category, on_delete=models.PROTECT, null=True, blank=True, related_name="posts",
        help_text="Required for new posts. Older rows may be blank until edited.",
    )
    title = models.CharField(max_length=120, blank=True)
    description = models.TextField(max_length=1600, blank=True)
    approximate_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.OPEN)
    returned_application = models.ForeignKey(
        "ClaimApplication", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="returned_posts",
    )
    claimed_by_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="items_claimed",
    )
    claimed_at = models.DateTimeField(null=True, blank=True)
    turned_over_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_reason = models.CharField(max_length=240, blank=True)
    is_archived = models.BooleanField(default=False)
    archived_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.display_title

    @property
    def display_title(self):
        """Title is optional; fall back to the category so lists stay readable."""
        if self.title:
            return self.title
        if self.category_id:
            return f"{self.get_post_type_display()} {self.category.name.lower()}"
        return f"{self.get_post_type_display()} item"

    @property
    def cover_photo(self):
        photos = getattr(self, "_prefetched_objects_cache", {}).get("photos")
        if photos is not None:
            return photos[0] if photos else None
        return self.photos.order_by("order", "pk").first()

    @property
    def can_accept_claims(self):
        return (
            self.post_type == self.Type.FOUND
            and self.status in {self.Status.OPEN, self.Status.CLAIM_IN_PROGRESS}
            and not self.is_archived
        )

    @property
    def author_name(self):
        return self.author.get_full_name() or self.author.username


class PostPhoto(models.Model):
    """One of 1-20 photos attached to a post. Photos are required; video is not allowed."""
    post = models.ForeignKey(ItemPost, on_delete=models.CASCADE, related_name="photos")
    image = models.ImageField(upload_to="item-photos/%Y/%m/", validators=[validate_photo])
    order = models.PositiveSmallIntegerField(default=0)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["order", "pk"]

    def __str__(self):
        return f"Photo {self.order} for {self.post}"


class VerificationQuestion(models.Model):
    post = models.ForeignKey(ItemPost, on_delete=models.CASCADE, related_name="verification_questions")
    order = models.PositiveSmallIntegerField()
    text = models.CharField(max_length=240)

    class Meta:
        ordering = ["order"]
        constraints = [
            models.UniqueConstraint(fields=["post", "order"], name="unique_post_question_order")
        ]

    def __str__(self):
        return f"{self.post.title} — question {self.order}"


class ClaimApplication(models.Model):
    class Status(models.TextChoices):
        SUBMITTED = "SUBMITTED", "Submitted"
        RETURNED = "RETURNED", "Returned to claimant"
        COMPLETED = "COMPLETED", "Completed"
        CLOSED = "CLOSED", "Closed"
        EXPIRED = "EXPIRED", "Expired"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    post = models.ForeignKey(ItemPost, on_delete=models.CASCADE, related_name="claims")
    claimant = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="claims")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.SUBMITTED)
    submitted_at = models.DateTimeField(auto_now_add=True)
    last_activity_at = models.DateTimeField(default=timezone.now)
    closed_at = models.DateTimeField(null=True, blank=True)
    close_reason = models.CharField(max_length=240, blank=True)

    class Meta:
        ordering = ["-submitted_at"]

    def __str__(self):
        return f"Claim by {self.claimant} for {self.post}"

    @property
    def is_open(self):
        return self.status == self.Status.SUBMITTED


class ClaimAnswer(models.Model):
    claim = models.ForeignKey(ClaimApplication, on_delete=models.CASCADE, related_name="answers")
    question = models.ForeignKey(VerificationQuestion, on_delete=models.CASCADE, related_name="answers")
    answer_text = models.TextField(max_length=1200)
    submitted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["claim", "question"], name="one_answer_per_claim_question")
        ]

    def __str__(self):
        return f"Answer for {self.question}"


class Comment(models.Model):
    class Moderation(models.TextChoices):
        VISIBLE = "VISIBLE", "Visible"
        HIDDEN = "HIDDEN", "Hidden"
        REMOVED = "REMOVED", "Removed"

    post = models.ForeignKey(ItemPost, on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="comments")
    text = models.CharField(max_length=1000)
    moderation_status = models.CharField(max_length=10, choices=Moderation.choices, default=Moderation.VISIBLE)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"Comment by {self.author}"


class Conversation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    post = models.ForeignKey(ItemPost, on_delete=models.CASCADE, related_name="conversations")
    claim = models.OneToOneField(
        ClaimApplication, on_delete=models.CASCADE, null=True, blank=True, related_name="conversation"
    )
    initiator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="started_conversations")
    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="received_conversations")
    is_paused = models.BooleanField(default=False)
    is_closed = models.BooleanField(default=False)
    pause_reason = models.CharField(max_length=240, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["post", "initiator", "recipient"],
                condition=models.Q(claim__isnull=True),
                name="unique_lost_post_conversation",
            )
        ]

    def __str__(self):
        return f"Conversation about {self.post}"

    def includes(self, user):
        return user.pk in {self.initiator_id, self.recipient_id}


class ChatMessage(models.Model):
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="messages")
    sender = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="sent_messages")
    text = models.CharField(max_length=2000)
    sent_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sent_at"]

    def __str__(self):
        return f"Message from {self.sender}"


class PostStatusEvent(models.Model):
    post = models.ForeignKey(ItemPost, on_delete=models.CASCADE, related_name="status_events")
    previous_status = models.CharField(max_length=24, blank=True)
    new_status = models.CharField(max_length=24, choices=ItemPost.Status.choices)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="post_status_events"
    )
    related_claim = models.ForeignKey(ClaimApplication, on_delete=models.SET_NULL, null=True, blank=True)
    note = models.CharField(max_length=240, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class InAppNotification(models.Model):
    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="findit_notifications")
    post = models.ForeignKey(ItemPost, on_delete=models.CASCADE, related_name="notifications")
    claim = models.ForeignKey(ClaimApplication, on_delete=models.SET_NULL, null=True, blank=True)
    text = models.CharField(max_length=240)
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]


class AbuseReport(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending review"
        RESOLVED = "RESOLVED", "Resolved"
        ACTION_TAKEN = "ACTION_TAKEN", "Action taken"

    class Reason(models.TextChoices):
        HARASSMENT = "HARASSMENT", "Harassment or abusive messages"
        THREATS = "THREATS", "Threats or unsafe contact"
        SPAM = "SPAM", "Spam or repeated unwanted contact"
        FRAUD = "FRAUD", "False claim or suspected fraud"
        OTHER = "OTHER", "Other concern"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reporter = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="submitted_reports")
    conversation = models.ForeignKey(Conversation, on_delete=models.SET_NULL, null=True, blank=True, related_name="reports")
    target_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="reports_about")
    reason = models.CharField(max_length=16, choices=Reason.choices)
    details = models.TextField(max_length=1500, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    admin_note = models.TextField(max_length=1500, blank=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="reviewed_reports")
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["status", "-created_at"]


class SystemSettings(models.Model):
    """A single configurable settings row. Blank timers mean disabled."""
    guardhouse_grace_days = models.PositiveSmallIntegerField(null=True, blank=True)
    archive_purge_days = models.PositiveSmallIntegerField(null=True, blank=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def current(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return "Find-IT system settings"


class StaffAuditLog(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="staff_audit_events")
    action = models.CharField(max_length=80)
    target_type = models.CharField(max_length=60)
    target_id = models.CharField(max_length=80)
    details = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
