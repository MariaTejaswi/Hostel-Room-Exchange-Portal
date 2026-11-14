import pytest
from io import StringIO
from django.core.management import call_command
from django.core.management.base import CommandError
from django.core.exceptions import ValidationError, PermissionDenied
from django.db import IntegrityError
from django.test import Client
from apps.accounts.models import User
from apps.accounts.services import create_user_with_role, ROLE_STUDENT
from apps.hostels.models import Hostel, Room, Bed, Block, Floor, OperationalStatus, StudentOccupancy
from apps.hostels.forms import HostelForm, OccupancyAssignForm
from apps.hostels.services import create_hostel, create_room, update_bed_status, assign_student_to_bed
from apps.hostels.selectors import get_student_verified_eligibility


@pytest.mark.django_db
def test_hostel_creation_succeeds(institution, admin_user):
    hostel = create_hostel(
        institution=institution,
        name="East Residence",
        code="ER-01",
        resident_category=Hostel.ResidentCategory.MALE,
        acting_user=admin_user,
    )
    assert hostel.id is not None
    assert hostel.code == "ER-01"
    assert hostel.resident_category == Hostel.ResidentCategory.MALE


@pytest.mark.django_db
def test_invalid_categories_beyond_coed_rejected_in_service(institution, admin_user):
    """Verify that any category other than 'male' or 'female' is strictly rejected."""
    for invalid_cat in ["coed", "postgraduate", "unknown", "other", "", "nonbinary"]:
        with pytest.raises(ValidationError) as exc_info:
            create_hostel(
                institution=institution,
                name=f"Hostel {invalid_cat}",
                code=f"INV-{invalid_cat[:4]}",
                resident_category=invalid_cat,
                acting_user=admin_user,
            )
        err = str(exc_info.value)
        assert ("Co-ed hostels are not supported" in err) or ("Invalid resident category" in err)


@pytest.mark.django_db
def test_coed_and_invalid_categories_rejected_in_form():
    """Verify that HostelForm rejects 'coed', 'postgraduate', and unsupported categories."""
    for invalid_cat in ["coed", "postgraduate", "other", "unknown"]:
        form = HostelForm(
            data={
                "name": "Test Hostel",
                "code": "TH-01",
                "resident_category": invalid_cat,
                "status": "active",
            },
        )
        assert not form.is_valid()
        assert "resident_category" in form.errors


@pytest.mark.django_db
def test_coed_category_rejected_by_db_constraint(institution):
    """Verify that the PostgreSQL check constraint prevents resident_category='coed'."""
    hostel = Hostel(
        institution=institution,
        name="Coed Direct",
        code="CD-99",
        resident_category="coed",
    )
    with pytest.raises(IntegrityError):
        hostel.save()


@pytest.mark.django_db
def test_academic_level_preserved_independently(institution, admin_user):
    """Verify academic_level is stored on Hostel as an independent attribute."""
    hostel = create_hostel(
        institution=institution,
        name="Grad Hall",
        code="GH-GRAD",
        resident_category=Hostel.ResidentCategory.MALE,
        acting_user=admin_user,
    )
    hostel.academic_level = Hostel.AcademicLevel.POSTGRADUATE
    hostel.save(update_fields=["academic_level"])
    hostel.refresh_from_db()
    assert hostel.academic_level == Hostel.AcademicLevel.POSTGRADUATE
    assert hostel.resident_category == Hostel.ResidentCategory.MALE


@pytest.mark.django_db
def test_duplicate_hostel_code_rejected(institution, admin_user):
    create_hostel(
        institution=institution,
        name="Hostel One",
        code="H-01",
        acting_user=admin_user,
    )
    with pytest.raises(ValidationError):
        create_hostel(
            institution=institution,
            name="Hostel Two",
            code="H-01",
            acting_user=admin_user,
        )


@pytest.mark.django_db
def test_duplicate_bed_label_rejected(hostel_setup):
    room = hostel_setup["room"]
    with pytest.raises(IntegrityError):
        Bed.objects.create(room=room, bed_label="Bed A")


@pytest.mark.django_db
def test_room_creation_generates_correct_beds(hostel_setup, admin_user):
    floor = hostel_setup["floor"]
    room = create_room(
        floor=floor,
        room_number="102",
        room_type=Room.RoomType.TRIPLE,
        create_beds=True,
        acting_user=admin_user,
    )
    assert room.beds.count() == 3
    assert set(room.beds.values_list("bed_label", flat=True)) == {"Bed A", "Bed B", "Bed C"}


