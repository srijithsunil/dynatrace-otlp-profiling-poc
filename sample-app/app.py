"""
Sample application with intentional CPU hotspots.

Each endpoint runs a different CPU-heavy algorithm so the profiler
captures distinct method-level hotspots that are easy to identify.
Trace correlation is enabled: each request's traceId/spanId is attached
to every profile sample captured during that request.

Endpoints
─────────
  GET /fibonacci?n=<int>     recursive Fibonacci     — call-depth hotspot
  GET /primes?limit=<int>    Sieve of Eratosthenes   — loop + array hotspot
  GET /matrix?size=<int>     naive matrix multiply   — nested-loop hotspot
  GET /sort?n=<int>          repeated list sort      — sort internals hotspot
  GET /health                healthcheck

Trace IDs are included in every JSON response so you can copy one directly
into a Dynatrace DQL query to see the profile for that specific request:

  fetch logs
  | filter log.source == "continuous_profiler" and trace.id == "<trace_id>"
  | summarize cpu_ms = sum(toLong(profile.cpu_ns))/1000000, by:{profile.leaf_function}
  | sort cpu_ms desc
"""

import math
import os
import random
import time
import logging

from flask import Flask, jsonify, request

# ── OpenTelemetry setup ───────────────────────────────────────────────────────
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.sdk.resources import Resource, SERVICE_NAME, SERVICE_VERSION
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.flask import FlaskInstrumentor

from dt_profiler import start_profiler, init_flask_profiling

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

_dt_endpoint   = os.getenv("DT_ENDPOINT", "").rstrip("/")
_dt_api_token  = os.getenv("DT_API_TOKEN", "")
_service_name  = os.getenv("OTEL_SERVICE_NAME", "profiling-demo-app")
_service_ver   = os.getenv("OTEL_SERVICE_VERSION", "1.0.0")

resource = Resource.create({SERVICE_NAME: _service_name, SERVICE_VERSION: _service_ver})
provider = TracerProvider(resource=resource)

# Export traces to Dynatrace when a real DT endpoint is configured;
# fall back to stdout so trace IDs are still visible in dev mode.
_is_real_dt = _dt_endpoint and _dt_api_token and "validator" not in _dt_endpoint
if _is_real_dt:
    provider.add_span_processor(BatchSpanProcessor(
        OTLPSpanExporter(
            endpoint=f"{_dt_endpoint}/api/v2/otlp/v1/traces",
            headers={"Authorization": f"Api-Token {_dt_api_token}"},
        )
    ))
    log.info("Trace exporter → %s/api/v2/otlp/v1/traces", _dt_endpoint)
else:
    provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    log.info("Trace exporter → stdout (dev mode — no real DT endpoint)")

trace.set_tracer_provider(provider)

# ── Profiler ──────────────────────────────────────────────────────────────────
# Start before Flask so every thread is profiled from the beginning.
start_profiler(
    sample_interval_ms=float(os.getenv("PROFILER_SAMPLE_INTERVAL_MS", "10")),
    flush_interval_s=int(os.getenv("PROFILER_FLUSH_INTERVAL_S", "30")),
    service_version=_service_ver,
    extra_attributes={"host.name": os.getenv("HOSTNAME", "unknown")},
)

app = Flask(__name__)

# Auto-instrument Flask — creates a span per request using the provider above.
FlaskInstrumentor().instrument_app(app)

# Register before_request / teardown hooks that read the active OTel span and
# register its traceId/spanId with the profiler for the duration of the request.
init_flask_profiling(app)


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

def _current_trace_id() -> str:
    """Return the active trace ID as a hex string, or empty string if none."""
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else ""


@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": _service_name})


@app.route("/fibonacci")
def fib_endpoint():
    n = min(int(request.args.get("n", 30)), 38)
    t0 = time.perf_counter()
    result = fibonacci(n)
    return jsonify({
        "n": n,
        "result": result,
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
        "trace_id": _current_trace_id(),
    })


@app.route("/primes")
def primes_endpoint():
    limit = min(int(request.args.get("limit", 500_000)), 2_000_000)
    t0 = time.perf_counter()
    primes = sieve_of_eratosthenes(limit)
    return jsonify({
        "limit": limit,
        "count": len(primes),
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
        "trace_id": _current_trace_id(),
    })


@app.route("/matrix")
def matrix_endpoint():
    size = min(int(request.args.get("size", 80)), 200)
    t0 = time.perf_counter()
    matrix_multiply(size)
    return jsonify({
        "size": size,
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
        "trace_id": _current_trace_id(),
    })


@app.route("/sort")
def sort_endpoint():
    n = min(int(request.args.get("n", 5_000)), 50_000)
    t0 = time.perf_counter()
    repeated_sort(n)
    return jsonify({
        "n": n,
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
        "trace_id": _current_trace_id(),
    })


# ── Startup ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, threaded=True)
