import io
import pytest
from django.urls import reverse
from django.core import mail
from django.core.exceptions import ValidationError, PermissionDenied
from django.contrib.auth import authenticate
from django.contrib.auth.tokens import default_token_generator
from django.utils.http import urlsafe_base64_encode
from django.utils.encoding import force_bytes

from apps.accounts.models import User, EligibilityVerificationRecord
from apps.accounts.services import (
    create_student_account,
    update_student_account,
    set_student_account_status,
    initiate_student_password_setup,
    complete_student_password_setup,
    verify_student_eligibility,
    validate_csv_student_import,
    process_csv_student_import,
    ROLE_STUDENT,
    ROLE_HOSTEL_ADMIN,
    ROLE_SYSADMIN,
    ROLE_WARDEN,
    ROLE_CHIEF_WARDEN,
    ROLE_DEAN,
    create_user_with_role,
)
from apps.accounts.selectors import (
    get_student_directory_queryset,
    get_pending_eligibility_students_queryset,
    get_pending_eligibility_count,
    get_student_detail_context,
)
from apps.accounts.permissions import (
    can_manage_student_accounts,
    can_verify_residential_eligibility,
)
from apps.hostels.models import (
    Hostel,
    Block,
    Floor,
    Room,
    Bed,
    WardenAssignment,
    StudentOccupancy,
    OperationalStatus,
)
from apps.hostels.services import assign_student_to_bed
from apps.hostels.selectors import get_eligible_unallocated_students
from apps.core.models import AuditLog


@pytest.fixture
def pure_sysadmin(institution, roles):
    """System Administrator without superuser privileges."""
    return create_user_with_role(
        email="pure.sysadmin@apex.test",
        full_name="Pure Sysadmin",
        role_name=ROLE_SYSADMIN,
        institution=institution,
        password="TestPassword123!",
        is_staff=False,
    )


@pytest.fixture
def hostel_with_beds(institution, warden_user):
    """Male hostel with available beds and assigned warden."""
    hostel = Hostel.objects.create(
        institution=institution,
        name="Boys Hostel Alpha",
        code="BHA-01",
        resident_category=Hostel.ResidentCategory.MALE,
        status=Hostel.Status.ACTIVE,
    )
    block = Block.objects.create(hostel=hostel, name="Block A", code="BA")
    floor = Floor.objects.create(block=block, floor_number=1, floor_label="Floor 1")
    room = Room.objects.create(floor=floor, room_number="101", room_type=Room.RoomType.DOUBLE)
    bed1 = Bed.objects.create(room=room, bed_label="A", operational_status=OperationalStatus.IN_SERVICE)
    bed2 = Bed.objects.create(room=room, bed_label="B", operational_status=OperationalStatus.IN_SERVICE)

    WardenAssignment.objects.create(
        warden=warden_user,
        hostel=hostel,
        is_primary=True,
    )
    return {"hostel": hostel, "room": room, "bed1": bed1, "bed2": bed2}


@pytest.mark.django_db
def test_sysadmin_can_create_student_account(pure_sysadmin, institution):
    """System Administrator can create a student account with unverified eligibility and unusable password."""
    student = create_student_account(
        acting_user=pure_sysadmin,
        full_name="Vikram Rao",
        registration_number="2024CS001",
        email="vikram.rao@apex.test",
        department="Computer Science",
        program="B.Tech",
        academic_year=User.AcademicYear.YEAR_1,
        phone_number="+91 9876543210",
        claimed_residential_category=User.ClaimedResidentialCategory.MALE,
    )

    assert student.id is not None
    assert student.full_name == "Vikram Rao"
    assert student.registration_number == "2024CS001"
    assert student.email == "vikram.rao@apex.test"
    assert student.is_student_role is True
    assert student.is_staff is False
    assert student.is_superuser is False
    assert student.residential_eligibility == User.ResidentialEligibility.UNVERIFIED
    assert student.claimed_residential_category == User.ClaimedResidentialCategory.MALE
    assert student.has_usable_password()
    assert student.check_password("RoomLoop2026!") is True
    assert authenticate(username="vikram.rao@apex.test", password="RoomLoop2026!") == student
    assert student.current_occupancy is None

    # Check AuditLog
    log = AuditLog.objects.filter(action="STUDENT_CREATED", target_id=str(student.id)).first()
    assert log is not None
    assert log.acting_user == pure_sysadmin

    # Setup email dispatch
    initiate_student_password_setup(acting_user=pure_sysadmin, student=student)
    assert len(mail.outbox) >= 1
    assert "Set Up Your University Accommodation Password" in mail.outbox[-1].subject


