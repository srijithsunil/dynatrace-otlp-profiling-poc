"""
Sample application with intentional CPU hotspots.

Each endpoint runs a different CPU-heavy algorithm so the profiler
captures distinct method-level hotspots that are easy to identify.

Endpoints
─────────
  GET /fibonacci?n=<int>     recursive Fibonacci     — call-depth hotspot
  GET /primes?limit=<int>    Sieve of Eratosthenes   — loop + array hotspot
  GET /matrix?size=<int>     naive matrix multiply   — nested-loop hotspot
  GET /sort?n=<int>          repeated list sort      — sort internals hotspot
  GET /health                healthcheck
"""

import math
import os
import random
import time
import logging

from flask import Flask, jsonify, request
from dt_profiler import start_profiler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

# Start before Flask so every thread is profiled from the beginning.
# All config is read from environment variables — no hardcoded values.
start_profiler(
    sample_interval_ms=float(os.getenv("PROFILER_SAMPLE_INTERVAL_MS", "10")),
    flush_interval_s=int(os.getenv("PROFILER_FLUSH_INTERVAL_S", "30")),
    service_version=os.getenv("OTEL_SERVICE_VERSION", "1.0.0"),
    extra_attributes={"host.name": os.getenv("HOSTNAME", "unknown")},
)

app = Flask(__name__)


# ── CPU hotspot implementations ───────────────────────────────────────────────

def fibonacci(n: int) -> int:
    if n <= 1:
        return n
    return fibonacci(n - 1) + fibonacci(n - 2)


def sieve_of_eratosthenes(limit: int) -> list:
    is_prime = bytearray([1]) * (limit + 1)
    is_prime[0] = is_prime[1] = 0
    for i in range(2, int(math.isqrt(limit)) + 1):
        if is_prime[i]:
            is_prime[i * i :: i] = bytearray(len(is_prime[i * i :: i]))
    return [i for i, v in enumerate(is_prime) if v]


def matrix_multiply(size: int) -> list:
    a = [[random.random() for _ in range(size)] for _ in range(size)]
    b = [[random.random() for _ in range(size)] for _ in range(size)]
    c = [[0.0] * size for _ in range(size)]
    for i in range(size):
        for k in range(size):
            for j in range(size):
                c[i][j] += a[i][k] * b[k][j]
    return c


def repeated_sort(n: int, iterations: int = 25) -> list:
    data = [random.randint(0, 100_000) for _ in range(n)]
    for _ in range(iterations):
        data = sorted(data, key=lambda x: (x % 7, -x))
    return data


# ── Routes ────────────────────────────────────────────────────────────────────

SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "profiling-demo-app")


@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": SERVICE_NAME})


@app.route("/fibonacci")
def fib_endpoint():
    n = min(int(request.args.get("n", 30)), 38)
    t0 = time.perf_counter()
    result = fibonacci(n)
    return jsonify({"n": n, "result": result, "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2)})


@app.route("/primes")
def primes_endpoint():
    limit = min(int(request.args.get("limit", 500_000)), 2_000_000)
    t0 = time.perf_counter()
    primes = sieve_of_eratosthenes(limit)
    return jsonify({"limit": limit, "count": len(primes), "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2)})


@app.route("/matrix")
def matrix_endpoint():
    size = min(int(request.args.get("size", 80)), 200)
    t0 = time.perf_counter()
    matrix_multiply(size)
    return jsonify({"size": size, "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2)})


@app.route("/sort")
def sort_endpoint():
    n = min(int(request.args.get("n", 5_000)), 50_000)
    t0 = time.perf_counter()
    repeated_sort(n)
    return jsonify({"n": n, "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2)})


# ── Startup ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, threaded=True)
