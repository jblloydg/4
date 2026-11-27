from datetime import timedelta

from django.contrib import messages
from django.contrib.auth import authenticate, get_user_model, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.http import Http404, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .forms import (
    AdminReviewForm, CategoryForm, ClaimAnswerForm, CloseReasonForm, CommentForm, QuestionTemplateForm,
    FindItLoginForm, ItemPostForm, ReportForm, SignupForm, StaffAccountForm,
    SystemSettingsForm,
)
from .models import (
    AbuseReport, Category, ClaimAnswer, ClaimApplication, Comment, Conversation,
    ChatQuestion, InAppNotification, ItemPost, PostShare, PostStatusEvent, Profile, QuestionTemplate, StaffAuditLog,
    SystemSettings, VerificationQuestion,
)
from .permissions import is_admin, is_staff_or_admin, role_of
from .services import (
    archive_post, audit, close_claim, close_post, confirm_claimed,
    answer_chat_question, apply_post_questions, approve_claim, ask_chat_questions, can_decide_claim,
    chat_questions_ready, chat_roles, claim_counts, create_claim_conversation, create_lost_post_conversation,
    current_round_questions, decline_claim, notify_user, reapply_status, record_status, reopen_lost_report,
    return_to_claimant, turn_over_to_guardhouse,
)

User = get_user_model()


def _safe_next(request, fallback="home"):
    target = request.POST.get("next") or request.GET.get("next") or ""
    if target and url_has_allowed_host_and_scheme(target, {request.get_host()}):
        return target
    return reverse(fallback)


def landing(request):
    if request.user.is_authenticated:
        return redirect("home")
    return render(request, "core/landing.html", {"login_form": FindItLoginForm()})


def login_view(request):
    if request.user.is_authenticated:
        return redirect(_safe_next(request))
    form = FindItLoginForm(request, data=request.POST or None)
    if request.method == "POST" and form.is_valid():
        login(request, form.get_user())
        return redirect(_safe_next(request))
    return render(request, "core/login.html", {"form": form, "next": request.GET.get("next", "")})


def signup_view(request):
    if request.user.is_authenticated:
        return redirect("home")
    form = SignupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        messages.success(request, "Your demo account is ready.")
        return redirect(_safe_next(request))
    return render(request, "core/signup.html", {"form": form})


@require_POST
def logout_view(request):
    logout(request)
    return redirect("landing")


@login_required
def home(request):
    posts = (
        ItemPost.objects.filter(is_archived=False)
        .select_related("author", "author__profile", "category")
        .prefetch_related("photos")
    )
    q = request.GET.get("q", "").strip()
    post_type = request.GET.get("type", "").strip()
    status = request.GET.get("status", "").strip()
    category = request.GET.get("category", "").strip()
    if q:
        posts = posts.filter(
            Q(title__icontains=q) | Q(description__icontains=q) | Q(category__name__icontains=q)
        )
    if post_type in {ItemPost.Type.LOST, ItemPost.Type.FOUND}:
        posts = posts.filter(post_type=post_type)
    if status in dict(ItemPost.Status.choices):
        posts = posts.filter(status=status)
    if category.isdigit():
        posts = posts.filter(category_id=int(category))
    return render(request, "core/home.html", {
        "posts": posts[:100],
        "q": q,
        "selected_type": post_type,
        "selected_status": status,
        "selected_category": category,
        "status_choices": ItemPost.Status.choices,
        "categories": Category.objects.filter(is_active=True),
    })


def _can_view_archived(post, user):
    # Soft removal hides the listing from active search but preserves direct links.
    return True


def _conversation_for_post(post, user):
    if post.is_archived or post.status in {
        ItemPost.Status.TURNED_OVER, ItemPost.Status.CLAIMED, ItemPost.Status.CLOSED
    }:
        return None
    if post.post_type == ItemPost.Type.FOUND:
        claim = post.claims.filter(claimant=user, status__in=ClaimApplication.ACTIVE_STATUSES).first()
        if claim:
            try:
                return claim.conversation
            except Conversation.DoesNotExist:
                return create_claim_conversation(claim)
    return create_lost_post_conversation(post, user)