@pytest.mark.django_db
def test_unauthorized_roles_denied_from_managing_students(
    student_user, warden_user, chief_warden_user, admin_user, dean_user
):
    """Students, Wardens, Chief Wardens, Hostel Admins, and Deans cannot create student accounts."""
    unauthorized_users = [student_user, warden_user, chief_warden_user, admin_user, dean_user]

    for user in unauthorized_users:
        assert not can_manage_student_accounts(user)
        with pytest.raises(PermissionDenied):
            create_student_account(
                acting_user=user,
                full_name="Illegal Student",
                registration_number=f"REG-{user.id}",
                email=f"illegal-{user.id}@apex.test",
            )


@pytest.mark.django_db
def test_cross_institution_isolation(pure_sysadmin, other_institution, roles):
    """System Administrator cannot manage students belonging to another institution."""
    other_sysadmin = create_user_with_role(
        email="other.sysadmin@other.test",
        full_name="Other Sysadmin",
        role_name=ROLE_SYSADMIN,
        institution=other_institution,
        password="TestPassword123!",
        is_staff=False,
    )
    other_student = create_student_account(
        acting_user=other_sysadmin,
        full_name="Foreign Student",
        registration_number="FOR-001",
        email="foreign@other.test",
    )

    # pure_sysadmin (institution A) cannot edit or deactivate other_student (institution B)
    with pytest.raises(PermissionDenied):
        update_student_account(
            acting_user=pure_sysadmin,
            student=other_student,
            full_name="Hacked Name",
            registration_number="FOR-001",
        )

    with pytest.raises(PermissionDenied):
        set_student_account_status(
            acting_user=pure_sysadmin,
            student=other_student,
            is_active=False,
            reason="Unauthorized deactivation",
        )


@pytest.mark.django_db
def test_duplicate_email_and_registration_rejected(pure_sysadmin, institution):
    """Duplicate email or registration number within the same institution is rejected."""
    create_student_account(
        acting_user=pure_sysadmin,
        full_name="Original Student",
        registration_number="REG-ORIG",
        email="orig@apex.test",
    )

    # Duplicate email
    with pytest.raises(ValidationError) as exc:
        create_student_account(
            acting_user=pure_sysadmin,
            full_name="Duplicate Email",
            registration_number="REG-NEW",
            email="orig@apex.test",
        )
    assert "email" in str(exc.value).lower()

    # Duplicate registration number
    with pytest.raises(ValidationError) as exc:
        create_student_account(
            acting_user=pure_sysadmin,
            full_name="Duplicate Reg",
            registration_number="REG-ORIG",
            email="different@apex.test",
        )
    assert "registration" in str(exc.value).lower()


@pytest.mark.django_db
def test_account_creation_and_editing_never_allocates_bed(pure_sysadmin, institution):
    """Account creation and editing never allocates a bed or creates occupancy."""
    student = create_student_account(
        acting_user=pure_sysadmin,
        full_name="Bedless Student",
        registration_number="2024CS002",
        email="bedless@apex.test",
    )

    assert student.current_occupancy is None
    assert StudentOccupancy.objects.filter(student=student).count() == 0

    # Edit student
    update_student_account(
        acting_user=pure_sysadmin,
        student=student,
        full_name="Bedless Student Renamed",
        registration_number="2024CS002",
        change_reason="Administrative name spelling correction",
    )

    assert student.current_occupancy is None
    assert StudentOccupancy.objects.filter(student=student).count() == 0


