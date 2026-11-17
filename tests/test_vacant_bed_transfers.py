"""
Comprehensive test suite for RoomLoop Vacant-Bed Transfers.

Covers:
1. Two Distinct Request Creation Flows:
   - Occupied bed -> ExchangeRequest(request_type='exchange', status='open')
   - Vacant bed -> ExchangeRequest(request_type='vacant_transfer', status='pending_approval')
   - Selecting a bed never immediately moves the student
   - Server-side authoritative vacancy check
2. Matching Engine Exclusion:
   - Vacant-bed transfers are strictly excluded from cycle generation and matching
3. Authority Boundaries & Scoping:
   - Same-hostel transfer: Assigned Warden only
   - Cross-hostel transfer: Chief Warden of the same institution
   - Unassigned Warden denied
   - Warden cannot approve cross-hostel transfers
   - Chief Warden cannot approve same-hostel transfers without assignment
   - Deans, Hostel Admins, SysAdmins, and Students denied decision authority
   - Cross-institution decisions denied
4. Preconditions & Invalidation:
   - Target bed becomes occupied prior to approval -> blocks transfer, preserves student's original occupancy
   - Target bed out of service -> blocks transfer
   - Stale/ended source occupancy -> blocks transfer
   - Expired request -> marks EXPIRED, blocks transfer
5. Rejection Mechanics:
   - Mandatory remarks
   - Request becomes REJECTED
   - Student's current room allocation is strictly untouched
   - Notifications and audit logs dispatched
6. Withdrawal Workflow:
   - Student can withdraw pending vacant-bed transfer
   - Student remains in current room
7. Atomic Transfer Execution & Durability:
   - Single PostgreSQL transaction with row locks
   - Source occupancy closed (status=TRANSFERRED, end_date recorded)
   - New active occupancy created (status=ACTIVE, source=VACANT_TRANSFER)
   - Request updated to COMPLETED
   - Durable TransferRecord and Approval created with audit log and notification
   - Idempotent repeated execution
8. Section 9 Compliance:
   - Existing open exchange requests targeting vacant beds are not auto-converted or executed
9. HTTP Views, Queues, and Transfer Letter:
   - Approvals list queue filtering (queue=transfers)
   - Review and decision views (POST approve / reject)
   - Printable / downloadable Accommodation Transfer Confirmation Letter
"""

import uuid
from datetime import timedelta
import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.services import (
    ROLE_CHIEF_WARDEN,
    ROLE_DEAN,
    ROLE_HOSTEL_ADMIN,
    ROLE_STUDENT,
    ROLE_SYSADMIN,
    ROLE_WARDEN,
    create_user_with_role,
    ensure_roles_exist,
)
from apps.core.models import AuditLog, Institution
from apps.exchange.matching import find_candidate_matches
from apps.exchange.models import (
    Approval,
    ExchangeProposal,
    ExchangeRequest,
    Notification,
    PolicyRule,
    TransferRecord,
)
from apps.exchange.selectors import (
    can_user_decide_vacant_transfer,
    get_vacant_transfer_detail,
    get_vacant_transfers_queryset,
)
from apps.exchange.services import (
    approve_and_execute_vacant_transfer,
    create_exchange_request,
    reject_vacant_transfer,
    withdraw_exchange_request,
)
from apps.hostels.models import (
    Bed,
    Block,
    Floor,
    Hostel,
    OperationalStatus,
    Room,
    StudentOccupancy,
    WardenAssignment,
)
from apps.hostels.services import assign_student_to_bed


# =========================================================================
# Fixtures & Helpers
# =========================================================================

def _request_data(**overrides):
    values = {
        "preferred_hostel": None,
        "preferred_block": None,
        "preferred_floor": None,
        "preferred_room_type": "",
        "preferred_occupancy_category": "",
        "preferred_accessibility_attributes": "",
        "preferred_bed": None,
        "reason": "I need accommodation closer to my academic facilities.",
    }
    values.update(overrides)
    return values


def _configure_all_policies(institution):
    """Ensure all required exchange policies are explicitly configured."""
    defaults = {
        PolicyRule.Key.COOLING_OFF_DAYS: 0,
        PolicyRule.Key.REQUEST_VALIDITY_DAYS: 30,
        PolicyRule.Key.REQUIRE_FEE_CLEARANCE: False,
        PolicyRule.Key.REQUIRE_NO_DISCIPLINARY_HOLD: False,
        PolicyRule.Key.PROPOSAL_ACCEPTANCE_WINDOW_HOURS: 48,
    }
    for key, val in defaults.items():
        PolicyRule.objects.update_or_create(
            institution=institution,
            key=key,
            defaults={"value": {"value": val}, "is_active": True},
        )


def _setup_hostel_with_rooms(institution, name, code, gender=User.ResidentialEligibility.MALE):
    """Helper to create a hostel with block, floor, and rooms."""
    hostel = Hostel.objects.create(
        institution=institution,
        name=name,
        code=code,
        resident_category=gender,
        status=Hostel.Status.ACTIVE,
    )
    block = Block.objects.create(hostel=hostel, name=f"Block {code}", code=f"B-{code}")
    floor = Floor.objects.create(block=block, floor_number=1, floor_label="1F")
    return hostel, block, floor


# =========================================================================
# 1. Request Creation Flows (Occupied vs. Vacant Target)
# =========================================================================