def post_detail(request, post_id):
    post = get_object_or_404(
        ItemPost.objects.select_related("author", "category").prefetch_related("photos"), pk=post_id
    )
    if not _can_view_archived(post, request.user):
        raise Http404
    if not request.user.is_authenticated and not post.is_shared:
        # Not shared yet: the link only works for signed-in users.
        from urllib.parse import quote
        return redirect(f"{reverse('login')}?next={quote(request.get_full_path())}")

    resume = request.GET.get("resume", "")
    if request.user.is_authenticated and resume == "message" and request.user.pk != post.author_id:
        conversation = _conversation_for_post(post, request.user)
        if conversation:
            return redirect("chat-room", conversation_id=conversation.pk)
        messages.info(request, "This listing is no longer accepting new online conversations.")

    comments = post.comments.filter(moderation_status=Comment.Moderation.VISIBLE).select_related("author")
    user_claim = None
    latest_claim = None
    claim_form = None
    reapply_from = None
    if request.user.is_authenticated and post.post_type == ItemPost.Type.FOUND:
        mine = post.claims.filter(claimant=request.user).order_by("-submitted_at")
        user_claim = mine.filter(status__in=ClaimApplication.ACTIVE_STATUSES).first()
        latest_claim = user_claim or mine.first()
        if post.can_accept_claims and request.user.pk != post.author_id and not user_claim:
            can_apply, reapply_from = reapply_status(post, request.user)
            if can_apply:
                saved = request.session.get("claim_form_data") or {}
                data = saved.get("data") if saved.get("post") == str(post.pk) else None
                claim_form = ClaimAnswerForm(data, questions=post.verification_questions.all())

    can_manage = request.user.is_authenticated and (
        request.user.pk == post.author_id or is_staff_or_admin(request.user)
    )
    can_moderate = request.user.is_authenticated and is_staff_or_admin(request.user)
    can_interact = (
        not post.is_archived
        and post.status not in {
            ItemPost.Status.TURNED_OVER, ItemPost.Status.CLAIMED, ItemPost.Status.CLOSED
        }
    )
    applications = []
    account_list = []
    if can_manage:
        applications = list(post.claims.select_related("claimant", "finder").order_by("-submitted_at"))
    elif request.user.is_authenticated and post.post_type == ItemPost.Type.LOST:
        applications = list(post.claims.filter(finder=request.user).select_related("claimant", "finder"))
    for application in applications:
        application.can_decide = can_decide_claim(request.user, application)
        application.rank = application.approval_rank
    panel_conversation = None
    if request.user.is_authenticated and request.user.pk != post.author_id:
        if post.post_type == ItemPost.Type.FOUND and latest_claim is not None:
            panel_conversation = getattr(latest_claim, "conversation", None)
        elif post.post_type == ItemPost.Type.LOST:
            panel_conversation = post.conversations.filter(initiator=request.user).first()
    if can_moderate and post.status == ItemPost.Status.TURNED_OVER:
        account_list = User.objects.filter(profile__role=Profile.Role.STUDENT, is_active=True).order_by("first_name")
    context = {
        "post": post,
        "comments": comments,
        "comment_form": CommentForm() if request.user.is_authenticated else None,
        "claim_form": claim_form,
        "user_claim": user_claim,
        "latest_claim": latest_claim,
        "reapply_from": reapply_from,
        "claim_counts": claim_counts(post),
        "can_manage": can_manage,
        "can_moderate": can_moderate,
        "can_interact": can_interact,
        "applications": applications,
        "account_list": account_list,
        "resume": resume,
        "direct_link": request.build_absolute_uri(reverse("post-detail", args=[post.pk])),
        "shared_by_author": post.shares.filter(user_id=post.author_id).exists(),
        "share_count": post.shares.count(),
        "panel_conversation": panel_conversation,
        "panel_messages": list(panel_conversation.messages.select_related("sender").order_by("-sent_at")[:50])[::-1] if panel_conversation else [],
        "panel_can_send": bool(
            panel_conversation and panel_conversation.includes(request.user)
            and not panel_conversation.is_closed and not panel_conversation.is_paused
        ),
    }
    return render(request, "core/post_detail.html", context)


POSTS_PER_HOUR_LIMIT = 10


def _human_check(request, refresh=False):
    """A simple offline 'are you human' question used for uploads above 10 photos.
    The expected answer is kept in the session, never in the page."""
    import secrets
    challenge = request.session.get("post_human_check")
    if refresh or not challenge:
        a, b = secrets.randbelow(8) + 2, secrets.randbelow(8) + 2
        challenge = {"question": f"What is {a} + {b}?", "answer": a + b}
        request.session["post_human_check"] = challenge
    return challenge


def _question_options(form):
    """Question bank rows for the picker, with selection state and category ids."""
    raw = form["questions"].value() or []
    selected = {str(v) for v in raw}
    options = []
    for template in form.fields["questions"].queryset:
        options.append({
            "template": template,
            "selected": str(template.pk) in selected,
            "category_ids": " ".join(str(c.pk) for c in template.categories.all()),
        })
    return options


