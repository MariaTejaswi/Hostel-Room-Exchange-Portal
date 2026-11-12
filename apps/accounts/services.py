import logging
from django.db import transaction
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from apps.core.models import AuditLog, Institution
from .models import User

logger = logging.getLogger(__name__)

# Standard Role Names
ROLE_STUDENT = "Student"
ROLE_WARDEN = "Warden"
ROLE_CHIEF_WARDEN = "Chief Warden"
ROLE_HOSTEL_ADMIN = "Hostel Administrator"
ROLE_DEAN = "Dean of Student Welfare"
ROLE_SYSADMIN = "System Administrator"

ALL_ROLES = [
    ROLE_STUDENT,
    ROLE_WARDEN,
    ROLE_CHIEF_WARDEN,
    ROLE_HOSTEL_ADMIN,
    ROLE_DEAN,
    ROLE_SYSADMIN,
]


def ensure_roles_exist():
    """Ensure all core Django groups exist."""
    groups = {}
    for role_name in ALL_ROLES:
        group, _ = Group.objects.get_or_create(name=role_name)
        groups[role_name] = group
    return groups


@transaction.atomic
def create_user_with_role(
    *,
    email: str,
    full_name: str,
    role_name: str,
    institution: Institution | None = None,
    password: str | None = None,
    registration_number: str | None = None,
    phone_number: str = "",
    is_staff: bool = False,
    acting_user: User | None = None,
    ip_address: str | None = None,
) -> User:
    """Create a user, assign them to their designated Group, and record an audit log."""
    email = email.strip().lower()
    if role_name not in ALL_ROLES:
        raise ValueError(f"Invalid role name: {role_name}")

    if role_name == ROLE_STUDENT and not registration_number:
        raise ValueError("Students must have a registration number.")

    user = User(
        email=email,
        full_name=full_name.strip(),
        institution=institution,
        registration_number=registration_number.strip().upper() if registration_number else None,
        phone_number=phone_number.strip(),
        is_staff=is_staff or role_name in (ROLE_SYSADMIN, ROLE_HOSTEL_ADMIN),
    )
    if password:
        user.set_password(password)
    else:
        user.set_unusable_password()

    user.full_clean()
    user.save()

    # Assign group
    group, _ = Group.objects.get_or_create(name=role_name)
    user.groups.add(group)

    # Audit log
    AuditLog.objects.create(
        acting_user=acting_user,
        institution=institution,
        action="USER_CREATED",
        target_type="User",
        target_id=str(user.id),
        change_summary=f"Created user {user.email} with role {role_name}",
        ip_address=ip_address,
    )

    return user


@transaction.atomic
def update_user_profile(
    *,
    user: User,
    full_name: str,
    phone_number: str,
    avatar=None,
    acting_user: User | None = None,
    ip_address: str | None = None,
) -> User:
    """Update editable profile fields for a user."""
    old_name = user.full_name
    old_phone = user.phone_number

    user.full_name = full_name.strip()
    user.phone_number = phone_number.strip()
    if avatar:
        user.avatar = avatar

    user.full_clean()
    user.save(update_fields=["full_name", "phone_number", "avatar", "updated_at"])

    AuditLog.objects.create(
        acting_user=acting_user or user,
        institution=user.institution,
        action="PROFILE_UPDATED",
        target_type="User",
        target_id=str(user.id),
        change_summary=f"Updated profile: Name ('{old_name}' -> '{user.full_name}'), Phone ('{old_phone}' -> '{user.phone_number}')",
        ip_address=ip_address,
    )

    return user