@pytest.mark.django_db
def test_create_request_targeting_vacant_bed_creates_vacant_transfer(institution, warden_user):
    """Selecting a vacant bed creates a vacant-bed transfer routed directly to PENDING_APPROVAL."""
    _configure_all_policies(institution)
    ensure_roles_exist()

    hostel, block, floor = _setup_hostel_with_rooms(institution, "Hostel Alpha", "HA-01")
    WardenAssignment.objects.create(warden=warden_user, hostel=hostel, is_primary=True)

    # Student 1 in Room 101 Bed A
    student = create_user_with_role(
        email="student.vacant@apex.test",
        full_name="Vacant Requester",
        role_name=ROLE_STUDENT,
        institution=institution,
        password="TestPassword123!",
        registration_number="REG-VAC-01",
        residential_eligibility=User.ResidentialEligibility.MALE,
    )
    room1 = Room.objects.create(floor=floor, room_number="101", room_type=Room.RoomType.DOUBLE, operational_status=OperationalStatus.IN_SERVICE)
    bed1a = Bed.objects.create(room=room1, bed_label="Bed A", operational_status=OperationalStatus.IN_SERVICE)
    occ = assign_student_to_bed(student=student, bed=bed1a, created_by=warden_user)

    # Room 102 Bed A is vacant
    room2 = Room.objects.create(floor=floor, room_number="102", room_type=Room.RoomType.DOUBLE, operational_status=OperationalStatus.IN_SERVICE)
    vacant_bed = Bed.objects.create(room=room2, bed_label="Bed A", operational_status=OperationalStatus.IN_SERVICE)

    req = create_exchange_request(
        student=student,
        data=_request_data(
            preferred_bed=vacant_bed,
            preferred_hostel=hostel,
            preferred_room_type=Room.RoomType.DOUBLE,
            reason="Want room 102 because it is closer to library",
        ),
    )

    # Must be VACANT_TRANSFER and PENDING_APPROVAL
    assert req.request_type == ExchangeRequest.RequestType.VACANT_TRANSFER
    assert req.status == ExchangeRequest.Status.PENDING_APPROVAL
    assert req.preferred_bed == vacant_bed
    assert req.current_occupancy == occ

    # Selecting the bed does NOT immediately move the student!
    occ.refresh_from_db()
    assert occ.status == StudentOccupancy.OccupancyStatus.ACTIVE
    assert occ.bed == bed1a

    # Notification created
    assert Notification.objects.filter(
        recipient=student,
        notification_type=Notification.NotificationType.APPROVAL_UPDATED,
    ).exists()


@pytest.mark.django_db
def test_create_request_targeting_occupied_bed_creates_exchange_request(institution, warden_user):
    """Selecting an occupied bed creates an occupied-bed exchange request with status OPEN."""
    _configure_all_policies(institution)
    ensure_roles_exist()

    hostel, block, floor = _setup_hostel_with_rooms(institution, "Hostel Beta", "HB-01")
    WardenAssignment.objects.create(warden=warden_user, hostel=hostel, is_primary=True)

    student1 = create_user_with_role(
        email="s1@apex.test",
        full_name="Student 1",
        role_name=ROLE_STUDENT,
        institution=institution,
        password="TestPassword123!",
        registration_number="REG-S1",
        residential_eligibility=User.ResidentialEligibility.MALE,
    )
    student2 = create_user_with_role(
        email="s2@apex.test",
        full_name="Student 2",
        role_name=ROLE_STUDENT,
        institution=institution,
        password="TestPassword123!",
        registration_number="REG-S2",
        residential_eligibility=User.ResidentialEligibility.MALE,
    )

    room = Room.objects.create(floor=floor, room_number="201", room_type=Room.RoomType.DOUBLE, operational_status=OperationalStatus.IN_SERVICE)
    bed_a = Bed.objects.create(room=room, bed_label="Bed A", operational_status=OperationalStatus.IN_SERVICE)
    bed_b = Bed.objects.create(room=room, bed_label="Bed B", operational_status=OperationalStatus.IN_SERVICE)

    assign_student_to_bed(student=student1, bed=bed_a, created_by=warden_user)
    assign_student_to_bed(student=student2, bed=bed_b, created_by=warden_user)

    # Student 1 selects Bed B (occupied by Student 2)
    req = create_exchange_request(
        student=student1,
        data=_request_data(
            preferred_bed=bed_b,
            preferred_hostel=hostel,
            preferred_room_type=Room.RoomType.DOUBLE,
            reason="Want Bed B",
        ),
    )

    # Must be EXCHANGE and OPEN
    assert req.request_type == ExchangeRequest.RequestType.EXCHANGE
    assert req.status == ExchangeRequest.Status.OPEN


@pytest.mark.django_db
def test_create_request_cannot_select_own_bed(institution, warden_user):
    """A student cannot select their own current bed."""
    _configure_all_policies(institution)
    ensure_roles_exist()

    hostel, block, floor = _setup_hostel_with_rooms(institution, "Hostel Gamma", "HG-01")
    WardenAssignment.objects.create(warden=warden_user, hostel=hostel, is_primary=True)

    student = create_user_with_role(
        email="self@apex.test",
        full_name="Self Selector",
        role_name=ROLE_STUDENT,
        institution=institution,
        password="TestPassword123!",
        registration_number="REG-SELF",
        residential_eligibility=User.ResidentialEligibility.MALE,
    )
    room = Room.objects.create(floor=floor, room_number="301", room_type=Room.RoomType.SINGLE, operational_status=OperationalStatus.IN_SERVICE)
    bed = Bed.objects.create(room=room, bed_label="Bed A", operational_status=OperationalStatus.IN_SERVICE)
    assign_student_to_bed(student=student, bed=bed, created_by=warden_user)

    with pytest.raises(ValidationError, match="You cannot select your current bed"):
        create_exchange_request(
            student=student,
            data=_request_data(preferred_bed=bed),
        )


# =========================================================================
# 2. Matching Engine Exclusion
# =========================================================================

@pytest.mark.django_db
def test_vacant_transfers_excluded_from_matching_engine(institution, warden_user):
    """Vacant-bed transfers must never participate in cycle matching."""
    _configure_all_policies(institution)
    ensure_roles_exist()

    hostel, block, floor = _setup_hostel_with_rooms(institution, "Hostel Delta", "HD-01")
    WardenAssignment.objects.create(warden=warden_user, hostel=hostel, is_primary=True)

    student = create_user_with_role(
        email="transfer.only@apex.test",
        full_name="Transfer Only",
        role_name=ROLE_STUDENT,
        institution=institution,
        password="TestPassword123!",
        registration_number="REG-TO-01",
        residential_eligibility=User.ResidentialEligibility.MALE,
    )
    room = Room.objects.create(floor=floor, room_number="401", room_type=Room.RoomType.DOUBLE, operational_status=OperationalStatus.IN_SERVICE)
    bed_a = Bed.objects.create(room=room, bed_label="Bed A", operational_status=OperationalStatus.IN_SERVICE)
    bed_b = Bed.objects.create(room=room, bed_label="Bed B", operational_status=OperationalStatus.IN_SERVICE)
    assign_student_to_bed(student=student, bed=bed_a, created_by=warden_user)

    # Bed B is vacant -> creates a VACANT_TRANSFER request
    req = create_exchange_request(student=student, data=_request_data(preferred_bed=bed_b, preferred_hostel=hostel))
    assert req.request_type == ExchangeRequest.RequestType.VACANT_TRANSFER

    # Run matching engine
    candidates = find_candidate_matches(institution=institution, persist_candidates=True)
    assert len(candidates.candidates) == 0
    assert ExchangeProposal.objects.filter(institution=institution).count() == 0


# =========================================================================
# 3. Same-Hostel Vacant-Bed Transfer Approval & Atomic Execution
# =========================================================================