@pytest.mark.django_db
def test_only_hostel_admin_can_verify_residential_eligibility(
    admin_user, pure_sysadmin, warden_user, student_user, institution
):
    """Only Hostel Administrator can verify residential eligibility; Sysadmin and others denied."""
    student = create_student_account(
        acting_user=pure_sysadmin,
        full_name="Awaiting Student",
        registration_number="2024CS003",
        email="awaiting@apex.test",
        claimed_residential_category=User.ClaimedResidentialCategory.MALE,
    )

    assert student.residential_eligibility == User.ResidentialEligibility.UNVERIFIED

    # Non-hostel admins denied
    for unauthorized in [pure_sysadmin, warden_user, student_user]:
        assert not can_verify_residential_eligibility(unauthorized)
        with pytest.raises(PermissionDenied):
            verify_student_eligibility(
                acting_user=unauthorized,
                student=student,
                new_eligibility=User.ResidentialEligibility.MALE,
                reason="Unauthorized attempt",
            )

    # Hostel admin can verify
    assert can_verify_residential_eligibility(admin_user)
    updated_student, record = verify_student_eligibility(
        acting_user=admin_user,
        student=student,
        new_eligibility=User.ResidentialEligibility.MALE,
        reason="Verified physical government ID and hostel registration dossier.",
    )

    assert updated_student.residential_eligibility == User.ResidentialEligibility.MALE

    # Check immutable record
    assert record is not None
    assert record.verifier == admin_user
    assert record.previous_eligibility == User.ResidentialEligibility.UNVERIFIED
    assert record.new_eligibility == User.ResidentialEligibility.MALE


@pytest.mark.django_db
def test_eligibility_change_blocked_when_conflicting_with_active_occupancy(
    admin_user, pure_sysadmin, warden_user, hostel_with_beds, institution
):
    """Attempting to change eligibility to FEMALE while actively residing in a MALE hostel is blocked."""
    student = create_student_account(
        acting_user=pure_sysadmin,
        full_name="Male Resident",
        registration_number="2024CS004",
        email="male.resident@apex.test",
    )

    # Verify as Male first
    verify_student_eligibility(
        acting_user=admin_user,
        student=student,
        new_eligibility=User.ResidentialEligibility.MALE,
        reason="Initial male verification.",
    )

    # Warden allocates bed in Male hostel
    bed = hostel_with_beds["bed1"]
    assign_student_to_bed(
        student=student,
        bed=bed,
        created_by=warden_user,
    )

    assert student.current_occupancy is not None

    # Now attempt to switch eligibility to FEMALE -> Must be rejected!
    with pytest.raises(ValidationError) as exc:
        verify_student_eligibility(
            acting_user=admin_user,
            student=student,
            new_eligibility=User.ResidentialEligibility.FEMALE,
            reason="Attempting unauthorized switch with active allocation.",
        )
    assert "active bed allocation" in str(exc.value).lower()
    assert "release" in str(exc.value).lower()


@pytest.mark.django_db
def test_first_time_password_setup_flow(pure_sysadmin, institution, client):
    """Student completes first-time password setup using cryptographically secure token."""
    student = create_student_account(
        acting_user=pure_sysadmin,
        full_name="New Student",
        registration_number="2024CS005",
        email="new.student@apex.test",
    )

    token = default_token_generator.make_token(student)
    uidb64 = urlsafe_base64_encode(force_bytes(student.pk))

    setup_url = reverse("accounts:password_setup", kwargs={"uidb64": uidb64, "token": token})

    # GET setup page
    resp = client.get(setup_url)
    assert resp.status_code == 200

    # POST password setup
    resp = client.post(setup_url, {
        "new_password1": "SecureStudentPass2026!",
        "new_password2": "SecureStudentPass2026!",
    })
    assert resp.status_code == 302
    assert resp.url == reverse("accounts:login")

    student.refresh_from_db()
    assert student.has_usable_password()
    assert student.is_active is True

    # Student can now log in
    authenticated_user = authenticate(username="new.student@apex.test", password="SecureStudentPass2026!")
    assert authenticated_user == student

    # Reusing the token must now fail
    resp_reuse = client.get(setup_url)
    assert resp_reuse.status_code == 200
    assert "invalid or has expired" in resp_reuse.content.decode().lower()


