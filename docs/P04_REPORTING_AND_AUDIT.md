# P04 M7 Reporting & Audit

## Purpose and boundary

This document defines the P04 M7 exchange metrics before their implementation. The reports are read-only: they do not update exchange requests, proposals, approvals, policies, occupancies, transfer records, or audit records. They report exchange workflow data; they are not occupancy-utilization reports and they do not claim concurrent-user load capacity.

All calculations are institution-scoped in the backend. The report timezone is Django's active timezone. A selected date range is inclusive by calendar date and is converted to a half-open timestamp interval: `start date 00:00 <= event < day after end date 00:00`. This avoids end-of-day precision errors and makes daylight-saving boundaries deterministic.

## Access and hostel scope

| Role | Report access | Hostel scope | Mutating authority changed? |
|---|---|---|---|
| Dean of Student Welfare | Read-only | All hostels in the user's institution | No |
| Chief Warden | Read-only | All hostels in the user's institution | No; cross-hostel decisions remain Chief-Warden-only |
| Warden | Read-only | Assigned hostels only | No; same-hostel decisions remain assigned-Warden-only |
| Hostel Administrator | None | None | No; this role retains exclusive exchange-policy editing |
| Student / System Administrator | None | None | No |

The optional hostel filter can only select a hostel already present in the user's reporting scope. Request-oriented metrics use the request's source hostel (the hostel on `current_occupancy`). Completion detail uses the historical source bed on `TransferRecord`. Approval aggregates include decisions on proposals or vacant transfers involving an in-scope source hostel. A Warden's visible audit events are restricted to requests and proposals involving their assigned source hostels; record-detail links are only emitted when the existing operational selector also authorizes that detail view.

## Metric definitions

### 1. Request volume by hostel and period

- **Unit:** persisted `ExchangeRequest` rows, not proposals or movements.
- **Date basis:** `ExchangeRequest.submitted_at` within the selected interval.
- **Grouping:** source hostel from the request's historical `current_occupancy` bed.
- **Included states:** open, matched, pending approval, rejected, withdrawn, expired, and completed.
- **Withdrawals/expiries/rejections:** included because they were submitted demand during the period; state totals identify their current outcomes.
- **Retries:** a retry that updates the same request remains one request. A later separately persisted request is a separate unit.
- **Missing data:** `submitted_at` and `current_occupancy` are required by the schema. If a legacy relationship is unavailable, it is labelled `Not available`, never assigned to an invented hostel.

### 2. Match rate

- **Denominator:** distinct occupied-bed exchange requests (`request_type=exchange`) submitted in the selected period and source-hostel scope.
- **Numerator:** distinct denominator requests that have participated in at least one proposal whose state proves activation (any proposal state except `draft`).
- **Date basis:** request cohort by `ExchangeRequest.submitted_at`; activation may occur after the cohort's end date.
- **Draft candidates:** excluded. A draft is a candidate, not a successful match.
- **Later rejection, expiry, cancellation, or completion:** remains matched historically and stays in the numerator because the terminal non-draft proposal state preserves that activation.
- **Multiple candidates/retries:** `DISTINCT` request identity prevents double-counting when one request appears in several proposals.
- **Vacant-bed transfers:** excluded because they do not pass through the matching engine.
- **Zero denominator:** displayed as `Not available`, not `0%`.

### 3. Average time to completion

- **Unit:** individual completed student movements represented by durable `TransferRecord` rows.
- **Start event:** the linked request's `submitted_at`; for exchange movements this is the participant's linked exchange request, and for vacant transfers it is the transfer request.
- **End event:** `TransferRecord.transferred_at`, the durable timestamp written when that student's allocation movement executes.
- **Date basis:** end event within the selected interval.
- **Included states:** durable completed movements only. Draft, activated, rejected, expired, withdrawn, and cancelled workflows without a transfer record are excluded.
- **Direct and multi-party exchanges:** each participant movement is one duration sample. The UI separately reports distinct completed exchange workflows and individual movement count so those units are not confused.
- **Retries/repeated execution:** database uniqueness and idempotent execution make an existing movement one sample; an idempotent retry does not add another `TransferRecord`.
- **Missing data:** samples without a reliable linked request start timestamp are excluded from the average and counted as unavailable. If no valid samples remain, the result is `Not available`, not zero.

### 4. Approval and rejection patterns by deciding authority

