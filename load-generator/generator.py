"""
Continuous load generator — drives the sample app so the profiler always has
data to collect. Weighted random selection across endpoints produces a realistic
mix of hotspots rather than a single flat signal.

Trace correlation
─────────────────
Each outgoing HTTP request is wrapped in an OTel span. RequestsInstrumentor
injects a W3C traceparent header so the sample app's FlaskInstrumentor
continues the same trace as a child span. Profile samples captured in the
sample app are then tagged with that shared trace ID.

The trace ID is logged on every request line so you can copy it directly
into a Dynatrace DQL query:

  fetch logs
  | filter log.source == "continuous_profiler" and trace.id == "<trace_id>"
  | summarize cpu_ms = sum(toLong(profile.cpu_ns))/1000000, by:{profile.leaf_function}
  | sort cpu_ms desc
"""
import os
import random
import time
import logging

import requests as req_lib

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.sdk.resources import Resource, SERVICE_NAME, SERVICE_VERSION
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.requests import RequestsInstrumentor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger(__name__)

TARGET   = os.getenv("TARGET_URL", "http://localhost:8080")
RPS      = float(os.getenv("REQUESTS_PER_SECOND", "3"))
INTERVAL = 1.0 / RPS

_dt_endpoint  = os.getenv("DT_ENDPOINT", "").rstrip("/")
_dt_api_token = os.getenv("DT_API_TOKEN", "")

# ── Tracer setup ──────────────────────────────────────────────────────────────
resource = Resource.create({
    SERVICE_NAME:    os.getenv("OTEL_SERVICE_NAME", "load-generator"),
    SERVICE_VERSION: os.getenv("OTEL_SERVICE_VERSION", "1.0.0"),
})
provider = TracerProvider(resource=resource)

_is_real_dt = _dt_endpoint and _dt_api_token and "validator" not in _dt_endpoint
if _is_real_dt:
    provider.add_span_processor(BatchSpanProcessor(
        OTLPSpanExporter(
            endpoint=f"{_dt_endpoint}/api/v2/otlp/v1/traces",
            headers={"Authorization": f"Api-Token {_dt_api_token}"},
        )
    ))
else:
    provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

trace.set_tracer_provider(provider)
tracer = trace.get_tracer("load-generator")

# Auto-inject W3C traceparent header into every outgoing requests.Session call.
RequestsInstrumentor().instrument()

# ── Endpoints ────────────────────────────────────────────────────────────────
# (path, weight, params_fn)
# Weights are intentionally uneven — fibonacci dominates so it shows up
# most prominently in the flame graph (makes for a better demo).
ENDPOINTS = [
    ("/fibonacci", 0.40, lambda: {"n": random.randint(28, 35)}),
    ("/primes",    0.25, lambda: {"limit": random.randint(300_000, 900_000)}),
    ("/matrix",    0.20, lambda: {"size": random.randint(60, 130)}),
    ("/sort",      0.15, lambda: {"n": random.randint(3_000, 10_000)}),
]


def pick() -> tuple:
    paths, weights, pfns = zip(*ENDPOINTS)
    idx = random.choices(range(len(paths)), weights=weights, k=1)[0]
    return paths[idx], pfns[idx]()


def wait_for_app() -> None:
    log.info("Waiting for %s/health ...", TARGET)
    for _ in range(60):
        try:
            req_lib.get(f"{TARGET}/health", timeout=2).raise_for_status()
            log.info("App is up")
            return
        except Exception:
            time.sleep(2)
    raise RuntimeError(f"App at {TARGET} never became healthy")


def run() -> None:
    wait_for_app()
    session = req_lib.Session()
    log.info("Generating load at %.1f RPS against %s  (trace export: %s)",
             RPS, TARGET, "Dynatrace" if _is_real_dt else "stdout")

    while True:
        path, params = pick()
        t0 = time.perf_counter()
        try:
            # Each iteration gets its own root span.  RequestsInstrumentor creates
            # a child CLIENT span and injects traceparent — the sample app continues
            # the trace as a SERVER span so both ends share the same trace ID.
            with tracer.start_as_current_span(f"load-gen {path.lstrip('/')}") as span:
                resp    = session.get(f"{TARGET}{path}", params=params, timeout=60)
                elapsed = (time.perf_counter() - t0) * 1000
                ctx     = span.get_span_context()
                tid     = format(ctx.trace_id, "032x") if ctx.is_valid else "n/a"
                log.info("%-12s %-30s → %5.0fms  trace=%s", path, str(params), elapsed, tid)
        except Exception as exc:
            log.warning("Request failed: %s", exc)

        time.sleep(INTERVAL)


if __name__ == "__main__":
    run()
