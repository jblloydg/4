import re

from django import forms
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.forms import AuthenticationForm, SetPasswordForm
from django.core.exceptions import ValidationError

from django.db import models
from django.db.models import Q

from .models import AbuseReport, Category, ItemPost, PostPhoto, Profile, SystemSettings
from .validators import validate_photo

User = get_user_model()
STUDENT_ID_PATTERN = re.compile(r"^\d{4}-\d{4}$")
MAX_POST_PHOTOS = 20
HUMAN_CHECK_PHOTO_THRESHOLD = 10


class FindItLoginForm(AuthenticationForm):
    username = forms.CharField(
        label="School ID or staff username",
        widget=forms.TextInput(attrs={"autocomplete": "username", "placeholder": "2024-0122"}),
    )
    password = forms.CharField(
        label="Password",
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password", "placeholder": "Your password"}),
    )


class SignupForm(forms.Form):
    display_name = forms.CharField(max_length=80, label="Name")
    student_id = forms.CharField(max_length=9, label="School ID", help_text="Format: YYYY-NNNN")
    password1 = forms.CharField(label="Create password", widget=forms.PasswordInput)
    password2 = forms.CharField(label="Confirm password", widget=forms.PasswordInput)

    def clean_student_id(self):
        value = self.cleaned_data["student_id"].strip()
        if not STUDENT_ID_PATTERN.fullmatch(value):
            raise ValidationError("Use the school ID format YYYY-NNNN.")
        if User.objects.filter(username=value).exists():
            raise ValidationError("That school ID already has an account.")
        if Profile.objects.filter(student_id=value).exists():
            raise ValidationError("That school ID already has an account.")
        return value

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("password1") and cleaned.get("password2"):
            if cleaned["password1"] != cleaned["password2"]:
                self.add_error("password2", "The passwords do not match.")
            else:
                candidate = User(username=cleaned.get("student_id", ""), first_name=cleaned.get("display_name", ""))
                try:
                    from django.contrib.auth.password_validation import validate_password
                    validate_password(cleaned["password1"], candidate)
                except ValidationError as exc:
                    self.add_error("password1", exc)
        return cleaned

    def save(self):
        user = User.objects.create_user(
            username=self.cleaned_data["student_id"],
            first_name=self.cleaned_data["display_name"].strip(),
            password=self.cleaned_data["password1"],
        )
        user.profile.student_id = self.cleaned_data["student_id"]
        user.profile.save(update_fields=["student_id"])
        return user