def _posting_rate_limited(user):
    since = timezone.now() - timedelta(hours=1)
    return ItemPost.objects.filter(author=user, created_at__gte=since).count() >= POSTS_PER_HOUR_LIMIT


@login_required
def create_post(request):
    challenge = _human_check(request)
    form = ItemPostForm(request.POST or None, request.FILES or None, human_check_answer=challenge["answer"])
    if request.method == "POST":
        if _posting_rate_limited(request.user):
            messages.error(request, f"You can create up to {POSTS_PER_HOUR_LIMIT} posts per hour. Please try again later.")
            return redirect("my-posts")
        if form.is_valid():
            with transaction.atomic():
                post = form.save(commit=False)
                post.author = request.user
                post.save()
                form.save_photos(post)
                apply_post_questions(post, form.picked_questions())
                PostStatusEvent.objects.create(
                    post=post,
                    previous_status="",
                    new_status=post.status,
                    actor=request.user,
                    note="Listing created.",
                )
            _human_check(request, refresh=True)
            messages.success(request, "Your post is live.")
            return redirect("post-detail", post_id=post.pk)
        challenge = _human_check(request, refresh=True)
        form.human_check_answer = challenge["answer"]
    return render(request, "core/post_form.html", {
        "form": form, "human_check_question": challenge["question"], "question_options": _question_options(form),
    })


@login_required
def edit_post(request, post_id):
    post = get_object_or_404(ItemPost, pk=post_id)
    if post.author_id != request.user.pk and not is_admin(request.user):
        return HttpResponseForbidden("Only the post author or an administrator may edit this post.")
    challenge = _human_check(request)
    form = ItemPostForm(request.POST or None, request.FILES or None, instance=post, human_check_answer=challenge["answer"])
    if post.claims.exists():
        form.fields["post_type"].disabled = True
        form.fields["questions"].disabled = True
        form.fields["questions"].help_text = "Locked after a claim is submitted so earlier answers stay attached to their questions."
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            updated = form.save()
            form.save_photos(updated)
            if not post.claims.exists():
                apply_post_questions(updated, form.picked_questions())
        messages.success(request, "Your post was updated.")
        return redirect("post-detail", post_id=post.pk)
    if request.method == "POST" and not form.is_valid():
        challenge = _human_check(request, refresh=True)
        form.human_check_answer = challenge["answer"]
    return render(request, "core/post_form.html", {
        "form": form, "editing": True, "post": post, "human_check_question": challenge["question"],
        "question_options": _question_options(form), "questions_locked": post.claims.exists(),
        "locked_questions": post.verification_questions.all(),
    })


@login_required
@require_POST
def submit_claim(request, post_id):
    post = get_object_or_404(ItemPost, pk=post_id)
    if post.author_id == request.user.pk:
        messages.error(request, "You cannot claim your own post.")
        return redirect("post-detail", post_id=post.pk)
    if not post.can_accept_claims:
        messages.error(request, "This item is not accepting online claims.")
        return redirect("post-detail", post_id=post.pk)
    if post.claims.filter(claimant=request.user, status__in=ClaimApplication.ACTIVE_STATUSES).exists():
        messages.info(request, "You already have a claim for this item.")
        return redirect("my-claims")
    can_apply, reapply_from = reapply_status(post, request.user)
    if not can_apply:
        messages.error(request, "You have already used your one re-application for this item.")
        return redirect("my-claims")

    questions = list(post.verification_questions.order_by("order"))
    if len(questions) < 3:
        messages.error(request, "This post is missing its private ownership questions. Please contact staff.")
        return redirect("post-detail", post_id=post.pk)
    form = ClaimAnswerForm(request.POST, questions=questions)
    if not form.is_valid():
        request.session["claim_form_data"] = {"post": str(post.pk), "data": request.POST.dict()}
        messages.error(request, "Please answer every question and write a short description (at least 10 characters).")
        return redirect(f"{reverse('post-detail', args=[post.pk])}?resume=claim#claim-form")
    request.session.pop("claim_form_data", None)

    with transaction.atomic():
        claim = ClaimApplication.objects.create(
            post=post, claimant=request.user, description=form.cleaned_data["description"].strip(),
            reapply_of=reapply_from,
        )
        for question in questions:
            ClaimAnswer.objects.create(
                claim=claim,
                question=question,
                answer_text=form.cleaned_data[f"answer_{question.pk}"].strip(),
            )
        create_claim_conversation(claim)
        if post.status == ItemPost.Status.OPEN:
            record_status(post, ItemPost.Status.CLAIM_IN_PROGRESS, request.user, "A claim was submitted.", claim)
        claim.last_activity_at = timezone.now()
        claim.save(update_fields=["last_activity_at"])
        notify_user(post.author, post, "A new private claim form was submitted for your found-item post.", claim)
    messages.success(request, "Your private claim application was submitted.")
    return redirect("my-claims")


