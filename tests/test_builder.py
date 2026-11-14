import json
import pytest
from django.urls import reverse
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.accounts.services import (
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
    StudentOccupancy,
    OperationalStatus,
)
from apps.hostels.services import save_custom_hostel_configuration
from apps.hostels.selectors import (
    get_hostel_builder_initial_data,
    calculate_configuration_metrics,
    get_hostel_queryset_with_stats,
)


@pytest.mark.django_db
class TestHostelBuilderPermissions:
    """Test role-based access control for custom hostel builder."""

    def test_dean_can_access_builder_get(self, client, dean_user):
        client.force_login(dean_user)
        response = client.get(reverse("hostels:hostel_builder"))
        assert response.status_code == 200
        assert "Custom Hostel Inventory Builder" in response.content.decode()

    def test_hostel_admin_can_access_builder_get(self, client, admin_user):
        client.force_login(admin_user)
        response = client.get(reverse("hostels:hostel_builder"))
        assert response.status_code == 200

    def test_warden_forbidden_from_builder(self, client, warden_user):
        client.force_login(warden_user)
        response = client.get(reverse("hostels:hostel_builder"))
        assert response.status_code == 403

    def test_chief_warden_forbidden_from_builder(self, client, chief_warden_user):
        client.force_login(chief_warden_user)
        response = client.get(reverse("hostels:hostel_builder"))
        assert response.status_code == 403

    def test_student_forbidden_from_builder(self, client, student_user):
        client.force_login(student_user)
        response = client.get(reverse("hostels:hostel_builder"))
        assert response.status_code == 403

    def test_sysadmin_forbidden_from_builder(self, client, sysadmin_user):
        client.force_login(sysadmin_user)
        response = client.get(reverse("hostels:hostel_builder"))
        assert response.status_code == 403

    def test_unauthenticated_redirected(self, client):
        response = client.get(reverse("hostels:hostel_builder"))
        assert response.status_code == 302
        assert reverse("accounts:login") in response.url


