"""
Locust scenarios for Omixa. Run each scenario SEPARATELY so the bottleneck is attributable:

  locust -f loadtests/locustfile.py --headless -H https://STAGING \
         -u 1000 -r 50 -t 10m --tags landing        # then api, upload, poll, download, webhook

Pick user counts per stage (1k, 10k, 100k, ...). One machine tops out at a few thousand users;
for 10k+ use distributed Locust (--master/--worker) or a hosted generator, and generate load from
outside your own network. NEVER point this at production. Use staging with real Postgres/Redis/S3
and a Paystack TEST key. Webhook scenario needs OMIXA_LOAD_WEBHOOK_SECRET (the TEST secret).
"""
import hashlib
import hmac
import io
import json
import os
import random
import time
import uuid

from locust import HttpUser, between, tag, task

CSV = b"name,age,email\n" + b"\n".join(
    f"person {i},{20 + i % 50},p{i}@example.com".encode() for i in range(2000))


class Visitor(HttpUser):
    """Landing pages + static + health: edge/CDN and stateless API throughput."""
    wait_time = between(1, 5)
    weight = 10

    @tag("landing")
    @task(5)
    def home(self):
        self.client.get("/", name="/")

    @tag("landing")
    @task(2)
    def page(self):
        self.client.get("/clean", name="/clean")

    @tag("api")
    @task
    def health(self):
        self.client.get("/healthz", name="/healthz")


class Cleaner(HttpUser):
    """Full journey: upload -> analysis -> clean -> poll -> download (queue mode)."""
    wait_time = between(3, 10)
    weight = 3

    def _poll(self, job_id, done, name):
        deadline, delay = time.time() + 600, 1.0
        t0 = time.time()
        while time.time() < deadline:
            r = self.client.get(f"/api/jobs/{job_id}", name="/api/jobs/[id]")
            if r.status_code == 429:
                time.sleep(int(r.headers.get("Retry-After", "3"))); continue
            j = r.json() if r.ok else {}
            if j.get("status") == "failed":
                return None
            if done(j):
                self.environment.events.request.fire(request_type="E2E", name=name, response_time=(time.time() - t0) * 1000,
                                                    response_length=0, exception=None, context={})
                return j
            time.sleep(delay); delay = min(delay * 1.4, 5)
        return None

    @tag("upload", "poll", "download")
    @task
    def journey(self):
        r = self.client.post("/api/upload/", files={"file": ("load.csv", io.BytesIO(CSV), "text/csv")}, name="/api/upload")
        if r.status_code != 201:
            return
        job_id = r.json()["job_id"]
        r = self.client.get(f"/api/report/{job_id}", name="/api/report/[id]")
        if r.status_code == 202:
            self._poll(job_id, lambda j: j.get("status") == "uploaded" and j.get("analysis"), "e2e: analysis")
        r = self.client.post(f"/api/process/{job_id}", json={}, name="/api/process/[id]")
        if r.status_code == 202:
            if self._poll(job_id, lambda j: j.get("status") == "processed", "e2e: clean"):
                self.client.get(f"/api/download/{job_id}?format=url", name="/api/download/[id]")


class PaystackWebhook(HttpUser):
    """Duplicate + concurrent webhook deliveries (use the Paystack TEST secret on staging)."""
    wait_time = between(0.1, 1)
    weight = 1

    @tag("webhook")
    @task
    def deliver(self):
        secret = os.environ.get("OMIXA_LOAD_WEBHOOK_SECRET", "")
        if not secret:
            return
        ref = f"load-{random.randint(1, 50)}"   # only 50 refs: most deliveries are duplicates on purpose
        body = json.dumps({"event": "charge.success", "data": {"reference": ref, "status": "success",
                           "amount": 100, "currency": "NGN", "metadata": {"uid": "load-user"}}}).encode()
        sig = hmac.new(secret.encode(), body, hashlib.sha512).hexdigest()
        self.client.post("/api/billing/webhook", data=body, headers={"x-paystack-signature": sig,
                         "Content-Type": "application/json"}, name="/api/billing/webhook")