@login_required
@require_POST
def add_comment(request, post_id):
    post = get_object_or_404(ItemPost, pk=post_id)
    if post.is_archived or post.status in {
        ItemPost.Status.TURNED_OVER, ItemPost.Status.CLAIMED, ItemPost.Status.CLOSED
    }:
        messages.error(request, "Comments are closed for this listing.")
        return redirect("post-detail", post_id=post.pk)
    form = CommentForm(request.POST)
    if form.is_valid():
        Comment.objects.create(post=post, author=request.user, text=form.cleaned_data["text"].strip())
        messages.success(request, "Comment added.")
    else:
        messages.error(request, "Add a comment of up to 1,000 characters.")
    return redirect(f"{reverse('post-detail', args=[post.pk])}#comments")


@login_required
def my_posts(request):
    posts = (
        ItemPost.objects.filter(author=request.user)
        .select_related("category").prefetch_related("photos", "claims")
        .order_by("-created_at")
    )
    return render(request, "core/my_posts.html", {"posts": posts})


@login_required
def my_claims(request):
    claims = list(
        ClaimApplication.objects.filter(claimant=request.user)
        .select_related("post", "post__author", "post__category", "finder", "conversation")
        .prefetch_related("post__photos")
    )
    for claim in claims:
        claim.rank = claim.approval_rank
        if claim.status == ClaimApplication.Status.DECLINED:
            can_apply, linked = reapply_status(claim.post, request.user)
            claim.can_reapply = can_apply and linked is not None and linked.pk == claim.pk and claim.post.can_accept_claims
            if claim.is_lost_report:
                claim.can_reapply = not claim.reapply_of_id and not claim.reapplications.exists()
    active = [c for c in claims if c.status in ClaimApplication.ACTIVE_STATUSES]
    declined = [c for c in claims if c.status == ClaimApplication.Status.DECLINED]
    history = [c for c in claims if c not in active and c not in declined]
    return render(request, "core/my_claims.html", {"active": active, "declined": declined, "history": history})


@login_required
def claim_review(request, claim_id):
    claim = get_object_or_404(
        ClaimApplication.objects.select_related("post", "claimant"),
        pk=claim_id,
    )
    if claim.post.author_id != request.user.pk and not is_staff_or_admin(request.user):
        return HttpResponseForbidden("Only the finder or authorized staff may review this private form.")
    if is_staff_or_admin(request.user) and claim.post.author_id != request.user.pk:
        audit(request.user, "viewed_claim_form", claim, "Staff opened private ownership answers.")
    answers = claim.answers.select_related("question").order_by("question__order")
    return render(request, "core/claim_review.html", {"claim": claim, "answers": answers})


@login_required
@require_POST
def close_claim_view(request, claim_id):
    claim = get_object_or_404(ClaimApplication.objects.select_related("post", "post__author"), pk=claim_id)
    if request.user.pk not in {claim.post.author_id, claim.claimant_id} and not is_staff_or_admin(request.user):
        return HttpResponseForbidden("Only the claim participants or staff can close this claim.")
    reason_form = CloseReasonForm(request.POST)
    if reason_form.is_valid():
        close_claim(claim, request.user, reason_form.cleaned_data.get("reason") or "Closed by participant.")
        messages.success(request, "The claim was closed. The listing can remain open for other claimants.")
    return redirect("post-detail", post_id=claim.post_id)


@login_required
@require_POST
def turnover_view(request, post_id):
    post = get_object_or_404(ItemPost, pk=post_id)
    if request.user.pk != post.author_id and not is_staff_or_admin(request.user):
        return HttpResponseForbidden("Only the finder or authorized staff can record guard-house turnover.")
    if post.post_type != ItemPost.Type.FOUND or post.status in {ItemPost.Status.CLAIMED, ItemPost.Status.CLOSED}:
        messages.error(request, "This item cannot be turned over in its current state.")
        return redirect("post-detail", post_id=post.pk)
    turn_over_to_guardhouse(post, request.user)
    messages.success(request, "The item is now listed as at the guard house. Existing claims were notified and closed.")
    return redirect("post-detail", post_id=post.pk)


@login_required
@require_POST
def returned_view(request, post_id):
    post = get_object_or_404(ItemPost, pk=post_id)
    claim = get_object_or_404(ClaimApplication.objects.select_related("post", "finder"), pk=request.POST.get("claim_id"), post=post)
    if request.user.pk != claim.finder_user.pk:
        return HttpResponseForbidden("Only the finder can record a return.")
    try:
        return_to_claimant(post, claim, request.user)
    except (ValueError, PermissionError) as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Return recorded. The claimant can confirm receipt.")
    return redirect("post-detail", post_id=post.pk)