@pytest.mark.django_db
class TestConcreteHostelConfiguration:
    """
    Test the concrete required layout:
    Floor 1 with 2 rooms (2 and 5 beds)
    Floor 2 with 3 rooms (1, 3, 6 beds)
    Total: 2 floors, 5 rooms, 17 beds.
    """

    @pytest.fixture
    def concrete_17_bed_payload(self):
        return {
            "name": "Aryabhata Hall",
            "code": "ABH-01",
            "resident_category": "male",
            "status": "active",
            "address": "North Campus Quad",
            "blocks": [
                {
                    "name": "Block 1",
                    "code": "B1",
                    "floors": [
                        {
                            "floor_number": 1,
                            "floor_label": "Ground Floor",
                            "rooms": [
                                {
                                    "room_number": "101",
                                    "room_type": "double",
                                    "target_capacity": 2,
                                    "operational_status": "in_service",
                                    "beds": [
                                        {"bed_label": "B1-101-A", "operational_status": "in_service"},
                                        {"bed_label": "B1-101-B", "operational_status": "in_service"},
                                    ],
                                },
                                {
                                    "room_number": "102",
                                    "room_type": "custom",
                                    "target_capacity": 5,
                                    "operational_status": "in_service",
                                    "beds": [
                                        {"bed_label": f"B1-102-{chr(65+i)}", "operational_status": "in_service"}
                                        for i in range(5)
                                    ],
                                },
                            ],
                        },
                        {
                            "floor_number": 2,
                            "floor_label": "First Floor",
                            "rooms": [
                                {
                                    "room_number": "201",
                                    "room_type": "single",
                                    "target_capacity": 1,
                                    "operational_status": "in_service",
                                    "beds": [
                                        {"bed_label": "B1-201-A", "operational_status": "in_service"},
                                    ],
                                },
                                {
                                    "room_number": "202",
                                    "room_type": "triple",
                                    "target_capacity": 3,
                                    "operational_status": "in_service",
                                    "beds": [
                                        {"bed_label": f"B1-202-{chr(65+i)}", "operational_status": "in_service"}
                                        for i in range(3)
                                    ],
                                },
                                {
                                    "room_number": "203",
                                    "room_type": "six",
                                    "target_capacity": 6,
                                    "operational_status": "in_service",
                                    "beds": [
                                        {"bed_label": f"B1-203-{chr(65+i)}", "operational_status": "in_service"}
                                        for i in range(6)
                                    ],
                                },
                            ],
                        },
                    ],
                }
            ],
        }

    def test_create_concrete_17_bed_hostel_via_service(self, institution, dean_user, concrete_17_bed_payload):
        hostel = save_custom_hostel_configuration(
            data=concrete_17_bed_payload,
            institution=institution,
            user=dean_user,
        )

        assert hostel.id is not None
        assert hostel.name == "Aryabhata Hall"
        assert hostel.code == "ABH-01"

        # Verify hierarchy counts
        blocks = hostel.blocks.filter(is_active=True)
        assert blocks.count() == 1
        block = blocks.first()
        assert block.name == "Block 1"

        floors = block.floors.filter(is_active=True).order_by("floor_number")
        assert floors.count() == 2

        # Floor 1
        f1 = floors[0]
        assert f1.floor_number == 1
        f1_rooms = f1.rooms.filter(is_active=True).order_by("room_number")
        assert f1_rooms.count() == 2
        assert f1_rooms[0].beds.filter(is_active=True).count() == 2
        assert f1_rooms[1].beds.filter(is_active=True).count() == 5

        # Floor 2
        f2 = floors[1]
        assert f2.floor_number == 2
        f2_rooms = f2.rooms.filter(is_active=True).order_by("room_number")
        assert f2_rooms.count() == 3
        assert f2_rooms[0].beds.filter(is_active=True).count() == 1
        assert f2_rooms[1].beds.filter(is_active=True).count() == 3
        assert f2_rooms[2].beds.filter(is_active=True).count() == 6

        # Total counts across hostel
        total_rooms = Room.objects.filter(floor__block__hostel=hostel, is_active=True).count()
        total_beds = Bed.objects.filter(room__floor__block__hostel=hostel, is_active=True).count()
        assert total_rooms == 5
        assert total_beds == 17

        # Verify selector metrics
        annotated_hostels = get_hostel_queryset_with_stats(institution=institution)
        ann = annotated_hostels.get(id=hostel.id)
        assert ann.total_rooms == 5
        assert ann.total_beds == 17
        assert ann.occupied_beds == 0
        assert ann.available_beds == 17
        assert ann.occupancy_rate == 0.0

    def test_create_concrete_hostel_via_http_post(self, client, admin_user, concrete_17_bed_payload):
        client.force_login(admin_user)
        response = client.post(
            reverse("hostels:hostel_builder"),
            data=json.dumps(concrete_17_bed_payload),
            content_type="application/json",
        )
        assert response.status_code == 200
        resp_data = response.json()
        assert resp_data["success"] is True
        assert resp_data["metrics"]["total_rooms"] == 5
        assert resp_data["metrics"]["total_capacity"] == 17
        assert resp_data["metrics"]["available_beds"] == 17


