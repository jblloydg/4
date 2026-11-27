from django.core.exceptions import ValidationError

MAX_PHOTO_BYTES = 5 * 1024 * 1024
ALLOWED_PHOTO_TYPES = {"image/jpeg", "image/png", "image/webp"}


def validate_photo(file):
    if file.size > MAX_PHOTO_BYTES:
        raise ValidationError("Choose an image smaller than 5 MB.")
    content_type = getattr(file, "content_type", "")
    if content_type and content_type not in ALLOWED_PHOTO_TYPES:
        raise ValidationError("Upload a JPG, PNG, or WebP exterior photo.")
