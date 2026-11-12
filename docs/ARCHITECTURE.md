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
│   └── images/logo.svg          # RoomLoop brand icon
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

## 3. Preparation for Phase 2 Module Integration

In Phase 1, we implemented:
- **M0**: Authentication and User Management (Custom User, Groups, Scoped Roles)
- **M8**: Hostel and Occupancy Management (Relational Inventory & Live Student Occupancy)

### How Future Exchange Modules Will Connect:
1. **M1 — Exchange Request Management**:
   A student initiating a swap request will query `apps.hostels.selectors.get_student_active_occupancy(student)`. If no active occupancy exists, the exchange request creation is rejected by policy validation (M2).
2. **M3 — Matching Engine**:
   The graph-based matching algorithm will query vacant beds or compatible student preferences, without altering active occupancy until final approval.
3. **M5 & M6 — Approval Workflow & Transfer Execution**:
   Upon warden approval of a 2-way direct swap or N-way multi-party cycle, the execution engine calls the existing Phase 1 service `apps.hostels.services.release_student_occupancy` with `source='direct_swap'` / `'multi_swap'` followed by `assign_student_to_bed` in a single atomic transaction. No schema restructuring is required.
