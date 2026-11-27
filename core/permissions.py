from .models import Profile


def role_of(user):
    if not getattr(user, "is_authenticated", False):
        return None
    if user.is_superuser:
        return Profile.Role.ADMIN
    try:
        return user.profile.role
    except Profile.DoesNotExist:
        return Profile.Role.STUDENT


def is_staff_or_admin(user):
    return role_of(user) in {Profile.Role.STAFF, Profile.Role.ADMIN}


def is_admin(user):
    return role_of(user) == Profile.Role.ADMIN


def can_manage_any_post(user):
    return is_staff_or_admin(user)