@login_required
@require_POST
def claimed_view(request, post_id):
    post = get_object_or_404(ItemPost, pk=post_id)
    claim = post.returned_application
    witnessed = False
    recipient = None
    if post.status == ItemPost.Status.TURNED_OVER:
        if not is_staff_or_admin(request.user):
            return HttpResponseForbidden("Only staff can record an in-person guard-house pickup.")
        witnessed = True
        if request.POST.get("claim_id"):
            claim = get_object_or_404(ClaimApplication, pk=request.POST.get("claim_id"), post=post, status=ClaimApplication.Status.APPROVED)
        else:
            claim = None
            recipient = get_object_or_404(
                User, pk=request.POST.get("recipient_id"), profile__role=Profile.Role.STUDENT
            )
    elif not claim:
        messages.error(request, "The finder has not recorded a return for this item.")
        return redirect("post-detail", post_id=post.pk)
    elif is_staff_or_admin(request.user):
        witnessed = True
    try:
        confirm_claimed(post, request.user, claim=claim, witnessed=witnessed, recipient=recipient)
    except PermissionError as exc:
        return HttpResponseForbidden(str(exc))
    messages.success(request, "The claim is complete. The item is archived for history.")
    return redirect("post-detail", post_id=post.pk)


@login_required
@require_POST
def close_post_view(request, post_id):
    post = get_object_or_404(ItemPost, pk=post_id)
    if request.user.pk != post.author_id and not is_staff_or_admin(request.user):
        return HttpResponseForbidden("Only the author or staff can close this listing.")
    form = CloseReasonForm(request.POST)
    reason = form.cleaned_data.get("reason", "") if form.is_valid() else ""
    close_post(post, request.user, reason)
    messages.success(request, "The listing is closed and retained in history.")
    return redirect("post-detail", post_id=post.pk)


@login_required
@require_POST
def remove_post_view(request, post_id):
    post = get_object_or_404(ItemPost, pk=post_id)
    if request.user.pk != post.author_id and not is_staff_or_admin(request.user):
        return HttpResponseForbidden("Only the author or staff can remove this listing.")
    archive_post(post, request.user)
    messages.success(request, "The post was removed from active listings. Its claims and chats remain in history.")
    return redirect("my-posts" if request.user.pk == post.author_id else "staff-dashboard")


@login_required
def chat_room(request, conversation_id):
    conversation = get_object_or_404(
        Conversation.objects.select_related("post", "initiator", "recipient"),
        pk=conversation_id,
    )
    staff_view = is_staff_or_admin(request.user) and not conversation.includes(request.user)
    if not conversation.includes(request.user) and not staff_view:
        return HttpResponseForbidden("This conversation is private to its participants and authorized staff.")
    if is_staff_or_admin(request.user):
        audit(request.user, "reviewed_chat", conversation, "Staff opened private chat history.")
    messages_qs = conversation.messages.select_related("sender").all()
    asker, answerer = chat_roles(conversation)
    claim = conversation.claim
    questions = list(conversation.questions.all())
    InAppNotification.objects.filter(
        recipient=request.user, post=conversation.post, is_priority=True, read_at__isnull=True,
    ).update(read_at=timezone.now())
    can_send = conversation.includes(request.user) and not conversation.is_closed and not conversation.is_paused
    is_asker = bool(asker and asker.pk == request.user.pk)
    is_answerer = bool(answerer and answerer.pk == request.user.pk)
    question_bank = []
    round_count = 0
    if is_asker and can_send and (claim is None or claim.status in {"SUBMITTED", "APPROVED"}):
        round_count = current_round_questions(conversation).count() if claim else 0
        if round_count < 5:
            question_bank = list(QuestionTemplate.objects.filter(is_active=True).prefetch_related("categories"))
    can_decide = bool(claim and can_decide_claim(request.user, claim))
    return render(request, "core/chat_room.html", {
        "conversation": conversation,
        "chat_messages": messages_qs,
        "staff_view": staff_view,
        "can_send": can_send,
        "questions": questions,
        "is_asker": is_asker,
        "is_answerer": is_answerer,
        "question_bank": question_bank,
        "questions_left": 5 - round_count,
        "claim": claim,
        "claim_rank": claim.approval_rank if claim else None,
        "can_decide": can_decide,
        "lost_ready": chat_questions_ready(claim) if claim and claim.is_lost_report else True,
        "can_reverify": bool(
            claim and claim.is_lost_report and claim.status == "DECLINED" and request.user.pk == claim.claimant_id
            and not claim.reapply_of_id and not claim.reapplications.exists()
        ),
    })


