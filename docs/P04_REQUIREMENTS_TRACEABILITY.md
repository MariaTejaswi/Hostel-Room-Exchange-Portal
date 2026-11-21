# P04 Requirements Traceability and Verification Audit

**Audit date:** 20 November 2026  
**Authoritative source:** `docs/EduRev Project List - Dual Technology Stack.pdf` (the filename uses an en dash)  
**Project path:** `C:\Users\gudel\Hostel-Room-Exchange-Portal`  
**Delivery path:** Track P, Path P1 - Django templates, Django 5.x, PostgreSQL

## 1. Scope and requirement sources

This document distinguishes three kinds of requirements:

1. **PDF requirements** - EduRev Common Engineering Standard (CES), printed pages 1-5, and P04, printed pages 18-20.
2. **Confirmed RoomLoop decisions** - project-specific authority, inventory, UI, and data-preservation rules supplied by the project owner. These override older project notes where they differ from the PDF's generic wording.
3. **Optional improvements** - useful work that is not presented as a mandatory PDF requirement.

P03 is used only for its occupancy contract. P03 printed page 16 says it publishes an occupancy API consumed by P04; P04 printed page 19 permits bed and occupancy state to be read from P03's data **or** through its API. RoomLoop uses a shared relational occupancy module rather than a separate P03 service.

Status meanings:

- **Completed** - implementation and relevant automated evidence exist.
- **Partial** - some implementation or evidence exists, but the full requirement is not demonstrated.
- **Missing** - required capability or evidence was not found.
- **Unverified** - implementation may exist, but the required environment or acceptance evidence was not available.

## 2. Confirmed RoomLoop decisions

| Decision | Source | Implementation | Status |
|---|---|---|---|
| Same-hostel exchanges and transfers are decided only by the assigned hostel Warden. | Confirmed project decision | `apps/exchange/selectors.py:get_approvals_queryset`, `can_user_decide_proposal`, `can_user_decide_vacant_transfer`; `apps/exchange/services.py:decide_exchange_proposal`, `execute_exchange_transfer`, `approve_and_execute_vacant_transfer`, `reject_vacant_transfer` | Completed |
| Cross-hostel exchanges and transfers are decided only by the Chief Warden; no two-Warden step. | Confirmed project decision; compatible with P04 role split on pp. 18-19 | Same approval selectors and services; focused approval, vacant-transfer, and direct-exchange tests | Completed |
| Institution and assigned-hostel scoping remain server-side. | Confirmed project decision | Selectors, service permission checks, `WardenAssignment`, institution comparisons | Completed |
| Exchange-policy editing is restricted to the Hostel Administrator. Ordinary Wardens and Chief Wardens cannot open or submit the policy settings view and cannot call the policy-update service. | Confirmed project decision | `exchange_policy_settings_view`; `update_institution_exchange_policy`; `test_unauthorized_roles_denied_policy_settings_view_and_service` | Completed and reverified on PostgreSQL |
| Boys and Girls hostel classifications remain separate; standard seed is BH-01..BH-10 and GH-01..GH-07. | Confirmed project decision | `apps/hostels/models.py`, `seed_hostel_inventory.py` | Completed for standard seed definitions |
| Additional legitimate hostels are not deleted to force a total of 17. | Confirmed project decision | Seeder preserves existing non-standard hostels; this audit made no writes | Completed |
| Preserve coffee-brown/maroon Warden UI, interaction states, responsiveness, and static hostel badges. | Confirmed project decision | Existing dashboard templates and `frontend/src/input.css`; no dashboard redesign in this task | Completed / unchanged |
| Never reset, flush, reseed, or mutate live allocation and occupancy data during this audit. | Confirmed project decision | Only aggregate SELECT queries were run against live PostgreSQL | Completed |

## 3. P04 functional modules (M1-M7)

### M1 - Exchange Request Management

**PDF source:** P04 sections 3-5, printed p. 18: create, modify, withdraw, expire requests; target preferences and request history.