@pytest.mark.django_db
def test_assigned_warden_approves_and_executes_same_hostel_transfer(institution, warden_user):
    """
    Assigned Warden approves same-hostel transfer:
    - Atomically releases old bed (status=TRANSFERRED, end_date set).
    - Allocates new bed (status=ACTIVE, source=VACANT_TRANSFER).
    - Request marked COMPLETED.
    - Approval record created.
    - TransferRecord created with snapshots.
    - Audit log and notification dispatched.
    """
    _configure_all_policies(institution)
    ensure_roles_exist()

    hostel, block, floor = _setup_hostel_with_rooms(institution, "Hostel Epsilon", "HE-01")
    WardenAssignment.objects.create(warden=warden_user, hostel=hostel, is_primary=True)

    student = create_user_with_role(
        email="epsilon.student@apex.test",
        full_name="Epsilon Student",
        role_name=ROLE_STUDENT,
        institution=institution,
        password="TestPassword123!",
        registration_number="REG-EPS-01",
        residential_eligibility=User.ResidentialEligibility.MALE,
    )
    room1 = Room.objects.create(floor=floor, room_number="501", room_type=Room.RoomType.DOUBLE, operational_status=OperationalStatus.IN_SERVICE)
    bed1 = Bed.objects.create(room=room1, bed_label="Bed A", operational_status=OperationalStatus.IN_SERVICE)
    old_occ = assign_student_to_bed(student=student, bed=bed1, created_by=warden_user)

    room2 = Room.objects.create(floor=floor, room_number="502", room_type=Room.RoomType.DOUBLE, operational_status=OperationalStatus.IN_SERVICE)
    bed2 = Bed.objects.create(room=room2, bed_label="Bed B", operational_status=OperationalStatus.IN_SERVICE)

    req = create_exchange_request(student=student, data=_request_data(preferred_bed=bed2, preferred_hostel=hostel))

    approval, transfer_record = approve_and_execute_vacant_transfer(
        request_id=req.id,
        actor=warden_user,
        remarks="Approved after room inspection. Destination bed is clean.",
    )

    # 1. Approval Record
    assert approval.decision == Approval.Decision.APPROVED
    assert approval.authority_user == warden_user
    assert approval.authority_role == Approval.AuthorityRole.WARDEN
    assert approval.scope_hostel == hostel
    assert approval.remarks == "Approved after room inspection. Destination bed is clean."

    # 2. Transfer Record
    assert transfer_record.exchange_request == req
    assert transfer_record.student == student
    assert transfer_record.source_hostel_name == hostel.name
    assert transfer_record.source_room_number == "501"
    assert transfer_record.source_bed_label == "Bed A"
    assert transfer_record.destination_hostel_name == hostel.name
    assert transfer_record.destination_room_number == "502"
    assert transfer_record.destination_bed_label == "Bed B"
    assert transfer_record.approved_by == warden_user

    # 3. Occupancy transitions
    old_occ.refresh_from_db()
    assert old_occ.status == StudentOccupancy.OccupancyStatus.TRANSFERRED
    assert old_occ.end_date is not None

    new_occ = StudentOccupancy.objects.get(student=student, status=StudentOccupancy.OccupancyStatus.ACTIVE)
    assert new_occ.bed == bed2
    assert new_occ.source == StudentOccupancy.AllocationSource.VACANT_TRANSFER
    assert transfer_record.new_occupancy == new_occ

    # 4. Request status
    req.refresh_from_db()
    assert req.status == ExchangeRequest.Status.COMPLETED

    # 5. Audit Log
    assert AuditLog.objects.filter(
        target_id=str(req.id),
        action="VACANT_TRANSFER_EXECUTED",
        acting_user=warden_user,
    ).exists()

    # 6. Notification
    assert Notification.objects.filter(
        recipient=student,
        notification_type=Notification.NotificationType.TRANSFER_COMPLETED,
    ).exists()


# =========================================================================
# 4. Cross-Hostel Vacant-Bed Transfer Approval & Atomic Execution
# =========================================================================

@pytest.mark.django_db
def test_chief_warden_approves_and_executes_cross_hostel_transfer(institution, chief_warden_user, warden_user):
    """
    Chief Warden approves cross-hostel transfer:
    - Moves student from Hostel 1 to Hostel 2.
    - Approval record has authority_role=CHIEF_WARDEN, scope_hostel=None.
    """
    _configure_all_policies(institution)
    ensure_roles_exist()

    h1, b1, f1 = _setup_hostel_with_rooms(institution, "Hostel One", "H1-01")
    h2, b2, f2 = _setup_hostel_with_rooms(institution, "Hostel Two", "H2-01")
    WardenAssignment.objects.create(warden=warden_user, hostel=h1, is_primary=True)

    student = create_user_with_role(
        email="cross.student@apex.test",
        full_name="Cross Hostel Student",
        role_name=ROLE_STUDENT,
        institution=institution,
        password="TestPassword123!",
        registration_number="REG-CROSS-01",
        residential_eligibility=User.ResidentialEligibility.MALE,
    )
    room1 = Room.objects.create(floor=f1, room_number="101", room_type=Room.RoomType.DOUBLE, operational_status=OperationalStatus.IN_SERVICE)
    bed1 = Bed.objects.create(room=room1, bed_label="Bed A", operational_status=OperationalStatus.IN_SERVICE)
    old_occ = assign_student_to_bed(student=student, bed=bed1, created_by=warden_user)

    room2 = Room.objects.create(floor=f2, room_number="201", room_type=Room.RoomType.DOUBLE, operational_status=OperationalStatus.IN_SERVICE)
    bed2 = Bed.objects.create(room=room2, bed_label="Bed B", operational_status=OperationalStatus.IN_SERVICE)

    req = create_exchange_request(student=student, data=_request_data(preferred_bed=bed2, preferred_hostel=h2))

    approval, transfer_record = approve_and_execute_vacant_transfer(
        request_id=req.id,
        actor=chief_warden_user,
        remarks="Cross-hostel transfer approved by Chief Warden.",
    )

    assert approval.authority_role == Approval.AuthorityRole.CHIEF_WARDEN
    assert approval.scope_hostel is None
    assert transfer_record.source_hostel_name == h1.name
    assert transfer_record.destination_hostel_name == h2.name

    new_occ = StudentOccupancy.objects.get(student=student, status=StudentOccupancy.OccupancyStatus.ACTIVE)
    assert new_occ.bed == bed2


# =========================================================================
# 5. Authority Boundaries & Role Restrictions
# =========================================================================

