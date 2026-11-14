"""
Centralized Role and Permission Authorization Rules for RoomLoop.

Defines business authority boundaries for the six system roles:
1. Student
2. Warden
3. Chief Warden
4. Hostel Administrator
5. Dean of Student Welfare
6. System Administrator

Key Rules:
- Infrastructure Mutation: Dean and Hostel Administrator ONLY.
  Chief Warden retains oversight, metrics, and escalation responsibility WITHOUT infrastructure mutation.
- Operational Bed Allocation & Release: Destination Warden (routine) and Chief Warden (escalations).
  Hostel Administrator is DENIED routine allocations; emergency relocation is deferred to a dedicated workflow.
  Dean of Student Welfare and System Administrator are STRICTLY FORBIDDEN from allocating or releasing beds.
  Generic is_superuser or is_staff flags DO NOT grant business allocation or infrastructure authority.
"""

import logging
from apps.accounts.models import User
from apps.hostels.models import Hostel, Bed, StudentOccupancy, WardenAssignment

logger = logging.getLogger(__name__)


def can_manage_infrastructure(user: User) -> bool:
    """
    Check if user is authorized to create, configure, or edit hostel infrastructure.
    Authorized: Dean of Student Welfare, Hostel Administrator.
    Forbidden: Chief Warden (oversight/escalation only, no infrastructure mutation),
               Student, Warden (routine only), System Administrator (technical only).
    Note: Neither is_superuser nor is_staff grants infrastructure mutation authority.
    """
    if not user.is_authenticated:
        return False

    # Block accounts whose business role explicitly prohibits infrastructure mutation
    if user.is_sysadmin_role or user.is_student_role or user.is_warden_role or user.is_chief_warden_role:
        return False

    return user.is_dean_role or user.is_hostel_admin_role


def can_edit_hostel(user: User, hostel: Hostel) -> bool:
    """
    Check if user can edit a specific hostel.
    Authorized: Dean of Student Welfare, Hostel Administrator.
    Forbidden: Chief Warden, Warden, Student, System Administrator.
    Must belong to the same institution.
    """
    if not can_manage_infrastructure(user):
        return False
    if user.institution and hostel.institution_id != user.institution.id:
        return False
    return True


def can_manage_rooms(user: User, hostel: Hostel | None = None) -> bool:
    """
    Check if user can add or edit rooms in a hostel.
    Authorized: Dean of Student Welfare, Hostel Administrator.
    Forbidden: Chief Warden, Warden, Student, System Administrator.
    """
    if not can_manage_infrastructure(user):
        return False
    if hostel and user.institution and hostel.institution_id != user.institution.id:
        return False
    return True


def can_assign_occupancy(user: User, bed: Bed | None = None) -> bool:
    """
    Check if user can allocate a student to a bed in the routine allocation workflow.
    
    Authority Boundaries:
    - Warden: Authorized ONLY for beds in their assigned hostels (routine allocation).
    - Chief Warden: Authorized for institutional escalations and exceptions.
    - Hostel Administrator: STRICTLY DENIED routine allocations. Emergency relocation
      with mandatory audit & justification is deferred. Generic endpoints cannot be used.
    - Dean of Student Welfare: STRICTLY FORBIDDEN (Executive policy & infrastructure only).
      Cannot gain allocation authority through is_superuser or is_staff.
    - System Administrator: STRICTLY FORBIDDEN (Technical administration only).
      Cannot gain allocation authority through is_superuser or is_staff.
    - Student: STRICTLY FORBIDDEN.
    """
    if not user.is_authenticated:
        return False

    # Explicitly forbidden roles
    if (
        user.is_dean_role
        or user.is_sysadmin_role
        or user.is_student_role
        or user.is_hostel_admin_role
    ):
        return False

    # Allocation authority strictly requires operational staff role (Warden or Chief Warden).
    # Generic superusers or staff without operational role cannot allocate beds.
    if not (user.is_warden_role or user.is_chief_warden_role):
        return False

    # Chief Warden has institutional escalation authority
    if user.is_chief_warden_role:
        if bed and user.institution:
            return bed.room.floor.block.hostel.institution_id == user.institution.id
        return True

    # Warden has routine allocation authority ONLY within assigned hostels
    if user.is_warden_role:
        if bed is not None:
            hostel = bed.room.floor.block.hostel
            return WardenAssignment.objects.filter(warden=user, hostel=hostel).exists()
        # General permission check: warden must have at least one assigned hostel
        return WardenAssignment.objects.filter(warden=user).exists()

    return False


def can_release_occupancy(user: User, occupancy: StudentOccupancy | None = None) -> bool:
    """
    Check if user can release an active student occupancy in routine workflow.
    
    Authority Boundaries:
    - Warden: Authorized ONLY within assigned hostels.
    - Chief Warden: Authorized for institutional escalations/exceptions.
    - Hostel Administrator: STRICTLY DENIED routine releases. Emergency relocation deferred.
    - Dean of Student Welfare: STRICTLY FORBIDDEN. Cannot gain authority through superuser/staff flags.
    - System Administrator: STRICTLY FORBIDDEN. Cannot gain authority through superuser/staff flags.
    - Student: STRICTLY FORBIDDEN.
    """
    if not user.is_authenticated:
        return False

    if (
        user.is_dean_role
        or user.is_sysadmin_role
        or user.is_student_role
        or user.is_hostel_admin_role
    ):
        return False

    if not (user.is_warden_role or user.is_chief_warden_role):
        return False

    if user.is_chief_warden_role:
        if occupancy and user.institution:
            return occupancy.institution_id == user.institution.id
        return True

    if user.is_warden_role:
        if occupancy is not None:
            hostel = occupancy.bed.room.floor.block.hostel
            return WardenAssignment.objects.filter(warden=user, hostel=hostel).exists()
        return WardenAssignment.objects.filter(warden=user).exists()

    return False


def can_update_bed_status(user: User, bed: Bed | None = None) -> bool:
    """
    Check if user can change bed operational status (e.g. Under Maintenance, In Service).
    Authorized: Dean, Hostel Administrator, assigned Warden.
    Forbidden: Chief Warden (no infrastructure mutation), Student, System Administrator.
    """
    if not user.is_authenticated:
        return False

    if user.is_student_role or user.is_sysadmin_role or user.is_chief_warden_role:
        return False

    if user.is_dean_role or user.is_hostel_admin_role:
        if bed and user.institution:
            return bed.room.floor.block.hostel.institution_id == user.institution.id
        return True

    if user.is_warden_role:
        if bed is not None:
            hostel = bed.room.floor.block.hostel
            return WardenAssignment.objects.filter(warden=user, hostel=hostel).exists()
        return WardenAssignment.objects.filter(warden=user).exists()

    return False


def can_view_occupancy_roster(user: User) -> bool:
    """
    Check if user can view the occupancy roster.
    Authorized: Staff roles (Warden, Chief Warden, Hostel Admin, Dean, Sysadmin for audit).
    Forbidden: Student (students can only see their own occupancy).
    """
    if not user.is_authenticated:
        return False
    return not user.is_student_role
