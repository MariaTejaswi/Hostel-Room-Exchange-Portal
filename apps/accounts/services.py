import logging
import csv
import io
from django.db import transaction
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError, PermissionDenied
from django.core.validators import validate_email
from django.contrib.auth.tokens import default_token_generator
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from django.utils.encoding import force_bytes, force_str
from django.contrib.auth.password_validation import validate_password
from django.core.mail import send_mail
from django.conf import settings
from apps.core.models import AuditLog, Institution
from apps.hostels.models import Hostel
from .models import User, EligibilityVerificationRecord
from .permissions import can_manage_student_accounts, can_verify_residential_eligibility

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

DEFAULT_STUDENT_PASSWORD = "RoomLoop2026!"


def _normalize_academic_year(year_val: str) -> str:
    """Normalize freeform or abbreviation strings to User.AcademicYear choices."""
    if not year_val:
        return ""
    y = year_val.strip().lower()
    mapping = {
        "1": User.AcademicYear.YEAR_1,
        "1st": User.AcademicYear.YEAR_1,
        "1st year": User.AcademicYear.YEAR_1,
        "first_year": User.AcademicYear.YEAR_1,
        "first year": User.AcademicYear.YEAR_1,
        "year 1": User.AcademicYear.YEAR_1,
        "year_1": User.AcademicYear.YEAR_1,
        "2": User.AcademicYear.YEAR_2,
        "2nd": User.AcademicYear.YEAR_2,
        "2nd year": User.AcademicYear.YEAR_2,
        "second_year": User.AcademicYear.YEAR_2,
        "second year": User.AcademicYear.YEAR_2,
        "year 2": User.AcademicYear.YEAR_2,
        "year_2": User.AcademicYear.YEAR_2,
        "3": User.AcademicYear.YEAR_3,
        "3rd": User.AcademicYear.YEAR_3,
        "3rd year": User.AcademicYear.YEAR_3,
        "third_year": User.AcademicYear.YEAR_3,
        "third year": User.AcademicYear.YEAR_3,
        "year 3": User.AcademicYear.YEAR_3,
        "year_3": User.AcademicYear.YEAR_3,
        "4": User.AcademicYear.YEAR_4,
        "4th": User.AcademicYear.YEAR_4,
        "4th year": User.AcademicYear.YEAR_4,
        "fourth_year": User.AcademicYear.YEAR_4,
        "fourth year": User.AcademicYear.YEAR_4,
        "year 4": User.AcademicYear.YEAR_4,
        "year_4": User.AcademicYear.YEAR_4,
        "5": User.AcademicYear.YEAR_5,
        "5th": User.AcademicYear.YEAR_5,
        "5th year": User.AcademicYear.YEAR_5,
        "fifth_year": User.AcademicYear.YEAR_5,
        "fifth year": User.AcademicYear.YEAR_5,
        "year 5": User.AcademicYear.YEAR_5,
        "year_5": User.AcademicYear.YEAR_5,
        "pg": User.AcademicYear.POSTGRAD,
        "postgrad": User.AcademicYear.POSTGRAD,
        "post-graduate": User.AcademicYear.POSTGRAD,
        "postgraduate": User.AcademicYear.POSTGRAD,
        "mtech": User.AcademicYear.POSTGRAD,
        "m.tech": User.AcademicYear.POSTGRAD,
        "msc": User.AcademicYear.POSTGRAD,
        "m.sc": User.AcademicYear.POSTGRAD,
        "research": User.AcademicYear.RESEARCH,
        "research scholar": User.AcademicYear.RESEARCH,
        "research_scholar": User.AcademicYear.RESEARCH,
        "phd": User.AcademicYear.RESEARCH,
        "ph.d": User.AcademicYear.RESEARCH,
        "other": User.AcademicYear.OTHER,
    }
    if y in mapping:
        return mapping[y]
    if year_val in User.AcademicYear.values:
        return year_val
    return User.AcademicYear.OTHER


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
    residential_eligibility: str = User.ResidentialEligibility.UNVERIFIED,
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
        residential_eligibility=residential_eligibility,
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
def create_student_account(
    *,
    acting_user: User,
    full_name: str,
    registration_number: str,
    email: str,
    department: str = "",
    program: str = "",
    academic_year: str = "",
    phone_number: str = "",
    claimed_residential_category: str = User.ClaimedResidentialCategory.UNSPECIFIED,
    ip_address: str | None = None,
) -> User:
    """
    Create a new student account scoped to the acting System Administrator's institution.
    """
    if not can_manage_student_accounts(acting_user):
        raise PermissionDenied("Only authorized System Administrators can create student accounts.")

    institution = acting_user.institution
    if not institution:
        raise ValidationError("System Administrator must be associated with an institution to manage student accounts.")

    full_name = full_name.strip()
    if len(full_name) < 2:
        raise ValidationError({"full_name": "Full name must be at least 2 characters long."})

    email = email.strip().lower()
    try:
        validate_email(email)
    except ValidationError:
        raise ValidationError({"email": "Enter a valid email address."})

    registration_number = registration_number.strip().upper()
    if not registration_number:
        raise ValidationError({"registration_number": "Registration number is required for students."})

    # Uniqueness checks
    if User.objects.filter(email=email).exists():
        raise ValidationError({"email": "An account with this email address already exists."})

    if User.objects.filter(institution=institution, registration_number=registration_number).exists():
        raise ValidationError({"registration_number": "A student with this registration number already exists in your institution."})

    # Normalize academic year
    academic_year = _normalize_academic_year(academic_year)

    # Validate claimed residential category choice if provided
    if claimed_residential_category and claimed_residential_category not in User.ClaimedResidentialCategory.values:
        claimed_residential_category = User.ClaimedResidentialCategory.UNSPECIFIED

    user = User(
        email=email,
        full_name=full_name,
        institution=institution,
        registration_number=registration_number,
        department=department.strip(),
        program=program.strip(),
        academic_year=academic_year,
        phone_number=phone_number.strip(),
        claimed_residential_category=claimed_residential_category,
        residential_eligibility=User.ResidentialEligibility.UNVERIFIED,
        is_staff=False,
        is_superuser=False,
        account_status=User.AccountStatus.ACTIVE,
        is_active=True,
    )
    user.set_password(DEFAULT_STUDENT_PASSWORD)
    user.full_clean()
    user.save()

    # Assign to Student group
    group, _ = Group.objects.get_or_create(name=ROLE_STUDENT)
    user.groups.add(group)

    # Log creation event in AuditLog
    AuditLog.objects.create(
        acting_user=acting_user,
        institution=institution,
        action="STUDENT_CREATED",
        target_type="User",
        target_id=str(user.id),
        change_summary=(
            f"Created student account {user.full_name} ({user.email}, Reg: {user.registration_number}, "
            f"Dept: '{user.department or 'N/A'}', Claimed: '{user.get_claimed_residential_category_display()}')"
        ),
        ip_address=ip_address,
    )

    return user