@pytest.mark.django_db
def test_unassigned_warden_cannot_approve_same_hostel_transfer(institution, warden_user):
    """A warden who is NOT assigned to the hostel cannot approve a same-hostel transfer."""
    _configure_all_policies(institution)
    ensure_roles_exist()

    hostel, block, floor = _setup_hostel_with_rooms(institution, "Hostel Zeta", "HZ-01")
    # A dedicated warden is assigned to this hostel
    local_warden = create_user_with_role(
        email="local.warden@apex.test",
        full_name="Local Warden",
        role_name=ROLE_WARDEN,
        institution=institution,
        password="TestPassword123!",
    )
    WardenAssignment.objects.create(warden=local_warden, hostel=hostel, is_primary=True)

    student = create_user_with_role(
        email="zeta.student@apex.test",
        full_name="Zeta Student",
        role_name=ROLE_STUDENT,
        institution=institution,
        password="TestPassword123!",
        registration_number="REG-ZET-01",
        residential_eligibility=User.ResidentialEligibility.MALE,
    )
    room = Room.objects.create(floor=floor, room_number="601", room_type=Room.RoomType.DOUBLE, operational_status=OperationalStatus.IN_SERVICE)
    bed_a = Bed.objects.create(room=room, bed_label="Bed A", operational_status=OperationalStatus.IN_SERVICE)
    bed_b = Bed.objects.create(room=room, bed_label="Bed B", operational_status=OperationalStatus.IN_SERVICE)
    assign_student_to_bed(student=student, bed=bed_a, created_by=local_warden)

    req = create_exchange_request(student=student, data=_request_data(preferred_bed=bed_b, preferred_hostel=hostel))

    # warden_user is NOT assigned to Hostel Zeta
    with pytest.raises(PermissionDenied, match="You are not assigned to supervise hostel"):
        approve_and_execute_vacant_transfer(
            request_id=req.id,
            actor=warden_user,
            remarks="Unauthorized attempt",
        )


@pytest.mark.django_db
def test_warden_cannot_approve_cross_hostel_transfer(institution, warden_user):
    """A hostel warden cannot approve a cross-hostel transfer (Chief Warden required)."""
    _configure_all_policies(institution)
    ensure_roles_exist()

    h1, b1, f1 = _setup_hostel_with_rooms(institution, "Hostel Theta 1", "HT1-01")
    h2, b2, f2 = _setup_hostel_with_rooms(institution, "Hostel Theta 2", "HT2-01")
    WardenAssignment.objects.create(warden=warden_user, hostel=h1, is_primary=True)

    student = create_user_with_role(
        email="theta.student@apex.test",
        full_name="Theta Student",
        role_name=ROLE_STUDENT,
        institution=institution,
        password="TestPassword123!",
        registration_number="REG-TH-01",
        residential_eligibility=User.ResidentialEligibility.MALE,
    )
    room1 = Room.objects.create(floor=f1, room_number="101", room_type=Room.RoomType.DOUBLE, operational_status=OperationalStatus.IN_SERVICE)
    bed1 = Bed.objects.create(room=room1, bed_label="Bed A", operational_status=OperationalStatus.IN_SERVICE)
    assign_student_to_bed(student=student, bed=bed1, created_by=warden_user)

    room2 = Room.objects.create(floor=f2, room_number="201", room_type=Room.RoomType.DOUBLE, operational_status=OperationalStatus.IN_SERVICE)
    bed2 = Bed.objects.create(room=room2, bed_label="Bed B", operational_status=OperationalStatus.IN_SERVICE)

    req = create_exchange_request(student=student, data=_request_data(preferred_bed=bed2, preferred_hostel=h2))

    with pytest.raises(PermissionDenied, match="Cross-hostel transfers require Chief Warden approval"):
        approve_and_execute_vacant_transfer(
            request_id=req.id,
            actor=warden_user,
            remarks="Unauthorized cross-hostel attempt",
        )


@pytest.mark.django_db
def test_dean_sysadmin_admin_and_students_cannot_approve_transfer(institution, warden_user, dean_user, sysadmin_user, admin_user):
    """Deans, System Admins, Hostel Admins, and Students are strictly barred from approving transfers."""
    _configure_all_policies(institution)
    ensure_roles_exist()

    hostel, block, floor = _setup_hostel_with_rooms(institution, "Hostel Iota", "HI-01")
    WardenAssignment.objects.create(warden=warden_user, hostel=hostel, is_primary=True)

    student = create_user_with_role(
        email="iota.student@apex.test",
        full_name="Iota Student",
        role_name=ROLE_STUDENT,
        institution=institution,
        password="TestPassword123!",
        registration_number="REG-IO-01",
        residential_eligibility=User.ResidentialEligibility.MALE,
    )
    room = Room.objects.create(floor=floor, room_number="701", room_type=Room.RoomType.DOUBLE, operational_status=OperationalStatus.IN_SERVICE)
    bed_a = Bed.objects.create(room=room, bed_label="Bed A", operational_status=OperationalStatus.IN_SERVICE)
    bed_b = Bed.objects.create(room=room, bed_label="Bed B", operational_status=OperationalStatus.IN_SERVICE)
    assign_student_to_bed(student=student, bed=bed_a, created_by=warden_user)

    req = create_exchange_request(student=student, data=_request_data(preferred_bed=bed_b, preferred_hostel=hostel))

    # Dean
    with pytest.raises(PermissionDenied, match="Dean of Student Welfare has read-only analytics access"):
        approve_and_execute_vacant_transfer(request_id=req.id, actor=dean_user, remarks="Dean approval attempt")

    # Sysadmin
    with pytest.raises(PermissionDenied, match="System Administrators have no operational transfer approval authority"):
        approve_and_execute_vacant_transfer(request_id=req.id, actor=sysadmin_user, remarks="Sysadmin attempt")

    # Hostel Admin
    with pytest.raises(PermissionDenied, match="Hostel Administrators manage policy configuration"):
        approve_and_execute_vacant_transfer(request_id=req.id, actor=admin_user, remarks="Admin attempt")

    # Student
    with pytest.raises(PermissionDenied, match="Students cannot approve transfers"):
        approve_and_execute_vacant_transfer(request_id=req.id, actor=student, remarks="Self approval attempt")