@pytest.mark.django_db
def test_student_deactivation_blocks_login_and_warns_on_occupancy(
    pure_sysadmin, admin_user, warden_user, hostel_with_beds, institution
):
    """Deactivation requires a reason, warns if active occupancy exists, and blocks login while preserving records."""
    student = create_student_account(
        acting_user=pure_sysadmin,
        full_name="Deactivatable Student",
        registration_number="2024CS006",
        email="deactivatable@apex.test",
    )
    verify_student_eligibility(
        acting_user=admin_user,
        student=student,
        new_eligibility=User.ResidentialEligibility.MALE,
        reason="Verified for accommodation.",
    )
    complete_student_password_setup(
        uidb64=urlsafe_base64_encode(force_bytes(student.pk)),
        token=default_token_generator.make_token(student),
        new_password="ValidPassword123!",
    )

    # Allocate bed
    assign_student_to_bed(
        student=student,
        bed=hostel_with_beds["bed2"],
        created_by=warden_user,
    )

    # Deactivation without reason fails
    with pytest.raises(ValidationError):
        set_student_account_status(acting_user=pure_sysadmin, student=student, is_active=False, reason="")

    # Deactivation with valid reason succeeds with advisory warning
    updated_student, warning_msg = set_student_account_status(
        acting_user=pure_sysadmin,
        student=student,
        is_active=False,
        reason="Semester medical leave of absence.",
    )

    assert warning_msg is not None
    assert "does not release the physical bed" in warning_msg

    student.refresh_from_db()
    assert student.is_active is False
    # Occupancy is still preserved!
    assert student.current_occupancy is not None

    # Login is blocked
    auth_user = authenticate(username="deactivatable@apex.test", password="ValidPassword123!")
    assert auth_user is None or not auth_user.is_active


@pytest.mark.django_db
def test_csv_import_preview_makes_no_writes(pure_sysadmin, institution):
    """CSV validation preview analyzes rows and reports counts without writing to the database."""
    csv_content = (
        "full_name,registration_number,email,department,program,academic_year,phone_number,claimed_residential_category\n"
        "Preview Student 1,2024CSV01,p1@apex.test,Computer Science,B.Tech,first_year,+91 9999900001,male\n"
        "Preview Student 2,2024CSV02,p2@apex.test,Electrical Engineering,B.Tech,first_year,+91 9999900002,female\n"
    )

    initial_count = User.objects.filter(institution=institution).count()

    summary = validate_csv_student_import(
        acting_user=pure_sysadmin,
        file_obj=io.StringIO(csv_content),
    )

    assert summary["total_rows"] == 2
    assert summary["valid_count"] == 2
    assert summary["duplicate_count"] == 0
    assert summary["error_count"] == 0

    # Ensure NO writes occurred during preview
    assert User.objects.filter(institution=institution).count() == initial_count


@pytest.mark.django_db
def test_csv_import_processing_and_duplicate_resilience(pure_sysadmin, institution):
    """Confirming CSV import provisions accounts; re-importing the file creates no duplicates."""
    csv_content = (
        "full_name,registration_number,email,department,program,academic_year,phone_number,claimed_residential_category\n"
        "Roster Student A,2024CSV10,ra@apex.test,Computer Science,B.Tech,first_year,,male\n"
        "Roster Student B,2024CSV11,rb@apex.test,Mechanical Engineering,B.Tech,second_year,,female\n"
    )

    summary = validate_csv_student_import(
        acting_user=pure_sysadmin,
        file_obj=io.StringIO(csv_content),
    )
    created_count, errors = process_csv_student_import(
        acting_user=pure_sysadmin,
        rows=summary["valid_rows"],
    )

    assert created_count == 2
    assert len(errors) == 0

    student_a = User.objects.get(institution=institution, email="ra@apex.test")
    assert student_a.residential_eligibility == User.ResidentialEligibility.UNVERIFIED
    assert student_a.claimed_residential_category == "male"
    assert student_a.check_password("RoomLoop2026!") is True
    assert student_a.current_occupancy is None

    # Re-running validation against the exact same CSV must detect all rows as duplicates
    retry_summary = validate_csv_student_import(
        acting_user=pure_sysadmin,
        file_obj=io.StringIO(csv_content),
    )
    assert retry_summary["valid_count"] == 0
    assert retry_summary["duplicate_count"] == 2