@pytest.mark.django_db
def test_seed_hostel_inventory_command_dry_run(institution):
    """Verify that seed_hostel_inventory --dry-run accurately specifies the 17-hostel collection."""
    out = StringIO()
    call_command("seed_hostel_inventory", dry_run=True, institution_code=institution.code, stdout=out)
    output = out.getvalue()
    assert "DRY RUN SIMULATION" in output
    assert "17 created" in output
    assert "170 created" in output
    assert "3400 created" in output
    assert "13600 created" in output
    assert "8000 beds" in output
    assert "5600 beds" in output
    assert "Zero database modifications were written" in output


@pytest.mark.django_db
def test_seed_hostel_inventory_idempotent(institution):
    """Verify running seed_hostel_inventory creates the 17 hostels and subsequent runs create 0 duplicates."""
    out = StringIO()
    call_command("seed_hostel_inventory", institution_code=institution.code, stdout=out)
    output = out.getvalue()
    assert "17 created" in output or "17 existing" in output

    # Re-run to ensure strict idempotency
    out_rerun = StringIO()
    call_command("seed_hostel_inventory", institution_code=institution.code, stdout=out_rerun)
    rerun_output = out_rerun.getvalue()
    assert "0 created, 17 existing" in rerun_output
    assert "0 created, 170 existing" in rerun_output
    assert "0 created, 3400 existing" in rerun_output
    assert "0 created, 13600 existing" in rerun_output


@pytest.mark.django_db
def test_seeding_invalid_institution_code_raises_command_error():
    """Verify seeder raises CommandError on invalid institution code and never silently falls back."""
    with pytest.raises(CommandError) as exc_info:
        call_command("seed_hostel_inventory", institution_code="INVALID_NON_EXISTENT_CODE")
    assert "not found" in str(exc_info.value)


@pytest.mark.django_db
def test_seeding_rerun_preserves_dean_customizations(institution):
    """Verify seeder rerun preserves custom bed statuses, capacities, and deactivated beds."""
    call_command("seed_hostel_inventory", institution_code=institution.code)

    # Dean customizes BH-01: set Bed A in room 101 to MAINTENANCE, and deactivate Bed D in room 101
    bh_01 = Hostel.objects.get(code="BH-01", institution=institution)
    room_101 = Room.objects.get(floor__block__hostel=bh_01, room_number="101")
    bed_a = room_101.beds.get(bed_label="Bed A")
    bed_a.operational_status = OperationalStatus.MAINTENANCE
    bed_a.save(update_fields=["operational_status"])

    bed_d = room_101.beds.get(bed_label="Bed D")
    bed_d.is_active = False
    bed_d.save(update_fields=["is_active"])

    # Re-run seeder
    out = StringIO()
    call_command("seed_hostel_inventory", institution_code=institution.code, stdout=out)

    # Verify Dean's changes were preserved intact
    bed_a.refresh_from_db()
    bed_d.refresh_from_db()
    assert bed_a.operational_status == OperationalStatus.MAINTENANCE
    assert not bed_d.is_active


@pytest.mark.django_db
def test_student_eligibility_enforced_server_side_on_hostel_list(client, institution, hostel_setup):
    """Verify students only see hostels matching verified eligibility, and overrides are rejected."""
    # Create female hostel in same institution
    female_hostel = Hostel.objects.create(
        institution=institution,
        name="South Women Residence",
        code="SWR-01",
        resident_category=Hostel.ResidentCategory.FEMALE,
    )

    # 1. Male verified student
    male_student = create_user_with_role(
        email="male.student@apex.test",
        full_name="Male Student",
        role_name=ROLE_STUDENT,
        institution=institution,
        password="TestPassword123!",
        registration_number="REG-M-001",
        residential_eligibility=User.ResidentialEligibility.MALE,
    )
    client.force_login(male_student)

    # Regular request
    resp = client.get("/hostels/")
    assert resp.status_code == 200
    content = resp.content.decode()
    assert hostel_setup["hostel"].name in content
    assert female_hostel.name not in content  # Blocked from seeing female hostels

    # Tampered query-string parameter (?category=female) must be overridden server-side
    resp_tampered = client.get("/hostels/?category=female")
    assert resp_tampered.status_code == 200
    content_tampered = resp_tampered.content.decode()
    assert female_hostel.name not in content_tampered
    assert hostel_setup["hostel"].name in content_tampered

    # 2. Unverified student
    unverified_student = create_user_with_role(
        email="unverified.student@apex.test",
        full_name="Unverified Student",
        role_name=ROLE_STUDENT,
        institution=institution,
        password="TestPassword123!",
        registration_number="REG-U-001",
        residential_eligibility=User.ResidentialEligibility.UNVERIFIED,
    )
    client.force_login(unverified_student)
    resp_unv = client.get("/hostels/")
    assert resp_unv.status_code == 200
    assert "Residential Eligibility Pending Institutional Verification" in resp_unv.content.decode()


