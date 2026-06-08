"""
Continuous load generator — drives the sample app so the profiler always has
data to collect. Weighted random selection across endpoints produces a realistic
mix of hotspots rather than a single flat signal.
"""
import os
import random
import time
import logging

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger(__name__)

TARGET   = os.getenv("TARGET_URL", "http://localhost:8080")
RPS      = float(os.getenv("REQUESTS_PER_SECOND", "3"))
INTERVAL = 1.0 / RPS

# (path, weight, params_fn)
# Weights are intentionally uneven — fibonacci dominates so it shows up
# most prominently in the flame graph (makes for a better demo).
ENDPOINTS = [
    ("/fibonacci", 0.40, lambda: {"n": random.randint(28, 35)}),
    ("/primes",    0.25, lambda: {"limit": random.randint(300_000, 900_000)}),
    ("/matrix",    0.20, lambda: {"size": random.randint(60, 130)}),
    ("/sort",      0.15, lambda: {"n": random.randint(3_000, 10_000)}),
]


def pick() -> tuple[str, dict]:
    paths, weights, pfns = zip(*ENDPOINTS)
    idx = random.choices(range(len(paths)), weights=weights, k=1)[0]
    return paths[idx], pfns[idx]()


def wait_for_app() -> None:
    log.info("Waiting for %s/health ...", TARGET)
    for _ in range(60):
        try:
            requests.get(f"{TARGET}/health", timeout=2).raise_for_status()
            log.info("App is up")
            return
        except Exception:
            time.sleep(2)
    raise RuntimeError(f"App at {TARGET} never became healthy")


def run() -> None:
    wait_for_app()
    session = requests.Session()
    log.info("Generating load at %.1f RPS against %s", RPS, TARGET)
    while True:
        path, params = pick()
        t0 = time.perf_counter()
        try:
            resp = session.get(f"{TARGET}{path}", params=params, timeout=60)
            data = resp.json()
            elapsed = (time.perf_counter() - t0) * 1000
            log.info("%-12s %-35s → %5.0fms", path, str(params), elapsed)
        except Exception as exc:
            log.warning("Request failed: %s", exc)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    run()