class StaffAccountForm(forms.Form):
    display_name = forms.CharField(max_length=80)
    username = forms.RegexField(
        regex=r"^[A-Za-z0-9._-]{3,32}$",
        help_text="Use a staff username, not a student ID.",
    )
    password = forms.CharField(widget=forms.PasswordInput, min_length=10)

    def clean_username(self):
        value = self.cleaned_data["username"].strip()
        if User.objects.filter(username__iexact=value).exists():
            raise ValidationError("That username is already in use.")
        return value

    def clean_password(self):
        password = self.cleaned_data["password"]
        candidate = User(username=self.cleaned_data.get("username", ""), first_name=self.cleaned_data.get("display_name", ""))
        from django.contrib.auth.password_validation import validate_password
        validate_password(password, candidate)
        return password

    def save(self):
        user = User.objects.create_user(
            username=self.cleaned_data["username"],
            first_name=self.cleaned_data["display_name"].strip(),
            password=self.cleaned_data["password"],
        )
        user.profile.role = Profile.Role.STAFF
        user.profile.student_id = None
        user.profile.save(update_fields=["role", "student_id"])
        return user


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleImageField(forms.ImageField):
    """Accepts several images in one input. Each file is checked by Pillow and validate_photo."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", MultipleFileInput(attrs={"accept": "image/jpeg,image/png,image/webp", "multiple": True}))
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        single_clean = super().clean
        if isinstance(data, (list, tuple)):
            files = [single_clean(item, initial) for item in data if item]
        elif data:
            files = [single_clean(data, initial)]
        else:
            files = []
        for item in files:
            validate_photo(item)
        return files


class ItemPostForm(forms.ModelForm):
    """Create/edit a post. Photos (1-20) and a category are required; title and
    description are optional, like a social-media post."""

    photos = MultipleImageField(
        required=False,
        label="Photos",
        help_text=f"Add 1 to {MAX_POST_PHOTOS} photos (JPG, PNG or WebP, up to 5 MB each). No video.",
    )
    remove_photos = forms.ModelMultipleChoiceField(
        queryset=PostPhoto.objects.none(), required=False,
        widget=forms.CheckboxSelectMultiple, label="Remove photos",
    )
    human_check = forms.CharField(
        required=False, max_length=8, label="Quick check",
        widget=forms.TextInput(attrs={"inputmode": "numeric", "autocomplete": "off"}),
    )
    question_1 = forms.CharField(max_length=240, required=False, label="Private question 1")
    question_2 = forms.CharField(max_length=240, required=False, label="Private question 2")
    question_3 = forms.CharField(max_length=240, required=False, label="Private question 3")
    question_4 = forms.CharField(max_length=240, required=False, label="Private question 4")
    question_5 = forms.CharField(max_length=240, required=False, label="Private question 5")

    class Meta:
        model = ItemPost
        fields = ["post_type", "category", "title", "description", "approximate_date"]
        widgets = {
            "post_type": forms.Select(attrs={"data-post-type": "true"}),
            "category": forms.Select(attrs={"data-category": "true"}),
            "title": forms.TextInput(attrs={"placeholder": "Optional: a broad name, such as blue water bottle"}),
            "description": forms.Textarea(attrs={"rows": 4, "placeholder": "Optional: describe visible details without revealing private identifiers."}),
            "approximate_date": forms.DateInput(attrs={"type": "date"}),
        }
        labels = {"title": "Title (optional)", "description": "Description (optional)"}

    def __init__(self, *args, human_check_answer=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.human_check_answer = human_check_answer
        categories = Category.objects.filter(is_active=True)
        if self.instance.pk and self.instance.category_id:
            categories = Category.objects.filter(Q(is_active=True) | Q(pk=self.instance.category_id))
        self.fields["category"].queryset = categories
        self.fields["category"].required = True
        self.fields["category"].empty_label = "Choose a category"
        if self.instance.pk:
            self.fields["remove_photos"].queryset = self.instance.photos.all()

    def existing_photo_count(self):
        return self.instance.photos.count() if self.instance.pk else 0

    def clean(self):
        cleaned = super().clean()
        new_photos = cleaned.get("photos") or []
        removing = cleaned.get("remove_photos") or []
        total = self.existing_photo_count() - len(removing) + len(new_photos)
        if total < 1:
            self.add_error("photos", "Add at least one photo of the item.")
        elif total > MAX_POST_PHOTOS:
            self.add_error("photos", f"A post can have at most {MAX_POST_PHOTOS} photos. You selected {total}.")
        elif new_photos and total > HUMAN_CHECK_PHOTO_THRESHOLD:
            answer = (cleaned.get("human_check") or "").strip()
            if not self.human_check_answer or answer != str(self.human_check_answer):
                self.add_error(
                    "human_check",
                    f"Uploading more than {HUMAN_CHECK_PHOTO_THRESHOLD} photos needs the quick check. Enter the answer shown.",
                )
        if cleaned.get("post_type") == ItemPost.Type.FOUND:
            questions = [cleaned.get(f"question_{n}", "").strip() for n in range(1, 6)]
            if any(not question for question in questions):
                raise ValidationError("A found-item post needs exactly five private verification questions.")
            if len({q.casefold() for q in questions}) != 5:
                raise ValidationError("Use five different private questions.")
        return cleaned

    def question_texts(self):
        return [self.cleaned_data[f"question_{n}"].strip() for n in range(1, 6)]

    def save_photos(self, post):
        """Apply photo removals and additions after the post itself is saved."""
        for photo in self.cleaned_data.get("remove_photos") or []:
            photo.image.delete(save=False)
            photo.delete()
        next_order = (post.photos.aggregate(models.Max("order"))["order__max"] or 0) + 1
        for offset, upload in enumerate(self.cleaned_data.get("photos") or []):
            PostPhoto.objects.create(post=post, image=upload, order=next_order + offset)


class CommentForm(forms.Form):
    text = forms.CharField(
        max_length=1000,
        widget=forms.Textarea(attrs={"rows": 2, "placeholder": "Add a public comment"}),
    )


class ClaimAnswerForm(forms.Form):
    def __init__(self, *args, questions=None, **kwargs):
        super().__init__(*args, **kwargs)
        for question in questions or []:
            self.fields[f"answer_{question.pk}"] = forms.CharField(
                label=question.text,
                max_length=1200,
                widget=forms.Textarea(attrs={"rows": 3, "placeholder": "Your private answer"}),
            )


class ReportForm(forms.ModelForm):
    class Meta:
        model = AbuseReport
        fields = ["reason", "details"]
        widgets = {"details": forms.Textarea(attrs={"rows": 4})}


class SystemSettingsForm(forms.ModelForm):
    class Meta:
        model = SystemSettings
        fields = ["guardhouse_grace_days", "archive_purge_days"]
        widgets = {
            "guardhouse_grace_days": forms.NumberInput(attrs={"min": 1, "placeholder": "Disabled"}),
            "archive_purge_days": forms.NumberInput(attrs={"min": 1, "placeholder": "Disabled"}),
        }

    def clean_guardhouse_grace_days(self):
        return self._clean_timer("guardhouse_grace_days")

    def clean_archive_purge_days(self):
        return self._clean_timer("archive_purge_days")

    def _clean_timer(self, field):
        value = self.cleaned_data.get(field)
        if value is not None and value < 1:
            raise ValidationError("Leave blank to keep this timer disabled, or enter at least one day.")
        if value is not None and value > 3650:
            raise ValidationError("Choose a value of 3650 days or less.")
        return value


class AdminReviewForm(forms.Form):
    action = forms.ChoiceField(choices=[
        ("resolve", "Resolve with no account action"),
        ("suspend", "Suspend reported account"),
        ("restore", "Restore account"),
    ])
    admin_note = forms.CharField(max_length=1500, required=False, widget=forms.Textarea(attrs={"rows": 3}))


class CloseReasonForm(forms.Form):
    reason = forms.CharField(max_length=240, required=False, label="Reason (optional)")


class CategoryForm(forms.ModelForm):
    class Meta:
        model = Category
        fields = ["name", "sort_order", "is_active"]
        labels = {"sort_order": "Display order (lower shows first)", "is_active": "Active (shown on the post form)"}

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        clash = Category.objects.filter(name__iexact=name)
        if self.instance.pk:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise ValidationError("A category with that name already exists.")
        return name

    def clean_is_active(self):
        active = self.cleaned_data["is_active"]
        if not active and self.instance.pk and self.instance.is_active:
            if Category.objects.filter(is_active=True).exclude(pk=self.instance.pk).count() == 0:
                raise ValidationError("Keep at least one category active.")
        return active
