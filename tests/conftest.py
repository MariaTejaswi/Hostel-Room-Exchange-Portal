import pytest
from django.test import Client
from django.contrib.auth.models import Group
from apps.core.models import Institution, AuditLog
from apps.accounts.models import User, EligibilityVerificationRecord
from apps.accounts.services import (
    ensure_roles_exist,
    create_user_with_role,
    ROLE_STUDENT,
    ROLE_WARDEN,
    ROLE_CHIEF_WARDEN,
    ROLE_HOSTEL_ADMIN,
    ROLE_DEAN,
    ROLE_SYSADMIN,
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
from apps.exchange.models import (
    Approval,
    ExchangeProposal,
    ExchangeRequest,
    Notification,
    PolicyRule,
    ProposalParticipant,
    TransferRecord,
)


@pytest.fixture
def client():
    return Client()


@pytest.fixture
def institution():
    inst, _ = Institution.objects.get_or_create(
        code="APEX-TEST",
        defaults={
            "name": "Apex University",
            "contact_email": "admin@apex.test",
            "is_active": True,
        },
    )
    return inst


@pytest.fixture
def other_institution():
    inst, _ = Institution.objects.get_or_create(
        code="OTHER-TEST",
        defaults={
            "name": "Other University",
            "contact_email": "admin@other.test",
            "is_active": True,
        },
    )
    return inst


@pytest.fixture
def roles():
    return ensure_roles_exist()


@pytest.fixture
def student_user(institution, roles):
    user = User.objects.filter(email="student.test@apex.test").first()
    if user:
        return user
    return create_user_with_role(
        email="student.test@apex.test",
        full_name="Test Student",
        role_name=ROLE_STUDENT,
        institution=institution,
        password="TestPassword123!",
        registration_number="REG-001",
        residential_eligibility=User.ResidentialEligibility.MALE,
    )


@pytest.fixture
def other_student_user(institution, roles):
    user = User.objects.filter(email="other.student@apex.test").first()
    if user:
        return user
    return create_user_with_role(
        email="other.student@apex.test",
        full_name="Other Student",
        role_name=ROLE_STUDENT,
        institution=institution,
        password="TestPassword123!",
        registration_number="REG-002",
        residential_eligibility=User.ResidentialEligibility.MALE,
    )


@pytest.fixture
def warden_user(institution, roles):
    user = User.objects.filter(email="warden.test@apex.test").first()
    if user:
        return user
    return create_user_with_role(
        email="warden.test@apex.test",
        full_name="Test Warden",
        role_name=ROLE_WARDEN,
        institution=institution,
        password="TestPassword123!",
    )


@pytest.fixture
def chief_warden_user(institution, roles):
    user = User.objects.filter(email="chief.warden@apex.test").first()
    if user:
        return user
    return create_user_with_role(
        email="chief.warden@apex.test",
        full_name="Chief Warden",
        role_name=ROLE_CHIEF_WARDEN,
        institution=institution,
        password="TestPassword123!",
    )


@pytest.fixture
def admin_user(institution, roles):
    user = User.objects.filter(email="admin.test@apex.test").first()
    if user:
        return user
    return create_user_with_role(
        email="admin.test@apex.test",
        full_name="Hostel Admin",
        role_name=ROLE_HOSTEL_ADMIN,
        institution=institution,
        password="TestPassword123!",
        is_staff=True,
    )


@pytest.fixture
def sysadmin_user(institution, roles):
    user = User.objects.filter(email="sysadmin.test@apex.test").first()
    if user:
        return user
    user = create_user_with_role(
        email="sysadmin.test@apex.test",
        full_name="System Administrator",
        role_name=ROLE_SYSADMIN,
        institution=institution,
        password="TestPassword123!",
        is_staff=True,
    )
    user.is_superuser = True
    user.save()
    return user


@pytest.fixture
def dean_user(institution, roles):
    user = User.objects.filter(email="dean.test@apex.test").first()
    if user:
        return user
    return create_user_with_role(
        email="dean.test@apex.test",
        full_name="Dean Test",
        role_name=ROLE_DEAN,
        institution=institution,
        password="TestPassword123!",
    )


@pytest.fixture
def hostel_setup(institution, warden_user):
    hostel = Hostel.objects.create(
        institution=institution,
        name="North Wing",
        code="NW-01",
        resident_category=Hostel.ResidentCategory.MALE,
    )
    WardenAssignment.objects.create(warden=warden_user, hostel=hostel, is_primary=True)
    block = Block.objects.create(hostel=hostel, name="Block A", code="BA")
    floor = Floor.objects.create(block=block, floor_number=1, floor_label="1st Floor")
    room = Room.objects.create(
        floor=floor,
        room_number="101",
        room_type=Room.RoomType.DOUBLE,
        operational_status=OperationalStatus.IN_SERVICE,
    )
    bed_a = Bed.objects.create(room=room, bed_label="Bed A", operational_status=OperationalStatus.IN_SERVICE)
    bed_b = Bed.objects.create(room=room, bed_label="Bed B", operational_status=OperationalStatus.IN_SERVICE)
    return {
        "hostel": hostel,
        "block": block,
        "floor": floor,
        "room": room,
        "bed_a": bed_a,
        "bed_b": bed_b,
    }