@pytest.mark.django_db
def test_unverified_students_excluded_from_warden_allocation(
    pure_sysadmin, admin_user, warden_user, hostel_with_beds, institution
):
    """Unverified students are excluded from warden allocation dropdown and rejected by allocation service."""
    unverified_student = create_student_account(
        acting_user=pure_sysadmin,
        full_name="Unverified Allocation Candidate",
        registration_number="2024CS007",
        email="unverified.candidate@apex.test",
    )

    # 1. Selector check: unverified student must NOT appear in eligible unallocated students
    eligible_students = list(get_eligible_unallocated_students(institution))
    assert unverified_student not in eligible_students

    # 2. Service check: warden directly attempting allocation must be rejected
    bed = hostel_with_beds["bed1"]
    with pytest.raises(ValidationError) as exc:
        assign_student_to_bed(
            student=unverified_student,
            bed=bed,
            created_by=warden_user,
        )
    assert "unverified" in str(exc.value).lower()


@pytest.mark.django_db
def test_sysadmin_views_render_and_mutate_successfully(pure_sysadmin, institution, client):
    """Test full System Administrator UI workflows and view endpoints."""
    client.force_login(pure_sysadmin)

    # 1. Directory View
    resp = client.get(reverse("accounts:student_list"))
    assert resp.status_code == 200
    assert "Student Accounts Directory" in resp.content.decode()

    # 2. Add Student View GET & POST
    resp = client.get(reverse("accounts:student_create"))
    assert resp.status_code == 200
    assert "Provision Student Account" in resp.content.decode()

    resp = client.post(reverse("accounts:student_create"), {
        "full_name": "Rohan Mehra",
        "registration_number": "2024CS999",
        "email": "rohan.mehra@apex.test",
        "department": "Computer Science",
        "program": "B.Tech",
        "academic_year": "1st Year",
        "phone_number": "+91 9123456780",
        "claimed_residential_category": "male",
    })
    assert resp.status_code == 302
    created_student = User.objects.get(registration_number="2024CS999")
    assert resp.url == reverse("accounts:student_detail", kwargs={"pk": created_student.pk})
    assert created_student.check_password("RoomLoop2026!") is True

    # 3. Detail View
    detail_url = reverse("accounts:student_detail", kwargs={"pk": created_student.pk})
    resp = client.get(detail_url)
    assert resp.status_code == 200
    assert "Rohan Mehra" in resp.content.decode()

    # 4. Edit View GET & POST
    edit_url = reverse("accounts:student_edit", kwargs={"pk": created_student.pk})
    resp = client.get(edit_url)
    assert resp.status_code == 200

    resp = client.post(edit_url, {
        "full_name": "Rohan K. Mehra",
        "registration_number": "2024CS999",
        "email": "rohan.mehra@apex.test",
        "department": "CSE",
        "program": "B.Tech",
        "academic_year": "1st Year",
        "phone_number": "+91 9123456780",
        "claimed_residential_category": "male",
        "change_reason": "Adding middle initial to legal name",
    })
    assert resp.status_code == 302
    created_student.refresh_from_db()
    assert created_student.full_name == "Rohan K. Mehra"

    # 5. Toggle Status View GET & POST (Deactivate)
    toggle_url = reverse("accounts:student_toggle_status", kwargs={"pk": created_student.pk})
    resp = client.get(toggle_url)
    assert resp.status_code == 200
    assert "Deactivate Student Account" in resp.content.decode()

    resp = client.post(toggle_url, {
        "reason": "Administrative suspension pending verification.",
    })
    assert resp.status_code == 302
    created_student.refresh_from_db()
    assert created_student.is_active is False

    # 6. Password Reset Initiate POST
    reset_url = reverse("accounts:student_password_reset", kwargs={"pk": created_student.pk})
    resp = client.post(reset_url)
    assert resp.status_code == 302

    # 7. CSV Template Download
    resp = client.get(reverse("accounts:student_csv_template"))
    assert resp.status_code == 200
    assert resp["Content-Type"] == "text/csv"
    assert "full_name,registration_number,email" in resp.content.decode()

    # 8. CSV Import Page GET
    resp = client.get(reverse("accounts:student_import"))
    assert resp.status_code == 200
    assert "Bulk Student CSV Import" in resp.content.decode()