| Requirement | Implementation and tests | Status | Remaining action |
|---|---|---|---|
| Create requests with target preferences | `apps/exchange/services.py:create_exchange_request`; `forms.py:ExchangeRequestForm`; request views/templates; `tests/test_exchange_requests.py` | Completed | None |
| Modify an owned open request | `update_exchange_request`; edit view; ownership and state checks | Completed | None |
| Withdraw an owned request | `withdraw_exchange_request`; withdraw view; reservation cleanup | Completed | None |
| Expire requests automatically | `expire_exchange_requests`; Celery task and Beat schedule; indexed `expires_at` | Completed | Production Beat execution remains an operational verification item |
| Direct occupied-bed invitation without a reciprocal request | `create_exchange_request`, `activate_direct_occupied_bed_request`; `test_direct_occupied_bed_exchange.py` | Completed | None |
| Request history | Request list/detail plus persistent status/audit events | Completed | Reporting aggregation remains M7 work |

### M2 - Eligibility and Policy Validation

**PDF source:** P04 sections 3-5 and 8-12, printed pp. 18-19.

| Requirement | Implementation and tests | Status | Remaining action |
|---|---|---|---|
| Cooling-off, request validity, acceptance window | `PolicyRule`, `eligibility.py`, policy form/service, expiry tasks | Completed | None |
| Fee status and disciplinary holds | `ExternalEligibilityAdapter`; fail-closed validation and mock-adapter tests | Partial | Implement and contract-test real ERP adapters |
| Gender/block and residential eligibility rules | `accounts.User.residential_eligibility`; hostel selectors/services; exchange eligibility and matching revalidation | Completed | Resolve the legacy SH-01 classification through a separately authorized data decision |
| Policy validation with reasons | `ExchangeEligibilityReport` and structured checks | Completed | None |
| Cooling-off and rules configurable without deployment | Hostel Administrator policy UI and audited service | Completed | None |
| Maximum chain length configurable from 2 through 5 | Policy UI -> server validation -> `PolicyRule` -> matcher -> activation; default 5 | Completed in this revision | None |

### M3 - Matching Engine

**PDF source:** P04 sections 3-5 and 7-8, printed pp. 18-19: direct swaps, cycles 3-5, bounded to 5, under 5 seconds for 2,000 open requests.

| Requirement | Implementation and tests | Status | Remaining action |
|---|---|---|---|
| Direct swaps and canonical 3-, 4-, and 5-party cycles | `matching.py:build_exchange_graph`, `find_exchange_cycles`, `build_candidate_matches`; matching tests | Completed | None |
| Hard upper bound of 5 | Model check constraint and matcher validation | Completed | None |
| Institution-configured maximum 2-5 used by every entry point | `get_institution_max_cycle_length`, `get_effective_max_cycle_length`; service, background worker, CLI, and compatibility helper all converge on `find_candidate_matches` | Completed in this revision | None |
| Draft activation revalidates current policy | `activate_exchange_proposal` rejects an over-limit draft and leaves it unchanged | Completed in this revision | None |
| Existing active proposal after a policy decrease | Active proposals continue unchanged; policy affects future matching and draft activation only | Completed and documented | Add a direct regression assertion for an already-active proposal if desired |
| 2,000 open requests matched in under 5 seconds | `test_postgresql_matching_performance.py`; `P04_MATCHING_PERFORMANCE.md`. Six isolated-PostgreSQL runs include policy resolution, ORM loading, eligibility, graph construction, cycle discovery, explanations, and candidate materialization. Structured median/slowest: 0.991/1.247 s; dense-preference median/slowest: 1.835/2.048 s. | Completed on documented local hardware | Dense search reaches and discloses the existing 10,000-candidate limit; persistence and concurrent-user load are separate concerns. |

### M4 - Multi-Party Acceptance

**PDF source:** P04 sections 4-5, 8, 11 and 15, printed pp. 18-19.

| Requirement | Implementation and tests | Status | Remaining action |
|---|---|---|---|
| All affected students accept | `respond_to_proposal`; only unanimous acceptance reaches pending approval | Completed | None |
| Any decline closes the workflow without moving occupancy | Decline path releases reservations; acceptance tests | Completed | None |
| Partial acceptance never changes occupancy | `test_multi_party_acceptance.py` explicitly asserts all physical occupancies remain unchanged | Completed on SQLite test backend | Include in PostgreSQL acceptance run |

### M5 - Approval Workflow

**PDF source:** P04 sections 5-6 and 9, printed pp. 18-19. RoomLoop's confirmed authority rule specializes the generic responsible-authority requirement.