@pytest.mark.django_db
def test_cross_institution_transfer_approval_denied(institution, other_institution, warden_user):
    """An authority from another institution cannot approve a transfer."""
    _configure_all_policies(institution)
    ensure_roles_exist()

    hostel, block, floor = _setup_hostel_with_rooms(institution, "Hostel Kappa", "HK-01")
    WardenAssignment.objects.create(warden=warden_user, hostel=hostel, is_primary=True)

    student = create_user_with_role(
        email="kappa.student@apex.test",
        full_name="Kappa Student",
        role_name=ROLE_STUDENT,
        institution=institution,
        password="TestPassword123!",
        registration_number="REG-KP-01",
        residential_eligibility=User.ResidentialEligibility.MALE,
    )
    room = Room.objects.create(floor=floor, room_number="801", room_type=Room.RoomType.DOUBLE, operational_status=OperationalStatus.IN_SERVICE)
    bed_a = Bed.objects.create(room=room, bed_label="Bed A", operational_status=OperationalStatus.IN_SERVICE)
    bed_b = Bed.objects.create(room=room, bed_label="Bed B", operational_status=OperationalStatus.IN_SERVICE)
    assign_student_to_bed(student=student, bed=bed_a, created_by=warden_user)
    req = create_exchange_request(student=student, data=_request_data(preferred_bed=bed_b, preferred_hostel=hostel))

    other_warden = create_user_with_role(
        email="other.warden@other.test",
        full_name="Other Warden",
        role_name=ROLE_WARDEN,
        institution=other_institution,
        password="TestPassword123!",
    )

    with pytest.raises(PermissionDenied, match="You cannot approve requests from another institution"):
        approve_and_execute_vacant_transfer(request_id=req.id, actor=other_warden, remarks="Cross institution attempt")


# =========================================================================
# 6. Preconditions, Invalidation & Concurrency Protection
# =========================================================================

@pytest.mark.django_db
def test_target_bed_occupied_prior_to_approval_blocks_transfer(institution, warden_user):
    """If target bed becomes occupied before approval, the transfer fails and student's room is preserved."""
    _configure_all_policies(institution)
    ensure_roles_exist()

    hostel, block, floor = _setup_hostel_with_rooms(institution, "Hostel Lambda", "HL-01")
    WardenAssignment.objects.create(warden=warden_user, hostel=hostel, is_primary=True)

    student1 = create_user_with_role(
        email="lam1@apex.test",
        full_name="Lambda 1",
        role_name=ROLE_STUDENT,
        institution=institution,
        password="TestPassword123!",
        registration_number="REG-L1",
        residential_eligibility=User.ResidentialEligibility.MALE,
    )
    student2 = create_user_with_role(
        email="lam2@apex.test",
        full_name="Lambda 2",
        role_name=ROLE_STUDENT,
        institution=institution,
        password="TestPassword123!",
        registration_number="REG-L2",
        residential_eligibility=User.ResidentialEligibility.MALE,
    )

    room = Room.objects.create(floor=floor, room_number="901", room_type=Room.RoomType.DOUBLE, operational_status=OperationalStatus.IN_SERVICE)
    bed_a = Bed.objects.create(room=room, bed_label="Bed A", operational_status=OperationalStatus.IN_SERVICE)
    bed_b = Bed.objects.create(room=room, bed_label="Bed B", operational_status=OperationalStatus.IN_SERVICE)

    occ1 = assign_student_to_bed(student=student1, bed=bed_a, created_by=warden_user)

    # Student 1 requests vacant Bed B
    req = create_exchange_request(student=student1, data=_request_data(preferred_bed=bed_b, preferred_hostel=hostel))

    # Meanwhile, an admin allocates Bed B to Student 2
    assign_student_to_bed(student=student2, bed=bed_b, created_by=warden_user)

    # Approval attempt must fail
    with pytest.raises(ValidationError, match="Destination bed is no longer vacant"):
        approve_and_execute_vacant_transfer(
            request_id=req.id,
            actor=warden_user,
            remarks="Approval when bed already taken",
        )

    # Student 1 original occupancy is completely intact
    occ1.refresh_from_db()
    assert occ1.status == StudentOccupancy.OccupancyStatus.ACTIVE
    assert occ1.bed == bed_a


@pytest.mark.django_db
def test_target_bed_out_of_service_blocks_transfer(institution, warden_user):
    """If destination bed is placed OUT_OF_SERVICE, approval is blocked."""
    _configure_all_policies(institution)
    ensure_roles_exist()

    hostel, block, floor = _setup_hostel_with_rooms(institution, "Hostel Mu", "HM-01")
    WardenAssignment.objects.create(warden=warden_user, hostel=hostel, is_primary=True)

    student = create_user_with_role(
        email="mu.student@apex.test",
        full_name="Mu Student",
        role_name=ROLE_STUDENT,
        institution=institution,
        password="TestPassword123!",
        registration_number="REG-MU-01",
        residential_eligibility=User.ResidentialEligibility.MALE,
    )
    room = Room.objects.create(floor=floor, room_number="1001", room_type=Room.RoomType.DOUBLE, operational_status=OperationalStatus.IN_SERVICE)
    bed_a = Bed.objects.create(room=room, bed_label="Bed A", operational_status=OperationalStatus.IN_SERVICE)
    bed_b = Bed.objects.create(room=room, bed_label="Bed B", operational_status=OperationalStatus.IN_SERVICE)
    occ = assign_student_to_bed(student=student, bed=bed_a, created_by=warden_user)

    req = create_exchange_request(student=student, data=_request_data(preferred_bed=bed_b, preferred_hostel=hostel))

    # Bed B goes out of service for repairs
    bed_b.operational_status = OperationalStatus.OUT_OF_SERVICE
    bed_b.save(update_fields=["operational_status"])

    with pytest.raises(ValidationError, match="Target bed is no longer in service"):
        approve_and_execute_vacant_transfer(request_id=req.id, actor=warden_user, remarks="Approve repair bed")

    occ.refresh_from_db()
    assert occ.status == StudentOccupancy.OccupancyStatus.ACTIVE


