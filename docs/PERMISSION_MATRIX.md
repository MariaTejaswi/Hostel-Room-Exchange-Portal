# RoomLoop Role & Permission Matrix (Phase 1)

This document specifies the authorization capabilities and operational boundaries for each user role in RoomLoop Phase 1.

Authorization is enforced server-side using **centralized permissions** (`apps/accounts/permissions.py`), object-level filtering in selectors, and strict service layer validation.

---

## 1. Role Capabilities Matrix

| Capability / Action | Student | Warden | Chief Warden | Hostel Admin | Dean of Student Welfare | System Admin |
|---|:---:|:---:|:---:|:---:|:---:|:---:|
| **Authentication & Profile** |
| Login / Logout / Password Reset | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Edit Own Profile (Name, Phone, Avatar) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Change Own Password | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| **Dashboards** |
| Student Dashboard (Personal room card) | ✓ | — | — | — | — | — |
| Warden Dashboard (Assigned hostels only) | — | ✓ | — | — | — | — |
| Chief Warden Dashboard (All hostels) | — | — | ✓ | — | — | — |
| Hostel Admin Console (Infrastructure) | — | — | — | ✓ | — | — |
| Dean Executive & Infrastructure Console | — | — | — | — | ✓ | — |
| System Admin Technical Dashboard | — | — | — | — | — | ✓ |
| **Hostel & Room Infrastructure** |
| Browse Hostels Directory | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Create / Edit Hostel | — | — | — | ✓ | ✓ | — |
| View Hostel Blocks & Floor Details | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Browse Room Inventory & Availability | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Create New Room (and auto-generate beds) | — | — | — | ✓ | ✓ | — |
| View Room Details | ✓ | ✓ (assigned) | ✓ | ✓ | ✓ | — |
| View Other Student Resident Names | — | ✓ (assigned) | ✓ | ✓ | ✓ | — |
| **Bed Management** |
| View Bed Operational Status | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Toggle Maintenance / In Service Status | — | ✓ (assigned) | — | ✓ | ✓ | — |
| **Occupancy & Allocation** |
| View Own Active Bed Allocation | ✓ | ✓ | ✓ | ✓ | ✓ | — |
| Browse Overall Student Occupancy Roster | — | ✓ (assigned) | ✓ | ✓ | ✓ | — |
| Allocate Unassigned Student to Vacant Bed| — | ✓ (assigned) | — *(routine denied)* | — *(routine denied)* | — *(boundary)* | — *(boundary)* |
| Release Active Student Allocation | — | ✓ (assigned) | — *(routine denied)* | — *(routine denied)* | — *(boundary)* | — *(boundary)* |
| Self-Assign Bed | — | — | — | — | — | — |
| Emergency Relocation (Audited override) | — | — | — | *Deferred (Phase 2)* | — | — |
| **Technical Administration & Audit** |
| Access Django Admin Portal (`/admin/`) | — | — | — | — | — | ✓ *(superuser/staff)* |
| Manage User Accounts & Role Groups | — | — | — | — | — | ✓ |
| View Technical & Security Audit Logs | — | — | — | — | — | ✓ *(technical only)* |

---

## 2. Privacy & Operational Boundaries

1. **Student Privacy**:
   - Students can browse room inventory to observe room types, capacity, and whether a bed is occupied or vacant.
   - Students **cannot** see the names, emails, or registration numbers of other occupants in room details.
   - Staff roles (Warden, Chief Warden, Admin, Dean) can inspect resident rosters within their assigned scope.

2. **Warden Scoping**:
   - Wardens are assigned to specific hostels via `WardenAssignment`.
   - Routine allocations and releases are strictly restricted to the destination hostel warden.
   - Unauthorized attempts to mutate beds in unassigned hostels are rejected with `403 Forbidden` and service-level `ValidationError`.

3. **Chief Warden Oversight Scope**:
   - Chief Warden has campus-wide oversight and monitoring over capacity and utilization.
   - Chief Warden is **denied infrastructure mutation permissions** (creation/editing of hostels, floors, and rooms belongs to the Dean and Hostel Administrator).
   - Routine new allocations are performed directly by destination wardens; Chief Warden is reserved for escalations, disputes, and exceptions.

4. **Hostel Administrator Operational Boundary**:
   - Full authority over building infrastructure, maintenance, and room inventory.
   - Routine occupancy allocations and releases are **denied**.
   - A dedicated audited emergency relocation workflow with mandatory justification is planned for Phase 2. Generic endpoints cannot be used as an emergency workflow.

5. **Dean Authority Scope**:
   - Full institutional authority to create, configure, and edit boys' and girls' hostels, floors, and rooms.
   - Dean retains executive authority over serious policy appeals.
   - Dean is **strictly forbidden from performing direct student bed allocations or releases**; `is_superuser` and `is_staff` flags do not bypass this operational boundary.

6. **System Administrator Technical Boundary**:
   - Oversees technical operations, platform health, user provisioning, and security compliance.
   - System Administrators **cannot** allocate beds, approve room transfers, or mutate infrastructure.
   - Technical dashboard audit logs are restricted strictly to technical/authentication events, isolating operational student occupancy records and private welfare evidence.