@transaction.atomic
def update_student_account(
    *,
    acting_user: User,
    student: User,
    full_name: str,
    registration_number: str,
    department: str = "",
    program: str = "",
    academic_year: str = "",
    phone_number: str = "",
    claimed_residential_category: str = User.ClaimedResidentialCategory.UNSPECIFIED,
    change_reason: str = "",
    ip_address: str | None = None,
) -> User:
    """
    Safely edit administrative student account fields.
    Does NOT permit promoting role, modifying staff flags, moving institution,
    altering verified eligibility, or mutating occupancy.
    """
    if not can_manage_student_accounts(acting_user):
        raise PermissionDenied("Only authorized System Administrators can edit student accounts.")

    if student.institution_id != acting_user.institution_id:
        raise PermissionDenied("You cannot edit student accounts belonging to another institution.")

    if not student.is_student_role:
        raise ValidationError("Target user is not a Student account.")

    full_name = full_name.strip()
    if len(full_name) < 2:
        raise ValidationError({"full_name": "Full name must be at least 2 characters long."})

    registration_number = registration_number.strip().upper()
    if not registration_number:
        raise ValidationError({"registration_number": "Registration number cannot be empty."})

    # Check registration number uniqueness within institution if changed
    if registration_number != student.registration_number:
        if User.objects.filter(institution=acting_user.institution, registration_number=registration_number).exclude(id=student.id).exists():
            raise ValidationError({"registration_number": "Another student with this registration number already exists in your institution."})

    # Reason required if identity fields (name or reg number) change
    identity_changed = (full_name != student.full_name) or (registration_number != student.registration_number)
    if identity_changed and (not change_reason or len(change_reason.strip()) < 5):
        raise ValidationError({"change_reason": "A documented reason (minimum 5 characters) is required when updating student identity or registration number."})

    academic_year = _normalize_academic_year(academic_year)

    if claimed_residential_category and claimed_residential_category not in User.ClaimedResidentialCategory.values:
        claimed_residential_category = User.ClaimedResidentialCategory.UNSPECIFIED

    changes = []
    if full_name != student.full_name:
        changes.append(f"Name: '{student.full_name}' -> '{full_name}'")
        student.full_name = full_name
    if registration_number != student.registration_number:
        changes.append(f"Reg No: '{student.registration_number}' -> '{registration_number}'")
        student.registration_number = registration_number
    if department.strip() != student.department:
        changes.append(f"Dept: '{student.department}' -> '{department.strip()}'")
        student.department = department.strip()
    if program.strip() != student.program:
        changes.append(f"Program: '{student.program}' -> '{program.strip()}'")
        student.program = program.strip()
    if academic_year != student.academic_year:
        changes.append(f"Year: '{student.academic_year}' -> '{academic_year}'")
        student.academic_year = academic_year
    if phone_number.strip() != student.phone_number:
        changes.append(f"Phone: '{student.phone_number}' -> '{phone_number.strip()}'")
        student.phone_number = phone_number.strip()
    if claimed_residential_category != student.claimed_residential_category:
        changes.append(f"Claimed Category: '{student.claimed_residential_category}' -> '{claimed_residential_category}'")
        student.claimed_residential_category = claimed_residential_category

    student.full_clean()
    student.save()

    if changes:
        reason_suffix = f" (Reason: {change_reason.strip()})" if change_reason else ""
        AuditLog.objects.create(
            acting_user=acting_user,
            institution=acting_user.institution,
            action="STUDENT_UPDATED",
            target_type="User",
            target_id=str(student.id),
            change_summary=f"Updated student {student.email}: {', '.join(changes)}{reason_suffix}",
            ip_address=ip_address,
        )

    return student


