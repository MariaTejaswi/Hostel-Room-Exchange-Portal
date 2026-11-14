import os
from django.core.management.base import BaseCommand
from django.db import transaction
from django.conf import settings
from apps.core.models import Institution, AuditLog
from apps.accounts.models import User
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
from apps.hostels.services import (
    assign_student_to_bed,
    assign_warden_to_hostel,
)


class Command(BaseCommand):
    help = "Idempotently seed demonstration data for RoomLoop Phase 1."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force-production",
            action="store_true",
            help="Explicitly permit running in a non-DEBUG production environment.",
        )

    def handle(self, *args, **options):
        if not settings.DEBUG and not options.get("force_production"):
            self.stderr.write(
                self.style.ERROR(
                    "Safety check: seed_demo cannot run in production (DEBUG=False) without --force-production."
                )
            )
            return

        self.stdout.write(self.style.MIGRATE_HEADING("Seeding RoomLoop Phase 1 demonstration data..."))

        with transaction.atomic():
            ensure_roles_exist()

            # 1. Institution
            institution, _ = Institution.objects.get_or_create(
                code="DEMO-UNIV",
                defaults={
                    "name": "Apex University",
                    "address": "100 Academic Way, Tech Corridor",
                    "city": "Bengaluru",
                    "state": "Karnataka",
                    "contact_email": "accommodation@apexuniv.edu",
                    "is_active": True,
                },
            )
            self.stdout.write(f"✓ Institution: {institution.name} ({institution.code})")

            default_pwd = os.getenv("DEMO_DEFAULT_PASSWORD", "RoomLoop2026!")

            # 2. Staff Accounts
            admin_user = self._get_or_create_user(
                email=os.getenv("DEMO_SUPERUSER_EMAIL", "admin@roomloop.edu"),
                full_name="Chief System Administrator",
                role_name=ROLE_SYSADMIN,
                institution=institution,
                password=default_pwd,
                is_staff=True,
                is_superuser=True,
            )

            hostel_admin = self._get_or_create_user(
                email="hostel.admin@roomloop.edu",
                full_name="Suresh Verma (Hostel Director)",
                role_name=ROLE_HOSTEL_ADMIN,
                institution=institution,
                password=default_pwd,
                is_staff=True,
            )

            chief_warden = self._get_or_create_user(
                email="chief.warden@roomloop.edu",
                full_name="Prof. Ramanathan K.",
                role_name=ROLE_CHIEF_WARDEN,
                institution=institution,
                password=default_pwd,
            )

            warden_north = self._get_or_create_user(
                email="warden.north@roomloop.edu",
                full_name="Dr. Alok Sharma (North Warden)",
                role_name=ROLE_WARDEN,
                institution=institution,
                password=default_pwd,
            )

            warden_south = self._get_or_create_user(
                email="warden.south@roomloop.edu",
                full_name="Dr. Meenakshi Sundaram (South Warden)",
                role_name=ROLE_WARDEN,
                institution=institution,
                password=default_pwd,
            )

            warden_scholars = self._get_or_create_user(
                email="warden.scholars@roomloop.edu",
                full_name="Prof. David Miller (Scholars Hall Warden)",
                role_name=ROLE_WARDEN,
                institution=institution,
                password=default_pwd,
            )

            dean_user = self._get_or_create_user(
                email="dean@roomloop.edu",
                full_name="Prof. Aruna Roy (Dean of Student Welfare)",
                role_name=ROLE_DEAN,
                institution=institution,
                password=default_pwd,
            )

            self.stdout.write("✓ Created / verified staff and administrative accounts.")

            # 3. Hostels Setup
            # Hostel 1: North Residence
            h_north, _ = Hostel.objects.get_or_create(
                institution=institution,
                code="NR-01",
                defaults={
                    "name": "North Residence",
                    "resident_category": Hostel.ResidentCategory.MALE,
                    "address": "North Campus Quadrangle, Near Engineering Gate",
                    "description": "Primary undergraduate men's residential hall with sports and study lounges.",
                    "status": Hostel.Status.ACTIVE,
                },
            )
            assign_warden_to_hostel(warden=warden_north, hostel=h_north, is_primary=True)

            # Hostel 2: South Residence
            h_south, _ = Hostel.objects.get_or_create(
                institution=institution,
                code="SR-01",
                defaults={
                    "name": "South Residence",
                    "resident_category": Hostel.ResidentCategory.FEMALE,
                    "address": "South Lake View Campus, Near Health Center",
                    "description": "Undergraduate women's residential hall with courtyard gardens.",
                    "status": Hostel.Status.ACTIVE,
                },
            )
            assign_warden_to_hostel(warden=warden_south, hostel=h_south, is_primary=True)

            # Hostel 3: Scholars Hall
            h_scholars, _ = Hostel.objects.get_or_create(
                institution=institution,
                code="SH-01",
                defaults={
                    "name": "Scholars Hall",
                    "resident_category": Hostel.ResidentCategory.POSTGRADUATE,
                    "academic_level": Hostel.AcademicLevel.POSTGRADUATE,
                    "address": "Research Park Enclave, East Campus",
                    "description": "Postgraduate and doctoral residential apartments with quiet study areas.",
                    "status": Hostel.Status.ACTIVE,
                },
            )
            assign_warden_to_hostel(warden=warden_scholars, hostel=h_scholars, is_primary=True)

            self.stdout.write("✓ Configured hostels (North Residence, South Residence, Scholars Hall).")

            # 4. Inventory Structure (Blocks, Floors, Rooms, Beds)
            # North Residence Blocks
            blk_na, _ = Block.objects.get_or_create(hostel=h_north, code="BLK-NA", defaults={"name": "Block A (Sunrise Wing)"})
            blk_nb, _ = Block.objects.get_or_create(hostel=h_north, code="BLK-NB", defaults={"name": "Block B (Pioneer Wing)"})

            # South Residence Blocks
            blk_sc, _ = Block.objects.get_or_create(hostel=h_south, code="BLK-SC", defaults={"name": "Block C (Garden Wing)"})

            # Scholars Hall Block
            blk_sp, _ = Block.objects.get_or_create(hostel=h_scholars, code="BLK-SP", defaults={"name": "Block P (Scholars Suites)"})

            # Floors
            flr_na_0, _ = Floor.objects.get_or_create(block=blk_na, floor_number=0, defaults={"floor_label": "Ground Floor"})
            flr_na_1, _ = Floor.objects.get_or_create(block=blk_na, floor_number=1, defaults={"floor_label": "1st Floor"})
            flr_nb_1, _ = Floor.objects.get_or_create(block=blk_nb, floor_number=1, defaults={"floor_label": "1st Floor"})
            flr_sc_1, _ = Floor.objects.get_or_create(block=blk_sc, floor_number=1, defaults={"floor_label": "1st Floor"})
            flr_sp_1, _ = Floor.objects.get_or_create(block=blk_sp, floor_number=1, defaults={"floor_label": "1st Floor"})

            # Rooms
            # Room 101 in North Block A (Double)
            r_na_101 = self._get_or_create_room(flr_na_1, "101", Room.RoomType.DOUBLE)
            # Room 102 in North Block A (Triple)
            r_na_102 = self._get_or_create_room(flr_na_1, "102", Room.RoomType.TRIPLE)
            # Room 103 in North Block A (Single)
            r_na_103 = self._get_or_create_room(flr_na_1, "103", Room.RoomType.SINGLE)
            # Room 201 in North Block B (Double)
            r_nb_201 = self._get_or_create_room(flr_nb_1, "201", Room.RoomType.DOUBLE)
            # Room 301 in South Block C (Double)
            r_sc_301 = self._get_or_create_room(flr_sc_1, "301", Room.RoomType.DOUBLE)
            # Room 302 in South Block C (Four Sharing)
            r_sc_302 = self._get_or_create_room(flr_sc_1, "302", Room.RoomType.FOUR)
            # Room 401 in Scholars Block P (Single)
            r_sp_401 = self._get_or_create_room(flr_sp_1, "401", Room.RoomType.SINGLE)

            self.stdout.write("✓ Configured blocks, floors, rooms, and bed slots.")

            # Set one bed in Room 201 under Maintenance
            bed_201_b = r_nb_201.beds.filter(bed_label="Bed B").first()
            if bed_201_b and not bed_201_b.is_occupied:
                bed_201_b.operational_status = OperationalStatus.MAINTENANCE
                bed_201_b.save(update_fields=["operational_status"])

            # 5. Student Accounts & Allocations
            students_data = [
                ("arun.kumar@student.roomloop.edu", "Arun Kumar", "2024-NR-001", r_na_101, "Bed A", User.ResidentialEligibility.MALE),
                ("vikram.aditya@student.roomloop.edu", "Vikram Aditya", "2024-NR-002", r_na_101, "Bed B", User.ResidentialEligibility.MALE),
                ("karthik.raja@student.roomloop.edu", "Karthik Raja", "2024-NR-003", r_na_102, "Bed A", User.ResidentialEligibility.MALE),
                ("priya.sharma@student.roomloop.edu", "Priya Sharma", "2024-SR-101", r_sc_301, "Bed A", User.ResidentialEligibility.FEMALE),
                ("ananya.reddy@student.roomloop.edu", "Ananya Reddy", "2024-SR-102", r_sc_301, "Bed B", User.ResidentialEligibility.FEMALE),
                ("deepika.padman@student.roomloop.edu", "Deepika Padman", "2024-SR-103", r_sc_302, "Bed A", User.ResidentialEligibility.FEMALE),
                ("siddharth.roy@student.roomloop.edu", "Siddharth Roy (PhD)", "2024-SH-501", r_sp_401, "Bed A", User.ResidentialEligibility.MALE),
            ]

            # Map hostel to its assigned destination warden
            warden_map = {
                h_north.id: warden_north,
                h_south.id: warden_south,
                h_scholars.id: warden_scholars,
            }

            for email, name, reg_no, room, bed_label, elig in students_data:
                student = self._get_or_create_user(
                    email=email,
                    full_name=name,
                    role_name=ROLE_STUDENT,
                    institution=institution,
                    password=default_pwd,
                    registration_number=reg_no,
                    residential_eligibility=elig,
                )
                bed = room.beds.filter(bed_label=bed_label).first()
                if bed and not StudentOccupancy.objects.filter(student=student, status="active").exists():
                    if not StudentOccupancy.objects.filter(bed=bed, status="active").exists():
                        assigned_warden = warden_map.get(bed.room.floor.block.hostel_id, warden_north)
                        assign_student_to_bed(
                            student=student,
                            bed=bed,
                            source=StudentOccupancy.AllocationSource.SEED,
                            created_by=assigned_warden,
                        )

            # Create unallocated students for live assignment testing
            self._get_or_create_user(
                email="rohit.sharma@student.roomloop.edu",
                full_name="Rohit Sharma",
                role_name=ROLE_STUDENT,
                institution=institution,
                password=default_pwd,
                registration_number="2024-NR-099",
                residential_eligibility=User.ResidentialEligibility.MALE,
            )
            self._get_or_create_user(
                email="sneha.patel@student.roomloop.edu",
                full_name="Sneha Patel",
                role_name=ROLE_STUDENT,
                institution=institution,
                password=default_pwd,
                registration_number="2024-SR-099",
                residential_eligibility=User.ResidentialEligibility.FEMALE,
            )
            self._get_or_create_user(
                email="tarun.verma@student.roomloop.edu",
                full_name="Tarun Verma (Unverified)",
                role_name=ROLE_STUDENT,
                institution=institution,
                password=default_pwd,
                registration_number="2024-UNV-001",
                residential_eligibility=User.ResidentialEligibility.UNVERIFIED,
            )

            self.stdout.write("✓ Seeded active student occupants and unallocated testing students.")

        self.stdout.write(self.style.SUCCESS("✓ Demo data successfully seeded for RoomLoop Phase 1!"))

    def _get_or_create_user(self, email, full_name, role_name, institution, password, registration_number=None, residential_eligibility=User.ResidentialEligibility.UNVERIFIED, is_staff=False, is_superuser=False):
        user = User.objects.filter(email=email.strip().lower()).first()
        if not user:
            user = create_user_with_role(
                email=email,
                full_name=full_name,
                role_name=role_name,
                institution=institution,
                password=password,
                registration_number=registration_number,
                residential_eligibility=residential_eligibility,
                is_staff=is_staff,
            )
            if is_superuser:
                user.is_superuser = True
                user.save(update_fields=["is_superuser"])
        elif user.residential_eligibility != residential_eligibility and residential_eligibility != User.ResidentialEligibility.UNVERIFIED:
            user.residential_eligibility = residential_eligibility
            user.save(update_fields=["residential_eligibility"])
        return user

    def _get_or_create_room(self, floor, room_number, room_type):
        room = Room.objects.filter(floor=floor, room_number=room_number).first()
        if not room:
            room = Room.objects.create(
                floor=floor,
                room_number=room_number,
                room_type=room_type,
                operational_status=OperationalStatus.IN_SERVICE,
            )
            capacity = room.target_capacity
            bed_labels = ["Bed A", "Bed B", "Bed C", "Bed D"][:capacity]
            for label in bed_labels:
                Bed.objects.create(room=room, bed_label=label, operational_status=OperationalStatus.IN_SERVICE)
        return room