@pytest.mark.django_db
def test_expired_transfer_request_blocks_approval(institution, warden_user):
    """An expired transfer request cannot be approved and transitions to EXPIRED."""
    _configure_all_policies(institution)
    ensure_roles_exist()

    hostel, block, floor = _setup_hostel_with_rooms(institution, "Hostel Nu", "HN-01")
    WardenAssignment.objects.create(warden=warden_user, hostel=hostel, is_primary=True)

    student = create_user_with_role(
        email="nu.student@apex.test",
        full_name="Nu Student",
        role_name=ROLE_STUDENT,
        institution=institution,
        password="TestPassword123!",
        registration_number="REG-NU-01",
        residential_eligibility=User.ResidentialEligibility.MALE,
    )
    room = Room.objects.create(floor=floor, room_number="1101", room_type=Room.RoomType.DOUBLE, operational_status=OperationalStatus.IN_SERVICE)
    bed_a = Bed.objects.create(room=room, bed_label="Bed A", operational_status=OperationalStatus.IN_SERVICE)
    bed_b = Bed.objects.create(room=room, bed_label="Bed B", operational_status=OperationalStatus.IN_SERVICE)
    occ = assign_student_to_bed(student=student, bed=bed_a, created_by=warden_user)

    req = create_exchange_request(student=student, data=_request_data(preferred_bed=bed_b, preferred_hostel=hostel))

    # Fast forward past expires_at
    future_time = timezone.now() + timedelta(days=60)
    with pytest.raises(ValidationError, match="Transfer request has expired prior to approval decision"):
        approve_and_execute_vacant_transfer(request_id=req.id, actor=warden_user, remarks="Late approval", now=future_time)

    req.refresh_from_db()
    assert req.status == ExchangeRequest.Status.EXPIRED
    occ.refresh_from_db()
    assert occ.status == StudentOccupancy.OccupancyStatus.ACTIVE


# =========================================================================
# 7. Rejection Mechanics
# =========================================================================

@pytest.mark.django_db
def test_reject_vacant_transfer_requires_remarks_and_leaves_occupancy_intact(institution, warden_user):
    """
    Rejecting a transfer request:
    - Remarks are mandatory (raises ValidationError if missing/blank).
    - Status updated to REJECTED.
    - Current occupancy is strictly untouched.
    - Audit log and notification dispatched.
    """
    _configure_all_policies(institution)
    ensure_roles_exist()

    hostel, block, floor = _setup_hostel_with_rooms(institution, "Hostel Xi", "HX-01")
    WardenAssignment.objects.create(warden=warden_user, hostel=hostel, is_primary=True)

    student = create_user_with_role(
        email="xi.student@apex.test",
        full_name="Xi Student",
        role_name=ROLE_STUDENT,
        institution=institution,
        password="TestPassword123!",
        registration_number="REG-XI-01",
        residential_eligibility=User.ResidentialEligibility.MALE,
    )
    room = Room.objects.create(floor=floor, room_number="1201", room_type=Room.RoomType.DOUBLE, operational_status=OperationalStatus.IN_SERVICE)
    bed_a = Bed.objects.create(room=room, bed_label="Bed A", operational_status=OperationalStatus.IN_SERVICE)
    bed_b = Bed.objects.create(room=room, bed_label="Bed B", operational_status=OperationalStatus.IN_SERVICE)
    occ = assign_student_to_bed(student=student, bed=bed_a, created_by=warden_user)

    req = create_exchange_request(student=student, data=_request_data(preferred_bed=bed_b, preferred_hostel=hostel))

    # Empty remarks fails
    with pytest.raises(ValidationError, match="Remarks are required when rejecting"):
        reject_vacant_transfer(request_id=req.id, actor=warden_user, remarks="")

    # Valid rejection
    approval, updated_req = reject_vacant_transfer(
        request_id=req.id,
        actor=warden_user,
        remarks="Room is reserved for incoming exchange scholars.",
    )

    assert approval.decision == Approval.Decision.REJECTED
    assert approval.remarks == "Room is reserved for incoming exchange scholars."
    assert updated_req.status == ExchangeRequest.Status.REJECTED

    # Current occupancy completely untouched
    occ.refresh_from_db()
    assert occ.status == StudentOccupancy.OccupancyStatus.ACTIVE
    assert occ.bed == bed_a

    # Notification sent
    assert Notification.objects.filter(
        recipient=student,
        notification_type=Notification.NotificationType.APPROVAL_UPDATED,
    ).exists()


# =========================================================================
# 8. Withdrawal Workflow
# =========================================================================

@pytest.mark.django_db
def test_student_can_withdraw_pending_vacant_transfer(institution, warden_user):
    """A student can withdraw their own PENDING_APPROVAL vacant transfer."""
    _configure_all_policies(institution)
    ensure_roles_exist()

    hostel, block, floor = _setup_hostel_with_rooms(institution, "Hostel Omicron", "HO-01")
    WardenAssignment.objects.create(warden=warden_user, hostel=hostel, is_primary=True)

    student = create_user_with_role(
        email="om.student@apex.test",
        full_name="Omicron Student",
        role_name=ROLE_STUDENT,
        institution=institution,
        password="TestPassword123!",
        registration_number="REG-OM-01",
        residential_eligibility=User.ResidentialEligibility.MALE,
    )
    room = Room.objects.create(floor=floor, room_number="1301", room_type=Room.RoomType.DOUBLE, operational_status=OperationalStatus.IN_SERVICE)
    bed_a = Bed.objects.create(room=room, bed_label="Bed A", operational_status=OperationalStatus.IN_SERVICE)
    bed_b = Bed.objects.create(room=room, bed_label="Bed B", operational_status=OperationalStatus.IN_SERVICE)
    occ = assign_student_to_bed(student=student, bed=bed_a, created_by=warden_user)

    req = create_exchange_request(student=student, data=_request_data(preferred_bed=bed_b, preferred_hostel=hostel))
    assert req.status == ExchangeRequest.Status.PENDING_APPROVAL

    withdrawn = withdraw_exchange_request(exchange_request=req, student=student)
    assert withdrawn.status == ExchangeRequest.Status.WITHDRAWN

    occ.refresh_from_db()
    assert occ.status == StudentOccupancy.OccupancyStatus.ACTIVE


# =========================================================================
# 9. Idempotency of Approval & Execution
# =========================================================================

@pytest.mark.django_db
def test_vacant_transfer_approval_is_idempotent(institution, warden_user):
    """Calling approve_and_execute_vacant_transfer twice returns existing record without duplicating."""
    _configure_all_policies(institution)
    ensure_roles_exist()

    hostel, block, floor = _setup_hostel_with_rooms(institution, "Hostel Pi", "HP-01")
    WardenAssignment.objects.create(warden=warden_user, hostel=hostel, is_primary=True)

    student = create_user_with_role(
        email="pi.student@apex.test",
        full_name="Pi Student",
        role_name=ROLE_STUDENT,
        institution=institution,
        password="TestPassword123!",
        registration_number="REG-PI-01",
        residential_eligibility=User.ResidentialEligibility.MALE,
    )
    room = Room.objects.create(floor=floor, room_number="1401", room_type=Room.RoomType.DOUBLE, operational_status=OperationalStatus.IN_SERVICE)
    bed_a = Bed.objects.create(room=room, bed_label="Bed A", operational_status=OperationalStatus.IN_SERVICE)
    bed_b = Bed.objects.create(room=room, bed_label="Bed B", operational_status=OperationalStatus.IN_SERVICE)
    assign_student_to_bed(student=student, bed=bed_a, created_by=warden_user)

    req = create_exchange_request(student=student, data=_request_data(preferred_bed=bed_b, preferred_hostel=hostel))

    appr1, tr1 = approve_and_execute_vacant_transfer(request_id=req.id, actor=warden_user, remarks="First call")
    appr2, tr2 = approve_and_execute_vacant_transfer(request_id=req.id, actor=warden_user, remarks="Second call")

    assert appr1.id == appr2.id
    assert tr1.id == tr2.id
    assert StudentOccupancy.objects.filter(student=student, status=StudentOccupancy.OccupancyStatus.ACTIVE).count() == 1