- **Unit:** persisted, decided `Approval` rows.
- **Date basis:** `Approval.decided_at` within the selected interval.
- **Included decisions:** `approved` and `rejected`; pending rows are excluded.
- **Attribution:** the actual `authority_user` and stored `authority_role` on the decision, not an inferred role from the proposal's current state.
- **Grouping:** deciding staff member plus authority role, with approved, rejected, total, and approval-rate columns.
- **Retries:** an idempotent service retry that updates or returns the same approval remains one event. Separately persisted decisions are separate authority events.
- **Later workflow state:** does not erase the approval event; reporting reads the decision record rather than the proposal's current status.
- **Missing data:** a role group with no decision events is an empty state. A group with zero total does not receive a fabricated percentage.

### 5. Most-requested rooms

- **Unit:** distinct submitted requests in the request cohort.
- **Date basis:** `ExchangeRequest.submitted_at` within the selected interval.
- **Explicit target:** only a request with `preferred_bed` is attributed to that bed's room. Counts are grouped by the actual destination hostel and room.
- **Broad preference:** a request without `preferred_bed` is counted separately as broad preference demand, even when it names a hostel, block, floor, room type, occupancy category, or accessibility attributes.
- **States:** all submitted workflow states are included, matching request-volume demand semantics.
- **Retries/multiple candidates:** proposals do not affect this metric; each request contributes at most once.
- **Missing data:** a deleted or unavailable explicit target is labelled unavailable. A broad preference is never assigned to an invented room.

### 6. Workflow and movement summary

The report explicitly separates these units:

- **Requests:** persisted demand records submitted in the period.
- **Draft candidates:** `ExchangeProposal(status=draft)` rows created in the period; never counted as matches.
- **Activated proposals:** proposals with an `EXCHANGE_PROPOSAL_ACTIVATED` audit event in the period, supplemented by a non-draft `matched_at` timestamp when an older activation event is unavailable. Missing activation history is disclosed.
- **Completed exchanges:** distinct proposal IDs represented by transfer records completed in the period.
- **Completed vacant transfers:** distinct non-proposal request IDs represented by transfer records completed in the period.
- **Individual student movements:** transfer-record rows completed in the period.

### 7. Exchange audit trail

- **Source:** existing `AuditLog` rows whose action begins with `EXCHANGE_` or `VACANT_TRANSFER_`.
- **Date basis:** `AuditLog.timestamp` within the selected interval.
- **Display:** timestamp, action, safe target reference, and actor where operationally relevant. Raw free-text summaries are not exposed because older summaries can contain student names or rejection details.
- **Links:** an existing proposal or vacant-transfer detail link is shown only after its current backend selector authorizes the viewer.
- **Mutability limitation:** this is operational audit visibility, not proof of database-enforced append-only storage. `AuditLog` has no database trigger or database-role rule preventing update/delete, and universal before/after values are not stored. M7 does not rewrite or delete audit history and does not claim stronger immutability.

## Presentation and filtering

The report provides validated start/end dates, a role-scoped hostel selector, visible active filters, reset behavior, summary cards, accessible chart tables, paginated completion and audit detail, and explicit empty/unavailable states. Every section receives the same date and hostel inputs, with the metric-specific event timestamp documented above. No student name, registration number, email, eligibility result, fee status, disciplinary detail, or free-text request/rejection reason is included.

## Query and performance design

Aggregates live in `apps.exchange.reporting`, use institution predicates on every base query, use database `COUNT`, filtered aggregates, `DISTINCT`, `EXISTS`, and date truncation, and avoid iterating related objects. Paginated detail querysets use `select_related`. Join multiplication is neutralized with distinct request/proposal identifiers. A deterministic isolated-PostgreSQL test records report query count and wall time against the CES three-second interactive target.

## Verification record

### Implementation

- Aggregate selectors and scope enforcement: `apps/exchange/reporting.py`.
- Validated, role-scoped date/hostel form: `apps/exchange/forms.py:ExchangeReportFilterForm`.
- Read-only endpoint: `/exchange/reports/` (`exchange:reports`) in `apps/exchange/views.py` and `apps/exchange/urls.py`.
- Coffee-brown responsive interface: `templates/exchange/reports.html`; authorized navigation is in `templates/components/sidebar.html`.
- Completion timestamp capture: `execute_exchange_transfer` now persists `ExchangeProposal.completed_at` in the same atomic execution block. Existing historical proposals without that timestamp are not backfilled or guessed; movement timing uses the durable per-student `TransferRecord.transferred_at` event.
- Deterministic verification: `tests/test_exchange_reporting.py`; the existing direct-execution regression now asserts that new completed proposals receive `completed_at`.

### Deterministic expected totals

The accuracy fixture contains two institutions and, inside the reported institution, two hostels, eight in-window requests, one exact start-boundary request, one end-of-day boundary request, direct and three-party completed exchanges, draft/pending/rejected/expired/cancelled proposals, one request in multiple candidate proposals, one vacant transfer, duplicate approval audit evidence without a duplicate `Approval`, three explicit bed targets, five broad preferences, and one legacy movement without a linked request start.