| Requirement | Implementation and tests | Status | Remaining action |
|---|---|---|---|
| Same-hostel assigned-Warden-only approval/rejection | Approval selectors and services, Warden assignment checks | Completed | None |
| Cross-hostel Chief-Warden-only approval/rejection | Approval selectors and services reject hostel Wardens | Completed | None |
| Rejection remarks | Required by `decide_exchange_proposal` and vacant-transfer rejection | Completed | None |
| Queue, count, decision UI, backend, and execution use the same authority | Shared selectors/services and dashboard context | Completed | None |
| Dean read-only analytics; Hostel Administrator policy-only | Selector and service permission checks | Completed | Reporting content is incomplete under M7 |

### M6 - Occupancy Update & Transfer Record

**PDF source:** P04 sections 4-8 and 15, printed pp. 18-19: one atomic block, ordered `select_for_update`, transfer records and letters.

| Requirement | Implementation and tests | Status | Remaining action |
|---|---|---|---|
| Ordered row locks and atomic rotation | `execute_exchange_transfer`; `docs/P04_LOCKING_STRATEGY.md`; PostgreSQL `TransactionTestCase` uses real concurrent threads, barriers, distinct backend PIDs, and bounded database/test timeouts | Completed on isolated PostgreSQL | Keep the PostgreSQL test command in CI |
| Roll back the whole rotation on stale/invalid state | Existing SQLite cases plus `test_mid_execution_failure_rolls_back_and_retry_is_idempotent`, which raises during the second transfer-record write after occupancy mutation has begun | Completed on isolated PostgreSQL | None |
| Idempotent repeated execution | `test_duplicate_concurrent_approval_and_execution_is_idempotent` and the rollback/retry test prove one approval, one movement per participant, and stable record IDs on replay | Completed on isolated PostgreSQL | None |
| Transfer record per participant | `TransferRecord`, uniqueness constraints, execution tests | Completed | None |
| Letter generation | `documents.py:generate_transfer_letter_pdf`, protected download view | Completed functionally | CES specifically prefers WeasyPrint; current custom renderer is a CES deviation |
| Four-party rotation request -> proposal -> acceptance -> approval -> execution | Service-level tests cover detection and 4-party rotation execution | Partial acceptance evidence | Add one explicit full-journey test connecting every stage, preferably Playwright plus PostgreSQL service assertion |
| Concurrent overlapping approvals cannot corrupt occupancy | `test_overlapping_destination_approvals_cannot_partially_rotate`, duplicate same-proposal race, and independent-exchange race use synchronized threads and independent PostgreSQL connections. Active-reservation constraints prevent two valid proposals from sharing a student/current occupancy/request; the destination-overlap case proves the valid cycle commits and the non-closed competing cycle rolls back without partial movement. | Completed on isolated PostgreSQL | None |
| Exact reconciliation after 100 simulated transfers | `test_one_hundred_exchanges_reconcile_after_every_execution` executes 50 direct swaps and 50 three-party rotations, reconciling after every exchange | Completed on isolated PostgreSQL: 100 exchanges, 250 student movements | None |

### M7 - Reporting & Audit

**PDF source:** P04 sections 5, 9-10 and 22, printed pp. 18-20.

| Requirement | Implementation and tests | Status | Remaining action |
|---|---|---|---|
| Complete exchange audit visibility | `AuditLog` request/proposal/decision/execution/policy events are exposed through a privacy-minimal, paginated, institution/hostel-scoped table with selector-authorized detail links | Completed at application level | Database-enforced append-only storage and universal before/after values remain a CES gap |
| Request volume by hostel and period | `reporting.py:build_exchange_report`; source-hostel and local-date aggregation; all request states; accessible chart/table; deterministic boundary and institution-isolation tests | Completed | None |
| Match rate | Distinct request-cohort numerator, occupied-exchange denominator, DRAFT exclusion, terminal-state history preservation, zero-denominator N/A handling | Completed | Older deleted proposal history cannot be reconstructed |
| Average time to completion | Average of request `submitted_at` to durable movement `transferred_at`; distinct exchange/movement counts; missing-start samples disclosed | Completed | Historical records without a request link remain unavailable rather than guessed |
| Approval/rejection patterns by deciding authority | Decided `Approval` records grouped by actual `authority_user` plus stored `authority_role`; repeated audit attempts do not fabricate decisions | Completed | None |
| Most-requested rooms | Explicit `preferred_bed` requests grouped by destination room; broad preferences reported separately | Completed | A physically deleted legacy `SET_NULL` target cannot be distinguished from an originally broad request |
| Dean read-only analytics | `/exchange/reports/` is institution-wide read-only for Dean; Chief Warden is institution-wide; ordinary Warden is assigned-hostel scoped; Hostel Administrator, Student, and Sysadmin denied | Completed | None |
| CES report response under 3 seconds | Isolated PostgreSQL rendered response over 2,008 actual requests: 0.177997 s, 34 queries; selector report: 0.056812 s, 14 queries | Completed on documented local hardware | Production browser/network performance remains unverified |