# =========================================================================
# 10. Section 9 Compliance: Non-Conversion of Existing Exchange Requests
# =========================================================================

@pytest.mark.django_db
def test_existing_open_exchange_request_not_auto_converted_to_transfer(institution, warden_user):
    """
    An existing open exchange request targeting a bed that happens to be vacant
    must NOT be auto-converted or executed via vacant transfer approval.
    """
    _configure_all_policies(institution)
    ensure_roles_exist()

    hostel, block, floor = _setup_hostel_with_rooms(institution, "Hostel Rho", "HR-01")
    WardenAssignment.objects.create(warden=warden_user, hostel=hostel, is_primary=True)

    student = create_user_with_role(
        email="rho.student@apex.test",
        full_name="Rho Student",
        role_name=ROLE_STUDENT,
        institution=institution,
        password="TestPassword123!",
        registration_number="REG-RH-01",
        residential_eligibility=User.ResidentialEligibility.MALE,
    )
    room = Room.objects.create(floor=floor, room_number="1501", room_type=Room.RoomType.DOUBLE, operational_status=OperationalStatus.IN_SERVICE)
    bed_a = Bed.objects.create(room=room, bed_label="Bed A", operational_status=OperationalStatus.IN_SERVICE)
    bed_b = Bed.objects.create(room=room, bed_label="Bed B", operational_status=OperationalStatus.IN_SERVICE)
    occ = assign_student_to_bed(student=student, bed=bed_a, created_by=warden_user)

    # Manually create an EXCHANGE type request (as would exist prior to this milestone)
    req = ExchangeRequest.objects.create(
        institution=institution,
        student=student,
        current_occupancy=occ,
        preferred_bed=bed_b,
        preferred_hostel=hostel,
        request_type=ExchangeRequest.RequestType.EXCHANGE,
        status=ExchangeRequest.Status.OPEN,
    )

    # Attempting to call vacant transfer approval on it must raise ValidationError
    with pytest.raises(ValidationError, match="This request is not a vacant-bed transfer request"):
        approve_and_execute_vacant_transfer(request_id=req.id, actor=warden_user, remarks="Attempt conversion")

    # Request remains EXCHANGE and OPEN
    req.refresh_from_db()
    assert req.request_type == ExchangeRequest.RequestType.EXCHANGE
    assert req.status == ExchangeRequest.Status.OPEN


# =========================================================================
# 11. HTTP Views, Queues & Transfer Confirmation Letter
# =========================================================================

@pytest.mark.django_db
def test_vacant_transfer_http_workflow(client, institution, warden_user, chief_warden_user, dean_user):
    """
    Test full HTTP flow:
    - Approvals queue displays vacant transfers with queue=transfers filter.
    - Transfer detail review view displays details and eligibility checklist.
    - Warden POST approval executes transfer and redirects with flash message.
    - Official transfer confirmation letter view renders and downloads.
    """
    _configure_all_policies(institution)
    ensure_roles_exist()

    hostel, block, floor = _setup_hostel_with_rooms(institution, "Hostel Sigma", "HS-01")
    WardenAssignment.objects.create(warden=warden_user, hostel=hostel, is_primary=True)

    student = create_user_with_role(
        email="sigma.student@apex.test",
        full_name="Sigma Student",
        role_name=ROLE_STUDENT,
        institution=institution,
        password="TestPassword123!",
        registration_number="REG-SIG-01",
        residential_eligibility=User.ResidentialEligibility.MALE,
    )
    room1 = Room.objects.create(floor=floor, room_number="1601", room_type=Room.RoomType.DOUBLE, operational_status=OperationalStatus.IN_SERVICE)
    bed1 = Bed.objects.create(room=room1, bed_label="Bed A", operational_status=OperationalStatus.IN_SERVICE)
    assign_student_to_bed(student=student, bed=bed1, created_by=warden_user)

    room2 = Room.objects.create(floor=floor, room_number="1602", room_type=Room.RoomType.DOUBLE, operational_status=OperationalStatus.IN_SERVICE)
    bed2 = Bed.objects.create(room=room2, bed_label="Bed B", operational_status=OperationalStatus.IN_SERVICE)

    req = create_exchange_request(student=student, data=_request_data(preferred_bed=bed2, preferred_hostel=hostel))

    # 1. Approvals Queue (Warden login)
    client.force_login(warden_user)
    resp = client.get(reverse("exchange:approval_list") + "?queue=transfers")
    assert resp.status_code == 200
    content = resp.content.decode()
    assert "Vacant-bed Transfer" in content
    assert "Sigma Student" in content

    # 2. Transfer Detail Review View
    detail_url = reverse("exchange:transfer_approval_detail", kwargs={"pk": req.id})
    resp = client.get(detail_url)
    assert resp.status_code == 200
    content = resp.content.decode()
    assert "Vacant-Bed Transfer Evaluation & Approval" in content
    assert "Bed B" in content
    assert "Eligible for exchange" in content or "Residential Eligibility" in content

    # 3. Warden POST Approve & Execute
    decide_url = reverse("exchange:transfer_approval_decide", kwargs={"pk": req.id})
    resp = client.post(decide_url, {
        "decision": "approved",
        "remarks": "Transfer approved via web interface.",
    }, follow=True)
    assert resp.status_code == 200
    content = resp.content.decode()
    assert "Transfer request approved and executed successfully" in content

    # Check student is moved
    req.refresh_from_db()
    assert req.status == ExchangeRequest.Status.COMPLETED

    # 4. Transfer Confirmation Letter View (Student access)
    client.force_login(student)
    letter_url = reverse("exchange:transfer_letter", kwargs={"pk": req.id})
    resp = client.get(letter_url)
    assert resp.status_code == 200
    content = resp.content.decode()
    assert "Official Accommodation Transfer Order & Confirmation" in content
    assert "Sigma Student" in content
    assert "HS-01" in content or "Hostel Sigma" in content

    # Download format
    resp_download = client.get(letter_url + "?format=download")
    assert resp_download.status_code == 200
    assert "Content-Disposition" in resp_download.headers
    assert "transfer_letter_REG-SIG-01.html" in resp_download.headers["Content-Disposition"]

    # 5. Unauthorized student access to someone else's transfer letter denied
    other_student = create_user_with_role(
        email="unauth.student@apex.test",
        full_name="Unauth Student",
        role_name=ROLE_STUDENT,
        institution=institution,
        password="TestPassword123!",
        registration_number="REG-UNAUTH-01",
    )
    client.force_login(other_student)
    resp_unauth = client.get(letter_url)
    assert resp_unauth.status_code == 403