@transaction.atomic
def set_student_account_status(
    *,
    acting_user: User,
    student: User,
    is_active: bool,
    reason: str,
    ip_address: str | None = None,
) -> tuple[User, str | None]:
    """
    Activate or deactivate a student account.
    Deactivation blocks future sign-in but preserves all occupancy, request history,
    and audit trails. Warns administrator if active bed allocation exists.
    """
    if not can_manage_student_accounts(acting_user):
        raise PermissionDenied("Only authorized System Administrators can activate or deactivate student accounts.")

    if student.institution_id != acting_user.institution_id:
        raise PermissionDenied("You cannot modify students from another institution.")

    if not student.is_student_role:
        raise ValidationError("Target user is not a Student account.")

    reason = reason.strip()
    if not is_active and len(reason) < 5:
        raise ValidationError({"reason": "A documented reason (minimum 5 characters) is required to deactivate a student account."})

    warning_msg = None
    if not is_active:
        active_occ = student.current_occupancy
        if active_occ:
            hostel = active_occ.bed.room.floor.block.hostel
            warning_msg = (
                f"Notice: Student currently has an active bed allocation in {hostel.name} "
                f"(Room {active_occ.bed.room.room_number}, Bed {active_occ.bed.bed_label}). "
                "Account deactivation prevents login access but does not release the physical bed. "
                "The allocation remains recorded in the hostel roster."
            )
        student.is_active = False
        student.account_status = User.AccountStatus.INACTIVE
        action = "STUDENT_DEACTIVATED"
        summary = f"Deactivated student account {student.email}. Reason: {reason}"
    else:
        student.is_active = True
        student.account_status = User.AccountStatus.ACTIVE
        action = "STUDENT_ACTIVATED"
        summary = f"Reactivated student account {student.email}. Reason: {reason or 'Administrative reactivation'}"

    student.save(update_fields=["is_active", "account_status", "updated_at"])

    AuditLog.objects.create(
        acting_user=acting_user,
        institution=acting_user.institution,
        action=action,
        target_type="User",
        target_id=str(student.id),
        change_summary=summary,
        ip_address=ip_address,
    )

    return student, warning_msg