## 4. P04 integrations and communications

**PDF source:** P04 sections 11-12, printed p. 19.

| Requirement | Status | Evidence / gap |
|---|---|---|
| Proposal notifications (a separate P04 requirement, not module M4) | Partial | `dispatch_proposal_notification` creates in-app records with deadlines and attempts configured delivery; production email/push delivery evidence remains incomplete. |
| P03 occupancy integration (mandatory) | Completed as a shared interface | P04 reads and atomically updates the shared `apps.hostels.StudentOccupancy` ledger. This is the PDF-permitted shared-data option, not a network API. |
| ERP fee and disciplinary status | Partial | Adapter protocol and fail-closed behavior exist; no production ERP implementation. |
| Email | Partial | Django email delivery code and tests exist; production provider and delivery evidence are unverified. |
| Push | Missing | Provider detection exists, but the function explicitly reports that integration is pending. |
| SMS | Missing | No implementation found. The PDF lists SMS as a third-party integration but marks proposal notifications specifically as push and email. |
| University SSO | Missing | No `django-allauth`/OIDC integration found. |
| Admin TOTP MFA | Missing | Required by CES p. 1; no implementation found. |

## 5. P04 acceptance criteria

**PDF source:** P04 section 22, printed p. 20.

| Acceptance criterion | Status | Actual evidence |
|---|---|---|
| Four-party rotation detected, proposed, accepted, and executed end-to-end | Partial | Detection and service execution evidence exists, including a concurrent four-party PostgreSQL execution; no single browser journey connects request creation, matching, acceptance, approval, and execution. |
| Partial acceptance never mutates occupancy | Completed on SQLite | Explicit assertions in `test_multi_party_acceptance.py`. |
| Concurrent overlapping-chain approval cannot corrupt occupancy | Completed on isolated PostgreSQL | `PostgreSQLExchangeCorrectnessTests` uses `TransactionTestCase`, synchronized real threads, distinct PostgreSQL backend PIDs, lock/statement/future timeouts, and no mocked row locking. Overlapping destinations, duplicate calls, and independent exchanges preserve occupancy and transfer-record invariants. |
| P03 occupancy reconciles exactly after 100 simulated transfers | Completed on isolated PostgreSQL | 50 direct swaps plus 50 three-party rotations completed through real services: 100 completed exchanges and 250 individual student movements. Student allocation, bed uniqueness, occupancy history, request/proposal status, transfer records, and reservations were reconciled after every execution. |
| Cooling-off and policy rules configurable without deployment | Completed | Policy UI/service, including maximum chain length. |
| Cycle detection bounded to five and under 5 seconds for 2,000 open requests | Completed on isolated PostgreSQL | First run plus five repeats passed for both deterministic scenarios. Structured median/slowest: 0.991/1.247 s. Dense-preference median/slowest: 1.835/2.048 s with 198,000 edges and explicit `limit_reached=True` at 10,000 candidates. See `P04_MATCHING_PERFORMANCE.md`. |
| Complete audit trail per transfer | Completed at application level | Audit events and transfer records exist and are visible in the M7 report; database-enforced immutability is not proven. |

## 6. CES traceability

### 6.1 Stack and architecture

**PDF source:** CES sections 1.1-1.2, printed pp. 1-2.