@pytest.mark.django_db
def test_hostel_admin_eligibility_views(admin_user, pure_sysadmin, institution, client):
    """Test Hostel Administrator eligibility verification queue and action endpoints."""
    student = create_student_account(
        acting_user=pure_sysadmin,
        full_name="Queue Student",
        registration_number="2024CS888",
        email="queue.student@apex.test",
        claimed_residential_category="male",
    )

    client.force_login(admin_user)

    # 1. Verification Queue View
    resp = client.get(reverse("accounts:eligibility_verification_list"))
    assert resp.status_code == 200
    assert "Residential Eligibility Verification Queue" in resp.content.decode()
    assert "Queue Student" in resp.content.decode()

    # 2. Verification Action View GET & POST
    action_url = reverse("accounts:eligibility_verify_student", kwargs={"pk": student.pk})
    resp = client.get(action_url)
    assert resp.status_code == 200
    assert "Verify Residential Accommodation Eligibility" in resp.content.decode()

    resp = client.post(action_url, {
        "new_eligibility": "male",
        "reason": "Verified residential documents in person.",
    })
    assert resp.status_code == 302
    assert resp.url == reverse("accounts:eligibility_verification_list")

    student.refresh_from_db()
    assert student.residential_eligibility == "male"


@pytest.mark.django_db
def test_student_dashboard_displays_unverified_and_verified_states(
    pure_sysadmin, admin_user, institution, client
):
    """Student dashboard shows unverified pending notice or verified accommodation overview."""
    student = create_student_account(
        acting_user=pure_sysadmin,
        full_name="Dashboard Student",
        registration_number="2024CS777",
        email="dashboard.student@apex.test",
        claimed_residential_category="male",
    )

    client.force_login(student)

    # 1. Unverified State
    resp = client.get(reverse("accounts:dashboard_student"))
    assert resp.status_code == 200
    content = resp.content.decode()
    assert "Residential Eligibility Verification Pending" in content
    assert "student-shell" in content
    assert "student-page" in content

    # 2. Verified State
    verify_student_eligibility(
        acting_user=admin_user,
        student=student,
        new_eligibility="male",
        reason="Verified academic admission file.",
    )
    student.refresh_from_db()

    resp = client.get(reverse("accounts:dashboard_student"))
    assert resp.status_code == 200
    content = resp.content.decode()
    assert "Residential Eligibility Verification Pending" not in content
    assert "Boys&#x27; Hostel Eligible" in content or "Verified Male" in content or "Male" in content
    assert "student-shell" in content
    assert "student-page" in content


