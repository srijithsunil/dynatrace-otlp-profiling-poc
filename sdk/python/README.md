# dt-otlp-profiler — Python SDK

Adds continuous profiling to any Python app with two lines of code.
Profiles are sent as OTLP Logs to Dynatrace and can be correlated with distributed traces.

## Install

```bash
# From this repo (POC)
pip install ./sdk/python

# Future: from PyPI
pip install dt-otlp-profiler
```

## Quick start

```python
from dt_profiler import start_profiler
start_profiler()   # reads DT_ENDPOINT and DT_API_TOKEN from env
```

That's it. Add this before your web server starts.

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `DT_ENDPOINT` | Yes | `https://<env>.live.dynatrace.com` |
| `DT_API_TOKEN` | Yes | Token with `logs.ingest` scope |
| `OTEL_SERVICE_NAME` | No | Service name shown in Dynatrace (default: `unknown-service`) |
| `DEPLOYMENT_ENV` | No | Environment tag — `prod`, `staging`, etc. |

## Framework examples

### Flask

```python
from dt_profiler import start_profiler
from flask import Flask

start_profiler()     # before app creation
app = Flask(__name__)
```

### Django

In `manage.py` or `wsgi.py`, before `execute_from_command_line`:

```python
from dt_profiler import start_profiler
start_profiler()
```

### Gunicorn post-fork hook

```python
# gunicorn.conf.py
def post_fork(server, worker):
    from dt_profiler import start_profiler
    start_profiler()
```

### FastAPI / Uvicorn

```python
from contextlib import asynccontextmanager
from dt_profiler import start_profiler, stop_profiler
from fastapi import FastAPI

@asynccontextmanager
async def lifespan(app: FastAPI):
    start_profiler()
    yield
    stop_profiler()

app = FastAPI(lifespan=lifespan)
```

## Trace correlation

Link profiling data to distributed traces so you can navigate from a slow request
directly to the methods that consumed CPU during that request.

**Flask (one line — automatic):**

```python
from dt_profiler import start_profiler, init_flask_profiling

start_profiler()
app = Flask(__name__)
init_flask_profiling(app)    # reads active OTel span per request
```

Requires `opentelemetry-api`: `pip install "dt-otlp-profiler[otel]"`

**Any framework (context manager):**

```python
from dt_profiler import auto_trace_context   # reads active OTel span
# or
from dt_profiler import trace_context        # supply IDs manually

with auto_trace_context():
    handle_request()

# manual variant (no opentelemetry-api needed):
with trace_context(trace_id="4bf92f...", span_id="00f067..."):
    handle_request()
```

**DQL to query correlated profiles:**

```dql
fetch logs
| filter log.source == "continuous_profiler" and trace.id == "<your-trace-id>"
| summarize cpu_ms = sum(toLong(profile.cpu_ns)) / 1000000, by:{profile.leaf_function}
| sort cpu_ms desc
```

## Tuning

```python
start_profiler(
    sample_interval_ms=10,   # stack snapshot frequency (default: 10ms)
    flush_interval_s=30,     # export window size (default: 30s)
    service_version="2.1.0",
    environment="production",
    extra_attributes={"k8s.namespace": "payments"},
)
```

## Local validation (no DT tenant)

```bash
docker compose -f docker-compose.infra.yml up
```

Then point `DT_ENDPOINT=http://localhost:8888` — the validator prints
flame-graph summaries to stdout every flush window.
