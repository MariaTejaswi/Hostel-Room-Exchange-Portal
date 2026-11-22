# RoomLoop System Architecture (Phase 1)

## 1. Architectural Philosophy

RoomLoop is built as a **modular Django monolith** adhering to strict enterprise design patterns:
1. **Thin Views**: Views handle HTTP dispatch, validation error rendering, session flow, and redirection.
2. **Fat Services (`services.py`)**: All business logic, transaction boundaries (`@transaction.atomic`), row-locking (`select_for_update`), and audit logging reside strictly in service layers.
3. **Reusable Selectors (`selectors.py`)**: Database query logic, aggregate counts, filtered annotations, and role-scoped querysets are decoupled from views to maximize reuse and eliminate N+1 queries.
4. **Server-Rendered SaaS Design**: Modern, responsive UI built with Django Templates, local Tailwind CSS build pipeline, and lightweight Alpine.js/Vanilla JS.
5. **PostgreSQL-First Reliability**: Relational database constraints (conditional unique indexes, check constraints) guarantee data integrity under high concurrent loads.

---

## 2. Directory Structure

```text
roomloop/
├── config/
│   ├── settings/
│   │   ├── base.py              # Shared core settings
│   │   ├── development.py       # Development environment configuration
│   │   └── production.py        # WhiteNoise, SSL, HSTS, security headers
│   ├── urls.py                  # Primary URL routing
│   ├── asgi.py                  # ASGI async entrypoint
│   ├── wsgi.py                  # WSGI web entrypoint
│   └── celery.py                # Celery & Beat scheduler configuration
│
├── apps/
│   ├── core/                    # Institution & AuditLog models, context processors, commands
│   │   ├── models.py
│   │   ├── admin.py
│   │   ├── context_processors.py
│   │   └── management/commands/seed_demo.py
│   ├── accounts/                # Custom User, Groups, Roles, Auth, Profiles
│   │   ├── models.py
│   │   ├── views.py
│   │   ├── urls.py
│   │   ├── forms.py
│   │   ├── services.py
│   │   ├── selectors.py
│   │   └── admin.py
│   └── hostels/                 # Hostels, Blocks, Floors, Rooms, Beds, Occupancy
│       ├── models.py
│       ├── views.py
│       ├── urls.py
│       ├── forms.py
│       ├── services.py
│       ├── selectors.py
│       └── admin.py
│
├── templates/
│   ├── layouts/                 # base.html, auth.html, dashboard_base.html
│   ├── components/              # sidebar, topbar, stats_card, data_table, badges
│   ├── accounts/                # login, profile, password_reset, password_change
│   ├── dashboards/              # student, warden, chief_warden, hostel_admin, dean
│   └── hostels/                 # hostel_list, room_inventory, room_detail, occupancy
│
├── static/
│   ├── css/dist/styles.css      # Compiled Tailwind CSS design system
│   ├── js/main.js               # Drawer, dropdown, toast auto-dismiss scripts
│   └── images/logo.png          # RoomLoop brand icon
│
├── frontend/
│   ├── src/input.css            # Tailwind directives and custom component classes
│   └── tailwind.config.js       # Color tokens, fonts, spacing configuration
│
├── tests/                       # Pytest test suite (auth, permissions, inventory, occupancy)
├── docs/                        # Architecture, Schema, Permission Matrix
├── requirements/                # base.txt, local.txt, production.txt
├── .github/workflows/ci.yml     # Automated linting, checks, tests
├── Dockerfile                   # Multi-stage production container
├── docker-compose.yml           # Django + PostgreSQL 16 + Redis + Celery + Beat
└── manage.py
```

---

## 3. Exchange Schema Foundation

Phase 1 of the exchange implementation adds the schema foundation in `apps.exchange`.
`ExchangeRequest` reuses the existing active `StudentOccupancy` record and can target
hostel, block, floor, room type, occupancy category, and accessibility preferences.
`ExchangeProposal` and `ProposalParticipant` support direct swaps and cycles from two
through five participants. Active participant reservations use conditional unique
constraints on student, occupancy, and request so overlapping proposals cannot reserve
the same records.

`Approval`, `TransferRecord`, `PolicyRule`, and `Notification` provide the persistent
relationships required by later workflow phases. This phase intentionally does not add
matching, acceptance, approval, transfer, expiry, or notification delivery.

Phase 2 adds student-owned request management in `apps.exchange.services` and thin
student views. Creation and editing require an active, institution-scoped occupancy,
verified residential eligibility, compatible hostel preferences, and the configured
cooling-off policy. Fee clearance and disciplinary holds are checked through the
`ExternalEligibilityAdapter` interface in `apps.exchange.eligibility`; the default
adapter fails closed when an institution has enabled a check without supplying an
integration, so no external status is invented.

Phase 3 adds the directed graph matcher in `apps.exchange.matching`. Open eligible
requests are vertices, and an edge from request A to request B means A can receive
B's current bed. Canonical simple cycles from two through five requests become
pending-acceptance proposals after eligibility is revalidated. Proposal participants
reserve requests and students through the existing conditional constraints; no
occupancy transfer occurs during matching. Active proposals also store a canonical
cycle signature to prevent duplicate cycles.

The request form also supports an optional exact `preferred_bed` selection. It reuses
the existing hostel inventory hierarchy and allows occupied, in-service beds as swap
targets while excluding maintenance, inactive, cross-institution, and residentially
incompatible beds. The selected bed remains a preference only; no occupancy is changed
until a later transfer workflow.

## 4. Preparation for Future Module Integration

In Phase 1, we implemented:
- **M0**: Authentication and User Management (Custom User, Groups, Scoped Roles)
- **M8**: Hostel and Occupancy Management (Relational Inventory & Live Student Occupancy)

### How Future Exchange Modules Will Connect:
1. **M1 — Exchange Request Management**:
   Student request creation, listing, detail, editing, and withdrawal are implemented.
   The service reuses the existing active `StudentOccupancy` record and rejects requests
   without an eligible occupancy or with an ownership mismatch.
2. **M3 — Matching Engine**:
   The graph-based matching algorithm will query vacant beds or compatible student preferences, without altering active occupancy until final approval.
3. **M5 & M6 — Approval Workflow & Transfer Execution**:
   Upon warden approval of a 2-way direct swap or N-way multi-party cycle, the execution engine calls the existing Phase 1 service `apps.hostels.services.release_student_occupancy` with `source='direct_swap'` / `'multi_swap'` followed by `assign_student_to_bed` in a single atomic transaction. No schema restructuring is required.
