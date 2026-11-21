# P04 PostgreSQL Matching Performance Evidence

## Result

RoomLoop's production matching service meets the P04 target of processing 2,000
open exchange requests, with cycle discovery bounded to five participants, in under
five seconds on the environment below. Both deterministic scenarios passed on the
first run and all five repeat runs.

This is a matching-throughput benchmark for 2,000 open requests. It is not evidence
for 2,000 concurrent application users.

## Safety and reproducibility

The benchmark is `tests/test_postgresql_matching_performance.py`. It is a Django
`TransactionTestCase` and runs only when `connection.vendor == "postgresql"`. Before
creating fixtures it queries `current_database()` and refuses to continue unless the
name starts with `test_` and exactly matches the guarded test setting. The run below
used `test_roomloop_concurrency`; the live application database was never a target.

Run the complete benchmark from the repository root:

```powershell
$env:P04_BENCH_RUNS='6'
Remove-Item Env:P04_BENCH_SKIP_PERSIST,Env:P04_BENCH_PROFILE,Env:P04_BENCH_MODE -ErrorAction SilentlyContinue
.venv\Scripts\python.exe -m pytest -q -s tests\test_postgresql_matching_performance.py --ds=config.settings.test_postgresql --create-db -p no:cacheprovider
```

Set `P04_BENCH_PROFILE=1` and `P04_BENCH_RUNS=1` for a diagnostic cProfile run.
Profiler-inflated durations are diagnostic only and are not benchmark evidence. Set
`P04_BENCH_MODE=persistence` to run only the separate proposal-persistence case.

## Dataset composition and expected results

Fixture creation uses real PostgreSQL rows and is outside the timed interval. Each
scenario has its own institution and contains 2,000 active student accounts, 2,000
active beds, 2,000 active occupancies, complete institution policy rows, and 2,000
eligible OPEN exchange requests. Matching one institution must therefore produce a
2,000-node graph even though 4,000 requests exist in the test database.

### Structured scenario

- 400 direct two-person swaps: 800 requests.
- 80 three-person rotations: 240 requests.
- 40 four-person rotations: 160 requests.
- 20 five-person rotations: 100 requests.
- 700 deliberately unmatched requests: 250 request unavailable SINGLE rooms, 225
  request the unavailable `Quiet Wing` occupancy category, and 225 request unavailable
  wheelchair accessibility.
- Expected and verified: 1,300 directed edges and exactly 540 unique cycles, divided
  into 400/80/40/20 cycles of lengths 2/3/4/5.

### Broad-preference scenario

- Twenty active hostels with 100 requests each.
- Each request accepts a Standard DOUBLE room in its current hostel.
- Every request has 99 compatible destinations, producing 198,000 verified directed
  edges. This deliberately stresses graph growth and is not a sparse or precomputed
  input.
- The production `max_cycles=10_000` limit is intentionally retained. Every run
  returned 10,000 unique cycles and `limit_reached=True`: 1 two-person, 2
  three-person, 104 four-person, and 9,893 five-person cycles in deterministic DFS
  order. The dataset contains more valid cycles than are persisted or returned.

The benchmark invokes `run_exchange_matching`, including the real ORM queryset,
policy and reservation queries, eligibility checks, graph construction, cycle
search, explanations, and candidate objects. It does not mock row loading, policy
validation, graph construction, or cycle discovery.

Correctness assertions on every run verify exact known cycles and edges, the
configured five-participant bound, unique cycle signatures, distinct requests,
students and current beds, complete bed rotations, institution isolation, unchanged
physical occupancy, unchanged OPEN request status, and no active workflow
reservations. No proposal or cache is created during detection repeats.

## Timing boundaries

`MatchingMetrics.total_time_seconds` is the P04 matching interval. It begins before
the configured chain-limit lookup and ends after candidate materialization. It
includes:

1. policy/cycle-limit resolution;
2. database request loading and related-object hydration;
3. policy/reservation loading and per-request eligibility evaluation;
4. graph snapshot, index, and edge construction;
5. bounded cycle discovery and deduplication;
6. candidate materialization, including explanation generation.

Explanation time is a measured subset of candidate-materialization time and should
not be added to it again. Outer wall time also includes the benchmark wrapper and
query-capture overhead. Fixture creation (3.965 seconds for both institutions) is
excluded.

Proposal persistence begins after matching. It is reported separately and is not
part of the PDF's under-five-second matching target.

## Final measured results — 21 November 2026

Runtime: PostgreSQL 16.16 (64-bit), Django 5.2.19, Python 3.12.6, Windows 11
10.0.26300, AMD64, Intel64 Family 6 Model 154 Stepping 3, 12 logical CPUs.

All durations are seconds. `Explain` is contained within `Candidate`.

### Structured runs

