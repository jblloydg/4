"""Duplicate-submit protection.

Every POST form gets a one-time ``idempotency_key`` (added by static/js/app.js
when the page loads). If the same signed-in user sends the same key twice
(double click, refresh-and-resubmit, flaky connection), the second request is
not processed again; the user is sent back to the page with a notice.

Requests without a key (JavaScript off, API clients) still work: the service
layer also refuses repeated state changes (for example approving an already
approved claim does nothing).
"""
from django.contrib import messages
from django.db import IntegrityError, transaction
from django.shortcuts import redirect
from django.utils.http import url_has_allowed_host_and_scheme

from .models import IdempotencyKey

FIELD = "idempotency_key"


class IdempotencyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method == "POST" and getattr(request, "user", None) and request.user.is_authenticated:
            key = (request.POST.get(FIELD) or request.headers.get("Idempotency-Key") or "").strip()[:64]
            if key:
                try:
                    with transaction.atomic():
                        IdempotencyKey.objects.create(user=request.user, key=key, path=request.path[:255])
                except IntegrityError:
                    messages.info(request, "That action was already submitted, so it was not repeated.")
                    back = request.META.get("HTTP_REFERER", "")
                    if not back or not url_has_allowed_host_and_scheme(back, {request.get_host()}):
                        back = "/home/"
                    return redirect(back)
        return self.get_response(request)