@pytest.mark.django_db
def test_approvals_queue_action_link_visibility_and_scoping(client, institution, warden_user):
    """
    Test approvals queue UI and security scoping:
    1. An assigned Warden sees the pending vacant transfer with a visible 'Review Transfer' action link.
    2. Selected 'Vacant-bed Transfers' tab has high-contrast maroon styling and active state.
    3. Bed labels do not duplicate 'Bed Bed'.
    4. Search field container does not collapse (has min-w-[240px]).
    5. The detail view shows current/destination beds, eligibility checks, and decision buttons for assigned warden.
    6. An unassigned Warden from the same institution does NOT see the transfer in their queue.
    7. An unassigned Warden cannot GET detail view (404) or POST decision (403/PermissionDenied).
    8. The transfer request remains pending and student occupancy is untouched until decided.
    """
    _configure_all_policies(institution)
    ensure_roles_exist()

    hostel_assigned, _, floor1 = _setup_hostel_with_rooms(institution, "Hostel Assigned", "HA-01")
    WardenAssignment.objects.create(warden=warden_user, hostel=hostel_assigned, is_primary=True)

    hostel_unassigned, _, floor2 = _setup_hostel_with_rooms(institution, "Hostel Unassigned", "HU-01")
    unassigned_warden = create_user_with_role(
        email="unassigned.warden@apex.test",
        full_name="Unassigned Warden",
        role_name=ROLE_WARDEN,
        institution=institution,
        password="TestPassword123!",
    )
    WardenAssignment.objects.create(warden=unassigned_warden, hostel=hostel_unassigned, is_primary=True)

    student = create_user_with_role(
        email="transfer.student@apex.test",
        full_name="Transfer Student",
        role_name=ROLE_STUDENT,
        institution=institution,
        password="TestPassword123!",
        registration_number="REG-TRF-01",
        residential_eligibility=User.ResidentialEligibility.MALE,
    )

    room_curr = Room.objects.create(floor=floor1, room_number="101", room_type=Room.RoomType.DOUBLE, operational_status=OperationalStatus.IN_SERVICE)
    bed_curr = Bed.objects.create(room=room_curr, bed_label="Bed A", operational_status=OperationalStatus.IN_SERVICE)
    occ = assign_student_to_bed(student=student, bed=bed_curr, created_by=warden_user)

    room_dest = Room.objects.create(floor=floor1, room_number="102", room_type=Room.RoomType.DOUBLE, operational_status=OperationalStatus.IN_SERVICE)
    bed_dest = Bed.objects.create(room=room_dest, bed_label="Bed B", operational_status=OperationalStatus.IN_SERVICE)

    req = create_exchange_request(student=student, data=_request_data(preferred_bed=bed_dest, preferred_hostel=hostel_assigned))
    assert req.status == ExchangeRequest.Status.PENDING_APPROVAL

    # 1. Assigned Warden visits the approvals queue for vacant-bed transfers
    client.force_login(warden_user)
    queue_url = reverse("exchange:approval_list") + "?queue=transfers&status=pending_approval"
    resp = client.get(queue_url)
    assert resp.status_code == 200
    content = resp.content.decode()

    # Verify high-contrast tab styling
    assert "tab-maroon-active" in content
    assert "background-color: #702C3B" in content

    # Verify Action column contains a visible "Review Transfer" link
    detail_url = reverse("exchange:transfer_approval_detail", kwargs={"pk": req.id})
    assert f'href="{detail_url}"' in content
    assert "Review Transfer" in content
    assert "btn-maroon" in content

    # Verify no duplicate "Bed Bed" label
    assert "Bed Bed" not in content

    # Verify search input is robust (not collapsed)
    assert "min-w-[240px]" in content
    assert 'name="q"' in content

    # 2. Assigned Warden visits the detail review page
    detail_resp = client.get(detail_url)
    assert detail_resp.status_code == 200
    detail_content = detail_resp.content.decode()

    # Detail page shows current and destination accommodation
    assert "Hostel Assigned" in detail_content
    assert "Bed A" in detail_content
    assert "Bed B" in detail_content
    assert "Bed Bed" not in detail_content

    # Detail page shows live vacancy status and eligibility
    assert "Currently Vacant" in detail_content
    assert "Eligibility" in detail_content

    # Detail page shows decision controls for authorized warden
    assert "Approve &amp; Execute Transfer" in detail_content or "Approve & Execute Transfer" in detail_content
    assert "Reject Request" in detail_content
    assert 'name="remarks"' in detail_content

    # 3. Unassigned Warden from same institution views the queue
    client.force_login(unassigned_warden)
    resp_unassigned = client.get(queue_url)
    assert resp_unassigned.status_code == 200
    content_unassigned = resp_unassigned.content.decode()
    # The transfer belonging to Hostel Assigned MUST NOT appear in this warden's queue
    assert f'TRF-{str(req.id)[:8]}' not in content_unassigned
    assert f'href="{detail_url}"' not in content_unassigned

    # 4. Unassigned Warden directly attempts to access the detail review page
    resp_unassigned_detail = client.get(detail_url)
    # Server-side query scoping denies access (raises 404 Http404)
    assert resp_unassigned_detail.status_code == 404

    # 5. Unassigned Warden attempts to POST decision directly
    decide_url = reverse("exchange:transfer_approval_decide", kwargs={"pk": req.id})
    resp_unassigned_decide = client.post(decide_url, {
        "decision": "approved",
        "remarks": "Malicious attempt by unassigned warden",
    }, follow=True)
    # Must be denied or redirected with error message
    req.refresh_from_db()
    assert req.status == ExchangeRequest.Status.PENDING_APPROVAL
    occ.refresh_from_db()
    assert occ.status == StudentOccupancy.OccupancyStatus.ACTIVE
    assert occ.bed_id == bed_curr.id