@pytest.mark.django_db
class TestSafeEditingAndProtections:
    """Test safe editing, ID stability, and safety constraints on occupied beds."""

    def test_renaming_preserves_primary_keys_and_occupancies(self, institution, dean_user, warden_user, student_user):
        # 1. Create initial hostel
        initial_payload = {
            "name": "Hostel Alpha",
            "code": "HA-01",
            "resident_category": "male",
            "status": "active",
            "blocks": [
                {
                    "name": "Main Wing",
                    "code": "MW",
                    "floors": [
                        {
                            "floor_number": 1,
                            "floor_label": "Floor 1",
                            "rooms": [
                                {
                                    "room_number": "101",
                                    "room_type": "single",
                                    "beds": [{"bed_label": "Bed 1", "operational_status": "in_service"}],
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        hostel = save_custom_hostel_configuration(data=initial_payload, institution=institution, user=dean_user)
        block = hostel.blocks.first()
        floor = block.floors.first()
        room = floor.rooms.first()
        bed = room.beds.first()

        orig_hostel_id = hostel.id
        orig_block_id = block.id
        orig_floor_id = floor.id
        orig_room_id = room.id
        orig_bed_id = bed.id

        # Allocate bed to student
        StudentOccupancy.objects.create(
            institution=institution,
            student=student_user,
            bed=bed,
            status=StudentOccupancy.OccupancyStatus.ACTIVE,
            start_date=timezone.now().date(),
            created_by=warden_user,
        )

        # 2. Rename entities and submit with preserved IDs
        updated_payload = {
            "name": "Hostel Alpha Renamed",
            "code": "HA-01-R",
            "resident_category": "male",
            "status": "active",
            "initial_updated_at": hostel.updated_at.isoformat(),
            "blocks": [
                {
                    "id": orig_block_id,
                    "name": "Main Wing Renamed",
                    "code": "MWR",
                    "floors": [
                        {
                            "id": orig_floor_id,
                            "floor_number": 1,
                            "floor_label": "Floor 1 Renamed",
                            "rooms": [
                                {
                                    "id": orig_room_id,
                                    "room_number": "101-Renamed",
                                    "room_type": "single",
                                    "beds": [
                                        {
                                            "id": orig_bed_id,
                                            "bed_label": "Bed 1 Renamed",
                                            "operational_status": "in_service",
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ],
        }

        updated_hostel = save_custom_hostel_configuration(
            data=updated_payload,
            institution=institution,
            user=dean_user,
            hostel_id=orig_hostel_id,
        )

        assert updated_hostel.id == orig_hostel_id
        assert updated_hostel.name == "Hostel Alpha Renamed"

        upd_block = updated_hostel.blocks.first()
        assert upd_block.id == orig_block_id
        assert upd_block.name == "Main Wing Renamed"

        upd_floor = upd_block.floors.first()
        assert upd_floor.id == orig_floor_id
        assert upd_floor.floor_label == "Floor 1 Renamed"

        upd_room = upd_floor.rooms.first()
        assert upd_room.id == orig_room_id
        assert upd_room.room_number == "101-Renamed"

        upd_bed = upd_room.beds.first()
        assert upd_bed.id == orig_bed_id
        assert upd_bed.bed_label == "Bed 1 Renamed"

        # Verify occupancy is preserved and active
        active_occ = StudentOccupancy.objects.get(bed=upd_bed, status=StudentOccupancy.OccupancyStatus.ACTIVE)
        assert active_occ.student == student_user

    def test_cannot_deactivate_or_omit_occupied_bed(self, institution, dean_user, warden_user, student_user):
        payload = {
            "name": "Hostel Beta",
            "code": "HB-01",
            "resident_category": "male",
            "status": "active",
            "blocks": [
                {
                    "name": "Block B",
                    "code": "BB",
                    "floors": [
                        {
                            "floor_number": 1,
                            "floor_label": "Floor 1",
                            "rooms": [
                                {
                                    "room_number": "101",
                                    "room_type": "double",
                                    "beds": [
                                        {"bed_label": "Bed 1", "operational_status": "in_service"},
                                        {"bed_label": "Bed 2", "operational_status": "in_service"},
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        hostel = save_custom_hostel_configuration(data=payload, institution=institution, user=dean_user)
        room = hostel.blocks.first().floors.first().rooms.first()
        bed1, bed2 = room.beds.all()

        StudentOccupancy.objects.create(
            institution=institution,
            student=student_user,
            bed=bed1,
            status=StudentOccupancy.OccupancyStatus.ACTIVE,
            start_date=timezone.now().date(),
            created_by=warden_user,
        )

        # Attempt to omit Bed 1 (downsizing room to 1 bed)
        downsize_payload = {
            "name": "Hostel Beta",
            "code": "HB-01",
            "resident_category": "male",
            "status": "active",
            "initial_updated_at": hostel.updated_at.isoformat(),
            "blocks": [
                {
                    "id": hostel.blocks.first().id,
                    "name": "Block B",
                    "code": "BB",
                    "floors": [
                        {
                            "id": hostel.blocks.first().floors.first().id,
                            "floor_number": 1,
                            "floor_label": "Floor 1",
                            "rooms": [
                                {
                                    "id": room.id,
                                    "room_number": "101",
                                    "room_type": "single",
                                    "beds": [
                                        {"id": bed2.id, "bed_label": "Bed 2", "operational_status": "in_service"}
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ],
        }

        with pytest.raises(ValidationError) as excinfo:
            save_custom_hostel_configuration(
                data=downsize_payload,
                institution=institution,
                user=dean_user,
                hostel_id=hostel.id,
            )
        assert "Cannot remove or deactivate Bed" in str(excinfo.value)
        assert bed1.bed_label in str(excinfo.value)

    def test_cannot_change_resident_category_with_active_occupancies(self, institution, dean_user, warden_user, student_user):
        payload = {
            "name": "Hostel Gamma",
            "code": "HG-01",
            "resident_category": "male",
            "status": "active",
            "blocks": [
                {
                    "name": "Block G",
                    "code": "BG",
                    "floors": [
                        {
                            "floor_number": 1,
                            "floor_label": "Floor 1",
                            "rooms": [
                                {
                                    "room_number": "101",
                                    "room_type": "single",
                                    "beds": [{"bed_label": "Bed 1", "operational_status": "in_service"}],
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        hostel = save_custom_hostel_configuration(data=payload, institution=institution, user=dean_user)
        bed = hostel.blocks.first().floors.first().rooms.first().beds.first()

        StudentOccupancy.objects.create(
            institution=institution,
            student=student_user,
            bed=bed,
            status=StudentOccupancy.OccupancyStatus.ACTIVE,
            start_date=timezone.now().date(),
            created_by=warden_user,
        )

        update_category_payload = {
            "name": "Hostel Gamma",
            "code": "HG-01",
            "resident_category": "female",  # Changed to female!
            "status": "active",
            "initial_updated_at": hostel.updated_at.isoformat(),
            "blocks": [
                {
                    "id": hostel.blocks.first().id,
                    "name": "Block G",
                    "code": "BG",
                    "floors": [
                        {
                            "id": hostel.blocks.first().floors.first().id,
                            "floor_number": 1,
                            "floor_label": "Floor 1",
                            "rooms": [
                                {
                                    "id": hostel.blocks.first().floors.first().rooms.first().id,
                                    "room_number": "101",
                                    "room_type": "single",
                                    "beds": [{"id": bed.id, "bed_label": "Bed 1", "operational_status": "in_service"}],
                                }
                            ],
                        }
                    ],
                }
            ],
        }

        with pytest.raises(ValidationError) as excinfo:
            save_custom_hostel_configuration(
                data=update_category_payload,
                institution=institution,
                user=dean_user,
                hostel_id=hostel.id,
            )
        assert "Cannot change hostel resident category" in str(excinfo.value)


@pytest.mark.django_db
class TestAntiTamperingAndConcurrency:
    """Test child ID boundary checks, anti-tampering, and stale edit detection."""

    def test_foreign_child_id_tampering_rejected(self, institution, other_institution, dean_user):
        # Create a hostel in other_institution
        foreign_hostel = Hostel.objects.create(
            institution=other_institution,
            name="Foreign Hostel",
            code="FH-01",
            resident_category=Hostel.ResidentCategory.MALE,
        )
        foreign_block = Block.objects.create(hostel=foreign_hostel, name="Foreign Block", code="FB")

        # Now try to create/update a hostel in `institution` referencing `foreign_block.id`
        tampered_payload = {
            "name": "Local Hostel",
            "code": "LH-01",
            "resident_category": "male",
            "status": "active",
            "blocks": [
                {
                    "id": foreign_block.id,  # Tampered block ID
                    "name": "Tampered Block",
                    "code": "TB",
                    "floors": [
                        {
                            "floor_number": 1,
                            "floor_label": "Floor 1",
                            "rooms": [
                                {
                                    "room_number": "101",
                                    "room_type": "single",
                                    "beds": [{"bed_label": "B1", "operational_status": "in_service"}],
                                }
                            ],
                        }
                    ],
                }
            ],
        }

        with pytest.raises(ValidationError) as excinfo:
            save_custom_hostel_configuration(
                data=tampered_payload,
                institution=institution,
                user=dean_user,
            )
        assert "does not belong to hostel" in str(excinfo.value)

    def test_stale_edit_detection(self, institution, dean_user):
        initial_payload = {
            "name": "Hostel Concurrency",
            "code": "HC-01",
            "resident_category": "male",
            "status": "active",
            "blocks": [
                {
                    "name": "Block 1",
                    "code": "B1",
                    "floors": [
                        {
                            "floor_number": 1,
                            "floor_label": "Floor 1",
                            "rooms": [
                                {
                                    "room_number": "101",
                                    "room_type": "single",
                                    "beds": [{"bed_label": "B1", "operational_status": "in_service"}],
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        hostel = save_custom_hostel_configuration(data=initial_payload, institution=institution, user=dean_user)

        # Modify hostel in DB directly so updated_at changes
        hostel.name = "Hostel Concurrency Changed Concurrently"
        hostel.save()

        # Submit with old timestamp
        stale_payload = {
            "name": "Hostel Stale Submit",
            "code": "HC-01",
            "resident_category": "male",
            "status": "active",
            "initial_updated_at": "2020-01-01T00:00:00+00:00",  # Definitely stale
            "blocks": [
                {
                    "id": hostel.blocks.first().id,
                    "name": "Block 1",
                    "code": "B1",
                    "floors": [
                        {
                            "id": hostel.blocks.first().floors.first().id,
                            "floor_number": 1,
                            "floor_label": "Floor 1",
                            "rooms": [
                                {
                                    "id": hostel.blocks.first().floors.first().rooms.first().id,
                                    "room_number": "101",
                                    "room_type": "single",
                                    "beds": [{"id": hostel.blocks.first().floors.first().rooms.first().beds.first().id, "bed_label": "B1"}],
                                }
                            ],
                        }
                    ],
                }
            ],
        }

        with pytest.raises(ValidationError) as excinfo:
            save_custom_hostel_configuration(
                data=stale_payload,
                institution=institution,
                user=dean_user,
                hostel_id=hostel.id,
            )
        assert "Stale edit detected" in str(excinfo.value)

    def test_atomic_transaction_rollback_on_failure(self, institution, dean_user):
        initial_hostel_count = Hostel.objects.filter(institution=institution).count()
        initial_room_count = Room.objects.count()

        invalid_payload = {
            "name": "Atomic Rollback Test",
            "code": "ART-01",
            "resident_category": "male",
            "status": "active",
            "blocks": [
                {
                    "name": "Block A",
                    "code": "BA",
                    "floors": [
                        {
                            "floor_number": 1,
                            "floor_label": "Floor 1",
                            "rooms": [
                                {
                                    # Missing room_number -> will trigger validation failure
                                    "room_number": "",
                                    "room_type": "single",
                                    "beds": [{"bed_label": "Bed 1", "operational_status": "in_service"}],
                                }
                            ],
                        }
                    ],
                }
            ],
        }

        with pytest.raises(ValidationError):
            save_custom_hostel_configuration(
                data=invalid_payload,
                institution=institution,
                user=dean_user,
            )

        # Ensure no partial hostel, block, floor, room, or bed was created
        assert Hostel.objects.filter(institution=institution).count() == initial_hostel_count
        assert Room.objects.count() == initial_room_count


@pytest.mark.django_db
class TestCapacityCalculations:
    """Test capacity, maintenance, and availability calculation accuracy."""

    def test_capacity_and_maintenance_metrics(self, institution, dean_user):
        payload = {
            "name": "Metrics Hostel",
            "code": "MH-01",
            "resident_category": "male",
            "status": "active",
            "blocks": [
                {
                    "name": "Wing A",
                    "code": "WA",
                    "floors": [
                        {
                            "floor_number": 1,
                            "floor_label": "Floor 1",
                            "rooms": [
                                {
                                    "room_number": "101",
                                    "room_type": "double",
                                    "beds": [
                                        {"bed_label": "Bed A", "operational_status": "in_service"},
                                        {"bed_label": "Bed B", "operational_status": "maintenance"},
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        hostel = save_custom_hostel_configuration(data=payload, institution=institution, user=dean_user)

        # Check preview calculation function
        metrics = calculate_configuration_metrics(payload)
        assert metrics["total_rooms"] == 1
        assert metrics["total_capacity"] == 2
        assert metrics["maintenance_beds"] == 1
        assert metrics["available_beds"] == 1

        # Check persisted subquery metrics
        annotated = get_hostel_queryset_with_stats(institution=institution).get(id=hostel.id)
        assert annotated.total_rooms == 1
        assert annotated.total_beds == 2
        assert annotated.occupied_beds == 0
        assert annotated.maintenance_beds == 1
        assert annotated.available_beds == 1