@pytest.mark.django_db
def test_student_eligibility_enforced_on_hostel_detail_direct_url(client, institution, hostel_setup):
    """Verify direct URL access to ineligible hostel is blocked with HTTP 403 Forbidden."""
    male_hostel = hostel_setup["hostel"]
    female_hostel = Hostel.objects.create(
        institution=institution,
        name="South Women Residence",
        code="SWR-02",
        resident_category=Hostel.ResidentCategory.FEMALE,
    )

    # Verified male student accessing female hostel directly
    male_student = create_user_with_role(
        email="male2.student@apex.test",
        full_name="Male Student 2",
        role_name=ROLE_STUDENT,
        institution=institution,
        password="TestPassword123!",
        registration_number="REG-M-002",
        residential_eligibility=User.ResidentialEligibility.MALE,
    )
    client.force_login(male_student)

    resp_forbidden = client.get(f"/hostels/{female_hostel.id}/")
    assert resp_forbidden.status_code == 403

    resp_allowed = client.get(f"/hostels/{male_hostel.id}/")
    assert resp_allowed.status_code == 200
    assert "selection-preview-bar" in resp_allowed.content.decode()

    # Unverified student accessing male hostel directly: viewable, but selection locked
    unverified_student = create_user_with_role(
        email="unv2.student@apex.test",
        full_name="Unverified Student 2",
        role_name=ROLE_STUDENT,
        institution=institution,
        password="TestPassword123!",
        registration_number="REG-U-002",
        residential_eligibility=User.ResidentialEligibility.UNVERIFIED,
    )
    client.force_login(unverified_student)
    resp_unv = client.get(f"/hostels/{male_hostel.id}/")
    assert resp_unv.status_code == 200
    content = resp_unv.content.decode()
    assert "Restricted Inventory Selection" in content
    assert "Locked" in content


@pytest.mark.django_db
def test_student_blocked_from_ineligible_room_and_bed_rosters(client, institution, hostel_setup):
    """Verify students cannot access opposite gender rooms or administrative bed rosters."""
    female_hostel = Hostel.objects.create(
        institution=institution,
        name="Girls Hall",
        code="GH-ROOM",
        resident_category=Hostel.ResidentCategory.FEMALE,
    )
    blk = Block.objects.create(hostel=female_hostel, name="Block A", code="BA")
    flr = Floor.objects.create(block=blk, floor_number=1, floor_label="1st Floor")
    female_room = Room.objects.create(floor=flr, room_number="101", room_type=Room.RoomType.DOUBLE)

    male_student = create_user_with_role(
        email="male3.student@apex.test",
        full_name="Male Student 3",
        role_name=ROLE_STUDENT,
        institution=institution,
        password="TestPassword123!",
        registration_number="REG-M-003",
        residential_eligibility=User.ResidentialEligibility.MALE,
    )
    client.force_login(male_student)

    # Accessing room in female hostel
    resp_room = client.get(f"/rooms/{female_room.id}/")
    assert resp_room.status_code == 403

    # Accessing administrative bed roster
    resp_beds = client.get("/beds/")
    assert resp_beds.status_code == 403


@pytest.mark.django_db
def test_allocation_service_enforces_authoritative_eligibility(institution, admin_user, hostel_setup):
    """Verify assign_student_to_bed prevents allocations for unverified or mismatched students."""
    male_bed = hostel_setup["bed_b"]

    # 1. Unverified student
    unv_student = create_user_with_role(
        email="unv3.student@apex.test",
        full_name="Unverified Student 3",
        role_name=ROLE_STUDENT,
        institution=institution,
        password="TestPassword123!",
        registration_number="REG-U-003",
        residential_eligibility=User.ResidentialEligibility.UNVERIFIED,
    )
    with pytest.raises(ValidationError) as exc:
        assign_student_to_bed(student=unv_student, bed=male_bed)
    assert "Residential eligibility is unverified" in str(exc.value)

    # 2. Female verified student attempted on Male bed
    female_student = create_user_with_role(
        email="female1.student@apex.test",
        full_name="Female Student 1",
        role_name=ROLE_STUDENT,
        institution=institution,
        password="TestPassword123!",
        registration_number="REG-F-001",
        residential_eligibility=User.ResidentialEligibility.FEMALE,
    )
    with pytest.raises(ValidationError) as exc:
        assign_student_to_bed(student=female_student, bed=male_bed)
    assert "does not match hostel category" in str(exc.value)

    # 3. Male verified student on Male bed succeeds
    male_student = create_user_with_role(
        email="male4.student@apex.test",
        full_name="Male Student 4",
        role_name=ROLE_STUDENT,
        institution=institution,
        password="TestPassword123!",
        registration_number="REG-M-004",
        residential_eligibility=User.ResidentialEligibility.MALE,
    )
    occ = assign_student_to_bed(student=male_student, bed=male_bed)
    assert occ.id is not None
    assert occ.student == male_student
    assert occ.bed == male_bed
