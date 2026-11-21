import os
import sys
import time
import statistics

# Set environment
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.test")
sys.path.insert(0, os.getcwd())

import django
django.setup()

from django.test import Client
from django.db import connection, reset_queries
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

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
    Hostel, Block, Floor, Room, Bed, WardenAssignment, StudentOccupancy, OperationalStatus
)
from apps.exchange.models import (
    ExchangeProposal, ExchangeRequest, ProposalParticipant, Approval, Notification, PolicyRule
)

from django.core.management import call_command

def setup_benchmark_environment():
    """Setup complete representative dataset for benchmarking."""
    call_command("migrate", verbosity=0)
    roles = ensure_roles_exist()
    inst, _ = Institution.objects.get_or_create(
        code="APEX-BENCH",
        defaults={"name": "Apex University Bench", "contact_email": "admin@apex.bench", "is_active": True}
    )

    # 1. Hostels & Inventory
    hostel_male, _ = Hostel.objects.get_or_create(
        institution=inst, code="BH-01",
        defaults={"name": "Boys Hostel 01", "resident_category": Hostel.ResidentCategory.MALE, "status": Hostel.Status.ACTIVE}
    )
    hostel_female, _ = Hostel.objects.get_or_create(
        institution=inst, code="GH-01",
        defaults={"name": "Girls Hostel 01", "resident_category": Hostel.ResidentCategory.FEMALE, "status": Hostel.Status.ACTIVE}
    )

    # Blocks, Floors, Rooms, Beds
    beds_male = []
    for h in [hostel_male, hostel_female]:
        block, _ = Block.objects.get_or_create(hostel=h, code="BA", defaults={"name": "Block A", "is_active": True})
        floor, _ = Floor.objects.get_or_create(block=block, floor_number=1, defaults={"floor_label": "1st Floor", "is_active": True})
        for r_num in ["101", "102", "103", "104", "105"]:
            room, _ = Room.objects.get_or_create(
                floor=floor, room_number=r_num,
                defaults={"room_type": Room.RoomType.DOUBLE, "operational_status": OperationalStatus.IN_SERVICE, "is_active": True}
            )
            for b_lbl in ["Bed A", "Bed B"]:
                bed, _ = Bed.objects.get_or_create(
                    room=room, bed_label=b_lbl,
                    defaults={"operational_status": OperationalStatus.IN_SERVICE, "is_active": True}
                )
                if h == hostel_male:
                    beds_male.append(bed)

    # 2. Users for all 6 roles
    def get_or_make_user(email, name, role, reg=None, elig=None, is_staff=False, is_super=False):
        u = User.objects.filter(email=email).first()
        if not u:
            u = create_user_with_role(
                email=email, full_name=name, role_name=role,
                institution=inst, password="BenchPassword123!", registration_number=reg,
                residential_eligibility=elig or User.ResidentialEligibility.UNVERIFIED,
                is_staff=is_staff
            )
            if is_super:
                u.is_superuser = True
                u.save()
        return u

    student1 = get_or_make_user("student1@apex.bench", "Arun Kumar", ROLE_STUDENT, "REG-BENCH-01", User.ResidentialEligibility.MALE)
    student2 = get_or_make_user("student2@apex.bench", "Bobby Singh", ROLE_STUDENT, "REG-BENCH-02", User.ResidentialEligibility.MALE)
    warden = get_or_make_user("warden@apex.bench", "Dr. Alok Sharma", ROLE_WARDEN)
    chief_warden = get_or_make_user("chief.warden@apex.bench", "Prof. Ramanathan", ROLE_CHIEF_WARDEN)
    hostel_admin = get_or_make_user("admin@apex.bench", "Suresh Verma", ROLE_HOSTEL_ADMIN, is_staff=True)
    sysadmin = get_or_make_user("sysadmin@apex.bench", "System Admin", ROLE_SYSADMIN, is_staff=True, is_super=True)
    dean = get_or_make_user("dean@apex.bench", "Prof. Aruna Roy", ROLE_DEAN)

    # Assign warden to hostel
    WardenAssignment.objects.get_or_create(warden=warden, hostel=hostel_male, defaults={"is_primary": True})

    # Occupancies
    occ1, _ = StudentOccupancy.objects.get_or_create(
        student=student1, bed=beds_male[0], institution=inst,
        defaults={"status": StudentOccupancy.OccupancyStatus.ACTIVE}
    )
    occ2, _ = StudentOccupancy.objects.get_or_create(
        student=student2, bed=beds_male[1], institution=inst,
        defaults={"status": StudentOccupancy.OccupancyStatus.ACTIVE}
    )

    # Proposals & Notifications
    req1, _ = ExchangeRequest.objects.get_or_create(
        student=student1, institution=inst,
        defaults={"current_occupancy": occ1, "status": ExchangeRequest.Status.MATCHED, "reason": "Academic grouping"}
    )
    req2, _ = ExchangeRequest.objects.get_or_create(
        student=student2, institution=inst,
        defaults={"current_occupancy": occ2, "status": ExchangeRequest.Status.MATCHED, "reason": "Mutual consent"}
    )
    prop, _ = ExchangeProposal.objects.get_or_create(
        institution=inst,
        defaults={"cycle_length": 2, "status": ExchangeProposal.Status.PENDING_APPROVAL}
    )
    p1, _ = ProposalParticipant.objects.get_or_create(
        proposal=prop, student=student1,
        defaults={"sequence_order": 0, "exchange_request": req1, "current_occupancy": occ1, "proposed_bed": beds_male[1],
                  "response_status": ProposalParticipant.ResponseStatus.ACCEPTED}
    )
    p2, _ = ProposalParticipant.objects.get_or_create(
        proposal=prop, student=student2,
        defaults={"sequence_order": 1, "exchange_request": req2, "current_occupancy": occ2, "proposed_bed": beds_male[0],
                  "response_status": ProposalParticipant.ResponseStatus.ACCEPTED}
    )

    for u in [student1, warden, chief_warden, hostel_admin, sysadmin, dean]:
        Notification.objects.get_or_create(
            recipient=u, message="System operational check",
            defaults={"notification_type": Notification.NotificationType.PROPOSAL_CREATED}
        )

    return {
        "users": {
            "Student": student1,
            "Warden": warden,
            "Chief Warden": chief_warden,
            "Hostel Administrator": hostel_admin,
            "System Administrator": sysadmin,
            "Dean of Student Welfare": dean,
        },
        "password": "BenchPassword123!",
    }