@login_required
@require_POST
def pause_chat(request, conversation_id):
    if not is_staff_or_admin(request.user):
        return HttpResponseForbidden("Only staff can pause a conversation.")
    conversation = get_object_or_404(Conversation, pk=conversation_id)
    reason = request.POST.get("reason", "").strip()[:240] or "Paused for staff review."
    conversation.is_paused = True
    conversation.pause_reason = reason
    conversation.save(update_fields=["is_paused", "pause_reason", "updated_at"])
    audit(request.user, "paused_chat", conversation, reason)
    for participant in {conversation.initiator, conversation.recipient}:
        notify_user(participant, conversation.post, "A staff member paused this conversation for review.", conversation.claim)
    messages.success(request, "Chat paused. The participants can still read its history.")
    return redirect("chat-room", conversation_id=conversation.pk)


@login_required
@require_POST
def unpause_chat(request, conversation_id):
    if not is_admin(request.user):
        return HttpResponseForbidden("Only an administrator can restore a paused conversation.")
    conversation = get_object_or_404(Conversation, pk=conversation_id)
    if conversation.is_closed:
        messages.error(request, "A closed claim chat cannot be reopened.")
        return redirect("chat-room", conversation_id=conversation.pk)
    conversation.is_paused = False
    conversation.pause_reason = ""
    conversation.save(update_fields=["is_paused", "pause_reason", "updated_at"])
    audit(request.user, "resumed_chat", conversation, "Administrator restored messaging.")
    for participant in {conversation.initiator, conversation.recipient}:
        notify_user(participant, conversation.post, "An administrator restored messaging in this conversation.", conversation.claim)
    messages.success(request, "Chat restored.")
    return redirect("chat-room", conversation_id=conversation.pk)


@login_required
@require_POST
def report_chat(request, conversation_id):
    if not is_staff_or_admin(request.user):
        return HttpResponseForbidden("Only staff can submit reports for administrator review.")
    conversation = get_object_or_404(Conversation, pk=conversation_id)
    form = ReportForm(request.POST)
    if form.is_valid():
        target_id = request.POST.get("target_user_id") or None
        if target_id and str(target_id) not in {str(conversation.initiator_id), str(conversation.recipient_id)}:
            return HttpResponseForbidden("The reported account must be a participant in this conversation.")
        report = form.save(commit=False)
        report.reporter = request.user
        report.conversation = conversation
        report.target_user_id = target_id
        report.save()
        conversation.is_paused = True
        conversation.pause_reason = "Reported for administrator review."
        conversation.save(update_fields=["is_paused", "pause_reason", "updated_at"])
        audit(request.user, "reported_chat", report, "Sent to administrators.")
        messages.success(request, "Report submitted to administrators; chat paused for review.")
    else:
        messages.error(request, "Choose a report reason.")
    return redirect("chat-room", conversation_id=conversation.pk)


@login_required
def notifications(request):
    rows = InAppNotification.objects.filter(recipient=request.user).select_related("post", "claim")
    unread_ids = list(rows.filter(read_at__isnull=True).values_list("pk", flat=True)[:200])
    if unread_ids:
        InAppNotification.objects.filter(pk__in=unread_ids).update(read_at=timezone.now())
    return render(request, "core/notifications.html", {"notifications": rows[:200]})


@login_required
def messages_inbox(request):
    conversations = Conversation.objects.filter(
        Q(initiator=request.user) | Q(recipient=request.user)
    ).select_related("post", "initiator", "recipient").prefetch_related("messages")
    return render(request, "core/messages.html", {"conversations": conversations[:100]})


@login_required
def staff_dashboard(request):
    if not is_staff_or_admin(request.user):
        return HttpResponseForbidden("Staff access is required.")
    posts = ItemPost.objects.filter(is_archived=False).select_related("author").prefetch_related("claims")
    conversations = Conversation.objects.select_related("post", "initiator", "recipient")[:100]
    return render(request, "core/staff_dashboard.html", {
        "posts": posts[:100],
        "conversations": conversations,
        "recent_reports": AbuseReport.objects.filter(status=AbuseReport.Status.PENDING)[:20],
        "is_admin": is_admin(request.user),
    })