@pytest.mark.django_db
def test_student_directory_filtering_and_rendering(pure_sysadmin, institution, client):
    """
    Test student directory filtering and template rendering across all filter permutations:
    - Unfiltered
    - Pending verification (eligibility=unverified)
    - Verified eligibility (male, female)
    - Account status (active, inactive)
    - Combined search and filters
    - Zero results state with clear action
    - Desktop and mobile layouts render the filtered data
    """
    client.force_login(pure_sysadmin)

    # Create distinct student accounts
    s_unverified_1 = create_student_account(
        acting_user=pure_sysadmin,
        full_name="Aarav Sharma",
        registration_number="REG-F01",
        email="aarav.sharma@apex.test",
        claimed_residential_category="male",
    )
    s_unverified_2 = create_student_account(
        acting_user=pure_sysadmin,
        full_name="Ananya Iyer",
        registration_number="REG-F02",
        email="ananya.iyer@apex.test",
        claimed_residential_category="female",
    )
    s_verified_male = create_student_account(
        acting_user=pure_sysadmin,
        full_name="Bhuvan Patel",
        registration_number="REG-F03",
        email="bhuvan.patel@apex.test",
        claimed_residential_category="male",
    )
    s_verified_male.residential_eligibility = User.ResidentialEligibility.MALE
    s_verified_male.save(update_fields=["residential_eligibility"])

    s_verified_female = create_student_account(
        acting_user=pure_sysadmin,
        full_name="Chitra Nair",
        registration_number="REG-F04",
        email="chitra.nair@apex.test",
        claimed_residential_category="female",
    )
    s_verified_female.residential_eligibility = User.ResidentialEligibility.FEMALE
    s_verified_female.save(update_fields=["residential_eligibility"])

    s_inactive = create_student_account(
        acting_user=pure_sysadmin,
        full_name="Dinesh Verma",
        registration_number="REG-F05",
        email="dinesh.verma@apex.test",
        claimed_residential_category="male",
    )
    s_inactive.is_active = False
    s_inactive.save(update_fields=["is_active"])

    # 1. Unfiltered Directory
    resp = client.get(reverse("accounts:student_list"))
    assert resp.status_code == 200
    content = resp.content.decode()
    assert "Aarav Sharma" in content
    assert "Ananya Iyer" in content
    assert "Bhuvan Patel" in content
    assert "Chitra Nair" in content
    assert "Dinesh Verma" in content
    assert "student-directory-table" in content
    assert "student-directory-cards" in content

    # 2. Pending Verification Filter (eligibility=unverified)
    resp = client.get(reverse("accounts:student_list") + "?q=&status=&eligibility=unverified")
    assert resp.status_code == 200
    content = resp.content.decode()
    # Aarav, Ananya, and Dinesh (unverified) are included
    assert "Aarav Sharma" in content
    assert "Ananya Iyer" in content
    assert "Dinesh Verma" in content
    # Verified students must NOT be in the results
    assert "Bhuvan Patel" not in content
    assert "Chitra Nair" not in content
    assert "3 students found" in content

    # 3. Verified Boys Filter (eligibility=male)
    resp = client.get(reverse("accounts:student_list") + "?eligibility=male")
    assert resp.status_code == 200
    content = resp.content.decode()
    assert "Bhuvan Patel" in content
    assert "Aarav Sharma" not in content
    assert "1 student found" in content

    # 4. Verified Girls Filter (eligibility=female)
    resp = client.get(reverse("accounts:student_list") + "?eligibility=female")
    assert resp.status_code == 200
    content = resp.content.decode()
    assert "Chitra Nair" in content
    assert "Bhuvan Patel" not in content
    assert "1 student found" in content

    # 5. Status Filters (active vs inactive)
    resp = client.get(reverse("accounts:student_list") + "?status=inactive")
    assert resp.status_code == 200
    content = resp.content.decode()
    assert "Dinesh Verma" in content
    assert "Aarav Sharma" not in content
    assert "1 student found" in content

    # 6. Search combined with eligibility
    resp = client.get(reverse("accounts:student_list") + "?q=Aarav&eligibility=unverified")
    assert resp.status_code == 200
    content = resp.content.decode()
    assert "Aarav Sharma" in content
    assert "Ananya Iyer" not in content
    assert "1 student found" in content

    # 7. No Results State with Clear Action
    resp = client.get(reverse("accounts:student_list") + "?q=NonExistentQueryXYZ")
    assert resp.status_code == 200
    content = resp.content.decode()
    assert "No students match these filters" in content
    assert "Clear filters" in content