| CES requirement | Status | Evidence / remaining action |
|---|---|---|
| Python 3.12, Django 5.x, Django templates, PostgreSQL 16 | Completed | Project settings, requirements, Docker and live DB vendor check. |
| Redis and Celery; Celery Beat for scheduled work | Completed in code | Worker and Beat services and schedules exist; production runtime unverified. |
| Documented CSS architecture/design tokens | Completed | Tailwind/configuration and UI documentation. |
| Docker Compose with web, worker, PostgreSQL and Redis | Partial | Services exist, but the web container uses development settings and `runserver`. |
| GitHub Actions: Ruff, pytest-django, migration check, build, deploy on merge | Partial | Lint, CSS build, checks and tests exist; deployment stage is missing. |
| Gunicorn/Nginx or managed platform; managed PostgreSQL | Partial/unverified | Gunicorn dependency and production settings exist; deployment configuration/evidence is absent. |
| WeasyPrint for PDF | Partial | Transfer letters are valid PDFs but use a custom renderer, not WeasyPrint. |
| One app per functional module; documented internal interfaces; no cross-module direct DB access | Partial | Service/selectors are strong, but P04 directly imports hostel models and M1-M7 are combined in one exchange app. Documented deviation required. |
| Environment configuration and no repository secrets, secret scanning in CI | Partial | Settings use environment variables and `.env` is ignored; no secret-scanning CI step found. |
| Idempotent migrations and seed scripts proven on an empty database before demo | Partial | Migrations and idempotent seed tests exist; current task did not run an empty-database demo. |
| Multi-tenant-ready records carry institution identifiers | Partial | Core domain records are institution-scoped; not every subordinate inventory row carries a direct institution column and instead inherits it. |

### 6.2 Non-functional baseline

**PDF source:** CES section 1.3, printed p. 3.

| Target | Status | Evidence / gap |
|---|---|---|
| 2,000 sustained / 5,000 peak concurrent users | Unverified | No Locust/k6 report. |
| API p95 <400 ms reads / <800 ms writes | Unverified | Path P1 has mostly HTML endpoints; no equivalent endpoint latency report. |
| Interactive dashboards/reports <3 s | Completed locally for P04 M7 | Guarded PostgreSQL response benchmark renders the report over 2,008 persisted requests in 0.177997 s with 34 queries. This is not a production-browser or concurrent-user result. |
| LCP <2.5 s on 4G | Unverified | No Lighthouse/WebPageTest evidence. |
| 99.5% academic-hours availability | Unverified | No uptime/SLO evidence. |
| Seven-year academic-record retention, configurable per entity | Missing | No retention/archival policy implementation. |
| Automated daily backup and documented tested restore | Missing | PostgreSQL volume exists; no backup job or restore evidence. |
| WCAG 2.2 AA | Partial | Semantic/accessibility work exists; no axe CI or formal WCAG report. |
| Last two Chrome/Edge/Firefox/Safari versions and 360 px | Partial | Responsive styles exist; browser-matrix evidence is absent. |
| English launch; Hindi and Punjabi strings externalized | Missing | `USE_I18N` is enabled, but strings/catalogs are not systematically externalized. |

### 6.3 Security baseline

**PDF source:** CES section 1.4, printed p. 3.

| Requirement | Status | Evidence / gap |
|---|---|---|
| OWASP Top 10 addressed and endpoint validation | Partial | Django forms, CSRF, RBAC, scoped selectors and secure production settings exist; no OWASP evidence package. |
| Rate limiting and brute-force lockout | Missing | No implementation found. |
| TLS 1.2+, encryption at rest, field encryption for sensitive fields | Partial/unverified | HTTPS/HSTS production settings exist; infrastructure and field encryption evidence absent. |
| Immutable privileged-action audit with actor/action/target/before/after/time/IP | Partial | `AuditLog` captures much of this, but it is mutable and before/after values are not universal. |
| DPDP purpose, consent, access and deletion endpoints | Missing | No complete implementation found. |
| No personal data in logs/errors/analytics | Partial | Explicit privacy practices exist; no automated audit or structured logging policy proves it globally. |
| `pip-audit`, no High/Critical findings | Missing | No dependency-vulnerability CI step or handover report. |
| Secure upload validation/scanning/pre-signed storage | Unverified/not currently central to P04 | No complete object-storage/upload-security implementation found. |
| Admin TOTP MFA | Missing | No implementation found. |

### 6.4 Testing, documentation and Definition of Done

**PDF source:** CES sections 1.5-1.7, printed pp. 3-4.