@login_required
def staff_accounts(request):
    if not is_admin(request.user):
        return HttpResponseForbidden("Administrator access is required.")
    form = StaffAccountForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        audit(request.user, "created_staff_account", user, "Staff account created by administrator.")
        messages.success(request, f"Staff account for {user.get_full_name()} was created.")
        return redirect("staff-accounts")
    staff_users = User.objects.filter(profile__role=Profile.Role.STAFF).order_by("first_name")
    return render(request, "core/staff_accounts.html", {"form": form, "staff_users": staff_users})


@login_required
def admin_settings(request):
    if not is_admin(request.user):
        return HttpResponseForbidden("Administrator access is required.")
    settings_row = SystemSettings.current()
    form = SystemSettingsForm(request.POST or None, instance=settings_row)
    if request.method == "POST" and form.is_valid():
        setting = form.save(commit=False)
        setting.updated_by = request.user
        setting.save()
        audit(request.user, "updated_system_timers", setting, "Guard-house grace and archive purge timers changed.")
        messages.success(request, "Timer settings saved. Blank values keep the timers disabled.")
        return redirect("admin-settings")
    return render(request, "core/admin_settings.html", {"form": form, "settings_row": settings_row})


@login_required
def admin_reports(request):
    if not is_admin(request.user):
        return HttpResponseForbidden("Administrator access is required.")
    reports = AbuseReport.objects.select_related("reporter", "target_user", "conversation", "conversation__post", "reviewed_by")
    return render(request, "core/admin_reports.html", {"reports": reports[:200]})


@login_required
@require_POST
def review_report(request, report_id):
    if not is_admin(request.user):
        return HttpResponseForbidden("Only administrators can decide account restrictions.")
    report = get_object_or_404(AbuseReport.objects.select_related("target_user"), pk=report_id)
    form = AdminReviewForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Choose an administrator action.")
        return redirect("admin-reports")
    action = form.cleaned_data["action"]
    if action == "suspend" and report.target_user:
        report.target_user.is_active = False
        report.target_user.save(update_fields=["is_active"])
        report.status = AbuseReport.Status.ACTION_TAKEN
        decision = "Account suspended."
    elif action == "restore" and report.target_user:
        report.target_user.is_active = True
        report.target_user.save(update_fields=["is_active"])
        report.status = AbuseReport.Status.ACTION_TAKEN
        decision = "Account restored."
    else:
        report.status = AbuseReport.Status.RESOLVED
        decision = "Report resolved without account action."
    report.admin_note = form.cleaned_data["admin_note"]
    report.reviewed_by = request.user
    report.resolved_at = timezone.now()
    report.save(update_fields=["status", "admin_note", "reviewed_by", "resolved_at"])
    audit(request.user, "reviewed_report", report, decision)
    messages.success(request, decision)
    return redirect("admin-reports")


@login_required
@require_POST
def staff_moderate_comment(request, comment_id):
    if not is_staff_or_admin(request.user):
        return HttpResponseForbidden("Staff access is required.")
    comment = get_object_or_404(Comment, pk=comment_id)
    action = request.POST.get("action")
    if action not in {Comment.Moderation.VISIBLE, Comment.Moderation.HIDDEN, Comment.Moderation.REMOVED}:
        return HttpResponseForbidden("Unknown moderation action.")
    comment.moderation_status = action
    comment.save(update_fields=["moderation_status"])
    audit(request.user, f"comment_{action.lower()}", comment, "Comment moderation action.")
    messages.success(request, "Comment moderation updated.")
    return redirect("post-detail", post_id=comment.post_id)


@login_required
def admin_categories(request):
    if not is_admin(request.user):
        return HttpResponseForbidden("Administrator access is required.")
    form = CategoryForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        category = form.save()
        audit(request.user, "created_category", category, category.name)
        messages.success(request, f"Category “{category.name}” added.")
        return redirect("admin-categories")
    categories = Category.objects.annotate(post_count=Count("posts")).order_by("sort_order", "name")
    return render(request, "core/admin_categories.html", {"form": form, "categories": categories})


@login_required
def admin_category_edit(request, category_id):
    if not is_admin(request.user):
        return HttpResponseForbidden("Administrator access is required.")
    category = get_object_or_404(Category, pk=category_id)
    form = CategoryForm(request.POST or None, instance=category)
    if request.method == "POST" and form.is_valid():
        form.save()
        audit(request.user, "updated_category", category, f"{category.name} · active={category.is_active}")
        messages.success(request, "Category saved.")
        return redirect("admin-categories")
    return render(request, "core/admin_category_edit.html", {"form": form, "category": category})


@login_required
def admin_questions(request):
    if not is_admin(request.user):
        return HttpResponseForbidden("Administrator access is required.")
    q = request.GET.get("q", "").strip()
    templates = QuestionTemplate.objects.prefetch_related("categories").annotate(use_count=Count("post_questions"))
    if q:
        templates = templates.filter(text__icontains=q)
    return render(request, "core/admin_questions.html", {"templates": templates, "q": q})