def initiate_student_password_setup(
    *,
    acting_user: User,
    student: User,
    request=None,
    ip_address: str | None = None,
) -> str:
    """
    Initiate first-time password setup or administrative password reset.
    Compatible with accounts initialized with unusable passwords.
    """
    if not can_manage_student_accounts(acting_user):
        raise PermissionDenied("Only authorized System Administrators can initiate password setups.")

    if student.institution_id != acting_user.institution_id:
        raise PermissionDenied("You cannot initiate password reset for students in another institution.")

    uid = urlsafe_base64_encode(force_bytes(student.pk))
    token = default_token_generator.make_token(student)
    setup_path = f"/accounts/password-setup/{uid}/{token}/"

    setup_url = request.build_absolute_uri(setup_path) if request else setup_path

    subject = "RoomLoop — Set Up Your University Accommodation Password"
    message = (
        f"Hello {student.full_name},\n\n"
        f"Your university student accommodation account has been created on RoomLoop ({student.institution.name if student.institution else 'University Portal'}).\n\n"
        f"Please click the link below to set your account password and complete your registration:\n"
        f"{setup_url}\n\n"
        "This link is uniquely tied to your account and will expire after use.\n\n"
        "Regards,\nRoomLoop Accommodation Office"
    )

    send_mail(
        subject=subject,
        message=message,
        from_email=settings.DEFAULT_FROM_EMAIL if hasattr(settings, "DEFAULT_FROM_EMAIL") else "noreply@roomloop.internal",
        recipient_list=[student.email],
        fail_silently=False,
    )

    AuditLog.objects.create(
        acting_user=acting_user,
        institution=acting_user.institution,
        action="PASSWORD_RESET_INITIATED",
        target_type="User",
        target_id=str(student.id),
        change_summary=f"Initiated password setup for student {student.email}",
        ip_address=ip_address,
    )

    return setup_url


@transaction.atomic
def complete_student_password_setup(
    *,
    token: str,
    new_password: str,
    uidb64: str | None = None,
    student: User | None = None,
    ip_address: str | None = None,
) -> User:
    """
    Validate setup token and set new password on unusable or existing accounts.
    """
    if student is not None:
        user = student
    elif uidb64:
        try:
            uid = force_str(urlsafe_base64_decode(uidb64))
            user = User.objects.get(pk=uid)
        except (TypeError, ValueError, OverflowError, User.DoesNotExist):
            raise ValidationError("Invalid password setup link.")
    else:
        raise ValidationError("Student or uidb64 is required.")

    if not default_token_generator.check_token(user, token):
        raise ValidationError("This password setup link is invalid or has expired. Please request a new link.")

    validate_password(new_password, user)
    user.set_password(new_password)
    user.account_status = User.AccountStatus.ACTIVE
    user.is_active = True
    user.save(update_fields=["password", "account_status", "is_active", "updated_at"])

    AuditLog.objects.create(
        acting_user=user,
        institution=user.institution,
        action="PASSWORD_SET",
        target_type="User",
        target_id=str(user.id),
        change_summary=f"Password successfully established for user {user.email}",
        ip_address=ip_address,
    )

    return user