def run_benchmarks():
    data = setup_benchmark_environment()
    users = data["users"]
    password = data["password"]

    print("================================================================================")
    print("BASELINE PERFORMANCE MEASUREMENT (5 RUNS PER TARGET)")
    print("================================================================================")

    # 1. Login flow benchmarks
    print("\n--- 1. Authentication & Login Lifecycle ---")
    c_anon = Client()

    # Initial GET /login/
    get_times, get_queries, get_query_times = [], [], []
    for _ in range(5):
        t0 = time.perf_counter()
        with CaptureQueriesContext(connection) as ctx:
            r = c_anon.get("/login/")
        t_el = (time.perf_counter() - t0) * 1000
        get_times.append(t_el)
        get_queries.append(len(ctx.captured_queries))
        get_query_times.append(sum(float(q["time"]) for q in ctx.captured_queries) * 1000)
    print(f"GET /login/ initial: status={r.status_code}, time={statistics.median(get_times):.2f}ms, queries={statistics.median(get_queries)}, db_time={statistics.median(get_query_times):.2f}ms")

    # POST /login/ (submission + auth + redirect chain)
    post_times, post_queries, post_query_times = [], [], []
    for _ in range(5):
        c = Client()
        t0 = time.perf_counter()
        with CaptureQueriesContext(connection) as ctx:
            r = c.post("/login/", {"email": "student1@apex.bench", "password": password}, follow=True)
        t_el = (time.perf_counter() - t0) * 1000
        post_times.append(t_el)
        post_queries.append(len(ctx.captured_queries))
        post_query_times.append(sum(float(q["time"]) for q in ctx.captured_queries) * 1000)
    print(f"POST /login/ complete login+redirect: status={r.status_code}, time={statistics.median(post_times):.2f}ms, queries={statistics.median(post_queries)}, db_time={statistics.median(post_query_times):.2f}ms")

    # 2. Six Dashboards Benchmarks
    print("\n--- 2. Six Dashboard Role Views ---")
    dashboard_targets = [
        ("Student", "/dashboard/student/"),
        ("Warden", "/dashboard/warden/"),
        ("Chief Warden", "/dashboard/chief-warden/"),
        ("Hostel Administrator", "/dashboard/admin/"),
        ("System Administrator", "/dashboard/sysadmin/"),
        ("Dean of Student Welfare", "/dashboard/dean/"),
    ]

    for role_name, url in dashboard_targets:
        u = users[role_name]
        c = Client()
        c.force_login(u)

        times, queries, query_times = [], [], []
        detailed_queries = []
        for i in range(5):
            t0 = time.perf_counter()
            with CaptureQueriesContext(connection) as ctx:
                resp = c.get(url)
            t_el = (time.perf_counter() - t0) * 1000
            assert resp.status_code == 200, f"Expected 200 for {role_name} on {url}, got {resp.status_code}"
            times.append(t_el)
            queries.append(len(ctx.captured_queries))
            query_times.append(sum(float(q["time"]) for q in ctx.captured_queries) * 1000)
            if i == 0:
                detailed_queries = [q["sql"] for q in ctx.captured_queries]

        print(f"Role: {role_name:<24} | URL: {url:<25} | status=200 | Time: {statistics.median(times):6.2f}ms | Queries: {statistics.median(queries):2} | DB Time: {statistics.median(query_times):5.2f}ms")
        if detailed_queries:
            print(f"   Sample Queries ({len(detailed_queries)} total):")
            for q_sql in detailed_queries[:4]:
                cleaned = " ".join(q_sql.split())[:110]
                print(f"     - {cleaned}...")
            if len(detailed_queries) > 4:
                print(f"     ... and {len(detailed_queries)-4} more queries")

    # 3. Authorized Pages Benchmarks
    print("\n--- 3. Authorized Page Navigation ---")
    authorized_targets = [
        ("Student", "/profile/"),
        ("Student", "/exchange/proposals/"),
        ("Student", "/hostels/"),
        ("Warden", "/rooms/"),
        ("Warden", "/exchange/approvals/"),
        ("Chief Warden", "/exchange/approvals/"),
        ("Chief Warden", "/hostels/"),
        ("Hostel Administrator", "/rooms/"),
        ("Hostel Administrator", "/eligibility-verification/"),
        ("System Administrator", "/students/"),
        ("Dean of Student Welfare", "/hostels/"),
        ("Dean of Student Welfare", "/exchange/approvals/"),
    ]

    for role_name, url in authorized_targets:
        u = users[role_name]
        c = Client()
        c.force_login(u)

        times, queries, query_times = [], [], []
        for _ in range(5):
            t0 = time.perf_counter()
            with CaptureQueriesContext(connection) as ctx:
                resp = c.get(url)
            t_el = (time.perf_counter() - t0) * 1000
            assert resp.status_code == 200, f"Expected 200 for {role_name} on {url}, got {resp.status_code}"
            times.append(t_el)
            queries.append(len(ctx.captured_queries))
            query_times.append(sum(float(q["time"]) for q in ctx.captured_queries) * 1000)

        print(f"Role: {role_name:<24} | Page: {url:<25} | Time: {statistics.median(times):6.2f}ms | Queries: {statistics.median(queries):2} | DB Time: {statistics.median(query_times):5.2f}ms")

if __name__ == "__main__":
    run_benchmarks()
