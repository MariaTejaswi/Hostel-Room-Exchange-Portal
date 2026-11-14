# RoomLoop — Phase 2 Dynamic Hostel Inventory, Categories, & Student Eligibility

**Date:** November 2026  
**Environment:** Python 3.12, Django 5.2 LTS, PostgreSQL 16 (`roomloop_db`)  
**Scope:** Two-Category Restriction (Boys' & Girls' Hostels), Academic Level Separation, Authoritative Student Residential Eligibility, 17-Hostel Scaled Seeding (13,600 Beds), Dynamic Hostel Explorer, On-Demand Floor & Bed Navigation, Custom Builder Wizard, Concurrency & Data Safety

---

## 1. Executive Summary

Phase 2 delivers a complete, production-grade hostel inventory architecture and visual exploration experience for **RoomLoop** (University Hostel Room Exchange and Multi-Party Swap Platform).

Key achievements in this phase:
1. **Strict Two-Category Policy & Academic Level Separation**:
   - Constrained the residential category domain strictly to **Boys' Hostels** (`male`) and **Girls' Hostels** (`female`). Removed Co-ed and invalid categories from models, services, forms, builder interfaces, and database constraints.
   - Separated academic-level eligibility (`academic_level`: `all`, `undergraduate`, `postgraduate`) from residential category. Preserved `Scholars Hall`'s postgraduate academic eligibility as `academic_level = "postgraduate"`.
   - Staged the database allowlist check constraint (`resident_category IN ('male', 'female')`) pending administrative classification for `Scholars Hall` (`SH-01`), preventing data corruption or violation of unclassified historical records.
2. **Authoritative Student Residential Eligibility**:
   - Added an explicit, authoritative `residential_eligibility` field (`male`, `female`, `unverified`) to the `User` model.
   - Prohibited inferring residential eligibility from current occupancy, names, or guesses.
   - Enforced eligibility strictly server-side on hostel lists, hostel details, room detail views, room inventory views, bed rosters, and allocation services (`assign_student_to_bed`).
   - Provided informative UI feedback: unverified students receive explanatory warning banners and have bed selection locked until verified by the Welfare Office or Registrar.
3. **17-Hostel Standard Inventory (13,600 Beds) & Non-Destructive Seeding**:
   - Commissioned the campus residential inventory:
     - **10 Boys' Hostels** (`BH-01` through `BH-10`): 10 floors, 20 rooms/floor (rooms 101–120 through 1001–1020), 4 beds/room (`Bed A`, `Bed B`, `Bed C`, `Bed D`) = **8,000 beds**.
     - **7 Girls' Hostels** (`GH-01` through `GH-07`): 10 floors, 20 rooms/floor, 4 beds/room = **5,600 beds**.
     - **Total Seeded Inventory**: 17 hostels, 170 floors, 3,400 rooms, 13,600 beds.
     - **Overall Campus Inventory**: 20 hostels (17 seeded collection + 3 preserved historical demo hostels: `North Residence` [male], `South Residence` [female], `Scholars Hall` [postgraduate]).
   - Enhanced `seed_hostel_inventory`: Rejects invalid `--institution-code` with explicit errors (zero silent fallback), and preserves Dean customizations (deactivated beds, custom capacities, maintenance states) on re-runs.
