# RoomLoop Release Readiness Evidence

Last verified: 21 November 2026

## Release decision

The functional Django release checks, database correctness tests, matching performance
benchmark, migration check, deployment security check, frontend build, and configured CI
lint gate pass. Historical style debt remains explicitly baselined for incremental review;
CI continues to block syntax errors and undefined names.

## Verification evidence

| Check | Result |
|---|---|
| Focused notification, approval, policy, and matching tests | 71 passed |
| Full default test suite | 313 passed, 6 PostgreSQL-only tests skipped by design |
| PostgreSQL concurrency and row-lock suite | 5 passed |
| PostgreSQL 2,000-request benchmark | 1 passed; all 12 timed matching runs under 5 seconds |
| Structured benchmark | median 0.764s; slowest 0.866s |
| Dense broad-preference benchmark | median 1.249s; slowest 2.312s |
| Django system check | No issues |
| Django deployment security check | No issues with production settings and validation-only environment values |
| Migration drift check | No changes detected |
| Tailwind production CSS build | Passed |
| Stale cross-hostel approval wording scan | No active wording implying two-Warden approval |
| Repository-wide configured Ruff check | Passed; blocks syntax errors and undefined names |
| Shared-layout accessibility baseline | Skip navigation, focusable main landmark, named controls, and live toast announcements verified |

The PostgreSQL tests use the guarded `test_roomloop_concurrency` database. The application
database and existing hostel/occupancy records are not reset or mutated by these checks.

## Approval and notification model

- Same-hostel exchanges are routed only to an assigned Warden.
- Cross-hostel exchanges are routed only to a Chief Warden.
- Students receive proposal-created, participant-response, expiry, approval-result, and
  transfer-completion notifications as applicable.
- Dashboard unread counts are calculated from persistent notifications and marking a single
  notification or all notifications read updates the badge state.

## Security and deployment configuration

Production settings enable HTTPS redirection, secure session and CSRF cookies, HSTS with
subdomains and preload, content-type sniffing protection, frame denial, reverse-proxy HTTPS
support, and compressed manifest static files. Deployment must supply a strong
`DJANGO_SECRET_KEY`, explicit `DJANGO_ALLOWED_HOSTS`, and the existing PostgreSQL connection
configuration through environment variables. Secrets must never be committed or copied into
submission evidence.

## CI and handover

The CI workflow provisions PostgreSQL 16 and Redis 7, installs Python and Node dependencies,
builds CSS, runs Ruff, checks Django and migration drift, and runs pytest. `ruff.toml` records
the current correctness-grade baseline. Broader formatting and style rules should be enabled
incrementally; do not apply automatic unsafe fixes across the repository without review.

Recommended handover order:

1. Record stakeholder UAT sign-off, deployment owner, rollback owner, and environment-specific URLs.
2. Deploy migrations before application processes, then collect static files and start web
   and background-worker processes.
3. Verify health, login, notification delivery, matching worker execution, approval routing,
   and one controlled end-to-end exchange in the target environment.

## Related evidence

- `P04_REQUIREMENTS_TRACEABILITY.md`
- `P04_LOCKING_STRATEGY.md`
- `P04_MATCHING_PERFORMANCE.md`
- `P04_REPORTING_AND_AUDIT.md`
- `PERMISSION_MATRIX.md`
- `ARCHITECTURE.md`
