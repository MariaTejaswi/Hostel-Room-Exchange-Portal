# P04 PostgreSQL Locking Strategy and Correctness Evidence

## Scope and isolation

RoomLoop's occupied-bed exchange path uses PostgreSQL transactions and row locks. The
correctness suite is `tests/test_postgresql_exchange_correctness.py`, a Django
`TransactionTestCase` module run with `config.settings.test_postgresql`.

The PostgreSQL test settings inherit credentials without printing them and force the
test runner to use a separate database named `test_roomloop_concurrency` by default.
The settings module refuses a name that does not start with `test_` or that equals the
application database name. Every test also queries `current_database()` and fails
before fixture creation unless the active database is that configured isolated test
database. The live application database is never a test target.

## Transaction and lock boundaries

`decide_exchange_proposal` opens one `transaction.atomic()` block and acquires locks
in this order:

1. the `ExchangeProposal` row;
2. participant `User` rows, sorted by ID;
3. `ExchangeRequest` rows, sorted by ID;
4. all source and destination `Bed` rows, sorted by ID.

When approval requests immediate execution, `execute_exchange_transfer` runs as a
nested atomic block in the same database transaction. The proposal lock is already
held and is reacquired by the same transaction; execution then locks:

1. all source and destination `Bed` rows;
2. participant `User` rows;
3. `ExchangeRequest` rows;
4. current `StudentOccupancy` rows.

A standalone execution first locks the proposal and then uses the same execution
order. The service closes every old occupancy, creates every new occupancy, creates
all participant transfer records, completes requests/proposal, and releases workflow
reservations inside that single outer transaction. An exception at any step rolls the
whole unit back.

The database also enforces conditional uniqueness for one active occupancy per
student and bed, one active workflow reservation per student/current occupancy/request,
one transfer record per proposal/student, and one transfer record per exchange request.
Consequently two valid active proposals cannot share a student, current occupancy, or
request. Destination-bed overlap is still revalidated and locked at approval/execution.

## Concurrent test method

The concurrency cases use real `ThreadPoolExecutor` workers and a `Barrier`. Each
worker calls `close_old_connections()`, opens its own Django connection, and records
`pg_backend_pid()` plus `current_database()` before crossing the barrier. Tests assert
different backend PIDs and the same isolated `test_...` database. PostgreSQL
`lock_timeout` is 10 seconds, `statement_timeout` is 30 seconds, and each future has a
45-second bound. Row locking is not mocked.

Covered races:

- overlapping destination beds: one valid closed cycle completes while a competing
  non-closed proposal rolls back without an approval, movement, or transfer record;
- duplicate approval/execution of the same proposal: the proposal lock serializes the
  calls and both callers observe the same single approval and transfer-record set;
- independent direct and four-party exchanges: both complete on separate sessions.

## Reconciliation and rollback evidence

The PostgreSQL suite executes 100 successful exchanges through production services:
50 direct swaps and 50 three-party rotations. This is 250 individual student
movements. After every exchange it reconciles:

- exactly one active occupancy for every student;
- exactly one active occupant for every involved bed;
- the expected student-to-bed mapping;
- cumulative active and transferred occupancy history;
- completed proposal and request counts;
- one transfer record per participant with matching old/new occupancy and bed links;
- zero active workflow reservations for completed exchanges.

The rollback case injects an exception during the second transfer-record write, after
old occupancies have been closed and new occupancies created in the transaction. It
then proves that no mutation committed: original occupancies and reservations remain,
requests remain matched, the proposal remains approved, and no transfer record exists.
A normal retry completes, and a second execution returns the same transfer-record IDs
without new allocations or records.

## Evidence command

```powershell
.venv\Scripts\python.exe -m pytest -q tests\test_postgresql_exchange_correctness.py --ds=config.settings.test_postgresql
```

Evidence recorded on 20 November 2026: five tests passed on
`django.db.backends.postgresql`. SQLite results are not counted as row-lock evidence.

