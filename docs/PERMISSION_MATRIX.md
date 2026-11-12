# RoomLoop Role & Permission Matrix (Phase 1)

This document specifies the authorization capabilities and operational boundaries for each user role in RoomLoop Phase 1.

Authorization is enforced server-side using **Django Groups and Permissions**, object-level filtering in selectors, and service layer validation.

---

## 1. Role Capabilities Matrix

| Capability / Action | Student | Warden | Chief Warden | Hostel Admin | Dean of Student Welfare | System Admin |
|---|:---:|:---:|:---:|:---:|:---:|:---:|
| **Authentication & Profile** |
| Login / Logout / Password Reset | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Edit Own Profile (Name, Phone, Avatar) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Change Own Password | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| **Dashboards** |
| Student Dashboard (Personal room card) | ✓ | — | — | — | — | (view) |
| Warden Dashboard (Assigned hostels only) | — | ✓ | — | (all) | — | (all) |
| Chief Warden Dashboard (All hostels) | — | — | ✓ | ✓ | — | ✓ |
| Hostel Admin Console (Full management) | — | — | — | ✓ | — | ✓ |
| Dean Executive Analytics (Read-only) | — | — | — | — | ✓ | ✓ |
| **Hostel & Room Inventory** |
| Browse Hostels Directory | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Create / Edit Hostel | — | — | — | ✓ | — | ✓ |
| View Hostel Blocks & Floor Details | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Browse Room Inventory & Availability | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Create New Room (and auto-generate beds) | — | — | — | ✓ | — | ✓ |
| View Room Details | ✓ | ✓ (assigned) | ✓ | ✓ | ✓ | ✓ |
| View Other Student Resident Names | — | ✓ (assigned) | ✓ | ✓ | ✓ | ✓ |
| **Bed Management** |
| View Bed Operational Status | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Toggle Maintenance / In Service Status | — | ✓ (assigned) | ✓ | ✓ | — | ✓ |
| Add / Delete Beds | — | — | — | ✓ | — | ✓ |
| **Occupancy & Allocation** |
| View Own Active Bed Allocation | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Browse Overall Student Occupancy Roster | — | ✓ (assigned) | ✓ | ✓ | ✓ (read-only) | ✓ |
| Allocate Unassigned Student to Vacant Bed| — | ✓ (assigned) | ✓ | ✓ | — | ✓ |
| Release Active Student Allocation | — | ✓ (assigned) | ✓ | ✓ | — | ✓ |
| Self-Assign Bed | — | — | — | — | — | — |
| **System Administration** |
| Access Django Admin Portal (`/admin/`) | — | — | — | ✓ | — | ✓ |
| Manage User Groups & Global Settings | — | — | — | — | — | ✓ |
| Access Immutable System Audit Logs | — | — | — | ✓ | — | ✓ |

---

## 2. Privacy & Scoping Rules

1. **Student Privacy**:
   - Students can browse room inventory to observe room types, capacity, and whether a bed is occupied or vacant.
   - Students **cannot** see the names, emails, or registration numbers of other occupants in room details.
   - Staff roles (Warden, Chief Warden, Admin, Dean) can inspect resident rosters within their assigned scope.

2. **Warden Scoping**:
   - Wardens are assigned to one or more specific hostels via `WardenAssignment`.
   - All management actions (updating bed maintenance status, allocating students, releasing allocations) are verified against the warden's assigned hostels.
   - Unauthorized attempts to mutate beds in unassigned hostels are rejected with `403 Forbidden` (`PermissionDenied`).

3. **Dean Read-Only Guarantee**:
   - The Dean of Student Welfare dashboard provides high-level institution utilization metrics.
   - The interface contains **zero mutating buttons** (no add, edit, allocate, or release actions).
   - Any direct POST request made by a user in the Dean role to mutating endpoints is rejected with `PermissionDenied`.

4. **Multi-Party Concurrency Guarantee**:
   - Competing requests to assign the same vacant bed use `select_for_update()` on PostgreSQL row locks and the `unique_active_occupancy_per_bed` database constraint. Double-booking is physically prevented at the database engine level.
