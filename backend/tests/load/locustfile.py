"""Load test (PromptLint.md §13): 50 concurrent users against a running server.

    RATE_LIMIT=100000/hour uvicorn app.main:app --port 8787      # lift the per-IP limit first
    locust -f tests/load/locustfile.py --host http://localhost:8787 -u 50 -r 10 -t 2m --headless

By default most prompts repeat, so the server's hash cache absorbs them and the test measures
PromptLint itself. Set PL_UNIQUE=1 to make every prompt unique; each request then calls Jev, which
spends API credits and should stay under Jev's 1,200 requests/minute.
"""

import os
import random
import uuid

from locust import HttpUser, between, task

PROMPTS = [
    "write me a poem",
    "fix my code it doesnt work",
    "Summarize the causes of the 2008 financial crisis in 5 bullet points for a high-school class.",
    "Write a Python function that removes duplicates from a list while keeping order. Code only.",
    "Plan a 3-day trip to New York City for a family with two kids on a $1,500 budget, as a daily itinerary.",
    "Explain recursion.",
]
UNIQUE = os.environ.get("PL_UNIQUE") == "1"


class PromptLintUser(HttpUser):
    wait_time = between(1, 3)

    @task(5)
    def analyze(self) -> None:
        prompt = random.choice(PROMPTS)
        if UNIQUE:
            prompt = f"{prompt} (ref {uuid.uuid4().hex[:8]})"
        with self.client.post("/api/analyze", json={"prompt": prompt}, catch_response=True) as r:
            if r.status_code != 200:
                r.failure(f"{r.status_code}: {r.text[:120]}")

    @task(10)
    def live_token_count(self) -> None:
        self.client.post("/api/tokens", json={"prompt": random.choice(PROMPTS)})

    @task(1)
    def models(self) -> None:
        self.client.get("/api/models")