@transaction.atomic
def verify_student_eligibility(
    *,
    acting_user: User,
    student: User,
    new_eligibility: str,
    reason: str,
    ip_address: str | None = None,
) -> tuple[User, EligibilityVerificationRecord]:
    """
    Authoritative residential eligibility transition performed by Hostel Administrator.
    Validates institution scope, safety conflicts with active occupancies or exchange requests,
    and records immutable verification audit records.
    """
    if not can_verify_residential_eligibility(acting_user):
        raise PermissionDenied("Only authorized Hostel Administrators can verify residential eligibility.")

    if student.institution_id != acting_user.institution_id:
        raise PermissionDenied("You cannot verify students outside your institution.")

    if not student.is_student_role:
        raise ValidationError("Target account is not a student.")

    valid_choices = [
        User.ResidentialEligibility.MALE,
        User.ResidentialEligibility.FEMALE,
        User.ResidentialEligibility.UNVERIFIED,
    ]
    if new_eligibility not in valid_choices:
        raise ValidationError(f"Invalid eligibility option. Allowed: {valid_choices}")

    reason = reason.strip()
    if len(reason) < 5:
        raise ValidationError({"reason": "A documented verification reference or reason (minimum 5 characters) is required."})

    # Safety conflict check 1: Active bed allocation in an opposite-gender hostel
    active_occ = student.current_occupancy
    if active_occ:
        hostel = active_occ.bed.room.floor.block.hostel
        hostel_cat = hostel.resident_category
        if new_eligibility != User.ResidentialEligibility.UNVERIFIED and hostel_cat in [Hostel.ResidentCategory.MALE, Hostel.ResidentCategory.FEMALE]:
            if new_eligibility != hostel_cat:
                raise ValidationError(
                    f"Conflict: Student holds an active bed allocation in '{hostel.name}' ({hostel.get_resident_category_display()}). "
                    f"Changing verified eligibility to '{new_eligibility}' is blocked while this allocation is active. "
                    "The assigned warden must release the current bed before eligibility can be reassigned."
                )

    # Safety conflict check 2: In-flight room exchange requests
    try:
        from apps.exchange.models import ExchangeRequest
        open_requests = ExchangeRequest.objects.filter(
            student=student,
            status__in=[
                ExchangeRequest.Status.OPEN,
                ExchangeRequest.Status.MATCHED,
            ],
        )
        if open_requests.exists():
            raise ValidationError(
                "Conflict: Student has unresolved or pending room exchange requests. "
                "Active exchange requests must be finalized or cancelled before modifying residential eligibility."
            )
    except ImportError:
        pass

    previous_eligibility = student.residential_eligibility
    student.residential_eligibility = new_eligibility
    student.save(update_fields=["residential_eligibility", "updated_at"])

    record = EligibilityVerificationRecord.objects.create(
        student=student,
        institution=acting_user.institution,
        previous_eligibility=previous_eligibility,
        new_eligibility=new_eligibility,
        verifier=acting_user,
        reason=reason,
    )

    AuditLog.objects.create(
        acting_user=acting_user,
        institution=acting_user.institution,
        action="ELIGIBILITY_VERIFIED",
        target_type="User",
        target_id=str(student.id),
        change_summary=(
            f"Verified residential eligibility for {student.full_name} ({student.registration_number}): "
            f"'{previous_eligibility}' -> '{new_eligibility}'. Reference/Reason: {reason}"
        ),
        ip_address=ip_address,
    )

    return student, record