@login_required
def admin_question_edit(request, template_id=None):
    if not is_admin(request.user):
        return HttpResponseForbidden("Administrator access is required.")
    template = get_object_or_404(QuestionTemplate, pk=template_id) if template_id else None
    form = QuestionTemplateForm(request.POST or None, instance=template)
    if request.method == "POST" and form.is_valid():
        saved = form.save()
        audit(request.user, "updated_question" if template else "created_question", saved, saved.text[:200])
        messages.success(request, "Question saved. Posts that already use it keep their original wording.")
        return redirect("admin-questions")
    return render(request, "core/admin_question_edit.html", {"form": form, "template": template})


# ---------------------------------------------------------------------------
# Approve / decline / re-verify / chat questions
# ---------------------------------------------------------------------------

def _back_to(request, claim):
    target = request.POST.get("next", "")
    if target and url_has_allowed_host_and_scheme(target, {request.get_host()}):
        return redirect(target)
    return redirect("post-detail", post_id=claim.post_id)


@login_required
@require_POST
def approve_claim_view(request, claim_id):
    claim = get_object_or_404(ClaimApplication.objects.select_related("post", "finder"), pk=claim_id)
    try:
        approve_claim(claim, request.user)
    except PermissionError as exc:
        return HttpResponseForbidden(str(exc))
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Claim approved. The claimant was notified of their place in the approval order.")
    return _back_to(request, claim)


@login_required
@require_POST
def decline_claim_view(request, claim_id):
    claim = get_object_or_404(ClaimApplication.objects.select_related("post", "finder"), pk=claim_id)
    try:
        decline_claim(claim, request.user, request.POST.get("reason", ""))
    except PermissionError as exc:
        return HttpResponseForbidden(str(exc))
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Claim declined. Its chat is now read-only.")
    return _back_to(request, claim)


@login_required
@require_POST
def reverify_view(request, conversation_id):
    conversation = get_object_or_404(Conversation, pk=conversation_id)
    try:
        reopen_lost_report(conversation, request.user)
    except PermissionError as exc:
        return HttpResponseForbidden(str(exc))
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "You asked the finder to verify you again. This was your one re-application.")
    return redirect("chat-room", conversation_id=conversation.pk)


@login_required
@require_POST
def ask_questions_view(request, conversation_id):
    conversation = get_object_or_404(Conversation.objects.select_related("post", "claim"), pk=conversation_id)
    ids = [v for v in request.POST.getlist("questions") if v.isdigit()]
    templates = list(QuestionTemplate.objects.filter(pk__in=ids, is_active=True))
    if not templates:
        messages.error(request, "Pick at least one question to send.")
        return redirect("chat-room", conversation_id=conversation.pk)
    try:
        created = ask_chat_questions(conversation, request.user, templates)
    except PermissionError as exc:
        return HttpResponseForbidden(str(exc))
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Sent {len(created)} question(s). You'll get a notification when they're answered.")
    return redirect("chat-room", conversation_id=conversation.pk)


@login_required
@require_POST
def answer_question_view(request, question_id):
    question = get_object_or_404(ChatQuestion.objects.select_related("conversation"), pk=question_id)
    try:
        answer_chat_question(question, request.user, request.POST.get("answer", ""))
    except PermissionError as exc:
        return HttpResponseForbidden(str(exc))
    except ValueError as exc:
        messages.error(request, str(exc))
    if request.headers.get("x-requested-with") == "fetch":
        question.refresh_from_db()
        return JsonResponse({"ok": question.is_answered, "answer": question.answer_text})
    return redirect(f"{reverse('chat-room', args=[question.conversation_id])}#question-{question.pk}")


@login_required
@require_POST
def share_post(request, post_id):
    """Copy link: turns on public (signed-out) viewing for this post, permanently."""
    post = get_object_or_404(ItemPost, pk=post_id)
    PostShare.objects.get_or_create(post=post, user=request.user)
    if not post.is_shared:
        ItemPost.objects.filter(pk=post.pk, is_shared=False).update(is_shared=True, shared_at=timezone.now())
    link = request.build_absolute_uri(reverse("post-detail", args=[post.pk]))
    if request.headers.get("x-requested-with") == "fetch":
        return JsonResponse({
            "url": link,
            "shared_by_author": post.shares.filter(user_id=post.author_id).exists(),
            "share_count": post.shares.count(),
        })
    messages.success(request, f"Link ready to share: {link}")
    return redirect("post-detail", post_id=post.pk)