Verified institution-wide results:

- request volume: 8 (7 occupied-bed exchange requests, 1 vacant transfer), split 6/2 by source hostel;
- match rate: 6 distinct matched requests / 7 eligible exchange requests = 85.714%;
- explicit targets: 3; broad preferences: 5; destination-room ranking 2 then 1;
- workflow: 1 draft candidate, 6 dated activated proposals, and 1 non-draft proposal with unavailable activation history;
- authority decisions: Chief Warden 2 approved / 1 rejected; Warden 1 approved / 1 rejected;
- completion: 3 distinct exchanges (2 direct-classified, including the legacy record, and 1 multi-party), 1 vacant transfer, 7 student movements;
- average completion: 7 days 12 hours across 6 valid movement samples; 1 missing-start sample is disclosed and excluded.

The other institution is excluded. The assigned Warden sees only the six requests originating in the assigned hostel and cannot forge the other hostel filter. A zero-request period returns `Not available` for rate and average rather than a fabricated zero.

### PostgreSQL performance evidence

Backend: `django.db.backends.postgresql`, PostgreSQL 16.16, isolated database `test_roomloop_concurrency`. Runtime: Python 3.12.6, Django 5.2.19, Windows 11 10.0.26300, Intel Core i5-12450H (8 cores / 12 logical processors).

The selector-level deterministic report used **14 queries and 0.056812 seconds**. A rendered Dean response over **2,008 actual requests**, each additional request backed by a real user, bed, and active occupancy, used **34 queries and 0.177997 seconds**. Fixture creation and authentication setup are outside the response interval; the timed response includes authorization, form validation, all report aggregations, audit/movement pagination, context processors, and template rendering. This meets the CES interactive report target of under three seconds. Query count is constant with request volume; no per-request loop or related-object query occurs.

The first rendered benchmark before the focused optimization completed in 0.193331 seconds but issued 41 queries. Profiling showed repeated role-scope resolution and empty detail-link selector calls. Request-local scope caching and skipping empty target lookups reduced the final count to 34 without changing scope, workflow, matching, or transaction semantics.

### Exact validation commands

All commands were run from the repository root with `.venv\Scripts\python.exe`. The guarded settings refuse a non-PostgreSQL backend or a test database name that does not start with `test_`.

```powershell
$env:DJANGO_SETTINGS_MODULE='config.settings.test_postgresql'
$env:ROOMLOOP_POSTGRES_TEST_DB='test_roomloop_concurrency'

.venv\Scripts\python.exe manage.py check
.venv\Scripts\python.exe manage.py makemigrations --check --dry-run

.venv\Scripts\python.exe -m pytest -q -s --reuse-db tests\test_exchange_reporting.py
.venv\Scripts\python.exe -m pytest -q --reuse-db tests\test_exchange_reporting.py tests\test_exchange_execution.py tests\test_approval_workflow.py tests\test_p04_exchange_management.py tests\test_vacant_bed_transfers.py
.venv\Scripts\python.exe -m pytest -q --reuse-db tests\test_postgresql_exchange_correctness.py

.venv\Scripts\python.exe -m ruff check apps\exchange\reporting.py tests\test_exchange_reporting.py
```

Observed results:

- M7 report suite: 5 passed, including the 2,008-request rendered response benchmark.
- reporting/execution/approval/policy/vacant-transfer regressions: 75 passed.
- real-thread PostgreSQL transaction correctness: 5 passed.
- Django system check: no issues; migration drift: none.
- new M7 Python files: Ruff passed.

No stylesheet changed, so no frontend asset rebuild was required. Django rendered the complete report template at the endpoint in the authorization and performance tests. A browser automation/runtime was not available in this task, so desktop/mobile screenshots and a browser-matrix visual sign-off remain unverified; the implementation uses the existing responsive design tokens, stacks filters/cards at small breakpoints, and confines wide tables to local overflow containers.

### Remaining limitations

- Operational audit visibility is complete for existing exchange events, but database-enforced append-only immutability and universal before/after snapshots remain a CES gap.
- Older non-draft proposals with neither an activation audit event nor `matched_at` cannot be assigned to a report period and are disclosed as unavailable.
- `preferred_bed` uses `SET_NULL`; if a legacy explicit target were physically deleted, the current schema cannot distinguish it from an originally broad request. The report does not invent a room.
- Report evidence covers deterministic PostgreSQL correctness and interactive response time, not 2,000 concurrent users, production browser performance, or load capacity.