def validate_csv_student_import(acting_user: User, file_obj) -> dict:
    """
    Parse and validate a student import CSV without writing changes to the database.
    Returns structured preview statistics and categorized rows.
    """
    if not can_manage_student_accounts(acting_user):
        raise PermissionDenied("Only authorized System Administrators can import student rosters.")

    institution = acting_user.institution
    if not institution:
        raise ValidationError("Actor must be associated with an institution.")

    content = file_obj.read()
    if isinstance(content, bytes):
        try:
            content = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            content = content.decode("latin-1")

    reader = csv.DictReader(io.StringIO(content))
    if not reader.fieldnames:
        raise ValidationError("The uploaded CSV file is empty or missing headers.")

    normalized_fieldnames = {f.strip().lower(): f for f in reader.fieldnames if f}
    required_fields = ["full_name", "registration_number", "email"]
    for req in required_fields:
        if req not in normalized_fieldnames:
            raise ValidationError(f"Missing required CSV column: '{req}'. Required headers: full_name, registration_number, email")

    valid_rows = []
    duplicate_rows = []
    error_rows = []

    seen_emails_in_file = set()
    seen_regs_in_file = set()

    # Pre-fetch existing emails and reg numbers in institution
    existing_emails = set(User.objects.values_list("email", flat=True))
    existing_regs = set(
        User.objects.filter(institution=institution)
        .exclude(registration_number__isnull=True)
        .exclude(registration_number="")
        .values_list("registration_number", flat=True)
    )

    row_index = 1
    for raw_row in reader:
        row_index += 1
        if row_index > 502:  # Header + 500 rows limit
            break

        # Map case-insensitive keys
        row = {k.strip().lower(): (v.strip() if v else "") for k, v in raw_row.items() if k}
        
        full_name = row.get("full_name", "").strip()
        reg_no = row.get("registration_number", "").strip().upper()
        email = row.get("email", "").strip().lower()
        dept = row.get("department", "").strip()
        program = row.get("program", "").strip()
        year = _normalize_academic_year(row.get("academic_year", ""))
        phone = row.get("phone_number", "").strip()
        claimed = row.get("claimed_residential_category", "").strip().lower()

        # Map claimed category aliases
        if claimed in ("male", "boy", "boys", "boys' hostel", "boys hostel"):
            claimed = User.ClaimedResidentialCategory.MALE
        elif claimed in ("female", "girl", "girls", "girls' hostel", "girls hostel"):
            claimed = User.ClaimedResidentialCategory.FEMALE
        else:
            claimed = User.ClaimedResidentialCategory.UNSPECIFIED

        parsed_data = {
            "row_index": row_index,
            "full_name": full_name,
            "registration_number": reg_no,
            "email": email,
            "department": dept,
            "program": program,
            "academic_year": year,
            "phone_number": phone,
            "claimed_residential_category": claimed,
        }

        # Validate required fields
        errors = []
        if len(full_name) < 2:
            errors.append("Full name must be at least 2 characters long.")

        if not email:
            errors.append("Email address is required.")
        else:
            try:
                validate_email(email)
            except ValidationError:
                errors.append(f"Invalid email address '{email}'.")

        if not reg_no:
            errors.append("Registration number is required.")

        if errors:
            parsed_data["errors"] = errors
            error_rows.append(parsed_data)
            continue

        # In-file duplicate checks
        is_dup_in_file = False
        dup_reasons = []
        if email in seen_emails_in_file:
            is_dup_in_file = True
            dup_reasons.append(f"Duplicate email '{email}' appears multiple times in this file.")
        if reg_no in seen_regs_in_file:
            is_dup_in_file = True
            dup_reasons.append(f"Duplicate registration number '{reg_no}' appears multiple times in this file.")

        if is_dup_in_file:
            parsed_data["reason"] = "; ".join(dup_reasons)
            duplicate_rows.append(parsed_data)
            continue

        seen_emails_in_file.add(email)
        seen_regs_in_file.add(reg_no)

        # Database duplicate checks
        is_dup_db = False
        db_dup_reasons = []
        if email in existing_emails:
            is_dup_db = True
            db_dup_reasons.append(f"Account with email '{email}' already exists in database.")
        if reg_no in existing_regs:
            is_dup_db = True
            db_dup_reasons.append(f"Registration number '{reg_no}' already exists in your institution.")

        if is_dup_db:
            parsed_data["reason"] = "; ".join(db_dup_reasons)
            duplicate_rows.append(parsed_data)
            continue

        valid_rows.append(parsed_data)

    total_count = len(valid_rows) + len(duplicate_rows) + len(error_rows)
    return {
        "total_rows": total_count,
        "valid_count": len(valid_rows),
        "duplicate_count": len(duplicate_rows),
        "error_count": len(error_rows),
        "valid_rows": valid_rows,
        "duplicate_rows": duplicate_rows,
        "error_rows": error_rows,
    }


@transaction.atomic
def process_csv_student_import(
    *,
    acting_user: User,
    rows: list[dict],
    ip_address: str | None = None,
) -> tuple[int, list[str]]:
    """
    Commit a validated batch of student accounts transactionally.
    Re-validates each record against database in real time to eliminate race conditions.
    """
    if not can_manage_student_accounts(acting_user):
        raise PermissionDenied("Only authorized System Administrators can commit student imports.")

    created_count = 0
    errors = []

    for row in rows:
        try:
            create_student_account(
                acting_user=acting_user,
                full_name=row["full_name"],
                registration_number=row["registration_number"],
                email=row["email"],
                department=row.get("department", ""),
                program=row.get("program", ""),
                academic_year=row.get("academic_year", ""),
                phone_number=row.get("phone_number", ""),
                claimed_residential_category=row.get("claimed_residential_category", User.ClaimedResidentialCategory.UNSPECIFIED),
                ip_address=ip_address,
            )
            created_count += 1
        except Exception as e:
            msg = f"Row {row.get('row_index', '?')} ({row.get('email', '?')}): {e}"
            errors.append(msg)

    AuditLog.objects.create(
        acting_user=acting_user,
        institution=acting_user.institution,
        action="STUDENT_IMPORT_COMPLETED",
        target_type="User",
        target_id="BATCH",
        change_summary=f"Imported {created_count} student accounts from CSV batch",
        ip_address=ip_address,
    )

    return created_count, errors


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
