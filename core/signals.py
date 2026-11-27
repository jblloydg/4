from django.contrib.auth import get_user_model
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import ItemPost, Profile, TurnInTicket

User = get_user_model()


@receiver(post_save, sender=User)
def ensure_profile(sender, instance, created, **kwargs):
    if created:
        default_role = Profile.Role.ADMIN if instance.is_superuser else Profile.Role.STUDENT
        Profile.objects.get_or_create(user=instance, defaults={"role": default_role})


@receiver(post_save, sender=ItemPost)
def ensure_found_post_ticket(sender, instance, **kwargs):
    """A FOUND post automatically gets its private turn-in QR code."""
    if kwargs.get("raw"):
        return  # loading a backup: tickets come from the backup itself
    if instance.post_type == ItemPost.Type.FOUND:
        TurnInTicket.objects.get_or_create(post=instance, finder_id=instance.author_id)
