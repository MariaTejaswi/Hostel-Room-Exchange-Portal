# CES Load Test

This Locust profile measures the common engineering target of 2,000 sustained
concurrent users and a 5,000-user peak. It is separate from the P04 matcher test
over 2,000 open requests.

Run only against an approved staging/load-test deployment with production-like
Gunicorn workers, PostgreSQL, Redis, TLS, and connection pooling. Supply a seeded,
non-privileged test student through `ROOMLOOP_LOAD_EMAIL` and
`ROOMLOOP_LOAD_PASSWORD`; do not commit credentials.

Smoke validation:

```powershell
.venv\Scripts\locust.exe -f loadtests\locustfile.py --headless `
  --host https://staging.example.edu --users 20 --spawn-rate 5 `
  --run-time 1m --csv loadtests/results/smoke
```

Certification runs:

```powershell
# Sustained capacity
.venv\Scripts\locust.exe -f loadtests\locustfile.py --headless `
  --host https://staging.example.edu --users 2000 --spawn-rate 100 `
  --run-time 15m --csv loadtests/results/sustained-2000

# Peak capacity
.venv\Scripts\locust.exe -f loadtests\locustfile.py --headless `
  --host https://staging.example.edu --users 5000 --spawn-rate 250 `
  --run-time 5m --csv loadtests/results/peak-5000
```

The run exits non-zero if any named read has p95 above 400 ms, authenticated
login has p95 above 800 ms, or the aggregate failure ratio exceeds 0.1%.
Archive the CSV files with server CPU/memory, Gunicorn worker utilization,
PostgreSQL connection count, slow queries, Redis metrics, and deployment revision.

LCP under simulated 4G is a browser measurement and must be captured separately
against the same deployed revision using Lighthouse or Chrome DevTools. Target:
under 2.5 seconds on every student-facing critical page.