| Requirement | Status | Evidence / gap |
|---|---|---|
| >=70% statement coverage on domain/services | Unverified | Many tests exist, but no coverage measurement/configuration was found. |
| Component/form tests | Partial | Template/form assertions exist; exhaustive shared-component/form coverage is not demonstrated. |
| Every endpoint including authorization failure paths | Partial | Strong authorization tests, but no endpoint inventory-to-test matrix. |
| Playwright/Cypress critical journeys | Missing | No executable end-to-end suite found. |
| Locust/k6 concurrency report | Missing | No load-test artifact. |
| OWASP ZAP + pip-audit clean | Missing | No report or CI step. |
| axe-core zero critical violations | Missing | No axe CI. |
| Explicit multi-process/connection `TransactionTestCase` concurrency proof | Completed for P04 threading/connection criterion | `test_postgresql_exchange_correctness.py` uses real synchronized threads and independent PostgreSQL backend connections. It does not claim multi-process load evidence. |
| Ten-working-day client UAT with defect log/severity | Missing/unverified | No UAT artifact found. |
| Required repository documentation set | Partial | README, architecture, schema, permissions, UI and phase notes exist; ADRs, API spec, rollback, backup/restore, admin/end-user guides, handover recording, and debt register are incomplete or absent. |
| Reviewed PR, CI green, staging deployment, live demo, vulnerability/accessibility gates | Unverified | Local repository cannot prove the full milestone Definition of Done. |

### 6.5 Observability

**PDF source:** CES section 1.1, printed p. 2.

| Requirement | Status | Gap |
|---|---|---|
| Structured JSON logging | Missing | Standard Python logging only. |
| Sentry | Missing | No configuration found. |
| Uptime monitoring | Missing | No configuration/evidence found. |
| `/health` and `/ready` | Missing | No application endpoints found. |

## 7. Test environment and correctness evidence

The default `pytest.ini` and `config/settings/test.py` still configure ordinary tests to use `django.db.backends.sqlite3` and `test_roomloop.sqlite3`. Those results remain application-logic evidence only; they are not counted as PostgreSQL concurrency evidence.

The correctness milestone adds `config/settings/test_postgresql.py` and `tests/test_postgresql_exchange_correctness.py`. The settings module:

- requires `django.db.backends.postgresql`;
- forces Django's test runner to a separately named database beginning with `test_` (`test_roomloop_concurrency` by default);
- refuses a test name equal to the application database name;
- uses the existing credential configuration without logging secrets.

The `TransactionTestCase` additionally asserts `current_database()` before fixture creation. Concurrent workers use a barrier, distinct thread-local Django connections, distinct `pg_backend_pid()` values, and bounded PostgreSQL/test timeouts. No row-locking behavior is mocked. See `docs/P04_LOCKING_STRATEGY.md`.

The live application connection was not used by this milestone. Test-database creation, test fixture writes, test cleanup, and all 100 simulated exchanges occurred only in the isolated PostgreSQL test database.

## 8. Read-only hostel inventory audit

### 8.1 Live aggregate findings

The audit used aggregate SELECT queries only against institution `DEMO-UNIV`.

- Database backend: PostgreSQL.
- Total hostels: 20.
- Standard baseline present: all 17 codes, BH-01..BH-10 and GH-01..GH-07.
- Missing standard hostels: none.
- Standard classification mismatches: none. BH records are `male`; GH records are `female`.
- Standard layout: each standard hostel has 200 rooms and 800 beds.
- Standard Warden coverage: one assignment for each of the 17 standard hostels.
- Additional hostels: NR-01 (`male`, academic level `all`), SR-01 (`female`, academic level `all`), and SH-01 (`postgraduate`, academic level `postgraduate`).
- Total students: 353; active occupancies: 348; Warden assignments: 20.
- Standard-hostel active occupancies: BH-02 has 21; every other standard BH/GH hostel has 20.
- Additional-hostel dependencies: NR-01 has 4 rooms, 8 beds, 3 active occupancies and 1 Warden; SR-01 has 2 rooms, 6 beds, 3 active occupancies and 1 Warden; SH-01 has 1 room, 1 bed, 1 active occupancy and 1 Warden.

SH-01 is an explicit classification inconsistency with the confirmed two-category policy: `resident_category="postgraduate"` uses an academic level as a residential category. The audit does not infer a replacement category from its name and made no change.

### 8.2 Seeder review

`seed_hostel_inventory.py` defines exactly BH-01..BH-10 and GH-01..GH-07, assigns distinct seed Wardens, uses an atomic transaction, detects existing standard hostels by code, preserves their inventory, uses `get_or_create` for assignments, and reports additional hostels separately. Existing automated tests cover idempotent re-execution against the disposable test database.