| Run | Policy | DB load | Eligibility | Graph | Cycle | Candidate | Explain | Service | Wall | Queries |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 0.0022 | 0.8673 | 0.0254 | 0.0727 | 0.0054 | 0.0127 | 0.0016 | 0.9856 | 1.0038 | 4 |
| 2 | 0.0017 | 0.8230 | 0.0218 | 0.0642 | 0.0050 | 0.0142 | 0.0018 | 0.9299 | 0.9412 | 4 |
| 3 | 0.0019 | 0.8692 | 0.0232 | 0.0716 | 0.0057 | 0.0135 | 0.0017 | 0.9852 | 0.9960 | 4 |
| 4 | 0.0019 | 0.8693 | 0.0251 | 0.0735 | 0.0065 | 0.0192 | 0.0022 | 0.9955 | 1.0052 | 4 |
| 5 | 0.0018 | 1.0263 | 0.0212 | 0.0677 | 0.0047 | 0.0131 | 0.0017 | 1.1349 | 1.1439 | 4 |
| 6 | 0.0013 | 0.9993 | 0.0904 | 0.1120 | 0.0107 | 0.0328 | 0.0021 | 1.2465 | 1.2566 | 4 |
| Median | 0.0018 | 0.8693 | 0.0241 | 0.0722 | 0.0056 | 0.0138 | 0.0018 | **0.9906** | 1.0045 | 4 |

Slowest service run: **1.2465 seconds**. All six runs meet the five-second target.

### Broad-preference runs

| Run | Policy | DB load | Eligibility | Graph | Cycle | Candidate | Explain | Service | Wall | Queries |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 0.0018 | 0.6266 | 0.0228 | 0.2951 | 0.2415 | 0.5294 | 0.0697 | 1.7173 | 1.7286 | 4 |
| 2 | 0.0012 | 0.7321 | 0.0198 | 0.2988 | 0.2263 | 0.6034 | 0.0816 | 1.8817 | 1.8925 | 4 |
| 3 | 0.0018 | 0.7576 | 0.0218 | 0.2776 | 0.2805 | 0.5006 | 0.0647 | 1.8399 | 1.8497 | 4 |
| 4 | 0.0016 | 0.6909 | 0.0324 | 0.5150 | 0.2188 | 0.5892 | 0.0766 | 2.0478 | 2.0721 | 4 |
| 5 | 0.0015 | 0.6600 | 0.0243 | 0.3167 | 0.2850 | 0.5003 | 0.0648 | 1.7878 | 1.7981 | 4 |
| 6 | 0.0031 | 0.8383 | 0.0348 | 0.2928 | 0.1841 | 0.4773 | 0.0621 | 1.8305 | 1.8422 | 4 |
| Median | 0.0017 | 0.7115 | 0.0236 | 0.2970 | 0.2339 | 0.5150 | 0.0672 | **1.8352** | 1.8460 | 4 |

Slowest service run: **2.0478 seconds**. All six runs meet the five-second target,
while clearly reporting candidate truncation.

### Proposal persistence

The structured persistence run repeated matching and then created 540 DRAFT
proposals with 1,300 inactive-reservation participant rows:

- Matching before persistence: 1.2183 seconds.
- Proposal persistence: 9.6183 seconds.
- Total service including persistence: 10.8368 seconds.
- Outer wall time: 10.8448 seconds.
- Queries: 6,607.

Candidate persistence is idempotent at the service level, but its row-by-row
revalidation and proposal creation are a documented performance limitation. It is
not included in the P04 five-second cycle-detection target.

## Profile and optimization evidence

Before production changes, the six-run broad baseline had service times of 12.031,
23.012, 12.159, 10.710, 8.950, and 8.186 seconds (median 11.370; slowest
23.012). The structured median was 2.481 seconds and already passed.

A fresh cProfile run immediately before optimization showed 19,066,434 calls. The
largest cumulative costs were graph construction (11.543 profiled seconds),
198,000 repeated compatibility checks (7.665), candidate materialization (6.386),
and 49,889 explanation calls (3.632). Cycle discovery was 1.157 profiled seconds.

The focused optimization creates immutable primitive snapshots once per eligible
request and bed, pre-normalizes text once, retains every compatibility condition as
primitive comparisons, and reuses the snapshots for candidate explanations. It also
indexes resident category before edge checks. Eligibility, institution/gender
isolation, operational-state validation, request preferences, chain limits,
candidate limits, cycle semantics, proposal concurrency, and persistence behavior
are unchanged.

## Regression evidence

```powershell
.venv\Scripts\python.exe -m pytest -q tests\test_matching_engine.py -p no:cacheprovider
.venv\Scripts\python.exe -m pytest -q tests\test_exchange_matching.py tests\test_background_matching.py tests\test_p04_exchange_management.py -p no:cacheprovider
.venv\Scripts\python.exe -m pytest -q tests\test_postgresql_exchange_correctness.py --ds=config.settings.test_postgresql --create-db -p no:cacheprovider
```

Results: 26 matching tests passed; 46 matching workflow and policy/authorization
tests passed; and all 5 real-thread PostgreSQL correctness tests passed. The latter
includes 100 successful exchanges and 250 individual movements with reconciliation,
concurrent overlap/duplicate/independent cases, rollback, and idempotency.

## Remaining limitations

- Dense matching deliberately stops after the configured 10,000 candidates. It does
  not claim exhaustive enumeration of every cycle in the 198,000-edge graph.
- Proposal persistence remains query-heavy and slower than five seconds; it is
  measured separately from the PDF target.
- Timings are local-machine evidence, not a production-host capacity guarantee.
- No concurrent-user load, endpoint latency, M7 reporting, deployment, or external
  integration work is covered by this milestone.
