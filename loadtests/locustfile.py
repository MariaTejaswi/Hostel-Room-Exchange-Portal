"""CES concurrent-user load profile for a deployed RoomLoop environment."""

import os
import re

from locust import HttpUser, between, events, task


READ_P95_LIMIT_MS = 400
WRITE_P95_LIMIT_MS = 800
MAX_FAILURE_RATIO = 0.001
CSRF_RE = re.compile(r'name="csrfmiddlewaretoken" value="([^"]+)"')


class RoomLoopUser(HttpUser):
    wait_time = between(1, 3)

    def on_start(self):
        email = os.environ.get("ROOMLOOP_LOAD_EMAIL")
        password = os.environ.get("ROOMLOOP_LOAD_PASSWORD")
        if not email or not password:
            raise RuntimeError(
                "ROOMLOOP_LOAD_EMAIL and ROOMLOOP_LOAD_PASSWORD are required."
            )

        login_page = self.client.get("/login/", name="READ login page")
        match = CSRF_RE.search(login_page.text)
        if not match:
            raise RuntimeError("Login page did not contain a CSRF token.")

        with self.client.post(
            "/login/",
            {
                "email": email,
                "password": password,
                "csrfmiddlewaretoken": match.group(1),
            },
            headers={"Referer": f"{self.host}/login/"},
            name="WRITE authenticated login",
            catch_response=True,
        ) as response:
            if response.status_code != 200 or "/login/" in response.url:
                response.failure("Authentication failed or redirected back to login.")

    @task(5)
    def dashboard(self):
        self.client.get("/dashboard/", name="READ dashboard")

    @task(3)
    def dashboard_stats(self):
        self.client.get("/dashboard/api/stats/", name="READ dashboard stats")

    @task(2)
    def exchange_requests(self):
        self.client.get("/exchange/requests/", name="READ exchange requests")

    @task(1)
    def notifications(self):
        self.client.get("/exchange/notifications/", name="READ notifications")


@events.quitting.add_listener
def enforce_ces_thresholds(environment, **_kwargs):
    failures = []
    for entry in environment.stats.entries.values():
        if entry.num_requests == 0:
            continue
        limit = WRITE_P95_LIMIT_MS if entry.name.startswith("WRITE ") else READ_P95_LIMIT_MS
        p95 = entry.get_response_time_percentile(0.95)
        if p95 > limit:
            failures.append(f"{entry.name}: p95 {p95}ms > {limit}ms")

    if environment.stats.total.fail_ratio > MAX_FAILURE_RATIO:
        failures.append(
            f"failure ratio {environment.stats.total.fail_ratio:.3%} > "
            f"{MAX_FAILURE_RATIO:.3%}"
        )

    if failures:
        environment.process_exit_code = 1
        print("CES_LOAD_THRESHOLD_FAILURES=" + " | ".join(failures))