### 8.3 Proposed reconciliation plan - not executed

1. Obtain an authoritative classification decision for SH-01 independent of its name and keep `academic_level="postgraduate"` separate.
2. Inventory the provenance and business ownership of NR-01, SR-01 and SH-01; treat them as potentially legitimate until approved otherwise.
3. Export a dependency report for each additional hostel before any proposed reclassification, merge, or retirement.
4. If SH-01 is classified, use an audited data migration or controlled administrative workflow; never delete its active occupancy.
5. Re-run read-only classification, Warden, room, bed and occupancy checks.
6. Do not delete additional hostels merely to make the live total equal 17.

## 9. Maximum-chain-length behavior

- Supported values: integers 2, 3, 4 and 5.
- Default when no active stored rule exists: 5.
- Invalid values are rejected by both the Django form and service layer.
- The stored institution value is authoritative.
- A CLI caller can request a stricter maximum, but cannot exceed the institution policy.
- Celery/background matching and reconciliation call the same policy-aware matcher.
- Direct two-person swaps remain allowed at every supported value.
- When the value decreases, future matching uses the new limit immediately.
- Draft proposals above the new limit remain unchanged but cannot activate.
- Already-active acceptance/approval workflows continue unchanged; they are not silently cancelled or rewritten.
- Every authorized policy change creates an `EXCHANGE_POLICY_UPDATED` audit event.

## 10. Verification performed for this revision

At the time this document was written:

- PostgreSQL correctness command: 5 passed in `test_postgresql_exchange_correctness.py`.
- PostgreSQL matching benchmark: 12/12 timed detection runs below five seconds; four queries per run. Structured median/slowest 0.991/1.247 seconds; dense median/slowest 1.835/2.048 seconds.
- Benchmark correctness: exact 540 structured cycles, 198,000 dense graph edges, institution isolation, no invalid/duplicate cycles, no occupancy mutation, and explicit 10,000-candidate truncation.
- Separate persistence evidence: 540 DRAFT proposals and 1,300 inactive-reservation participant rows in 9.618 seconds; 6,607 queries. This is outside the PDF matching target.
- PostgreSQL approval/execution/policy regression command: 51 passed.
- Current matching regressions: 26 focused matcher tests and 46 matching-workflow/policy tests passed.
- Correctness backend: `django.db.backends.postgresql`, isolated database `test_roomloop_concurrency`.
- Simulation result: 100 completed exchanges and 250 individual student movements, reconciled after every execution.
- New-file Ruff check: passed.
- Migration drift check: no changes detected.
- Default test backend remains SQLite for the ordinary suite; those results are not counted as PostgreSQL concurrency evidence.
- The prior live inventory audit used PostgreSQL read-only; this correctness milestone did not connect tests to the live database.
- M7 reporting command: 5 passed on guarded PostgreSQL, including exact metrics, date boundaries, role/hostel scope, institution isolation, privacy, unavailable data, query budget, and a 2,008-request rendered response.
- M7 selector timing/query evidence: 0.056812 seconds and 14 queries. Rendered response: 0.177997 seconds and 34 queries, below the CES three-second target.
- Reporting/execution/approval/policy/vacant-transfer regressions: 75 passed; PostgreSQL real-thread transaction correctness: 5 passed after the completion-timestamp fix.
- Django system check found no issues; migration drift check found no changes. No CSS changed, so no frontend build was required.
- Browser automation was unavailable; rendered HTML is test-covered, while desktop/mobile screenshots and cross-browser visual sign-off remain unverified.

The pytest cache warning is an environment permission warning and not an application-test failure.

## 11. Prioritized remaining work

1. **P1 - One full four-party critical journey:** request through completed execution, including a browser-level journey.
2. **P1 - CES security:** admin MFA, rate limiting/lockout, pip-audit/ZAP, secret scanning, DPDP controls.
3. **P1 - CES operations:** health/readiness, JSON logging, Sentry, uptime, backups and tested restore.
4. **P2 - External integrations:** ERP, production email, push, SMS and SSO.
5. **P2 - CES evidence:** coverage threshold, axe/WCAG, browser matrix, load/latency/LCP, UAT and documentation set.

The isolated PostgreSQL concurrency, 100-transfer correctness, 2,000-open-request matching-performance, and M7 reporting/audit milestones are complete. M7 provides operational audit visibility but does not claim database-enforced audit immutability.
