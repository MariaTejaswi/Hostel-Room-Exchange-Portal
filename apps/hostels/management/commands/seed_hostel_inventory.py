import os
import sys
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Count, Q
from django.contrib.auth.models import Group
from django.contrib.auth.hashers import make_password

from apps.core.models import Institution, AuditLog
from apps.accounts.models import User
from apps.accounts.services import (
    ensure_roles_exist,
    ROLE_WARDEN,
    ROLE_STUDENT,
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


class Command(BaseCommand):
    help = (
        "Idempotently seed the standard 17-hostel collection (BH-01..10, GH-01..07) "
        "comprising 170 floors, 3,400 rooms, 13,600 beds, 17 distinct primary wardens, "
        "and sample demo students with valid active occupancies."
    )

    DEMO_WARDENS = {
        "BH-01": {"email": "warden.bh01@roomloop.edu", "name": "Dr. Rajesh Sharma"},
        "BH-02": {"email": "warden.bh02@roomloop.edu", "name": "Dr. Amit Patel"},
        "BH-03": {"email": "warden.bh03@roomloop.edu", "name": "Dr. Suresh Kumar"},
        "BH-04": {"email": "warden.bh04@roomloop.edu", "name": "Dr. Vikram Malhotra"},
        "BH-05": {"email": "warden.bh05@roomloop.edu", "name": "Dr. Pradeep Verma"},
        "BH-06": {"email": "warden.bh06@roomloop.edu", "name": "Dr. Alok Nath Mishra"},
        "BH-07": {"email": "warden.bh07@roomloop.edu", "name": "Dr. Ramesh Chandra"},
        "BH-08": {"email": "warden.bh08@roomloop.edu", "name": "Dr. Manoj Tiwari"},
        "BH-09": {"email": "warden.bh09@roomloop.edu", "name": "Dr. Sanjeev Gupta"},
        "BH-10": {"email": "warden.bh10@roomloop.edu", "name": "Dr. Harish Pandey"},
        "GH-01": {"email": "warden.gh01@roomloop.edu", "name": "Dr. Sunita Rao"},
        "GH-02": {"email": "warden.gh02@roomloop.edu", "name": "Dr. Meenakshi Sundaram"},
        "GH-03": {"email": "warden.gh03@roomloop.edu", "name": "Dr. Anita Desai"},
        "GH-04": {"email": "warden.gh04@roomloop.edu", "name": "Dr. Priya Nambiar"},
        "GH-05": {"email": "warden.gh05@roomloop.edu", "name": "Dr. Kavita Iyer"},
        "GH-06": {"email": "warden.gh06@roomloop.edu", "name": "Dr. Shalini Sen"},
        "GH-07": {"email": "warden.gh07@roomloop.edu", "name": "Dr. Neha Agarwal"},
    }

    BOYS_NAMES = [
        "Rohan Sharma", "Aarav Verma", "Siddharth Singh", "Vikram Kumar", "Aditya Gupta",
        "Rahul Patel", "Varun Mehta", "Nikhil Joshi", "Karthik Reddy", "Kunal Nair",
        "Arjun Rao", "Ankit Das", "Mayank Shah", "Harsh Bhat", "Gaurav Roy",
        "Abhishek Saxena", "Pranav Mishra", "Rishabh Agarwal", "Yash Sen", "Dev Choudhury",
    ]

    GIRLS_NAMES = [
        "Ananya Sharma", "Priya Verma", "Sneha Singh", "Pooja Kumar", "Riya Gupta",
        "Aditi Patel", "Kavya Mehta", "Shreya Joshi", "Neha Reddy", "Tanvi Nair",
        "Divya Rao", "Meera Das", "Ishita Shah", "Ritu Bhat", "Deepa Roy",
        "Swati Saxena", "Simran Mishra", "Bhavna Agarwal", "Nikita Sen", "Shruti Choudhury",
    ]

    def add_arguments(self, parser):
        parser.add_argument(
            "--institution-code",
            type=str,
            default=None,
            help="Institutional code to scope the inventory seed. If omitted, uses the unambiguous single active institution.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Inspect current inventory and simulate changes without committing to database.",
        )
        parser.add_argument(
            "--students-per-hostel",
            type=int,
            default=20,
            help="Number of demo students to create and allocate per seeded hostel (default: 20).",
        )
        parser.add_argument(
            "--demo-password",
            type=str,
            default=None,
            help="Default password for seeded demo wardens and students (defaults to DEMO_DEFAULT_PASSWORD env or secure default).",
        )

    def handle(self, *args, **options):
        inst_code = options.get("institution_code")
        dry_run = options["dry_run"]
        students_per_hostel = max(0, options.get("students_per_hostel", 20))
        demo_password = (
            options.get("demo_password")
            or os.environ.get("DEMO_DEFAULT_PASSWORD")
            or "RoomLoop2026!"
        )
        hashed_demo_password = make_password(demo_password)

        if inst_code:
            inst_code = inst_code.strip().upper()
            try:
                institution = Institution.objects.get(code=inst_code)
            except Institution.DoesNotExist:
                available = list(Institution.objects.values_list("code", flat=True))
                raise CommandError(
                    f"Institution with code '{inst_code}' not found. Available institutions: {available}"
                )
        else:
            inst_count = Institution.objects.count()
            if inst_count == 1:
                institution = Institution.objects.first()
            elif inst_count == 0:
                raise CommandError("No institutions exist in the database. Please create an institution before seeding.")
            else:
                available = list(Institution.objects.values_list("code", flat=True))
                raise CommandError(
                    f"Multiple institutions found ({available}). Please explicitly specify --institution-code."
                )

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"=== RoomLoop Hostel Inventory Seeder [{'DRY RUN SIMULATION' if dry_run else 'LIVE COMMIT'}] ==="
        ))
        self.stdout.write(f"Target Institution: {institution.name} ({institution.code})")

        # 1. Inventory Specifications
        specs = []
        for i in range(1, 11):
            specs.append({
                "code": f"BH-{i:02d}",
                "name": f"Boys Hostel {i:02d}",
                "category": Hostel.ResidentCategory.MALE,
                "address": f"Boys Residential Quadrangle, Sector {i}",
                "description": f"Standard undergraduate residential accommodation for boys ({i:02d}).",
            })
        for i in range(1, 8):
            specs.append({
                "code": f"GH-{i:02d}",
                "name": f"Girls Hostel {i:02d}",
                "category": Hostel.ResidentCategory.FEMALE,
                "address": f"Girls Residential Quadrangle, Enclave {i}",
                "description": f"Standard undergraduate residential accommodation for girls ({i:02d}).",
            })

        # 2. Inspect Existing Inventory
        existing_hostels = {
            h.code: h
            for h in Hostel.objects.filter(institution=institution)
        }

        # Track collection metrics
        collection_stats = {
            "hostels_created": 0,
            "hostels_existing": 0,
            "floors_created": 0,
            "floors_existing": 0,
            "rooms_created": 0,
            "rooms_existing": 0,
            "beds_created": 0,
            "beds_existing": 0,
            "boys_beds": 0,
            "girls_beds": 0,
            "wardens_created": 0,
            "wardens_existing": 0,
            "warden_assignments_created": 0,
            "warden_assignments_existing": 0,
            "students_created": 0,
            "students_existing": 0,
            "occupancies_created": 0,
            "occupancies_existing": 0,
            "conflicts": [],
        }

        ensure_roles_exist()
        warden_group = Group.objects.get(name=ROLE_WARDEN)
        student_group = Group.objects.get(name=ROLE_STUDENT)

        # Execution in transaction (or rollback if dry-run)
        with transaction.atomic():
            for spec in specs:
                code = spec["code"]
                name = spec["name"]
                cat = spec["category"]

                if code in existing_hostels:
                    hostel = existing_hostels[code]
                    collection_stats["hostels_existing"] += 1
                    if hostel.resident_category != cat:
                        collection_stats["conflicts"].append(
                            f"Hostel {code} exists with unexpected category '{hostel.resident_category}' (expected '{cat}')."
                        )
                    # Use efficient aggregate queries to inspect existing inventory without N+1 overhead
                    fl_count = Floor.objects.filter(block__hostel=hostel).count()
                    rm_count = Room.objects.filter(floor__block__hostel=hostel).count()
                    bd_count = Bed.objects.filter(room__floor__block__hostel=hostel).count()
                    collection_stats["floors_existing"] += fl_count
                    collection_stats["rooms_existing"] += rm_count
                    collection_stats["beds_existing"] += bd_count
                    if cat == Hostel.ResidentCategory.MALE:
                        collection_stats["boys_beds"] += bd_count
                    else:
                        collection_stats["girls_beds"] += bd_count
                else:
                    if dry_run:
                        hostel = Hostel(
                            institution=institution,
                            name=name,
                            code=code,
                            resident_category=cat,
                            address=spec["address"],
                            description=spec["description"],
                            status=Hostel.Status.ACTIVE,
                        )
                    else:
                        hostel = Hostel.objects.create(
                            institution=institution,
                            name=name,
                            code=code,
                            resident_category=cat,
                            address=spec["address"],
                            description=spec["description"],
                            status=Hostel.Status.ACTIVE,
                        )
                    collection_stats["hostels_created"] += 1

                if code not in existing_hostels:
                    if dry_run:
                        collection_stats["floors_created"] += 10
                        collection_stats["rooms_created"] += 200
                        collection_stats["beds_created"] += 800
                        if cat == Hostel.ResidentCategory.MALE:
                            collection_stats["boys_beds"] += 800
                        else:
                            collection_stats["girls_beds"] += 800
                    else:
                        block = Block.objects.create(
                            hostel=hostel,
                            code="BLK-1",
                            name="Block 1",
                            description="Primary Residential Block",
                        )

                        floors_to_create = [
                            Floor(block=block, floor_number=floor_num, floor_label=f"Floor {floor_num}")
                            for floor_num in range(1, 11)
                        ]
                        Floor.objects.bulk_create(floors_to_create)
                        created_floors = list(Floor.objects.filter(block=block).order_by("floor_number"))

                        for floor_obj in created_floors:
                            floor_num = floor_obj.floor_number
                            rooms_to_create = [
                                Room(
                                    floor=floor_obj,
                                    room_number=str((floor_num * 100) + room_idx),
                                    room_type=Room.RoomType.FOUR,
                                    operational_status=OperationalStatus.IN_SERVICE,
                                    is_active=True,
                                )
                                for room_idx in range(1, 21)
                            ]
                            Room.objects.bulk_create(rooms_to_create)
                            created_rooms = list(Room.objects.filter(floor=floor_obj).order_by("room_number"))

                            beds_to_create = [
                                Bed(
                                    room=room_obj,
                                    bed_label=bed_label,
                                    operational_status=OperationalStatus.IN_SERVICE,
                                    is_active=True,
                                )
                                for room_obj in created_rooms
                                for bed_label in ["Bed A", "Bed B", "Bed C", "Bed D"]
                            ]
                            Bed.objects.bulk_create(beds_to_create, batch_size=1000)

                        collection_stats["floors_created"] += 10
                        collection_stats["rooms_created"] += 200
                        collection_stats["beds_created"] += 800
                        if cat == Hostel.ResidentCategory.MALE:
                            collection_stats["boys_beds"] += 800
                        else:
                            collection_stats["girls_beds"] += 800

                # 3. Dedicated Primary Warden for this Hostel
                w_info = self.DEMO_WARDENS.get(code)
                if w_info:
                    warden_user = User.objects.filter(email=w_info["email"].lower()).first()
                    if not warden_user:
                        if dry_run:
                            collection_stats["wardens_created"] += 1
                        else:
                            warden_user = User(
                                email=w_info["email"].lower(),
                                full_name=w_info["name"],
                                institution=institution,
                                residential_eligibility=User.ResidentialEligibility.UNVERIFIED,
                                is_staff=False,
                                password=hashed_demo_password,
                            )
                            warden_user.save()
                            warden_user.groups.add(warden_group)
                            collection_stats["wardens_created"] += 1
                    else:
                        collection_stats["wardens_existing"] += 1
                        if not dry_run and not warden_user.groups.filter(name=ROLE_WARDEN).exists():
                            warden_user.groups.add(warden_group)

                    # Warden Assignment
                    if dry_run:
                        if hostel.id:
                            assignment_exists = WardenAssignment.objects.filter(
                                warden__email=w_info["email"].lower(),
                                hostel=hostel,
                            ).exists()
                            if assignment_exists:
                                collection_stats["warden_assignments_existing"] += 1
                            else:
                                collection_stats["warden_assignments_created"] += 1
                        else:
                            collection_stats["warden_assignments_created"] += 1
                    else:
                        assignment, a_created = WardenAssignment.objects.get_or_create(
                            warden=warden_user,
                            hostel=hostel,
                            defaults={"is_primary": True},
                        )
                        if a_created:
                            collection_stats["warden_assignments_created"] += 1
                        else:
                            collection_stats["warden_assignments_existing"] += 1
                            if not assignment.is_primary:
                                assignment.is_primary = True
                                assignment.save(update_fields=["is_primary"])

                # 4. Sample Students & Bed Allocations
                code_clean = code.replace("-", "").upper()
                name_list = self.BOYS_NAMES if cat == Hostel.ResidentCategory.MALE else self.GIRLS_NAMES
                res_eligibility = (
                    User.ResidentialEligibility.MALE
                    if cat == Hostel.ResidentCategory.MALE
                    else User.ResidentialEligibility.FEMALE
                )

                for s_idx in range(1, students_per_hostel + 1):
                    s_email = f"student.{code_clean.lower()}.{s_idx:02d}@student.roomloop.edu"
                    s_reg = f"STU-{code_clean}-{s_idx:03d}"
                    s_name = name_list[(s_idx - 1) % len(name_list)]

                    student_user = User.objects.filter(email=s_email).first()
                    if not student_user:
                        student_user = User.objects.filter(
                            institution=institution, registration_number=s_reg
                        ).first()

                    if not student_user:
                        if dry_run:
                            collection_stats["students_created"] += 1
                        else:
                            student_user = User(
                                email=s_email,
                                full_name=s_name,
                                institution=institution,
                                registration_number=s_reg,
                                residential_eligibility=res_eligibility,
                                is_staff=False,
                                password=hashed_demo_password,
                            )
                            student_user.save()
                            student_user.groups.add(student_group)
                            collection_stats["students_created"] += 1
                    else:
                        collection_stats["students_existing"] += 1
                        if not dry_run:
                            if not student_user.groups.filter(name=ROLE_STUDENT).exists():
                                student_user.groups.add(student_group)
                            if student_user.residential_eligibility != res_eligibility:
                                student_user.residential_eligibility = res_eligibility
                                student_user.save(update_fields=["residential_eligibility"])

                    # Allocation to bed
                    if dry_run:
                        if student_user and student_user.id:
                            has_active_occ = StudentOccupancy.objects.filter(
                                student=student_user,
                                status=StudentOccupancy.OccupancyStatus.ACTIVE,
                            ).exists()
                            if has_active_occ:
                                collection_stats["occupancies_existing"] += 1
                            else:
                                collection_stats["occupancies_created"] += 1
                        else:
                            collection_stats["occupancies_created"] += 1
                    else:
                        has_active_occ = StudentOccupancy.objects.filter(
                            student=student_user,
                            status=StudentOccupancy.OccupancyStatus.ACTIVE,
                        ).exists()
                        if has_active_occ:
                            collection_stats["occupancies_existing"] += 1
                        else:
                            # Select Floor 1 Room 100+s_idx, Bed A
                            room_num = str(100 + s_idx)
                            target_bed = Bed.objects.filter(
                                room__floor__block__hostel=hostel,
                                room__room_number=room_num,
                                bed_label="Bed A",
                                is_active=True,
                                operational_status=OperationalStatus.IN_SERVICE,
                            ).exclude(occupancies__status=StudentOccupancy.OccupancyStatus.ACTIVE).first()

                            if not target_bed:
                                # Fallback to any vacant in-service bed in this hostel
                                target_bed = (
                                    Bed.objects.filter(
                                        room__floor__block__hostel=hostel,
                                        is_active=True,
                                        operational_status=OperationalStatus.IN_SERVICE,
                                        room__is_active=True,
                                        room__operational_status=OperationalStatus.IN_SERVICE,
                                    )
                                    .exclude(occupancies__status=StudentOccupancy.OccupancyStatus.ACTIVE)
                                    .order_by("room__floor__floor_number", "room__room_number", "bed_label")
                                    .first()
                                )

                            if target_bed:
                                StudentOccupancy.objects.create(
                                    institution=institution,
                                    student=student_user,
                                    bed=target_bed,
                                    status=StudentOccupancy.OccupancyStatus.ACTIVE,
                                    source=StudentOccupancy.AllocationSource.ADMINISTRATIVE,
                                )
                                collection_stats["occupancies_created"] += 1

            # Audit record (in live mode only)
            if not dry_run and (
                collection_stats["hostels_created"] > 0
                or collection_stats["wardens_created"] > 0
                or collection_stats["students_created"] > 0
            ):
                AuditLog.objects.create(
                    institution=institution,
                    action="HOSTEL_INVENTORY_SEEDED",
                    target_type="HostelInventory",
                    target_id="SEED_17_COLLECTION",
                    change_summary=(
                        f"Seeded {collection_stats['hostels_created']} hostels, "
                        f"{collection_stats['floors_created']} floors, "
                        f"{collection_stats['rooms_created']} rooms, and "
                        f"{collection_stats['beds_created']} beds across institution {institution.code} "
                        f"with {collection_stats['wardens_created']} wardens and "
                        f"{collection_stats['students_created']} sample students."
                    ),
                )

            if dry_run:
                # Roll back any accidental simulated writes
                transaction.set_rollback(True)

        # 3. Output Formatted Seeding Report
        target_students = 17 * students_per_hostel
        self.stdout.write("\n" + self.style.SUCCESS("--- Seeded Collection Summary (17 Hostels) ---"))
        self.stdout.write(f"• Hostels: {collection_stats['hostels_created']} created, {collection_stats['hostels_existing']} existing (Target: 17)")
        self.stdout.write(f"• Floors:  {collection_stats['floors_created']} created, {collection_stats['floors_existing']} existing (Target: 170)")
        self.stdout.write(f"• Rooms:   {collection_stats['rooms_created']} created, {collection_stats['rooms_existing']} existing (Target: 3,400)")
        self.stdout.write(f"• Beds:    {collection_stats['beds_created']} created, {collection_stats['beds_existing']} existing (Target: 13,600)")
        self.stdout.write(f"• Boys' Bed Capacity:  {collection_stats['boys_beds']} beds (Target: 8,000)")
        self.stdout.write(f"• Girls' Bed Capacity: {collection_stats['girls_beds']} beds (Target: 5,600)")
        self.stdout.write(f"• Primary Wardens: {collection_stats['wardens_created']} created, {collection_stats['wardens_existing']} existing (Target: 17)")
        self.stdout.write(f"• Sample Students: {collection_stats['students_created']} created, {collection_stats['students_existing']} existing (Target: {target_students})")
        self.stdout.write(f"• Active Bed Occupancies: {collection_stats['occupancies_created']} created, {collection_stats['occupancies_existing']} existing")
        self.stdout.write("• Authentication: Demo credentials hashed with standard pbkdf2_sha256 algorithm.")

        if collection_stats["conflicts"]:
            self.stdout.write(self.style.WARNING("\nConflicts Detected:"))
            for conf in collection_stats["conflicts"]:
                self.stdout.write(self.style.WARNING(f"  ⚠ {conf}"))

        # 4. Overall Database Inventory Summary (including legacy / demo hostels)
        all_hostels = Hostel.objects.filter(institution=institution)
        total_hostels_count = all_hostels.count()
        if dry_run:
            total_hostels_count += collection_stats["hostels_created"]

        existing_demo = [h.name for h in all_hostels if h.code not in [s["code"] for s in specs]]

        self.stdout.write("\n" + self.style.HTTP_INFO("--- Overall Database Inventory ---"))
        self.stdout.write(f"• Total Hostels in Database: {total_hostels_count}")
        self.stdout.write(f"• Preserved Demo / Additional Hostels: {len(existing_demo)} ({', '.join(existing_demo) if existing_demo else 'None'})")

        if dry_run:
            self.stdout.write(self.style.NOTICE("\n[DRY RUN COMPLETE] Zero database modifications were written."))
        else:
            self.stdout.write(self.style.SUCCESS("\n[SEED COMPLETE] All 17 hostels, wardens, students, and bed inventories successfully persisted."))