4. **Polished Hostel Explorer**: Category tabs (All, Boys, Girls), student eligibility banner, name/code search, responsive cards with floor counts, room counts, bed capacities, occupied/available counts, text occupancy status labels, and Dean/Admin builder shortcuts.
5. **Interactive Floor, Room & Bed Navigation**: Block and floor selector (Floors 1–10), 20-room active floor grid, dynamic bed layouts with distinct states (Available, Occupied, Under Maintenance, Selected preview), and strict student privacy preservation (other students' identities remain masked as "Occupied").
6. **On-Demand Performance & Accessibility**: Eliminated Cartesian join blowout by loading floor rooms and beds on demand (20 rooms / 80 beds per page view instead of loading 13,600 beds). Smooth 150–250ms CSS transitions respecting `@media (prefers-reduced-motion: reduce)`.
7. **Full Test Suite & Clean Diagnostics**: 68 out of 68 tests passing across all test modules with 0 failures and 0 warnings.

---

## 2. Category Restriction & Academic Level Separation

### 2.1 Policy Enforcement
RoomLoop supports exactly two residential categories:
- **Boys' Hostel** (`resident_category = "male"`)
- **Girls' Hostel** (`resident_category = "female"`)

### 2.2 Academic Level Decoupling
Academic level eligibility is distinct from residential gender category. To cleanly decouple them:
- Added `AcademicLevel` TextChoices (`ALL`, `UNDERGRADUATE`, `POSTGRADUATE`) to `Hostel`.
- Added `academic_level = models.CharField(max_length=20, choices=AcademicLevel.choices, default=AcademicLevel.ALL, db_index=True)` to `apps/hostels/models.py`.
- Applied migration `apps/hostels/migrations/0004_hostel_academic_level.py`.
- Preserved `Scholars Hall` (`SH-01`) academic status: `academic_level = "postgraduate"`.

### 2.3 Status of Scholars Hall (`SH-01`) & Database Allowlist Constraint
- Existing database records:
  - 10 Seeded Boys' Hostels: `resident_category = "male"`
  - 1 Demo Boys' Hostel (`North Residence`): `resident_category = "male"`
  - 7 Seeded Girls' Hostels: `resident_category = "female"`
  - 1 Demo Girls' Hostel (`South Residence`): `resident_category = "female"`
  - 1 Legacy Demo Hostel (`Scholars Hall`): `resident_category = "postgraduate"`
- The current database check constraint `check_hostel_resident_category_not_coed` prevents `coed` hostels.
- The stricter allowlist constraint `resident_category IN ('male', 'female')` is prepared. Applying it before confirming whether Scholars Hall is Boys' or Girls' would violate database integrity or force an unverified guess.
- **Pending Decision**: Awaiting user confirmation whether Scholars Hall (`SH-01`) should be mapped to `male` (Boys') or `female` (Girls'). Once confirmed, the record will be updated and the allowlist constraint migration applied.

### 2.4 Server-Side Services & Forms Enforcement
- `create_hostel` and `update_hostel` in `apps/hostels/services.py`: Strictly validate `resident_category in ("male", "female")`. Rejects `"coed"`, `"postgraduate"`, `"unknown"`, or any other value with `ValidationError`.
- `save_custom_hostel_configuration`: Rejects unsupported categories and forbids altering categories when active student allocations exist.
- `HostelForm` in `apps/hostels/forms.py`: Restricts dropdown choices strictly to `ACTIVE_RESIDENT_CATEGORIES` (`male`, `female`).
- `hostel_builder.html`: Renders only `Boys' Hostel` and `Girls' Hostel` radio choices.

---

## 3. Authoritative Student Residential Eligibility

### 3.1 Architecture & Model (`apps/accounts/models.py`)
Residential eligibility cannot be inferred from occupancy (unallocated students have no occupancy) or names. An authoritative field is required:
- Added `ResidentialEligibility` TextChoices:
  - `MALE` (`"male"`): Verified for Boys' Hostels.
  - `FEMALE` (`"female"`): Verified for Girls' Hostels.
  - `UNVERIFIED` (`"unverified"`): Pending institutional verification.
- Added `residential_eligibility` field to `User` with `default=ResidentialEligibility.UNVERIFIED`.
- Applied migration `apps/accounts/migrations/0003_user_residential_eligibility.py`.

### 3.2 Authoritative Selector (`apps/hostels/selectors.py`)
Implemented `get_student_verified_eligibility(user)`:
- Returns `"male"` if `user.residential_eligibility == "male"`.
- Returns `"female"` if `user.residential_eligibility == "female"`.
- Returns `None` for unverified students or non-student roles.
- Never infers eligibility from occupancy, names, or guesses.

### 3.3 Server-Side Enforcement Across Views & Services
1. **Hostel Explorer (`hostel_list_view`)**:
   - Verified students are strictly scoped to their eligible hostels (`hostels = hostels.filter(resident_category=verified_category)`). Query-string overrides (e.g. `?category=female` by a male student) are ignored.
   - Unverified students see an informative warning banner explaining that their profile requires Welfare Office / Registrar verification.
2. **Hostel Detail (`hostel_detail_view`)**:
   - Verified student accessing an opposite-gender hostel URL directly is blocked with HTTP 403 `PermissionDenied`.
   - Unverified students can view the hostel for informational purposes, but bed slot selection is disabled (`can_select_beds = False`).
3. **Room Detail (`room_detail_view`)**:
   - Verified student accessing an opposite-gender room URL directly is blocked with HTTP 403 `PermissionDenied`.
4. **Room Inventory & Bed Rosters (`room_inventory_view`, `bed_list_view`)**:
   - Verified students can only list rooms matching their verified category; unverified students see empty querysets.
   - Direct access to `bed_list_view` by students is blocked with HTTP 403 `PermissionDenied` (reserved for staff).
5. **Allocation Service & Form (`assign_student_to_bed`, `OccupancyAssignForm`)**:
   - Rejects allocation with `ValidationError` if the student has `residential_eligibility == "unverified"`.
   - Rejects allocation with `ValidationError` if the student's verified eligibility does not match the hostel's `resident_category`.

---

## 4. Seeded 17-Hostel Inventory Specifications

### 4.1 Inventory Breakdown

| Category | Hostel Codes | Names | Floors / Hostel | Rooms / Floor | Beds / Room | Beds / Hostel | Total Hostels | Total Beds |
|:---|:---|:---|:---:|:---:|:---:|:---:|:---:|:---:|
| **Boys' Hostel** | `BH-01` to `BH-10` | Boys Hostel 01 – Boys Hostel 10 | 10 | 20 | 4 (`A`, `B`, `C`, `D`) | 800 | 10 | **8,000** |
| **Girls' Hostel** | `GH-01` to `GH-07` | Girls Hostel 01 – Girls Hostel 07 | 10 | 20 | 4 (`A`, `B`, `C`, `D`) | 800 | 7 | **5,600** |
| **Seeded Total** | — | — | **170** | **3,400** | — | — | **17** | **13,600** |

### 4.2 Canonical Room Numbering
Rooms are deterministically numbered per floor:
$$\text{Floor } f \implies \text{Rooms } (f \times 100 + 1) \text{ through } (f \times 100 + 20)$$
- Floor 1: Rooms 101 to 120
- Floor 2: Rooms 201 to 220
- ...
- Floor 10: Rooms 1001 to 1020

### 4.3 Bed Labels & Types
- Room Type: `Room.RoomType.FOUR` ("4-Bed Room").
- Bed Labels: `Bed A`, `Bed B`, `Bed C`, `Bed D`.
- Operational Status: `OperationalStatus.IN_SERVICE` (`in_service`).

### 4.4 Preservation of Demo Hostels
The 3 pre-existing demonstration hostels were preserved intact:
1. `North Residence` (`NR-01`): Male hostel, 2 floors, 4 rooms, 14 beds.
2. `South Residence` (`SR-01`): Female hostel, 2 floors, 4 rooms, 14 beds.
3. `Scholars Hall` (`SH-01`): Postgraduate hostel, 2 floors, 4 rooms, 14 beds (`academic_level = "postgraduate"`).

**Total Hostels in Campus Database:** $17 + 3 = 20\text{ Hostels}$ (13,642 total beds).

---

## 5. Seeding Management Command (`seed_hostel_inventory`)

### 5.1 CLI Usage
```bash
# Dry run simulation (no changes committed to database)
python manage.py seed_hostel_inventory --dry-run

# Targeted institution seeding
python manage.py seed_hostel_inventory --institution-code DEMO-UNIV

# Live execution against PostgreSQL 16
python manage.py seed_hostel_inventory
```

### 5.2 Seeding Safety & Customization Preservation
1. **Explicit Institution Scoping**:
   - If `--institution-code` is provided and invalid: raises `CommandError` with list of valid institution codes (zero silent fallback).
   - If omitted: auto-resolves only if exactly one institution exists; otherwise raises `CommandError`.
2. **Preservation of Dean & Administrator Customizations**:
   - When a seeded hostel already exists in the database, the seeder detects it and preserves all modifications (custom room capacities, deactivated beds, maintenance states, renamed blocks) rather than re-injecting the default layout.
3. **Separate Metric Reporting**:
   - Reports seeded collection totals (17 hostels, 170 floors, 3,400 rooms, 13,600 beds) distinctly from total database inventory (20 hostels, 13,642 beds).
4. **Strict Transactional Safety**:
   - Wrapped in `@transaction.atomic`.
   - Idempotent natural key resolution prevents duplicate objects.
   - Logs `AuditLog` entry with action `HOSTEL_INVENTORY_SEEDED`.

---

## 6. Verification & Automated Test Suite

### 6.1 Full Regression Suite Results
Executed across all test suites against PostgreSQL 16:

```text
======================= 68 passed in 129.93s (0:02:09) ========================
```

| Test Module | Tests | Status | Scope |
|:---|:---:|:---:|:---|
| `tests/test_authentication.py` | 5 | **PASS** | Login, password resets, session invalidation |
| `tests/test_authorization.py` | 5 | **PASS** | Role boundaries, cross-institution isolation |
| `tests/test_builder.py` | 16 | **PASS** | Builder RBAC, concrete creation, concurrency, anti-tampering |
| `tests/test_dashboard_flows.py` | 20 | **PASS** | All 6 role dashboards, empty states, capacity metrics |
| `tests/test_inventory.py` | 16 | **PASS** | Boys/Girls enforcement, academic level separation, seeder dry-run/idempotency/preservation, server-side eligibility enforcement |
| `tests/test_occupancy.py` | 6 | **PASS** | Bed allocation, double occupancy prevention, release history, verified eligibility validation |
| **Total** | **68** | **ALL PASSED** | **100% test pass rate** |

### 6.2 System Diagnostics
- `python manage.py check`: `System check identified no issues (0 silenced).`
- `python scripts/audit_roles.py`: All 6 roles verified with 100% compliance across authentication, RBAC boundaries, and dashboards.
- `pytest.ini`: Resolved `PytestConfigWarning: Unknown config option: django_settings_module` by aligning pytest configuration with `conftest.py` manual Django bootstrap.

---

## 7. Pending Classification Decision

> [!IMPORTANT]
> **Action Required: Scholars Hall (`SH-01`) Classification**
> 
> `Scholars Hall` (`SH-01`) currently has:
> - `academic_level`: `"postgraduate"` (preserved)
> - `resident_category`: `"postgraduate"` (legacy value)
> 
> The database check constraint `resident_category IN ('male', 'female')` is staged and ready to be applied once you specify whether `Scholars Hall` is:
> 1. **Boys' Hostel** (`male`)
> 2. **Girls' Hostel** (`female`)
> 
> All services, forms, views, and validations currently enforce only `male` and `female` for all new and edited records.

---

## 8. File Map & Artifact Links

- [apps/accounts/models.py](file:///c:/Users/gudel/Hostel-Room-Exchange-Portal/apps/accounts/models.py): Added `ResidentialEligibility` (`male`, `female`, `unverified`) to `User`.
- [apps/accounts/migrations/0003_user_residential_eligibility.py](file:///c:/Users/gudel/Hostel-Room-Exchange-Portal/apps/accounts/migrations/0003_user_residential_eligibility.py): Migration adding student residential eligibility field.
- [apps/hostels/models.py](file:///c:/Users/gudel/Hostel-Room-Exchange-Portal/apps/hostels/models.py): Category choices, `AcademicLevel` TextChoices, `academic_level` CharField, check constraint.
- [apps/hostels/migrations/0004_hostel_academic_level.py](file:///c:/Users/gudel/Hostel-Room-Exchange-Portal/apps/hostels/migrations/0004_hostel_academic_level.py): Safe schema migration adding academic level field.
- [apps/hostels/selectors.py](file:///c:/Users/gudel/Hostel-Room-Exchange-Portal/apps/hostels/selectors.py): Authoritative eligibility selector `get_student_verified_eligibility`.
- [apps/hostels/services.py](file:///c:/Users/gudel/Hostel-Room-Exchange-Portal/apps/hostels/services.py): Strict two-category validation and authoritative student eligibility checks on bed allocation.
- [apps/hostels/forms.py](file:///c:/Users/gudel/Hostel-Room-Exchange-Portal/apps/hostels/forms.py): Form validation for categories and student allocation eligibility.
- [apps/hostels/management/commands/seed_hostel_inventory.py](file:///c:/Users/gudel/Hostel-Room-Exchange-Portal/apps/hostels/management/commands/seed_hostel_inventory.py): Safe 17-hostel seeder command with customization preservation and explicit institution scoping.
- [apps/hostels/views.py](file:///c:/Users/gudel/Hostel-Room-Exchange-Portal/apps/hostels/views.py): Server-side eligibility filtering, detail route access protection, and locked bed state handling.
- [templates/hostels/hostel_list.html](file:///c:/Users/gudel/Hostel-Room-Exchange-Portal/templates/hostels/hostel_list.html): Responsive explorer with verified student eligibility banner and unverified warning.
- [templates/hostels/hostel_detail.html](file:///c:/Users/gudel/Hostel-Room-Exchange-Portal/templates/hostels/hostel_detail.html): Interactive floor explorer with locked bed slots and student privacy preservation.
- [tests/test_inventory.py](file:///c:/Users/gudel/Hostel-Room-Exchange-Portal/tests/test_inventory.py): Automated tests for two-category rules, academic level decoupling, seeding safety, and student eligibility.
- [tests/test_occupancy.py](file:///c:/Users/gudel/Hostel-Room-Exchange-Portal/tests/test_occupancy.py): Automated tests for occupancy and verified eligibility requirements.
- [pytest.ini](file:///c:/Users/gudel/Hostel-Room-Exchange-Portal/pytest.ini): Clean test configuration without unknown option warnings.
